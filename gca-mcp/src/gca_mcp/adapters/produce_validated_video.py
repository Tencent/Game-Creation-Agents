"""Validated sprite-action video production with opt-in quality retries.

For each sprite action, candidate pipelines are retried serially. Within one
candidate, the two independent video-validation passes run concurrently and
are merged in canonical order. Validation is split across two inputs:

  - FRAMING pass — R1 (in-frame: nothing clipped off the canvas edge)
    and R2 (scale-locked: the character doesn't balloon / shrink from a
    silent zoom) — judged on the RAW mp4 (solid bg makes margins /
    framing legible).
  - CONTENT pass — R3 identity, R4 hand-prop attachment, R5 limb
    integrity, R6 loop coherence, R7 action fidelity — judged on the MATTED preview mp4
    (matting can eat a limb / weapon edge, so these judge the shipped
    output). It never falls back to the raw mp4 if preview encoding fails.

A take ships immediately if it clears BOTH passes. Quality failures only draw a
fresh take when ``n_candidates`` is explicitly above the global default of 1.
Seedance transport failures that return no usable video are retried separately
inside the Seedance adapter and do not create extra quality candidates.

Rationale: the framing defects are per-draw random — a given Seedance
sample either clips / zooms or it doesn't, and feedback prompting
doesn't reliably move them — so "draw, check, redraw if bad" converges
in a couple of tries. We stop as soon as a clean take appears (no
wasted API spend) and cap at ``n_candidates`` attempts. If every
attempt fails, the least-bad take (fewest rubric failures) is shipped
as a best-effort winner rather than blocking the pipeline.

Output structure (attempt dirs keep the ``candidate_NN`` name for
backward-compat with downstream artifact readers):
    output_dir/
    ├── candidate_01/  (video.mp4, rmbg2/, verdict.json)  ← attempt 1
    ├── candidate_02/                                       ← attempt 2 (only if 1 failed)
    ├── …
    ├── final/         (winner mirrored — video.mp4 + rmbg2/)
    └── _orchestrator_summary.json
"""

from __future__ import annotations

import json
import shutil
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from ..config import AssetSeedanceDefaults, Config
from ..errors import ToolError
from . import asset_video_validator, rmbg2_video, seedance


def _copy_attempt_artifacts(
    src_dir: Path,
    dst_dir: Path,
) -> None:
    if dst_dir.exists():
        shutil.rmtree(dst_dir)
    dst_dir.mkdir(parents=True, exist_ok=True)
    for name in ("video.mp4", "verdict.json", "prompt_used.txt"):
        src = src_dir / name
        if src.is_file():
            shutil.copy2(src, dst_dir / name)
    rmbg_src = src_dir / "rmbg2"
    if rmbg_src.is_dir():
        shutil.copytree(rmbg_src, dst_dir / "rmbg2")


def _fallback_score(c: dict[str, Any]) -> tuple[int, int]:
    # Used only when NO attempt passed both rubrics — pick the least-bad
    # take as a best-effort winner. Lower tuple wins:
    #   1) fewer rubric FAILs, then
    #   2) earlier attempt index (stable tie-break).
    return (c.get("n_failed", 999), c.get("candidate", 999))


