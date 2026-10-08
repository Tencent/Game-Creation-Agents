"""Top-level chained orchestrator: serial-retry first-frame → serial-retry video.

Wraps the two stage-orchestrators with **different validation
strategies per stage** (deliberate — they have different cost /
convergence profiles):

    - ``produce_validated_first_frame``  — serial retry up to
      ``image_max_attempts`` with prior-failure STRICTLY AVOID
      injection. Empirically converges on attempt 2-3 for the common
      first-frame defects (identity, hand-prop, view consistency)
      because gpt-image-2 actually responds to feedback. Image gen is
      cheap (~1-2 min/attempt), so serial cost is acceptable.

    - ``produce_validated_video``        — one quality candidate by default.
      Validation failures regenerate only when ``video_n_candidates`` is
      explicitly raised. Seedance no-output/network failures use an independent
      transient retry policy. Each quality candidate is a fresh Seedance draw
      (new seed) that is matted and checked against two
      HARD rubrics — R1 (in-frame: nothing clipped off the canvas) and
      R2 (scale-locked: the character doesn't balloon / shrink from a
      silent zoom). The loop stops at the first take that clears BOTH;
      if none does within the cap, the least-bad take ships as a
      best-effort winner. These two defects are per-draw random and
      don't respond to STRICTLY AVOID prompting, so draw-check-redraw
      converges in a couple of tries.

The image stage NEVER blocks the video stage — even if no image
attempt PASSes the 8-rubric I1..I8 check, the orchestrator proceeds
to the video stage with the LAST-TRIED image (per project policy
"max N attempts then proceed regardless").

Output structure:
    output_dir/
    ├── first_frame/    (serial-retry orchestrator output)
    │   ├── attempt_01/ … attempt_NN/
    │   ├── final/      (last attempt mirror — what feeds into video)
    │   └── _orchestrator_summary.json
    ├── video/          (serial-retry orchestrator output)
    │   ├── candidate_01/ … candidate_MM/   (one dir per attempt)
    │   ├── final/      (winner mirror — MP4 + transparent.gif + frames)
    │   └── _orchestrator_summary.json
    └── _full_chain_summary.json
"""

from __future__ import annotations

import json
import shutil
import time
from pathlib import Path
from typing import Any

from ..config import AssetSeedanceDefaults, Config
from ..errors import ToolError
from . import (
    asset_image_validator,
    asset_video_validator,
    produce_validated_first_frame as image_orch,
    produce_validated_video as video_orch,
    seedance,
)


