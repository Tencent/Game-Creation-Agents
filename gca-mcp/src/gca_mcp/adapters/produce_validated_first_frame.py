"""Closed-loop first-frame production: generate_image → validate_image → retry.

Mirrors ``produce_validated_video`` for static sprite first-frames.
On validator FAIL, prior failure evidence is grouped by rubric (via
``failure_summary.summarize_failures``), each rubric category gets a
generalised actionable directive, and the synthesised STRICTLY AVOID
addendum is appended to the next ``generate_image`` prompt. Hard cap at
``max_attempts`` (default 3); if no PASS, the **last** attempt is
selected (per project policy).

This catches the class of defect that the video-level validator
misses — composition issues that originate in the first frame and get
faithfully animated downstream by Seedance (e.g. shield rendered
floating beside the body with no visible holding hand → all 121
matted frames inherit the flaw → R2 PASS in video validator because
the prop "doesn't detach mid-animation" — it was never gripped to
begin with).

Output structure:
    output_dir/
    ├── attempt_01/  (image_prompt.txt, image.png, verdict.json)
    ├── attempt_02/
    ├── attempt_03/
    ├── final/      (mirror of selected attempt — what to feed into i2v)
    └── _orchestrator_summary.json
"""

from __future__ import annotations

import hashlib
import json
import shutil
import time
from pathlib import Path
from typing import Any

from ..config import Config
from ..errors import ToolError
from . import asset_image_validator, image_gen
from .failure_summary import compose_strictly_avoid_addendum


def _compose_prompt(base_prompt: str, prior_attempts: list[dict[str, Any]]) -> str:
    """Append synthesised STRICTLY AVOID addendum to base_prompt if any
    failures were observed in prior attempts; else return as-is."""
    addendum = compose_strictly_avoid_addendum(prior_attempts)
    if not addendum:
        return base_prompt
    return f"{base_prompt}\n\n{addendum}"


def _copy_attempt_to_final(
    attempt_dir: Path,
    final_dir: Path,
) -> None:
    if final_dir.exists():
        shutil.rmtree(final_dir)
    final_dir.mkdir(parents=True, exist_ok=True)
    for name in ("image.png", "verdict.json", "image_prompt.txt"):
        src = attempt_dir / name
        if src.is_file():
            shutil.copy2(src, final_dir / name)


def _complete_verdict(verdict: dict[str, Any]) -> bool:
    rubric_results = verdict.get("rubric_results") or []
    if not rubric_results:
        return False
    ids = [r.get("id") for r in rubric_results]
    if len(ids) != len(set(ids)):
        return False
    if any(r.get("verdict") not in ("PASS", "FAIL") for r in rubric_results):
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
    reported = verdict.get("overall_verdict_reported")
    derived = (
        "FAIL" if any(r.get("verdict") == "FAIL" for r in rubric_results) else "PASS"
    )
    if reported is not None and reported != derived:
        return False
    return True


def _strict_passed(verdict: dict[str, Any]) -> bool:
    """A first frame passes only with a complete, unique, all-PASS verdict."""
    return bool(
        _complete_verdict(verdict)
        and all(result.get("verdict") == "PASS" for result in verdict["rubric_results"])
    )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _text_sha256(value: str) -> str:
    return hashlib.sha256(value.strip().encode("utf-8")).hexdigest()