def _complete_for_ids(verdict: dict[str, Any], expected_ids: list[str]) -> bool:
    """VID-03: is this pass's rubric verdict COMPLETE (fully judged)?

    Complete = the rubric verdict exactly covers the expected IDs (each present
    once, no missing / duplicate / extra) AND has zero UNKNOWNs — i.e. the VLM
    produced a parseable verdict for every rubric. Unlike
    :func:`_strict_pass_for_ids` this does NOT require every result to be PASS:
    a fully-judged candidate with one real FAIL is COMPLETE (and may enter the
    least-bad fallback pool), whereas a candidate with an unparsed / missing /
    UNKNOWN rubric is INCOMPLETE and must NOT be treated as a judged take (the
    bug: ``0 FAIL / 2 UNKNOWN`` beating ``1 FAIL / 0 UNKNOWN`` in fallback).
    """
    results = verdict.get("rubric_results") or []
    got_ids = [str(r.get("id")) for r in results]
    if sorted(got_ids) != sorted(expected_ids):
        return False  # missing, duplicate, or extra rubric id
    if int(verdict.get("n_unknown", 0) or 0) != 0:
        return False
    if verdict.get("duplicate_ids"):
        return False
    if verdict.get("unexpected_ids"):
        return False
    if verdict.get("failed_line_mismatch"):
        return False
    if verdict.get("aggregate_duplicate"):
        return False
    if verdict.get("aggregate_missing"):
        return False
    reported = verdict.get("overall_verdict_reported")
    derived = (
        "FAIL"
        if any(r.get("verdict") == "FAIL" for r in results)
        else "PASS"
    )
    if reported is not None and reported != derived:
        return False
    return not any(
        r.get("verdict") not in ("PASS", "FAIL") for r in results
    )


def _strict_pass_for_ids(verdict: dict[str, Any], expected_ids: list[str]) -> bool:
    """VID-02 ship gate for one validation pass.

    A pass is clean ONLY when the rubric verdict exactly covers the expected
    rubric IDs — each expected id present exactly once, no extra/unattributed
    ids, every result PASS, and zero UNKNOWNs. This blocks a garbled VLM parse
    (missing / duplicated / unparsed rubric lines) from being treated as a
    clean pass even if the validator's own aggregate ``passed`` was True.
    """
    results = verdict.get("rubric_results") or []
    got_ids = [str(r.get("id")) for r in results]
    if sorted(got_ids) != sorted(expected_ids):
        return False  # missing, duplicate, or extra rubric id
    if any(r.get("verdict") != "PASS" for r in results):
        return False
    if int(verdict.get("n_unknown", 0) or 0) != 0:
        return False
    if verdict.get("duplicate_ids"):
        return False
    if verdict.get("unexpected_ids"):
        return False
    if verdict.get("failed_line_mismatch"):
        return False
    if verdict.get("aggregate_duplicate"):
        return False
    if verdict.get("aggregate_missing"):
        return False
    return verdict.get("overall_verdict_reported") != "FAIL"