def produce_validated_sprite_action(
    config: Config,
    *,
    image_prompt: str,
    character_description: str,
    video_user_content: str,
    output_dir: Path,
    # Image-stage params (serial retry)
    safe_bg_color: str = "#FF00FF",
    image_size: str = "1024x1024",
    image_quality: str = "low",
    image_max_attempts: int = 3,
    image_extra_rubrics: list[dict[str, str]] | None = None,
    # concept-grounding seed for the image stage: when set, the first frame is
    # produced via edit_image(input=ref_image) (the GDD §2 Ref Crop) so the
    # sprite is locked to the approved concept. Ignored when
    # first_frame_override is set (image stage is skipped entirely).
    ref_image: str | Path | None = None,
    # i2v / video-stage params (serial retry)
    duration: int = 5,
    ratio: str = "1:1",
    resolution: str = "720p",
    seedance_model: str = "fast",
    seed_base: int | None = None,
    bg_hex: str | None = None,
    aug_preset: str = "off",
    rmbg_fg_cutoff: int = 230,
    gif_alpha_threshold: int = 200,
    align_feet_to_bottom: bool | None = None,
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
    video_n_candidates: int | None = None,
    video_extra_rubrics: list[dict[str, str]] | None = None,
    # VLM
    vlm_model: str | None = None,
    # Effects control
    effects: bool | None = None,
    # Animation profile — selects the framing / alignment / rubric contract.
    #   "character"  → grounded figure: foot-anchor first frame, feet-aligned
    #                  matte, character I1..I8 + R1..R7 rubrics, effects off.
    #   "projectile" → free-flying projectile BODY (bullet/arrow/fireball/bolt):
    #                  centered (NOT grounded) first frame, no feet alignment,
    #                  projectile P1..P6 image + PR1..PR6 video rubrics, and
    #                  effects default OFF (the trail/impact/explosion are
    #                  runtime particles, not part of the body animation).
    # Explicit ``effects`` / ``align_feet_to_bottom`` args still override the
    # profile default.
    animation_profile: str = "character",
    # Skip image stage — use a pre-generated reference as first_frame
    first_frame_override: str | Path | None = None,
) -> dict[str, Any]:
    """Run the full sprite-action production chain end-to-end.

    Image stage uses serial retry with STRICTLY AVOID feedback
    injection (cap = ``image_max_attempts``); video stage defaults to one
    quality candidate and only uses validation-driven regeneration when
    ``video_n_candidates`` is explicitly raised. The
    image stage's last-attempt image is always forwarded to the video
    stage, PASS or not.

    If ``first_frame_override`` is provided, Stage 1 (image generation)
    is skipped entirely and the given image is used directly as the
    first_frame for Seedance i2v. Use this to share ONE reference image
    across all actions of the same character.

    If ``ref_image`` is provided (and ``first_frame_override`` is not), Stage 1
    still runs but each first-frame attempt is seeded via
    ``edit_image(input=ref_image)`` — the grounded GDD §2 Ref Crop — so the
    generated sprite matches the approved concept instead of being drawn from
    text alone.
    """
    if not video_user_content:
        raise ToolError(
            "INVALID_CONFIG",
            "`video_user_content` is required.",
        )
    if video_n_candidates is None:
        runtime_defaults = getattr(getattr(config, "assets", None), "seedance", None)
        video_n_candidates = int(
            getattr(
                runtime_defaults,
                "max_quality_candidates",
                AssetSeedanceDefaults.max_quality_candidates,
            )
        )
    if video_n_candidates < 1:
        raise ToolError(
            "INVALID_CONFIG",
            f"video_n_candidates must be >= 1, got {video_n_candidates}",
        )
    if not first_frame_override and not (image_prompt and character_description):
        raise ToolError(
            "INVALID_CONFIG",
            "`image_prompt` and `character_description` are required "
            "when `first_frame_override` is not provided.",
        )

    # ---- Resolve the animation profile → framing / alignment / rubric contract.
    if animation_profile not in ("character", "projectile"):
        raise ToolError(
            "INVALID_CONFIG",
            f"animation_profile must be 'character' or 'projectile', got "
            f"{animation_profile!r}",
        )
    is_projectile = animation_profile == "projectile"
    # Per-profile defaults; an EXPLICIT arg (not None) always wins.
    #   character  → grounded: foot-anchor first frame, feet-aligned matte,
    #                body animation only.
    #   projectile → free-flying: centered (no foot-anchor), no feet alignment,
    #                body animation only.
    # FX are runtime ParticleEmitter recipes for every profile, so effects stay
    # OFF unless a non-standard caller explicitly opts in.
    resolved_align_feet = (
        align_feet_to_bottom
        if align_feet_to_bottom is not None
        else (not is_projectile)
    )
    resolved_effects = effects if effects is not None else False
    # Rubric contracts: projectile bodies use the projectile-specific image +
    # video rubric groups (no hand-prop / limb / grounded checks). A caller who
    # supplies image_extra_rubrics still gets them appended (via extra_rubrics).
    image_rubric_override = (
        asset_image_validator.rubric_dicts(
            asset_image_validator.PROJECTILE_IMAGE_RUBRICS
        )
        if is_projectile
        else None
    )
    video_framing_group = (
        asset_video_validator.PROJECTILE_FRAMING_RUBRICS if is_projectile else None
    )
    video_content_group = (
        asset_video_validator.PROJECTILE_CONTENT_RUBRICS if is_projectile else None
    )

    output_dir.mkdir(parents=True, exist_ok=True)
    stale_video_dir = output_dir / "video"
    if stale_video_dir.exists():
        shutil.rmtree(stale_video_dir)
    chain_t0 = time.time()

    # ---- Stage 1: first frame (skip if override provided) ----
    # ``image_stage`` is an explicit tri-state so the overall pass computation
    # never depends on a possibly-absent ``image_result["passed"]`` key:
    #   - "passed"             → the image orchestrator cleared its rubric set
    #   - "override_validated" → a caller-provided frame whose exact bytes have
    #                            passing provenance, or which passed a fresh
    #                            image validation in this run.
    #   - "failed"             → an image was produced but did not pass
    if first_frame_override:
        override_path = Path(first_frame_override)
        if not override_path.exists():
            raise ToolError(
                "INPUT_FILE_NOT_FOUND",
                f"first_frame_override not found: {override_path}",
            )
        # Validate the override is a real, decodable image before trusting it.
        try:
            from PIL import Image

            with Image.open(str(override_path)) as im:
                im.verify()
        except Exception as e:  # noqa: BLE001
            raise ToolError(
                "INVALID_CONFIG",
                f"first_frame_override is not a decodable image: {override_path} ({e})",
            ) from e
        # Bind prompt + matting to a background that is ACTUALLY present.
        # Opaque keyed images must expose a dominant safe-color border.
        # Transparent images are explicitly composited onto a new keyed staging
        # frame. Opaque white/JPEG inputs are rejected rather than relabelled.
        detected_bg = seedance.detect_existing_safe_bg(override_path)
        staged_override: Path | None = None
        if detected_bg is None:
            if bg_hex is not None and bg_hex not in seedance.SAFE_BG_OPTIONS:
                raise ToolError(
                    "INVALID_CONFIG",
                    f"Unsupported bg_hex={bg_hex!r}; expected one of "
                    f"{seedance.SAFE_BG_OPTIONS}.",
                )
            staged_override = output_dir / "override_staged.png"
            detected_bg = seedance.stage_transparent_on_safe_bg(
                override_path,
                staged_override,
                bg_hex=bg_hex,
            )
        # If the caller ALSO passed an explicit matting bg that contradicts the
        # detected background, refuse rather than silently keying the wrong
        # color. Only ``bg_hex`` is checked: it defaults to None (so an explicit
        # value is unambiguous), and it — not ``safe_bg_color``, which only
        # drives the SKIPPED image-generation stage — is what matting keys on.
        if (
            bg_hex is not None
            and bg_hex in seedance.SAFE_BG_OPTIONS
            and bg_hex != detected_bg
        ):
            raise ToolError(
                "INVALID_CONFIG",
                f"first_frame_override background is {detected_bg} but "
                f"bg_hex={bg_hex!r} was given — they must match. Pass "
                f"bg_hex={detected_bg!r} or omit it so it is auto-detected.",
            )
        bg_hex_resolved = detected_bg
        final_image = str(staged_override or override_path)
        validation_description = character_description or video_user_content
        expected_image_rubrics = (
            image_rubric_override
            or asset_image_validator.rubric_dicts(
                asset_image_validator.DEFAULT_IMAGE_RUBRICS
            )
        )
        if image_extra_rubrics:
            expected_image_rubrics = list(expected_image_rubrics) + list(
                image_extra_rubrics
            )
        provenance_valid = (
            staged_override is None
            and image_orch.has_validated_provenance(
                override_path,
                expected_rubrics=list(expected_image_rubrics),
                character_description=validation_description,
                framing_profile=(
                    "centered_asset" if is_projectile else "grounded_sprite"
                ),
            )
        )
        image_result = {
            "skipped": True,
            "first_frame_override": str(override_path),
            "staged_first_frame": final_image if staged_override else None,
            "detected_bg_hex": detected_bg,
            "provenance_valid": provenance_valid,
        }
        if provenance_valid:
            image_stage = "override_validated"
            image_result["content_validated"] = True
        else:
            validation_kwargs: dict[str, Any] = {
                "image_path": Path(final_image),
                "character_description": validation_description,
                "extra_rubrics": image_extra_rubrics,
                "rubric_override": image_rubric_override,
            }
            if vlm_model:
                validation_kwargs["vlm_model"] = vlm_model
            try:
                override_verdict = asset_image_validator.validate_image(
                    config, **validation_kwargs
                )
            except ToolError as exc:
                image_stage = "failed"
                final_image = None
                image_result.update(
                    {
                        "content_validated": False,
                        "error": f"{exc.code}: {exc.message}",
                    }
                )
            else:
                override_passed = image_orch._strict_passed(override_verdict)
                image_stage = "override_validated" if override_passed else "failed"
                if not override_passed:
                    final_image = None
                image_result.update(
                    {
                        "content_validated": override_passed,
                        "passed": override_passed,
                        "verdict": override_verdict,
                    }
                )
    else:
        if bg_hex is not None and safe_bg_color is not None and bg_hex != safe_bg_color:
            raise ToolError(
                "INVALID_CONFIG",
                f"Generated first frame uses safe_bg_color={safe_bg_color!r} "
                f"but matting requested bg_hex={bg_hex!r}; they must match.",
            )
        bg_hex_resolved = bg_hex or safe_bg_color
        image_dir = output_dir / "first_frame"
        image_kwargs: dict[str, Any] = {
            "image_prompt": image_prompt,
            "character_description": character_description,
            "output_dir": image_dir,
            "size": image_size,
            "quality": image_quality,
            "safe_bg_color": safe_bg_color,
            # Framing is requested by profile. A projectile body is a centered
            # free-flying object (no feet / ground); a character is grounded.
            "framing_profile": (
                "centered_asset" if is_projectile else "grounded_sprite"
            ),
            "max_attempts": image_max_attempts,
            "extra_rubrics": image_extra_rubrics,
        }
        # Projectile bodies validate against the projectile image rubric set
        # (no hand-prop / limb / grounded-foot checks) instead of the character
        # I1..I8 default.
        if image_rubric_override is not None:
            image_kwargs["rubric_override"] = image_rubric_override
        if ref_image:
            image_kwargs["ref_image"] = str(ref_image)
        if vlm_model:
            image_kwargs["vlm_model"] = vlm_model
        image_result = image_orch.produce_validated_first_frame(config, **image_kwargs)
        final_image = image_result.get("final_image")
        image_stage = "passed" if image_result.get("passed") else "failed"
        if final_image:
            detected_generated_bg = seedance.detect_existing_safe_bg(Path(final_image))
            image_result["detected_bg_hex"] = detected_generated_bg
            if detected_generated_bg is None:
                image_stage = "failed"
                final_image = None
                image_result["error"] = (
                    "generated first frame has no detectable "
                    "#FF00FF/#00F000 border background"
                )
            elif (
                bg_hex_resolved is not None and detected_generated_bg != bg_hex_resolved
            ):
                image_stage = "failed"
                final_image = None
                image_result["error"] = (
                    f"generated first-frame background is "
                    f"{detected_generated_bg}, expected {bg_hex_resolved}"
                )
            else:
                bg_hex_resolved = detected_generated_bg
    if not final_image:
        chain_summary = {
            "stage": "first_frame",
            "passed": False,
            "image_stage": image_stage,
            "image_passed": image_stage in ("passed", "override_validated"),
            "image_result": image_result,
            "video_result": None,
            "abort_reason": (
                "No first-frame image was produced; cannot continue to video stage."
            ),
            "total_elapsed_s": round(time.time() - chain_t0, 1),
        }
        (output_dir / "_full_chain_summary.json").write_text(
            json.dumps(chain_summary, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        return chain_summary

    # ---- Stage 2: serial-retry video ----
    # P0 #1: the animation profile MUST reach build_i2v_prompt so a projectile
    # gets the free-flying (centered, no-ground-contact, no-character-language)
    # template. Previously only the rubric groups were profile-aware while the
    # prompt was always the grounded-character template — so a fireball was told
    # to "stand on the bottom edge with feet anchored" (generation) while the
    # projectile validator demanded it "must NOT touch the bottom edge"
    # (validation): mutually exclusive contracts.
    base_prompt = seedance.build_i2v_prompt(
        user_content=video_user_content,
        bg_hex=bg_hex_resolved,
        effects=resolved_effects,
        animation_profile=animation_profile,
    )
    video_dir = output_dir / "video"
    action_description = video_user_content
    video_kwargs: dict[str, Any] = {
        "base_prompt": base_prompt,
        "first_frame": final_image,
        "output_dir": video_dir,
        "action_description": action_description,
        "duration": duration,
        "ratio": ratio,
        "resolution": resolution,
        "model": seedance_model,
        "bg_hex": bg_hex_resolved,
        "aug_preset": aug_preset,
        "rmbg_fg_cutoff": rmbg_fg_cutoff,
        "gif_alpha_threshold": gif_alpha_threshold,
        "align_feet_to_bottom": resolved_align_feet,
        "foot_margin_px": foot_margin_px,
        "matte_workers": matte_workers,
        "max_frames": max_frames,
        "drop_bg_leak": drop_bg_leak,
        "bg_leak_area_threshold": bg_leak_area_threshold,
        "bg_leak_color_dist": bg_leak_color_dist,
        "bg_leak_alpha_threshold": bg_leak_alpha_threshold,
        "bg_leak_keep_min_frames": bg_leak_keep_min_frames,
        "bg_leak_alpha_weighted": bg_leak_alpha_weighted,
        "n_candidates": video_n_candidates,
        "extra_rubrics": video_extra_rubrics,
    }
    # Projectile bodies swap the character content/framing rubric groups for
    # the projectile-specific sets (no hand-prop / limb / grounded checks).
    if video_framing_group is not None:
        video_kwargs["framing_rubrics"] = video_framing_group
    if video_content_group is not None:
        video_kwargs["content_rubrics"] = video_content_group
    if seed_base is not None:
        video_kwargs["seed_base"] = seed_base
    if vlm_model:
        video_kwargs["vlm_model"] = vlm_model
    video_result = video_orch.produce_validated_video(config, **video_kwargs)

    # ``passed`` = the winning take cleared both hard rubrics (clean ship).
    # ``usable`` = a serial-retry winner with matted frames exists on disk
    # and can be packed into a spritesheet, even if it did not clear both
    # rubrics. Serial retry always yields a usable winner when any take
    # produced matted frames (the least-bad take), so packing must gate on
    # ``usable``, NOT ``passed`` —
    # a not-passed winner is a degraded take, never a reason to skip
    # packing and leave the action with no spritesheet at all.
    # ``best_effort`` flags exactly that degraded-but-usable case so the
    # caller can log it and optionally queue a regeneration later.
    video_usable = bool(video_result.get("usable"))
    image_stage_success = image_stage in ("passed", "override_validated")
    overall_passed = bool(
        image_stage_success and video_result.get("passed") and video_usable
    )
    chain_summary = {
        "passed": overall_passed,
        "usable": video_usable,
        "best_effort": video_usable and not overall_passed,
        "animation_profile": animation_profile,
        "image_content_validated": image_stage in ("passed", "override_validated"),
        "bg_hex": bg_hex_resolved,
        "image_stage": image_stage,
        "image_passed": image_stage_success,
        "video_passed": bool(video_result.get("passed")),
        "video_usable": video_usable,
        "matting": video_result.get("matting"),
        "matting_requested": video_result.get("matting_requested"),
        "matting_fallback": bool(video_result.get("matting_fallback")),
        "matting_fallback_code": video_result.get("matting_fallback_code"),
        "matting_fallback_reason": video_result.get("matting_fallback_reason"),
        "matting_key_color": video_result.get("matting_key_color"),
        "image_attempts": image_result.get("n_attempts"),
        "video_n_candidates": video_n_candidates,
        "video_winner_candidate": video_result.get("winner_candidate"),
        "final_image": final_image,
        "final_video": video_result.get("final_video"),
        "final_transparent_gif": video_result.get("final_transparent_gif"),
        "final_transparent_frames_dir": video_result.get(
            "final_transparent_frames_dir"
        ),
        "final_dir": video_result.get("final_dir"),
        "image_failures": image_result.get("final_failures"),
        "video_failures": video_result.get("final_failures"),
        "image_result": image_result,
        "video_result": video_result,
        "total_elapsed_s": round(time.time() - chain_t0, 1),
    }
    (output_dir / "_full_chain_summary.json").write_text(
        json.dumps(chain_summary, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    return chain_summary


__all__ = ["produce_validated_sprite_action"]