def _rubric_contract_sha256(rubrics: list[dict[str, str]]) -> str:
    normalized = sorted(
        (
            {
                "id": str(rubric["id"]).strip(),
                "name": str(rubric["name"]).strip(),
                "description": str(rubric["description"]).strip(),
            }
            for rubric in rubrics
        ),
        key=lambda rubric: rubric["id"].upper(),
    )
    payload = json.dumps(
        normalized, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def has_validated_provenance(
    image_path: Path,
    *,
    expected_rubrics: list[dict[str, str]],
    character_description: str,
    framing_profile: str,
) -> bool:
    """Return true when a sibling verdict proves this exact image passed."""
    verdict_path = image_path.parent / "verdict.json"
    if not image_path.is_file() or not verdict_path.is_file():
        return False
    try:
        verdict = json.loads(verdict_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return False
    contract = verdict.get("validation_contract") or {}
    return bool(
        verdict.get("image_sha256") == _sha256(image_path)
        and _strict_passed(verdict)
        and contract.get("rubric_contract_sha256")
        == _rubric_contract_sha256(expected_rubrics)
        and contract.get("character_description_sha256")
        == _text_sha256(character_description)
        and contract.get("framing_profile") == framing_profile
    )


def produce_validated_first_frame(
    config: Config,
    *,
    image_prompt: str,
    character_description: str,
    output_dir: Path,
    # generate_image params
    size: str = "1024x1024",
    quality: str = "low",
    safe_bg_color: str | None = "#FF00FF",
    # Framing block for the generated first frame. This adapter produces sprite
    # first frames, so it defaults to ``grounded_sprite`` (feet on the bottom
    # edge). It is passed explicitly to image_gen; a role-less safe-bg call
    # resolves to centered_asset, so grounded sprites must request this profile.
    # Projectile bodies pass ``centered_asset``.
    framing_profile: str = "grounded_sprite",
    enforce_safe_bg_avoidance: bool = True,
    image_max_retries: int = 3,
    image_retry_delay: float = 10.0,
    # concept-grounding seed: when set, each attempt is produced via
    # edit_image(input=ref_image) instead of text-only generate_image, so the
    # first frame is derived FROM the grounded concept crop (GDD §2 Ref Crop)
    # and the sprite reliably matches the approved concept. None → text-only.
    ref_image: str | None = None,
    # validator params
    vlm_model: str | None = None,
    extra_rubrics: list[dict[str, str]] | None = None,
    rubric_override: list[dict[str, str]] | None = None,
    # loop control
    max_attempts: int = 3,
) -> dict[str, Any]:
    """Closed-loop first-frame production.

    ``image_prompt`` — the user-content for ``generate_image`` (will be
    extended with safe-bg / framing blocks via image_gen's normal
    wrapping logic, plus the STRICTLY AVOID synthesised from each
    prior attempt's validator failures).

    ``character_description`` — passed straight to the validator's
    ``character_description`` arg (used by I1 identity match).

    ``ref_image`` — optional grounded concept crop (GDD §2 Ref Crop). When set,
    each attempt is produced via ``edit_image(input=ref_image)`` so the first
    frame is locked to the approved concept; when ``None`` the original
    text-only ``generate_image`` path is used (fully backward compatible).
    """
    if max_attempts < 1:
        raise ToolError(
            "INVALID_CONFIG",
            f"max_attempts must be >= 1, got {max_attempts}",
        )
    if not image_prompt.strip():
        raise ToolError("INVALID_CONFIG", "`image_prompt` must be non-empty.")
    if not character_description.strip():
        raise ToolError("INVALID_CONFIG", "`character_description` must be non-empty.")

    active_rubrics = (
        list(rubric_override)
        if rubric_override
        else asset_image_validator.rubric_dicts(
            asset_image_validator.DEFAULT_IMAGE_RUBRICS
        )
    )
    if extra_rubrics:
        active_rubrics += list(extra_rubrics)

    output_dir.mkdir(parents=True, exist_ok=True)
    for stale in output_dir.glob("attempt_*"):
        if stale.is_dir() and stale.name.removeprefix("attempt_").isdigit():
            shutil.rmtree(stale)
    stale_final = output_dir / "final"
    if stale_final.exists():
        shutil.rmtree(stale_final)
    attempts: list[dict[str, Any]] = []
    selected_idx: int | None = None
    t_total = time.time()

    for n in range(1, max_attempts + 1):
        t_attempt = time.time()
        attempt_dir = output_dir / f"attempt_{n:02d}"
        # FF (stale attempt): start every attempt from a CLEAN dir. Reusing an
        # attempt_NN/ from a previous run (same output_dir) left a stale
        # verdict.json on disk; if THIS run then wrote a fresh image.png but
        # errored before writing a new verdict (or fell back to this attempt),
        # `_copy_attempt_to_final` would copy the OLD verdict alongside the NEW
        # image — publishing a mismatched PASS verdict for the current image.
        # Clearing the owned dir first makes each attempt's artifacts belong to
        # exactly this run.
        if attempt_dir.exists():
            shutil.rmtree(attempt_dir)
        attempt_dir.mkdir(parents=True, exist_ok=True)

        prompt = _compose_prompt(image_prompt, attempts)
        (attempt_dir / "image_prompt.txt").write_text(prompt, encoding="utf-8")

        record: dict[str, Any] = {
            "attempt": n,
            "prior_failures_summarised": len(attempts) > 0
            and any(a.get("failures") for a in attempts),
            "prompt_chars": len(prompt),
            "mode": "edit_image" if ref_image else "generate_image",
        }

        # 1) generate image — from the grounded Ref Crop (edit_image) when a
        #    ref_image seed is provided, else text-only generate_image.
        img_path = attempt_dir / "image.png"
        try:
            if ref_image:
                image_gen.edit_image(
                    config,
                    prompt=prompt,
                    input_path=str(ref_image),
                    output=img_path,
                    size=size,
                    quality=quality,
                    safe_bg_color=safe_bg_color,
                    enforce_safe_bg_avoidance=enforce_safe_bg_avoidance,
                    framing_profile=framing_profile,
                )
            else:
                image_gen.generate_image(
                    config,
                    prompt=prompt,
                    output=img_path,
                    size=size,
                    quality=quality,
                    max_retries=image_max_retries,
                    retry_delay=image_retry_delay,
                    safe_bg_color=safe_bg_color,
                    enforce_safe_bg_avoidance=enforce_safe_bg_avoidance,
                    framing_profile=framing_profile,
                )
        except ToolError as e:
            record["error_stage"] = record["mode"]
            record["error"] = f"{e.code}: {e.message}"
            record["elapsed_s"] = round(time.time() - t_attempt, 1)
            attempts.append(record)
            break
        record["image"] = str(img_path)

        # 2) validate
        try:
            v_kwargs: dict[str, Any] = {
                "image_path": img_path,
                "character_description": character_description,
                "extra_rubrics": extra_rubrics,
                "rubric_override": rubric_override,
            }
            if vlm_model:
                v_kwargs["vlm_model"] = vlm_model
            verdict = asset_image_validator.validate_image(config, **v_kwargs)
        except ToolError as e:
            record["error_stage"] = "validate"
            record["error"] = f"{e.code}: {e.message}"
            record["elapsed_s"] = round(time.time() - t_attempt, 1)
            attempts.append(record)
            break
        record["verdict"] = verdict["verdict"]
        # FF-03: recompute the ship gate ourselves — a complete, unique,
        # all-PASS, zero-UNKNOWN rubric set — instead of trusting the
        # validator's own aggregate ``passed`` boolean.
        strict_pass = _strict_passed(verdict)
        validation_complete = _complete_verdict(verdict)
        record["passed"] = strict_pass
        record["validation_complete"] = validation_complete
        record["validator_passed"] = verdict["passed"]
        record["n_passed"] = verdict["n_passed"]
        record["n_failed"] = verdict["n_failed"]
        record["n_unknown"] = verdict["n_unknown"]
        record["failures"] = [
            {"id": f["id"], "name": f["name"], "evidence": f["evidence"]}
            for f in verdict["failures"]
        ]
        record["elapsed_s"] = round(time.time() - t_attempt, 1)
        attempts.append(record)

        (attempt_dir / "verdict.json").write_text(
            json.dumps(
                {
                    "verdict": verdict["verdict"],
                    "passed": strict_pass,
                    "validation_complete": validation_complete,
                    "validator_passed": verdict["passed"],
                    "rubric_results": verdict["rubric_results"],
                    "n_unknown": verdict["n_unknown"],
                    "duplicate_ids": verdict.get("duplicate_ids") or [],
                    "unexpected_ids": verdict.get("unexpected_ids") or [],
                    "overall_verdict_reported": verdict.get("overall_verdict_reported"),
                    "reported_failed_ids": verdict.get("reported_failed_ids"),
                    "failed_line_mismatch": verdict.get("failed_line_mismatch", False),
                    "aggregate_duplicate": verdict.get("aggregate_duplicate", False),
                    "aggregate_missing": verdict.get("aggregate_missing", False),
                    "validation_contract": {
                        "rubric_ids": [
                            str(result["id"]) for result in verdict["rubric_results"]
                        ],
                        "rubric_contract_sha256": _rubric_contract_sha256(
                            active_rubrics
                        ),
                        "character_description_sha256": _text_sha256(
                            character_description
                        ),
                        "framing_profile": framing_profile,
                    },
                    "raw_response": verdict["raw_response"],
                },
                indent=2,
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

        if strict_pass:
            selected_idx = n - 1
            break

    # --- Select the winner (FF-02) --------------------------------------
    # A clean PASS already set selected_idx above. Otherwise fall back to the
    # last attempt that actually produced an IMAGE this run — NOT simply the
    # last event (which may be an infra error with no image, discarding an
    # earlier generated-but-FAILED candidate).
    if selected_idx is None:
        # Prefer the latest image that actually received a complete verdict.
        # A later image whose validator crashed is not better evidence.
        for i in range(len(attempts) - 1, -1, -1):
            if "image" in attempts[i] and attempts[i].get("validation_complete"):
                selected_idx = i
                break
    if selected_idx is None:
        for i in range(len(attempts) - 1, -1, -1):
            if "image" in attempts[i]:
                selected_idx = i
                break
    selected = attempts[selected_idx] if selected_idx is not None else None

    # --- Publish final (FF-01) ------------------------------------------
    # final/ must reflect ONLY an artifact produced by THIS run. A stale
    # final/ from a previous run (reused output_dir) must never be reported
    # as the current result — so we clear it up-front and only repopulate it
    # when the selected attempt has a real image from this run.
    final_dir = output_dir / "final"
    final_has_current_image = False
    if selected and "image" in selected and Path(selected["image"]).is_file():
        _copy_attempt_to_final(
            output_dir / f"attempt_{selected['attempt']:02d}",
            final_dir,
        )
        final_has_current_image = (final_dir / "image.png").is_file()
        final_verdict_path = final_dir / "verdict.json"
        if final_has_current_image and final_verdict_path.is_file():
            final_verdict = json.loads(final_verdict_path.read_text(encoding="utf-8"))
            final_verdict["image_sha256"] = _sha256(final_dir / "image.png")
            final_verdict_path.write_text(
                json.dumps(final_verdict, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
    elif final_dir.exists():
        # No current image selected — do not let a leftover final/ masquerade
        # as this run's output.
        shutil.rmtree(final_dir)

    summary = {
        "passed": bool(selected and selected.get("passed")),
        "selected_attempt": selected["attempt"] if selected else None,
        "n_attempts": len(attempts),
        "max_attempts": max_attempts,
        "final_dir": str(final_dir) if final_has_current_image else None,
        "final_image": (
            str(final_dir / "image.png") if final_has_current_image else None
        ),
        "final_verdict": selected.get("verdict") if selected else None,
        "final_failures": selected.get("failures") if selected else None,
        "attempts": attempts,
        "total_elapsed_s": round(time.time() - t_total, 1),
    }
    (output_dir / "_orchestrator_summary.json").write_text(
        json.dumps(summary, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    return summary


__all__ = ["has_validated_provenance", "produce_validated_first_frame"]