def produce_validated_video(
    config: Config,
    *,
    base_prompt: str,
    first_frame: str,
    output_dir: Path,
    action_description: str,
    # seedance
    duration: int = 5,
    ratio: str = "1:1",
    resolution: str = "720p",
    model: str = "fast",
    seed_base: int | None = None,
    # matting
    bg_hex: str = "#FF00FF",
    aug_preset: str = "off",
    rmbg_fg_cutoff: int = 230,
    gif_alpha_threshold: int = 200,
    align_feet_to_bottom: bool = True,
    foot_margin_px: int = 2,
    matte_workers: int = 8,
    max_frames: int | None = None,
    # bg-leak frame purge (matting safety net for over-bright effects)
    drop_bg_leak: bool = True,
    bg_leak_area_threshold: float = 0.005,
    bg_leak_color_dist: int = 120,
    bg_leak_alpha_threshold: int = 128,
    bg_leak_keep_min_frames: int = 2,
    bg_leak_alpha_weighted: bool = True,
    # validator
    vlm_model: str | None = None,
    extra_rubrics: list[dict[str, str]] | None = None,
    rubric_override: list[dict[str, str]] | None = None,
    # Which default rubric groups drive the two-pass check. Defaults to the
    # character sets (FRAMING_RUBRICS + CONTENT_RUBRICS). Pass the projectile
    # groups for a projectile-body animation (no hand-prop / limb / grounded
    # checks). Ignored when ``rubric_override`` is set (that collapses to one
    # custom pass).
    framing_rubrics: tuple[Any, ...] | None = None,
    content_rubrics: tuple[Any, ...] | None = None,
    # serial-retry control (``n_candidates`` = max attempts; kept as the
    # param name for backward-compat with the tool schema / callers).
    n_candidates: int | None = None,
    # VID-03 escape hatch: when EVERY attempt failed to even reach validation
    # (service/parse error — no verdict at all), default behavior ships NOTHING
    # (validation_unavailable ≠ least-bad). Set True to allow an unvalidated
    # video through with an explicit risk marker.
    allow_unvalidated: bool = False,
) -> dict[str, Any]:
    """Serial-retry sprite-action video. Generate → matte → validate in
    two passes (framing rubrics on the raw mp4, content rubrics on the
    matted preview mp4); stop at the first take that passes BOTH passes,
    else retry up to ``n_candidates`` times and ship the least-bad take
    as a best-effort winner.
    """
    if n_candidates is None:
        runtime_defaults = getattr(getattr(config, "assets", None), "seedance", None)
        n_candidates = int(
            getattr(
                runtime_defaults,
                "max_quality_candidates",
                AssetSeedanceDefaults.max_quality_candidates,
            )
        )
    if n_candidates < 1:
        raise ToolError(
            "INVALID_CONFIG",
            f"n_candidates must be >= 1, got {n_candidates}",
        )
    if not base_prompt.strip():
        raise ToolError("INVALID_CONFIG", "`base_prompt` must be non-empty.")
    if not action_description.strip():
        raise ToolError(
            "INVALID_CONFIG", "`action_description` must be non-empty."
        )

    output_dir.mkdir(parents=True, exist_ok=True)
    for stale in output_dir.glob("candidate_*"):
        if stale.is_dir() and stale.name.removeprefix("candidate_").isdigit():
            shutil.rmtree(stale)
    stale_final = output_dir / "final"
    if stale_final.exists():
        shutil.rmtree(stale_final)
    if seed_base is None:
        seed_base = int(time.time() * 1000) & 0x7FFFFFFF
    t_total = time.time()

    def _attempt_job(idx: int) -> dict[str, Any]:
        cand_dir = output_dir / f"candidate_{idx:02d}"
        # VID-01: start every attempt from a CLEAN candidate dir. Reusing a
        # dir with exist_ok left a prior run's rmbg2/ (transparent gif +
        # frames) on disk; if this attempt's matting then failed, the stale
        # transparent output could be copied to final/ and reported usable.
        if cand_dir.exists():
            shutil.rmtree(cand_dir)
        cand_dir.mkdir(parents=True, exist_ok=True)
        seed = seed_base + idx
        record: dict[str, Any] = {"candidate": idx, "seed": seed}

        # 1) Persist the (identical across attempts) prompt for audit
        (cand_dir / "prompt_used.txt").write_text(base_prompt, encoding="utf-8")

        # 2) Seedance i2v
        mp4 = cand_dir / "video.mp4"
        try:
            t = time.time()
            seedance.generate_video(
                config,
                prompt=base_prompt,
                output=mp4,
                first_frame=first_frame,
                model=model,
                duration=duration,
                ratio=ratio,
                resolution=resolution,
                seed=seed,
            )
            record["video"] = str(mp4)
            record["seedance_elapsed_s"] = round(time.time() - t, 1)
        except ToolError as e:
            record["error_stage"] = "seedance"
            record["error"] = f"{e.code}: {e.message}"
            return record
        except Exception as e:  # noqa: BLE001
            record["error_stage"] = "seedance"
            record["error"] = f"INTERNAL_ERROR: {e}"
            return record

        # 3) Matting
        matte_dir = cand_dir / "rmbg2"
        try:
            t = time.time()
            rmbg = rmbg2_video.matte_video(
                config,
                input_video=mp4,
                output_dir=matte_dir,
                formats=["gif", "mp4_preview"],
                bg_hex=bg_hex,
                aug_preset=aug_preset,
                rmbg_fg_cutoff=rmbg_fg_cutoff,
                gif_alpha_threshold=gif_alpha_threshold,
                align_feet_to_bottom=align_feet_to_bottom,
                foot_margin_px=foot_margin_px,
                workers=matte_workers,
                max_frames=max_frames,
                drop_bg_leak=drop_bg_leak,
                bg_leak_area_threshold=bg_leak_area_threshold,
                bg_leak_color_dist=bg_leak_color_dist,
                bg_leak_alpha_threshold=bg_leak_alpha_threshold,
                bg_leak_keep_min_frames=bg_leak_keep_min_frames,
                bg_leak_alpha_weighted=bg_leak_alpha_weighted,
            )
            record["transparent_gif"] = rmbg.get("outputs", {}).get("gif")
            record["preview_mp4"] = rmbg.get("outputs", {}).get("mp4_preview")
            record["align_stats"] = rmbg.get("align_stats")
            record["bg_leak_stats"] = rmbg.get("bg_leak_stats")
            for field in (
                "matting",
                "matting_requested",
                "matting_fallback",
                "matting_fallback_code",
                "matting_fallback_reason",
                "matting_key_color",
                "rmbg_failures_before_fallback",
            ):
                record[field] = rmbg.get(field)
            # VID (GIF soft-fail): ``usable`` must be anchored to the actual
            # transparent PNG frame set produced THIS RUN — the exact inputs
            # ``pack_spritesheet`` consumes — NOT to whether the derived GIF
            # encode happened to succeed. GIF/preview are optional derived
            # artifacts and their soft-failure must not mark valid frames
            # unusable. Record the this-run frames dir + count.
            frames_dir = matte_dir / "transparent_frames"
            frame_pngs = (
                sorted(frames_dir.glob("*.png")) if frames_dir.is_dir() else []
            )
            record["transparent_frames_dir"] = (
                str(frames_dir) if frame_pngs else None
            )
            record["matte_frame_count"] = len(frame_pngs)
            # Total frames sent to RMBG and how many failed — the matte-health
            # gate needs the success ratio, not just the survivor count.
            record["matte_frames_total"] = int(rmbg.get("frames_total") or 0)
            record["matte_frames_failed"] = int(rmbg.get("frames_failed") or 0)
            record["matte_health"] = rmbg2_video.assess_matte_health(
                frames_dir,
                frames_total=record["matte_frames_total"],
                frames_failed=record["matte_frames_failed"],
                bg_leak_stats=record["bg_leak_stats"],
            )
            record["gif_encoded"] = bool(record["transparent_gif"])
            record["matte_elapsed_s"] = round(time.time() - t, 1)
            if not record["matte_health"]["healthy"]:
                record["error_stage"] = "matte_health"
                record["error"] = "Matted frame set failed publication health checks."
                return record
        except ToolError as e:
            record["error_stage"] = "matting"
            record["error"] = f"{e.code}: {e.message}"
            return record
        except Exception as e:  # noqa: BLE001
            record["error_stage"] = "matting"
            record["error"] = f"INTERNAL_ERROR: {e}"
            return record

        # 4) Validate in TWO passes on DIFFERENT inputs:
        #    - framing rubrics (R1 in-frame, R2 scale-locked) on the RAW
        #      mp4 — the solid bg is what makes margins / framing legible.
        #    - content rubrics (R3 identity, R4 hand-prop, R5 limb,
        #      R6 loop, R7 action) on the MATTED preview mp4 — matting can eat a
        #      limb / weapon edge, so these judge the shipped output.
        #    A take PASSes only if BOTH passes pass. PR #123 review #5: when the
        #    matted preview mp4 is unavailable (encoder soft-fail), the content
        #    pass is REBUILT from the surviving transparent PNG frames — it must
        #    NEVER fall back to the raw mp4, which cannot show matting defects
        #    (an erased limb, a background leak). If no preview can be built
        #    (no frames / ffmpeg missing), the content pass is marked INCOMPLETE
        #    and the take cannot be reported as a clean pass.
        try:
            t = time.time()
            preview_mp4 = record.get("preview_mp4")
            content_input: Path | None
            if preview_mp4 and Path(preview_mp4).is_file():
                content_input = Path(preview_mp4)
                content_input_kind = "matted"
            else:
                # Rebuild the preview deterministically from the matted frames
                # THIS run produced — never substitute the raw video.
                frames_dir = matte_dir / "transparent_frames"
                rebuilt = rmbg2_video.encode_preview_from_frames(
                    frames_dir,
                    cand_dir / "preview_from_frames.mp4",
                    fps=12.0,
                )
                if rebuilt:
                    content_input = Path(rebuilt)
                    content_input_kind = "matted(rebuilt)"
                    record["preview_rebuilt_from_frames"] = rebuilt
                else:
                    content_input = None
                    content_input_kind = "unavailable"
            framing_group = (
                framing_rubrics
                if framing_rubrics is not None
                else asset_video_validator.FRAMING_RUBRICS
            )
            content_group = (
                content_rubrics
                if content_rubrics is not None
                else asset_video_validator.CONTENT_RUBRICS
            )
            passes: list[tuple[str, Path | None, tuple[Any, ...], str]]
            if rubric_override:
                # A full override is a custom ship-quality contract, so judge
                # the actual matted product. Extra framing checks belong in the
                # normal two-pass flow via ``extra_rubrics``.
                passes = [("custom", content_input, (), content_input_kind)]
            else:
                passes = [
                    ("framing", mp4, framing_group, "raw"),
                    ("content", content_input, content_group, content_input_kind),
                ]
            merged_results: list[dict[str, Any]] = []
            merged_failures: list[dict[str, Any]] = []
            all_passed = True
            all_complete = True
            n_passed = n_failed = n_unknown = 0
            pass_detail: dict[str, Any] = {}

            def _validate_one_pass(
                pass_spec: tuple[str, Path | None, tuple[Any, ...], str],
            ) -> dict[str, Any]:
                pass_name, vid, group, input_kind = pass_spec
                override = asset_video_validator.rubrics_as_dicts(group)
                # Extra rubrics describe shipped visual/action content and run
                # once on the matted/custom pass. Applying them to both passes
                # duplicates IDs/results and can rank fallback candidates wrong.
                pass_extra_rubrics = (
                    extra_rubrics
                    if pass_name in ("content", "custom")
                    else None
                )
                effective_rubrics = list(rubric_override or override)
                if pass_extra_rubrics:
                    effective_rubrics = (
                        effective_rubrics + list(pass_extra_rubrics)
                    )
                expected_ids = [str(r["id"]) for r in effective_rubrics]
                # PR #123 review #5: a content pass with no matted input (no
                # preview, no rebuildable frames) is INCOMPLETE — we refuse to
                # judge it on the raw video. Record it as not-passed /
                # not-complete so the take can never ship as a clean pass and is
                # excluded from the fully-judged fallback pool.
                if vid is None:
                    return {
                        "pass_name": pass_name,
                        "expected_ids": expected_ids,
                        "verdict_result": None,
                        "detail": {
                            "input": None,
                            "input_kind": input_kind,
                            "passed": False,
                            "complete": False,
                            "validator_passed": False,
                            "verdict": "INCOMPLETE",
                            "reason": (
                                "no matted preview available and none could be "
                                "rebuilt from frames; raw video is NOT a valid "
                                "substitute for matted-content validation"
                            ),
                        },
                    }
                v_kwargs: dict[str, Any] = {
                    "video_path": vid,
                    "action_description": action_description,
                    "extra_rubrics": pass_extra_rubrics,
                    # rubric_override forces the group for this pass; a
                    # caller-supplied rubric_override replaces BOTH groups
                    # (advanced escape hatch) — honor it if provided.
                    "rubric_override": rubric_override or override,
                }
                if vlm_model:
                    v_kwargs["vlm_model"] = vlm_model
                verdict = asset_video_validator.validate_asset_video(
                    config, **v_kwargs
                )
                return {
                    "pass_name": pass_name,
                    "expected_ids": expected_ids,
                    "verdict_result": verdict,
                    "input": str(vid),
                    "input_kind": input_kind,
                }

            # Each pass is one independent, pinned request. Submit framing and
            # content once each, then merge in authored order rather than
            # completion order so verdict receipts remain deterministic.
            with ThreadPoolExecutor(
                max_workers=max(1, min(2, len(passes))),
                thread_name_prefix="video-vlm",
            ) as executor:
                pass_results = list(executor.map(_validate_one_pass, passes))

            for pass_result in pass_results:
                pass_name = str(pass_result["pass_name"])
                verdict = pass_result["verdict_result"]
                if verdict is None:
                    all_passed = False
                    all_complete = False
                    pass_detail[pass_name] = pass_result["detail"]
                    continue
                # VID-02: the ship gate is a COMPLETE, UNIQUE, all-PASS,
                # zero-UNKNOWN rubric set — computed here, not the validator's
                # own ``passed`` boolean. Missing / duplicate / UNKNOWN rubric
                # lines (a garbled VLM parse) must NOT count as a clean pass.
                expected_ids = pass_result["expected_ids"]
                pass_ok = _strict_pass_for_ids(verdict, expected_ids)
                # VID-03: independently track whether the pass was fully JUDGED
                # (complete + zero-UNKNOWN, regardless of PASS/FAIL). Only a
                # take whose every pass is complete may enter the least-bad
                # fallback pool — an UNKNOWN-riddled take is not "least-bad".
                pass_complete = _complete_for_ids(verdict, expected_ids)
                all_passed = all_passed and pass_ok
                all_complete = all_complete and pass_complete
                n_passed += verdict["n_passed"]
                n_failed += verdict["n_failed"]
                n_unknown += verdict["n_unknown"]
                merged_results.extend(verdict["rubric_results"])
                merged_failures.extend(
                    {"id": f["id"], "name": f["name"], "evidence": f["evidence"]}
                    for f in verdict["failures"]
                )
                pass_detail[pass_name] = {
                    "input": pass_result["input"],
                    "input_kind": pass_result["input_kind"],
                    "passed": pass_ok,
                    "complete": pass_complete,
                    "validator_passed": verdict["passed"],
                    "verdict": verdict["verdict"],
                    "rubric_results": verdict["rubric_results"],
                    "raw_response": verdict["raw_response"],
                }
            record["verdict"] = "PASS" if all_passed else "FAIL"
            record["passed"] = all_passed
            record["validation_complete"] = all_complete
            record["n_passed"] = n_passed
            record["n_failed"] = n_failed
            record["n_unknown"] = n_unknown
            record["failures"] = merged_failures
            record["validate_elapsed_s"] = round(time.time() - t, 1)
            (cand_dir / "verdict.json").write_text(
                json.dumps(
                    {
                        "verdict": record["verdict"],
                        "passed": all_passed,
                        "validation_complete": all_complete,
                        "rubric_results": merged_results,
                        "passes": pass_detail,
                    },
                    indent=2,
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
        except ToolError as e:
            record["error_stage"] = "validate"
            record["error"] = f"{e.code}: {e.message}"
        except Exception as e:  # noqa: BLE001
            record["error_stage"] = "validate"
            record["error"] = f"INTERNAL_ERROR: {e}"
        return record

    # ---- Serial retry loop: stop at the first take that clears both passes ----
    attempts: list[dict[str, Any]] = []
    winner: dict[str, Any] | None = None
    for idx in range(1, n_candidates + 1):
        record = _attempt_job(idx)
        attempts.append(record)
        if record.get("passed"):
            # Clean take — clears both validation passes (framing +
            # content, R1..R7).
            winner = record
            break

    # ---- Winner selection --------------------------------------------
    # A clean take (cleared both passes) wins outright. Otherwise, the fallback
    # pool is ONLY the fully-JUDGED takes (VID-03):
    #   validation_failed      → at least one attempt was fully judged
    #                            (validation_complete: complete rubric set, zero
    #                            UNKNOWN); ship the least-bad such take. A take
    #                            with UNKNOWN / unparsed rubrics is NOT judged and
    #                            can never win here — the "0 FAIL / 2 UNKNOWN
    #                            beats 1 FAIL / 0 UNKNOWN" bug.
    #   validation_incomplete  → attempts reached the validator but NONE produced
    #                            a complete verdict (only UNKNOWN / partial
    #                            parses). Nothing ships by default; only
    #                            ``allow_unvalidated=True`` lets the last such
    #                            take through with a risk marker.
    #   validation_unavailable → NO attempt ever reached validation (every
    #                            attempt errored in seedance/matte/validate, no
    #                            verdict anywhere). Nothing ships by default;
    #                            ``allow_unvalidated=True`` ships the last take.
    with_verdict = [c for c in attempts if "verdict" in c]
    judged = [c for c in with_verdict if c.get("validation_complete")]
    if winner is None:
        if judged:
            winner = min(judged, key=_fallback_score)
            validation_state = "validation_failed"
        elif with_verdict:
            # Reached the validator but never got a complete verdict.
            validation_state = "validation_incomplete"
            # Incomplete/UNKNOWN output is not an infrastructure outage and
            # cannot use the escape hatch; nothing ships.
        else:
            matte_failures = [
                c for c in attempts if c.get("error_stage") == "matte_health"
            ]
            validation_state = (
                "matte_failed" if matte_failures else "validation_unavailable"
            )
            if allow_unvalidated and not matte_failures:
                with_healthy_matte = [
                    c
                    for c in attempts
                    if "video" in c
                    and (c.get("matte_health") or {}).get("healthy")
                ]
                winner = (
                    with_healthy_matte[-1] if with_healthy_matte else None
                )
            # else: winner stays None → nothing ships.
    else:
        validation_state = "validated_pass"

    # ---- Publish final -------------------------------------------------
    # final/ must reflect ONLY this run. Copy the winner's own candidate dir
    # when there is one; otherwise clear any stale final/ from a previous run
    # so it is never reported as this run's output.
    final_dir = output_dir / "final"
    if winner and "video" in winner:
        cand_dir = Path(winner["video"]).parent
        _copy_attempt_artifacts(cand_dir, final_dir)
    elif final_dir.exists():
        shutil.rmtree(final_dir)

    # ``passed`` = the winning take cleared both hard rubrics (clean ship).
    # ``usable`` = the winner produced transparent PNG FRAMES THIS RUN that were
    # copied into final/, so it can be packed into a spritesheet even if it did
    # not clear both rubrics. Packing gates on ``usable`` (NOT ``passed``) — a
    # not-passed but validated winner is a degraded take, still shippable
    # best-effort.
    #
    # VID (GIF soft-fail): ``usable`` is anchored to the this-run PNG FRAME SET
    # (``matte_frame_count`` > 0 for the winner), NOT to the presence of the
    # derived ``transparent.gif``. The GIF is an optional encode; its soft-fail
    # must not make valid PNG frames — the exact inputs pack_spritesheet needs —
    # unusable. We still require the frames to actually be present in final/
    # (copied from the winner's own candidate dir), never merely lingering from
    # a prior run. ``best_effort`` flags the degraded-but-usable case.
    transparent_gif_path = final_dir / "rmbg2" / "transparent.gif"
    transparent_frames_dir = final_dir / "rmbg2" / "transparent_frames"
    winner_matted_this_run = bool(winner and winner.get("matte_frame_count"))
    final_frames = (
        sorted(transparent_frames_dir.glob("*.png"))
        if transparent_frames_dir.is_dir()
        else []
    )
    frames_present = bool(
        winner
        and "video" in winner
        and winner_matted_this_run
        and final_frames
    )
    # PR #123 review #5: presence of frames is NOT enough. A single alpha-zero
    # PNG, a 1-of-120 survival rate, or a ``would_empty`` leak state used to
    # pass this gate. Require the matted output to be HEALTHY — enough frames, a
    # real success ratio, visible alpha content, and a non-``would_empty`` leak
    # state — before declaring it usable / shippable.
    matte_health: dict[str, Any] | None = None
    if frames_present:
        matte_health = rmbg2_video.assess_matte_health(
            transparent_frames_dir,
            frames_total=int(
                winner.get("matte_frames_total")
                or winner.get("matte_frame_count")
                or len(final_frames)
            ),
            frames_failed=int(winner.get("matte_frames_failed") or 0),
            bg_leak_stats=winner.get("bg_leak_stats"),
        )
    else:
        matte_health = next(
            (
                attempt.get("matte_health")
                for attempt in reversed(attempts)
                if attempt.get("matte_health") is not None
            ),
            None,
        )
    has_matted_output = bool(
        frames_present and matte_health and matte_health["healthy"]
    )
    winner_passed = bool(
        winner and winner.get("passed") and has_matted_output
    )
    if winner and not has_matted_output and final_dir.exists():
        shutil.rmtree(final_dir)
    n_valid = len(with_verdict)
    n_judged = len(judged)

    summary = {
        "passed": winner_passed,
        "usable": has_matted_output,
        "best_effort": has_matted_output and not winner_passed,
        "matte_health": matte_health,
        "matting": winner.get("matting") if winner else None,
        "matting_requested": (
            winner.get("matting_requested") if winner else None
        ),
        "matting_fallback": bool(winner and winner.get("matting_fallback")),
        "matting_fallback_code": (
            winner.get("matting_fallback_code") if winner else None
        ),
        "matting_fallback_reason": (
            winner.get("matting_fallback_reason") if winner else None
        ),
        "matting_key_color": (
            winner.get("matting_key_color") if winner else None
        ),
        "validation_state": validation_state,
        "allow_unvalidated": allow_unvalidated,
        "winner_candidate": winner.get("candidate") if winner else None,
        "winner_validation_complete": bool(winner and winner.get("validation_complete")),
        "n_candidates": n_candidates,          # max attempts allowed
        "n_attempts": len(attempts),           # attempts actually run
        "n_valid": n_valid,                    # reached the validator (has a verdict)
        "n_judged": n_judged,                  # fully judged (complete, zero-UNKNOWN)
        "n_incomplete": n_valid - n_judged,    # verdict present but UNKNOWN/partial
        "n_passed_candidates": sum(1 for c in attempts if c.get("passed")),
        "final_dir": str(final_dir) if has_matted_output else None,
        "final_video": (
            str(final_dir / "video.mp4")
            if has_matted_output and (final_dir / "video.mp4").exists()
            else None
        ),
        "final_transparent_gif": (
            str(transparent_gif_path)
            if has_matted_output and transparent_gif_path.exists()
            else None
        ),
        "final_transparent_frames_dir": (
            str(transparent_frames_dir) if has_matted_output else None
        ),
        "final_frame_count": len(final_frames),
        "final_verdict": winner.get("verdict") if winner else None,
        "final_failures": winner.get("failures") if winner else None,
        # ``candidates`` retained as the key name for backward-compat with
        # downstream summary readers; entries are the serial
        # attempts (1 on a first-try pass, up to n_candidates otherwise).
        "candidates": attempts,
        "total_elapsed_s": round(time.time() - t_total, 1),
    }
    (output_dir / "_orchestrator_summary.json").write_text(
        json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    return summary


__all__ = ["produce_validated_video"]
