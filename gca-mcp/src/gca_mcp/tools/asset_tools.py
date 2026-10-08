"""Asset generation tools (API-backed): images + Seedance i2v video.

Hosts the full asset-generation surface in one module:
- Static images: ``generate_image``, ``edit_image``, ``extend_background``
- Video: ``generate_seedance_video``, ``rmbg2_video_matting``
- Helpers: ``build_i2v_prompt``

Single-image RMBG-2.0 matting (``rmbg2_matting``) lives next to the
deterministic post-processing tools in ``postprocess_tools.py`` because
it's conceptually post-processing, not generation.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from functools import wraps
from pathlib import Path
from typing import Any

from PIL import Image

from ..adapters import (
    asset_bbox,
    asset_image_validator,
    asset_slot_validator,
    asset_video_validator,
    bg_extend,
    image_gen,
    matting_availability,
    rmbg2,
    rmbg2_video,
    seedance,
    sprite_metadata_analyzer,
    spritesheet_packer,
)
from ..adapters import (
    produce_validated_first_frame as produce_validated_first_frame_adapter,
)
from ..adapters import (
    produce_validated_sprite_action as produce_validated_sprite_action_adapter,
)
from ..adapters import (
    produce_validated_video as produce_validated_video_adapter,
)
from ..config import DOUBAO_VLM_MODEL
from ..errors import ToolError, is_transient_rmbg_error
from ..evidence import (
    atomic_write_json,
    manifest_entries,
    secure_file_lock,
    workspace_lock_path,
)
from ..server import ToolCtx, ToolDef
from ..verify_epoch import build_mutation_flock
from ..workspace import validated_workspace_root
from ._shared import gdd_slot_context_sha256 as _gdd_slot_context_sha256
from ._shared import slot_contract_sha256 as _slot_contract_sha256

_OUTPUT_REGISTRY_LOCK = threading.Lock()
_ACTIVE_OUTPUT_DIRS: set[Path] = set()
_ASSET_MANIFEST_LOCK = threading.Lock()
_ASSET_WRITE_ROOTS = ("assets", "design")
_SLOT_EVIDENCE_ROOT = Path(".playtest") / "verify_middle" / "slot"
_PINNED_VLM_MODEL_SCHEMA = {
    "type": "string",
    "enum": [DOUBAO_VLM_MODEL],
    "description": "Pinned Doubao 2.0 model identity; alternate models are rejected.",
}
_READ_ONLY_REPO_REFERENCE_ROOTS = (
    Path(".claude") / "skills" / "game-pipeline" / "references",
)


@contextmanager
def _asset_manifest_lock(manifest_path: Path):
    """Serialize the single-writer reducer across threads and MCP processes."""
    with _ASSET_MANIFEST_LOCK:
        workspace_root = manifest_path.parent.parent
        lock_path = workspace_lock_path(workspace_root, "asset-manifest")
        with secure_file_lock(lock_path, exclusive=True):
            yield


def _require_workspace_root(ctx: ToolCtx) -> Path:
    workspace = getattr(ctx.state, "workspace", None)
    if workspace is None:
        raise ToolError(
            "NO_WORKSPACE_OPEN",
            "Asset file access requires an active workspace.",
            hint="Call init_workspace(path=…, mode=…) first.",
        )
    return validated_workspace_root(workspace)


def _resolve_under_workspace(ctx: ToolCtx, p: str | None) -> Path | None:
    """Resolve a path inside the active workspace without cwd fallback."""
    if not p:
        return None
    root = _require_workspace_root(ctx)
    path = Path(p).expanduser()
    resolved = (path if path.is_absolute() else root / path).resolve()
    if not resolved.is_relative_to(root):
        raise ToolError(
            "INVALID_PATH",
            f"Path escapes the active workspace: {resolved}",
        )
    return resolved


def _resolve_asset_output(
    ctx: ToolCtx,
    p: str,
    *,
    allow_slot_evidence: bool = False,
) -> Path:
    """Resolve a mutating path into an allowed workspace-owned subtree."""
    root = _require_workspace_root(ctx)
    raw = Path(p).expanduser()
    resolved = (raw if raw.is_absolute() else root / raw).resolve()
    if not resolved.is_relative_to(root):
        raise ToolError(
            "INVALID_PATH",
            f"Asset output escapes the active workspace: {resolved}",
        )
    relative = resolved.relative_to(root)
    allowed = bool(relative.parts and relative.parts[0] in _ASSET_WRITE_ROOTS)
    if allow_slot_evidence:
        lexical_evidence_root = root / _SLOT_EVIDENCE_ROOT
        evidence_root = lexical_evidence_root.resolve()
        if evidence_root != lexical_evidence_root:
            raise ToolError(
                "INVALID_PATH",
                "Slot-evidence authority path may not traverse symlinks.",
            )
        allowed = allowed or resolved.is_relative_to(evidence_root)
    if not allowed:
        raise ToolError(
            "INVALID_PATH",
            "Asset outputs are limited to assets/**, design/**, or the "
            "tool-owned slot-evidence subtree; src/**, .playtest/state.json, "
            "and .playtest/verify/** are authority-owned.",
        )
    return resolved


def _resolve_asset_file_output(
    ctx: ToolCtx,
    p: str,
    *,
    expected_ending: str | None = None,
) -> Path:
    """Resolve one strict output file without accepting roots or symlink aliases."""
    root = _require_workspace_root(ctx)
    raw = Path(p).expanduser()
    lexical = raw if raw.is_absolute() else root / raw
    lexical = Path(os.path.abspath(lexical))
    if not lexical.is_relative_to(root):
        raise ToolError(
            "INVALID_PATH",
            f"Asset output escapes the active workspace: {lexical}",
        )
    relative = lexical.relative_to(root)
    if len(relative.parts) < 2 or relative.parts[0] not in _ASSET_WRITE_ROOTS:
        raise ToolError(
            "INVALID_PATH",
            "Asset file outputs must be strict descendants of assets/ or design/; "
            "the subtree roots are not files.",
        )
    resolved = _resolve_asset_output(ctx, str(lexical))
    if resolved != lexical:
        raise ToolError(
            "INVALID_PATH",
            f"Asset file output may not traverse a symlink: {lexical}",
        )
    if expected_ending is not None and not resolved.name.endswith(expected_ending):
        raise ToolError(
            "INVALID_PATH",
            f"Asset file output must end with {expected_ending}: {resolved}",
        )
    if resolved.exists() and not resolved.is_file():
        raise ToolError(
            "INVALID_PATH",
            f"Asset file output is not a regular file path: {resolved}",
        )
    return resolved


def _resolve_static_asset_output(ctx: ToolCtx, p: str) -> Path:
    return _resolve_asset_file_output(ctx, p, expected_ending=".png")


def _require_pinned_static_workspace(ctx: ToolCtx):
    workspace = getattr(ctx.state, "workspace", None)
    required = ("assert_identity", "copy_from_pinned", "commit_files_to_pinned")
    if workspace is None or not all(
        callable(getattr(workspace, name, None)) for name in required
    ):
        raise ToolError(
            "WORKSPACE_CLOSED",
            "Static publication requires the active workspace's pinned filesystem APIs.",
        )
    return workspace


def _resolve_asset_input(
    ctx: ToolCtx,
    p: str,
    *,
    allow_repo_reference: bool = False,
) -> Path:
    """Resolve a read-only asset/design input, with an explicit repo allowlist."""
    root = _require_workspace_root(ctx)
    raw = Path(p).expanduser()
    resolved = (raw if raw.is_absolute() else root / raw).resolve()
    if resolved.is_relative_to(root):
        relative = resolved.relative_to(root)
        if relative.parts and relative.parts[0] in _ASSET_WRITE_ROOTS:
            return resolved
    if allow_repo_reference:
        repo_root = Path(ctx.repo_root).resolve()
        for relative_root in _READ_ONLY_REPO_REFERENCE_ROOTS:
            reference_root = (repo_root / relative_root).resolve()
            if resolved.is_relative_to(reference_root):
                return resolved
    raise ToolError(
        "INVALID_PATH",
        "Asset inputs must resolve under workspace assets/** or design/**. "
        "Repository references are read-only and must come from the explicit "
        ".claude/skills/game-pipeline/references allowlist.",
    )


def _resolve_owned_output_dir(ctx: ToolCtx, p: str) -> Path:
    """Resolve a pipeline-owned output directory and enforce containment."""
    resolved = _resolve_asset_output(ctx, p)
    root = _require_workspace_root(ctx)
    relative = resolved.relative_to(root)
    if len(relative.parts) == 1 and relative.parts[0] in _ASSET_WRITE_ROOTS:
        raise ToolError(
            "INVALID_PATH",
            f"Output directory must be a dedicated pipeline subdirectory: {resolved}",
        )
    return resolved


@contextmanager
def _workspace_output_registry(workspace_root: Path):
    """Serialize overlap discovery and lease mutation across MCP processes."""
    with _OUTPUT_REGISTRY_LOCK:
        lock_path = workspace_lock_path(workspace_root, "asset-registry")
        with secure_file_lock(lock_path, exclusive=True):
            yield


def _infer_workspace_root_from_output(output_dir: Path) -> Path:
    for candidate in (output_dir, *output_dir.parents):
        if candidate.name in _ASSET_WRITE_ROOTS:
            return candidate.parent.resolve()
    raise ToolError(
        "INVALID_PATH",
        f"Cannot infer workspace root for asset output: {output_dir}",
    )


def _iter_output_lease_markers(workspace_root: Path):
    for root_name in _ASSET_WRITE_ROOTS:
        asset_root = workspace_root / root_name
        if asset_root.is_dir():
            yield from asset_root.rglob(".gca-asset-run.lock")


@contextmanager
def _owned_output_lock(
    output_dir: Path,
    workspace_root: Path | None = None,
):
    """Lease a destructive output tree with cross-process overlap exclusion."""
    output_dir = output_dir.resolve()
    workspace_root = (
        workspace_root.resolve()
        if workspace_root is not None
        else _infer_workspace_root_from_output(output_dir)
    )
    if not output_dir.is_relative_to(workspace_root):
        raise ToolError(
            "INVALID_PATH",
            f"Output directory escapes the active workspace: {output_dir}",
        )
    relative_output = output_dir.relative_to(workspace_root)
    if not relative_output.parts or relative_output.parts[0] not in _ASSET_WRITE_ROOTS:
        raise ToolError(
            "INVALID_PATH",
            f"Output directory is outside asset/design trees: {output_dir}",
        )

    ownership_path = output_dir / ".gca-asset-output"
    lease_path = output_dir / ".gca-asset-run.lock"
    owned_names = {
        ".gca-asset-output",
        ".gca-asset-run.lock",
        "_full_chain_summary.json",
        "_orchestrator_summary.json",
        "final",
        "first_frame",
        "override_staged.png",
        "preview_on_black.mp4",
        "preview_on_white.gif",
        "transparent.gif",
        "transparent.mov",
        "transparent.webm",
        "transparent_frames",
        "video",
    }
    lease_id = uuid.uuid4().hex
    lease_payload = {
        "schema_version": 1,
        "producer": "gca-mcp.asset-output-lease",
        "lease_id": lease_id,
        "pid": os.getpid(),
        "output_dir": relative_output.as_posix(),
        "registered_at_ns": time.time_ns(),
    }

    with _workspace_output_registry(workspace_root):
        if workspace_root.resolve() != workspace_root:
            raise ToolError(
                "INVALID_PATH",
                "Workspace root changed while acquiring the asset-output lease.",
            )
        refreshed_output = output_dir.resolve()
        if refreshed_output != output_dir or not refreshed_output.is_relative_to(
            workspace_root
        ):
            raise ToolError(
                "INVALID_PATH",
                "Asset output path changed while acquiring its lease.",
            )
        output_dir.mkdir(parents=True, exist_ok=True)
        if ownership_path.is_symlink():
            raise ToolError(
                "INVALID_PATH",
                f"Asset output ownership marker may not be a symlink: {ownership_path}",
            )
        if not ownership_path.exists():
            unowned = [
                entry.name
                for entry in output_dir.iterdir()
                if entry.name not in owned_names
                and not entry.name.startswith(("attempt_", "candidate_"))
            ]
            if unowned:
                raise ToolError(
                    "INVALID_PATH",
                    f"Refusing destructive pipeline use of non-owned directory "
                    f"{output_dir}; unexpected entries: {unowned[:5]}",
                )
            atomic_write_json(
                ownership_path,
                {
                    "schema_version": 1,
                    "producer": "gca-mcp.asset-output",
                    "output_dir": relative_output.as_posix(),
                },
            )
        else:
            try:
                ownership = json.loads(ownership_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                ownership = None
            if ownership_path.read_text(encoding="utf-8") == (
                "gca asset pipeline output\n"
            ):
                atomic_write_json(
                    ownership_path,
                    {
                        "schema_version": 1,
                        "producer": "gca-mcp.asset-output",
                        "output_dir": relative_output.as_posix(),
                    },
                )
            elif (
                not isinstance(ownership, dict)
                or ownership.get("producer") != "gca-mcp.asset-output"
                or ownership.get("output_dir") != relative_output.as_posix()
            ):
                raise ToolError(
                    "INVALID_PATH",
                    f"Invalid asset output ownership marker: {ownership_path}",
                )

        conflict = next(
            (
                active
                for active in _ACTIVE_OUTPUT_DIRS
                if output_dir == active
                or output_dir.is_relative_to(active)
                or active.is_relative_to(output_dir)
            ),
            None,
        )
        if conflict is None:
            conflict_marker = next(
                (
                    marker
                    for marker in _iter_output_lease_markers(workspace_root)
                    if (
                        marker.parent.resolve() == output_dir
                        or marker.parent.resolve().is_relative_to(output_dir)
                        or output_dir.is_relative_to(marker.parent.resolve())
                    )
                ),
                None,
            )
            if conflict_marker is not None:
                conflict = conflict_marker.parent.resolve()
        if conflict is not None:
            raise ToolError(
                "INVALID_CONFIG",
                f"Another asset pipeline owns an overlapping output path: {conflict}",
            )
        try:
            fd = os.open(
                lease_path,
                os.O_CREAT
                | os.O_EXCL
                | os.O_WRONLY
                | getattr(os, "O_NOFOLLOW", 0)
                | getattr(os, "O_CLOEXEC", 0),
                0o600,
            )
        except FileExistsError as exc:
            raise ToolError(
                "INVALID_CONFIG",
                f"Another asset pipeline is already using {output_dir}.",
                hint=f"Wait for it to finish or remove the stale lease {lease_path}.",
            ) from exc
        try:
            os.write(
                fd,
                (json.dumps(lease_payload, sort_keys=True) + "\n").encode("utf-8"),
            )
            os.fsync(fd)
        finally:
            os.close(fd)
        _ACTIVE_OUTPUT_DIRS.add(output_dir)

    try:
        yield
    finally:
        with _workspace_output_registry(workspace_root):
            try:
                registered = json.loads(lease_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                registered = None
            if isinstance(registered, dict) and registered.get("lease_id") == lease_id:
                lease_path.unlink(missing_ok=True)
            _ACTIVE_OUTPUT_DIRS.discard(output_dir)


# -------------------- generate_image --------------------


def _generate_image(args: dict[str, Any], ctx: ToolCtx) -> dict[str, Any]:
    prompt = args.get("prompt")
    output = args.get("output")
    if not prompt or not output:
        raise ToolError("INVALID_CONFIG", "`prompt` and `output` are required.")

    defaults = ctx.config.assets.image
    quality = str(args.get("quality", defaults.default_quality))
    safe_bg_color = args.get("safe_bg_color")
    if safe_bg_color is None:
        # A matted asset_type (prop/tile/ui/…) with no explicit safe bg would
        # otherwise get an arbitrary, unremovable model-picked background. Fill
        # with the reserved chroma-key color so matting can strip it.
        safe_bg_color = image_gen.default_safe_bg_color(
            args.get("asset_type"), defaults.safe_bg_color
        )

    out_path = _resolve_asset_output(ctx, str(output))
    # Concept mockups live under design/ and are full FLAT-2D scenes. gpt-image-2
    # renders inherently-3D subjects (dominoes, dice, blocks, ramps) in 3D even
    # when the user prompt says "no 3D", so for mockups force the hard
    # FLAT_2D_SCENE_BLOCK and turn OFF the single-subject margin block (a mockup
    # is a full scene, not a centered subject). Both are overridable via args.
    is_mockup = any(p.lower() == "design" for p in out_path.parts)
    flat_2d = bool(args.get("flat_2d", is_mockup))
    subject_margin = bool(args.get("subject_margin", not is_mockup))

    result = image_gen.generate_image(
        ctx.config,
        prompt=str(prompt),
        output=out_path,
        size=str(args.get("size", defaults.default_size)),
        quality=quality,
        max_retries=int(args.get("max_retries", defaults.max_retries)),
        retry_delay=float(args.get("retry_delay", defaults.retry_delay_seconds)),
        safe_bg_color=safe_bg_color,
        enforce_safe_bg_avoidance=bool(
            args.get(
                "enforce_safe_bg_avoidance",
                defaults.enforce_safe_bg_avoidance,
            )
        ),
        subject_margin=subject_margin,
        flat_2d=flat_2d,
        framing_profile=args.get("framing_profile"),
        asset_type=args.get("asset_type"),
        aspect=args.get("aspect"),
    )
    return result


# -------------------- edit_image --------------------


def _edit_image(args: dict[str, Any], ctx: ToolCtx) -> dict[str, Any]:
    prompt = args.get("prompt")
    output = args.get("output")
    input_path = args.get("input")
    if not prompt or not output or not input_path:
        raise ToolError(
            "INVALID_CONFIG", "`prompt`, `input`, and `output` are required."
        )

    resolved_input = (
        input_path
        if str(input_path).startswith(("http://", "https://"))
        else str(
            _resolve_asset_input(
                ctx,
                str(input_path),
                allow_repo_reference=True,
            )
        )
    )

    defaults = ctx.config.assets.image
    size = str(args.get("size", defaults.edit_default_size))
    safe_bg_color = args.get("safe_bg_color")
    if safe_bg_color is None:
        # edit_image is overwhelmingly used to extract lettering / elements from
        # a design crop — those MUST land on a removable bg. So default to the
        # safe color even when asset_type is omitted; an OPAQUE scene edit
        # (e.g. map-base seeding) must declare asset_type=map/background/parallax
        # to keep its full background.
        safe_bg_color = image_gen.default_safe_bg_color(
            args.get("asset_type"),
            defaults.safe_bg_color,
            default_when_unknown=True,
        )
    result = image_gen.edit_image(
        ctx.config,
        prompt=str(prompt),
        input_path=resolved_input,
        output=_resolve_asset_output(ctx, str(output)),
        size=size,
        quality=str(args.get("quality", defaults.default_quality)),
        safe_bg_color=safe_bg_color,
        enforce_safe_bg_avoidance=bool(
            args.get(
                "enforce_safe_bg_avoidance",
                defaults.enforce_safe_bg_avoidance,
            )
        ),
        framing_profile=args.get("framing_profile"),
        asset_type=args.get("asset_type"),
    )
    return result


# -------------------- generate_seedance_video --------------------


def _normalise_image_arg(ctx: ToolCtx, value: Any) -> str | None:
    if value is None:
        return None
    s = str(value)
    if s.startswith(("http://", "https://")):
        return s
    return str(_resolve_asset_input(ctx, s, allow_repo_reference=True))


def _generate_seedance_video(args: dict[str, Any], ctx: ToolCtx) -> dict[str, Any]:
    prompt = args.get("prompt")
    output = args.get("output")
    if not prompt or not output:
        raise ToolError("INVALID_CONFIG", "`prompt` and `output` are required.")

    images = args.get("image") or []
    if isinstance(images, str):
        images = [images]
    images_resolved = [_normalise_image_arg(ctx, v) for v in images if v]

    save_response = args.get("save_response")
    save_response_path = (
        _resolve_asset_output(ctx, str(save_response)) if save_response else None
    )

    defaults = ctx.config.assets.seedance
    model = str(args.get("model", defaults.model))
    duration = args.get("duration", defaults.duration)
    resolution = str(args.get("resolution", defaults.resolution))
    ratio = str(args.get("ratio", defaults.ratio))
    result = seedance.generate_video(
        ctx.config,
        prompt=str(prompt),
        output=_resolve_asset_output(ctx, str(output)),
        model=model,
        model_id=args.get("model_id"),
        duration=duration,
        ratio=ratio,
        resolution=resolution,
        seed=args.get("seed"),
        camerafixed=args.get("camerafixed"),
        watermark=args.get("watermark"),
        image=[v for v in images_resolved if v],
        first_frame=_normalise_image_arg(ctx, args.get("first_frame")),
        last_frame=_normalise_image_arg(ctx, args.get("last_frame")),
        extra_params=args.get("extra_params") or None,
        save_response=save_response_path,
        server_timeout=int(args.get("server_timeout", defaults.server_timeout_seconds)),
        download_timeout=int(
            args.get("download_timeout", defaults.download_timeout_seconds)
        ),
        print_url_only=bool(args.get("print_url_only", False)),
    )
    return result


# -------------------- rmbg2_video_matting --------------------


def _rmbg2_video_matting(args: dict[str, Any], ctx: ToolCtx) -> dict[str, Any]:
    """Configured video matting → RGBA PNG sequence + encoded outputs."""
    inp = args.get("input")
    odir = args.get("output_dir")
    if not inp or not odir:
        raise ToolError(
            "INVALID_CONFIG", "`input` (mp4 path) and `output_dir` are required."
        )

    formats_arg = args.get("formats", ["gif"])
    if isinstance(formats_arg, str):
        formats = [f.strip() for f in formats_arg.split(",") if f.strip()]
    else:
        formats = [str(f) for f in formats_arg]

    defaults = ctx.config.assets.rmbg
    output_dir = _resolve_owned_output_dir(ctx, str(odir))
    with _owned_output_lock(output_dir, _require_workspace_root(ctx)):
        result = rmbg2_video.matte_video(
            ctx.config,
            input_video=_resolve_asset_input(ctx, str(inp)),
            output_dir=output_dir,
            formats=formats,
            endpoint=args.get("endpoint"),
            bg_hex=args.get("bg_hex"),
            aug_preset=str(args.get("aug_preset", defaults.aug_preset)),
            aug_hard=args.get("aug_hard"),
            aug_soft=args.get("aug_soft"),
            rmbg_fg_cutoff=int(args.get("rmbg_fg_cutoff", defaults.rmbg_fg_cutoff)),
            max_size=args.get("max_size", defaults.max_size),
            workers=int(args.get("workers", defaults.workers)),
            gif_max_size=int(args.get("gif_max_size", defaults.gif_max_size)),
            gif_alpha_threshold=int(
                args.get("gif_alpha_threshold", defaults.gif_alpha_threshold)
            ),
            keep_frames=bool(args.get("keep_frames", defaults.keep_frames)),
            timeout=int(args.get("timeout", defaults.timeout_seconds)),
            align_feet_to_bottom=bool(
                args.get("align_feet_to_bottom", defaults.align_feet_to_bottom)
            ),
            foot_margin_px=int(args.get("foot_margin_px", defaults.foot_margin_px)),
            drop_bg_leak=bool(args.get("drop_bg_leak", defaults.drop_bg_leak)),
            bg_leak_area_threshold=float(
                args.get("bg_leak_area_threshold", defaults.bg_leak_area_threshold)
            ),
            bg_leak_color_dist=int(
                args.get("bg_leak_color_dist", defaults.bg_leak_color_dist)
            ),
            bg_leak_alpha_threshold=int(
                args.get("bg_leak_alpha_threshold", defaults.bg_leak_alpha_threshold)
            ),
            bg_leak_keep_min_frames=int(
                args.get("bg_leak_keep_min_frames", defaults.bg_leak_keep_min_frames)
            ),
            bg_leak_alpha_weighted=bool(
                args.get("bg_leak_alpha_weighted", defaults.bg_leak_alpha_weighted)
            ),
        )
    return result


# -------------------- build_i2v_prompt --------------------


def _build_i2v_prompt(args: dict[str, Any], ctx: ToolCtx) -> dict[str, Any]:
    """Wrap user content with the iter_05-validated i2v constraint block."""
    user_content = args.get("user_content")
    if not user_content:
        raise ToolError("INVALID_CONFIG", "`user_content` is required.")

    palette = args.get("effect_palette")
    if isinstance(palette, str):
        palette = [c.strip() for c in palette.split(",") if c.strip()]

    ref = args.get("reference_image")
    ref_resolved = (
        str(
            _resolve_asset_input(
                ctx,
                str(ref),
                allow_repo_reference=True,
            )
        )
        if ref
        else None
    )

    prompt = seedance.build_i2v_prompt(
        str(user_content),
        preset=args.get("preset"),
        bg_hex=str(args.get("bg_hex") or ctx.config.assets.sprite.safe_bg_color),
        effect_palette=palette,
        foreground_recolor_instructions=str(
            args.get("foreground_recolor_instructions") or ""
        ),
        reference_image=ref_resolved,
        effects=bool(args.get("effects", ctx.config.assets.sprite.effects)),
        animation_profile=str(args.get("animation_profile", "character")),
    )

    # Re-derive the bg_hex actually used (after preset / border detection).
    actual_bg = ctx.config.assets.sprite.safe_bg_color
    if args.get("preset") in seedance.I2V_PROMPT_PRESETS:
        actual_bg = seedance.I2V_PROMPT_PRESETS[args["preset"]]["bg_hex"]
    elif ref_resolved:
        actual_bg = seedance.detect_existing_safe_bg(ref_resolved)
    elif args.get("bg_hex"):
        actual_bg = args["bg_hex"]

    return {
        "prompt": prompt,
        "preset": args.get("preset"),
        "bg_hex_used": actual_bg,
        "char_count": len(prompt),
    }


# -------------------- validate_asset_video --------------------


def _validate_asset_video(args: dict[str, Any], ctx: ToolCtx) -> dict[str, Any]:
    """Strict VLM rubric check for a generated game-asset animation video."""
    video = args.get("video")
    action = args.get("action_description")
    if not video or not action:
        raise ToolError(
            "INVALID_CONFIG",
            "`video` and `action_description` are required.",
        )

    defaults = ctx.config.assets.vlm
    kwargs: dict[str, Any] = {
        "video_path": _resolve_asset_input(ctx, str(video)),
        "action_description": str(action),
        "extra_rubrics": args.get("extra_rubrics"),
        "rubric_override": args.get("rubric_override"),
        "vlm_max_tokens": int(args.get("vlm_max_tokens", defaults.video_max_tokens)),
        "vlm_temperature": float(args.get("vlm_temperature", defaults.temperature)),
        "vlm_timeout": float(args.get("vlm_timeout", defaults.video_timeout_seconds)),
    }
    if args.get("vlm_model"):
        kwargs["vlm_model"] = str(args["vlm_model"])
    return asset_video_validator.validate_asset_video(ctx.config, **kwargs)


# -------------------- validate_image --------------------


def _validate_image(args: dict[str, Any], ctx: ToolCtx) -> dict[str, Any]:
    """Strict VLM rubric check for a sprite first-frame image."""
    image = args.get("image")
    desc = args.get("character_description")
    if not image or not desc:
        raise ToolError(
            "INVALID_CONFIG",
            "`image` and `character_description` are required.",
        )

    defaults = ctx.config.assets.vlm
    image_path = _resolve_asset_input(ctx, str(image))
    kwargs: dict[str, Any] = {
        "image_path": image_path,
        "character_description": str(desc),
        "extra_rubrics": args.get("extra_rubrics"),
        "rubric_override": args.get("rubric_override"),
        "vlm_max_tokens": int(args.get("vlm_max_tokens", defaults.image_max_tokens)),
        "vlm_temperature": float(args.get("vlm_temperature", defaults.temperature)),
        "vlm_timeout": float(args.get("vlm_timeout", defaults.image_timeout_seconds)),
    }
    if args.get("vlm_model"):
        kwargs["vlm_model"] = str(args["vlm_model"])
    return asset_image_validator.validate_image(ctx.config, **kwargs)


# -------------------- validate_asset_slot --------------------


def _slot_validation_lock_path(ctx: ToolCtx, slot_key: str) -> Path:
    digest = hashlib.sha256(slot_key.encode("utf-8")).hexdigest()[:16]
    return workspace_lock_path(
        _require_workspace_root(ctx),
        f"slot-validation-{digest}",
    )


def _slot_validation_policy_sha256(
    ctx: ToolCtx,
    *,
    extra_rubrics: Any,
    vlm_model: str,
    vlm_max_tokens: int,
    vlm_temperature: float,
    vlm_timeout: float,
    attempts_per_model: int,
) -> str:
    payload = {
        "canonical_rubric_sha256": (
            asset_slot_validator.CANONICAL_SLOT_RUBRIC_SHA256
        ),
        "extra_rubrics": extra_rubrics or [],
        "vlm_provider": "ark-doubao",
        "vlm_model": vlm_model,
        "vlm_max_tokens": vlm_max_tokens,
        "vlm_temperature": vlm_temperature,
        "vlm_timeout": vlm_timeout,
        "attempts_per_model": attempts_per_model,
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _load_reusable_slot_receipt(
    output: Path,
    *,
    expected: dict[str, Any],
) -> dict[str, Any] | None:
    try:
        receipt = json.loads(output.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if (
        not isinstance(receipt, dict)
        or receipt.get("passed") is not True
        or any(receipt.get(key) != value for key, value in expected.items())
    ):
        return None
    reused = dict(receipt)
    reused["reused"] = True
    reused["report_path"] = str(output)
    return reused


def _validate_asset_slot_one(args: dict[str, Any], ctx: ToolCtx) -> dict[str, Any]:
    """Validate the manifest-bound final runtime artifact against its design slot."""
    image = args.get("image")
    slot_key = args.get("slot_key")
    if not image or not slot_key:
        raise ToolError(
            "INVALID_CONFIG",
            "`image` and `slot_key` are required.",
        )
    if (
        not isinstance(slot_key, str)
        or re.fullmatch(r"[A-Za-z0-9._-]+", slot_key) is None
    ):
        raise ToolError("INVALID_CONFIG", "slot_key contains unsafe characters.")
    if "rubric_override" in args:
        raise ToolError(
            "INVALID_CONFIG",
            "validate_asset_slot always applies the canonical rubric; "
            "rubric_override is not accepted for published receipts.",
        )
    workspace = _require_workspace_root(ctx)
    manifest_path = _resolve_asset_input(ctx, "assets/asset-manifest.json")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ToolError(
            "INVALID_ARTIFACT",
            f"Cannot read asset-manifest.json: {exc}",
        ) from exc
    entries = manifest_entries(manifest)
    matches = [
        item
        for item in entries
        if isinstance(item, dict) and item.get("key") == slot_key
    ]
    if len(matches) != 1 or matches[0].get("sourceStrategy") != "generated-image":
        raise ToolError(
            "INVALID_ARTIFACT",
            f"slot_key {slot_key!r} must select exactly one generated-image entry.",
        )
    entry = matches[0]
    slot_description = entry.get("description")
    if not isinstance(slot_description, str) or not slot_description.strip():
        raise ToolError(
            "INVALID_ARTIFACT",
            f"Manifest entry {slot_key!r} lacks its GDD-derived description.",
        )
    actions = entry.get("actions")
    requested_action = args.get("action")
    action: str | None = None
    runtime_spec = entry
    if isinstance(actions, dict) and actions:
        action_value = requested_action or entry.get("defaultAction")
        if not isinstance(action_value, str) or not action_value:
            raise ToolError(
                "INVALID_ARTIFACT",
                f"Manifest entry {slot_key!r} lacks a valid defaultAction.",
            )
        if re.fullmatch(r"[A-Za-z0-9._-]+", action_value) is None:
            raise ToolError("INVALID_ARTIFACT", "Manifest action name is unsafe.")
        runtime_spec = actions.get(action_value)
        if not isinstance(runtime_spec, dict):
            raise ToolError(
                "INVALID_ARTIFACT",
                f"Manifest entry {slot_key!r} has no action {action_value!r}.",
            )
        action = action_value
        if action == entry.get("defaultAction") and entry.get(
            "path"
        ) != runtime_spec.get("path"):
            raise ToolError(
                "INVALID_ARTIFACT",
                f"Manifest entry {slot_key!r} does not mirror its default action path.",
            )
    elif requested_action is not None:
        raise ToolError(
            "INVALID_CONFIG",
            f"Manifest entry {slot_key!r} has no action map.",
        )

    runtime_path_value = runtime_spec.get("path")
    if (
        not isinstance(runtime_path_value, str)
        or not runtime_path_value
        or Path(runtime_path_value).is_absolute()
        or not Path(runtime_path_value).parts
        or Path(runtime_path_value).parts[0] != "assets"
    ):
        raise ToolError(
            "INVALID_ARTIFACT",
            f"Manifest runtime path for {slot_key!r} must be under assets/**.",
        )
    runtime_path = _resolve_asset_input(ctx, runtime_path_value)
    provided_path = _resolve_asset_input(ctx, str(image))
    if provided_path != runtime_path:
        raise ToolError(
            "SLOT_ARTIFACT_MISMATCH",
            "validate_asset_slot image must exactly match the selected manifest "
            "slot/action runtime artifact path; reference or sibling images "
            "cannot publish the receipt.",
        )
    if not runtime_path.is_file():
        raise ToolError(
            "MISSING_ARTIFACT",
            f"Manifest runtime artifact does not exist: {runtime_path}",
        )

    gdd_path = workspace / "GAME_DESIGN.md"
    try:
        gdd_text = gdd_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ToolError(
            "INVALID_ARTIFACT", f"Cannot read GAME_DESIGN.md: {exc}"
        ) from exc
    try:
        gdd_slot_context_sha256 = _gdd_slot_context_sha256(
            gdd_text,
            key=str(slot_key),
            description=str(slot_description),
            action=action,
        )
    except ValueError as exc:
        raise ToolError(
            "SLOT_DESCRIPTION_MISMATCH",
            f"Manifest description for {slot_key!r} is not uniquely bound "
            f"to its GAME_DESIGN.md slot: {exc}.",
        ) from exc

    validation_image = runtime_path
    validation_frame_relative: str | None = None
    category = str(entry.get("category") or "").lower()
    sprite_categories = {
        "sprite",
        "character",
        "enemy",
        "boss",
        "npc",
        "projectile",
    }
    if action is not None or category in sprite_categories:
        metadata_value = runtime_spec.get("metadata") or entry.get("metadata")
        if (
            not isinstance(metadata_value, str)
            or not metadata_value
            or Path(metadata_value).is_absolute()
        ):
            raise ToolError(
                "INVALID_ARTIFACT",
                "Spritesheet slot validation requires manifest-bound metadata.",
            )
        metadata_path = _resolve_asset_input(ctx, metadata_value)
        try:
            sheet_metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            frame_width = int(sheet_metadata["frameWidth"])
            frame_height = int(sheet_metadata["frameHeight"])
        except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
            raise ToolError(
                "INVALID_ARTIFACT",
                f"Cannot read spritesheet frame metadata: {metadata_path}",
            ) from exc
        try:
            with Image.open(runtime_path) as sheet:
                if (
                    frame_width < 1
                    or frame_height < 1
                    or frame_width > sheet.width
                    or frame_height > sheet.height
                ):
                    raise ToolError(
                        "INVALID_ARTIFACT",
                        "Spritesheet frame dimensions exceed the runtime artifact.",
                    )
                frame = sheet.crop((0, 0, frame_width, frame_height)).convert("RGBA")
        except ToolError:
            raise
        except OSError as exc:
            raise ToolError(
                "INVALID_ARTIFACT",
                f"Cannot read runtime spritesheet: {runtime_path}",
            ) from exc
        frame_name = (
            f"{slot_key}.{action}.frame.png"
            if action is not None
            else f"{slot_key}.frame.png"
        )
        frame_path = _resolve_asset_output(
            ctx,
            str(_SLOT_EVIDENCE_ROOT / frame_name),
            allow_slot_evidence=True,
        )
        frame_path.parent.mkdir(parents=True, exist_ok=True)
        frame_temp = frame_path.with_name(
            f".{frame_path.stem}.{uuid.uuid4().hex}.tmp.png"
        )
        try:
            frame.save(frame_temp, format="PNG")
            os.replace(frame_temp, frame_path)
        finally:
            frame_temp.unlink(missing_ok=True)
        validation_image = frame_path
        validation_frame_relative = frame_path.relative_to(workspace).as_posix()

    defaults = ctx.config.assets.vlm
    vlm_model = str(args.get("vlm_model", defaults.model))
    vlm_max_tokens = int(
        args.get("vlm_max_tokens", defaults.image_max_tokens)
    )
    vlm_temperature = float(
        args.get("vlm_temperature", defaults.temperature)
    )
    vlm_timeout = float(
        args.get("vlm_timeout", defaults.slot_timeout_seconds)
    )
    attempts_per_model = int(
        args.get(
            "attempts_per_model",
            defaults.slot_attempts_per_model,
        )
    )
    if vlm_timeout <= 0 or attempts_per_model < 1:
        raise ToolError(
            "INVALID_CONFIG",
            "Slot VLM timeout and attempts_per_model must be positive.",
        )

    artifact_sha256 = hashlib.sha256(runtime_path.read_bytes()).hexdigest()
    slot_description_sha256 = hashlib.sha256(
        str(slot_description).encode("utf-8")
    ).hexdigest()
    gdd_sha256 = hashlib.sha256(gdd_path.read_bytes()).hexdigest()
    manifest_sha256 = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
    slot_contract_sha256 = _slot_contract_sha256(entry)
    validation_policy_sha256 = _slot_validation_policy_sha256(
        ctx,
        extra_rubrics=args.get("extra_rubrics"),
        vlm_model=vlm_model,
        vlm_max_tokens=vlm_max_tokens,
        vlm_temperature=vlm_temperature,
        vlm_timeout=vlm_timeout,
        attempts_per_model=attempts_per_model,
    )
    validation_frame_sha256 = (
        hashlib.sha256(validation_image.read_bytes()).hexdigest()
        if validation_frame_relative is not None
        else None
    )
    output = _resolve_asset_output(
        ctx,
        str(_SLOT_EVIDENCE_ROOT / f"{slot_key}.json"),
        allow_slot_evidence=True,
    )
    expected = {
        "schema_version": 3,
        "producer": "gca-mcp.validate_asset_slot",
        "key": str(slot_key),
        "action": action,
        "path": runtime_path_value,
        "artifact_sha256": artifact_sha256,
        "slot_description_sha256": slot_description_sha256,
        "gdd_slot_context_sha256": gdd_slot_context_sha256,
        "slot_contract_sha256": slot_contract_sha256,
        "canonical_rubric_sha256": (
            asset_slot_validator.CANONICAL_SLOT_RUBRIC_SHA256
        ),
        "validation_policy_sha256": validation_policy_sha256,
    }
    if validation_frame_sha256 is not None:
        expected["validation_frame_sha256"] = validation_frame_sha256

    with secure_file_lock(
        _slot_validation_lock_path(ctx, str(slot_key)),
        exclusive=True,
    ):
        reusable = _load_reusable_slot_receipt(output, expected=expected)
        if reusable is not None:
            return reusable

        result = asset_slot_validator.validate_asset_slot(
            ctx.config,
            image_path=validation_image,
            slot_description=str(slot_description),
            slot_key=str(slot_key),
            extra_rubrics=args.get("extra_rubrics"),
            vlm_model=vlm_model,
            vlm_max_tokens=vlm_max_tokens,
            vlm_temperature=vlm_temperature,
            vlm_timeout=vlm_timeout,
            attempts_per_model=attempts_per_model,
        )
        result.update(
            {
                **expected,
                # Audit-only snapshots. Freshness is scoped to the selected
                # slot contract above; unrelated GDD/manifest edits must not
                # force every asset through VLM again.
                "gdd_sha256": gdd_sha256,
                "manifest_sha256": manifest_sha256,
                "logical_key": str(slot_key),
                "runtime_texture_key": (
                    runtime_spec.get("textureKey")
                    or entry.get("textureKey")
                    or str(slot_key)
                ),
                "image": runtime_path_value,
                "image_sha256": artifact_sha256,
                "canonical_rubric_version": (
                    asset_slot_validator.CANONICAL_SLOT_RUBRIC_VERSION
                ),
                "reused": False,
            }
        )
        if validation_frame_relative is not None:
            result["validation_frame"] = validation_frame_relative
            result["validation_frame_sha256"] = validation_frame_sha256
        atomic_write_json(output, result)
    result["report_path"] = str(output)
    return result


def _validate_asset_slot(args: dict[str, Any], ctx: ToolCtx) -> dict[str, Any]:
    """Validate one slot or fan out every item in one fully parallel batch."""
    items = args.get("items")
    if items is None:
        return _validate_asset_slot_one(args, ctx)
    if set(args) != {"items"}:
        raise ToolError(
            "INVALID_CONFIG",
            "Batch slot validation accepts only `items`; put per-slot options "
            "inside each item.",
        )
    if not isinstance(items, list) or not items:
        raise ToolError(
            "INVALID_CONFIG",
            "`items` must be a non-empty array of slot validation requests.",
        )
    if len(items) > 128:
        raise ToolError(
            "INVALID_CONFIG",
            "A slot validation batch may contain at most 128 items.",
        )
    normalized: list[dict[str, Any]] = []
    seen_keys: set[str] = set()
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            raise ToolError(
                "INVALID_CONFIG",
                f"items[{index}] must be an object.",
            )
        slot_key = item.get("slot_key")
        if not isinstance(slot_key, str) or not slot_key:
            raise ToolError(
                "INVALID_CONFIG",
                f"items[{index}].slot_key must be a non-empty string.",
            )
        if slot_key in seen_keys:
            raise ToolError(
                "INVALID_CONFIG",
                f"Duplicate batch slot_key {slot_key!r} would race one receipt.",
            )
        seen_keys.add(slot_key)
        normalized.append(dict(item))

    started = time.monotonic()
    ordered_results: list[dict[str, Any] | None] = [None] * len(normalized)

    def execute(index: int, item: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        try:
            return index, _validate_asset_slot_one(item, ctx)
        except ToolError as exc:
            return index, {
                "slot_key": item.get("slot_key"),
                "passed": False,
                "error": {
                    "code": exc.code,
                    "message": exc.message,
                    "hint": exc.hint,
                },
            }
        except Exception as exc:  # noqa: BLE001 - preserve sibling results.
            return index, {
                "slot_key": item.get("slot_key"),
                "passed": False,
                "error": {
                    "code": "INTERNAL_ERROR",
                    "message": str(exc),
                },
            }

    max_workers = len(normalized)
    with ThreadPoolExecutor(
        max_workers=max_workers,
        thread_name_prefix="slot-validation",
    ) as executor:
        futures = [
            executor.submit(execute, index, item)
            for index, item in enumerate(normalized)
        ]
        for future in as_completed(futures):
            index, result = future.result()
            ordered_results[index] = result

    results = [result for result in ordered_results if result is not None]
    passed_count = sum(result.get("passed") is True for result in results)
    reused_count = sum(result.get("reused") is True for result in results)
    return {
        "schema_version": 1,
        "mode": "batch",
        "passed": passed_count == len(results),
        "requested": len(normalized),
        "max_workers": max_workers,
        "n_passed": passed_count,
        "n_failed": len(results) - passed_count,
        "n_reused": reused_count,
        "elapsed_ms": round((time.monotonic() - started) * 1000),
        "results": results,
    }


# -------------------- produce_validated_first_frame --------------------


def _produce_validated_first_frame(
    args: dict[str, Any], ctx: ToolCtx
) -> dict[str, Any]:
    """Closed-loop generate_image → validate_image, retry on FAIL up to max_attempts."""
    image_prompt = args.get("image_prompt")
    desc = args.get("character_description")
    output_dir = args.get("output_dir")
    if not (image_prompt and desc and output_dir):
        raise ToolError(
            "INVALID_CONFIG",
            "`image_prompt`, `character_description`, and `output_dir` are required.",
        )

    image_defaults = ctx.config.assets.image
    sprite_defaults = ctx.config.assets.sprite
    animation_profile = str(args.get("animation_profile", "character"))
    if animation_profile not in ("character", "projectile"):
        raise ToolError(
            "INVALID_CONFIG",
            "`animation_profile` must be `character` or `projectile`.",
        )
    is_projectile = animation_profile == "projectile"
    rubric_override = args.get("rubric_override")
    if rubric_override is None and is_projectile:
        rubric_override = asset_image_validator.rubric_dicts(
            asset_image_validator.PROJECTILE_IMAGE_RUBRICS
        )
    kwargs: dict[str, Any] = {
        "image_prompt": str(image_prompt),
        "character_description": str(desc),
        "output_dir": _resolve_owned_output_dir(ctx, str(output_dir)),
        "size": str(args.get("size", sprite_defaults.image_size)),
        "quality": str(args.get("quality", sprite_defaults.image_quality)),
        "safe_bg_color": args.get("safe_bg_color", sprite_defaults.safe_bg_color),
        "framing_profile": ("centered_asset" if is_projectile else "grounded_sprite"),
        "enforce_safe_bg_avoidance": bool(
            args.get(
                "enforce_safe_bg_avoidance",
                image_defaults.enforce_safe_bg_avoidance,
            )
        ),
        "image_max_retries": int(
            args.get("image_max_retries", image_defaults.max_retries)
        ),
        "image_retry_delay": float(
            args.get("image_retry_delay", image_defaults.retry_delay_seconds)
        ),
        "extra_rubrics": args.get("extra_rubrics"),
        "rubric_override": rubric_override,
        "max_attempts": int(
            args.get("max_attempts", sprite_defaults.image_max_attempts)
        ),
    }
    ref_image = args.get("ref_image")
    if ref_image:
        kwargs["ref_image"] = (
            str(ref_image)
            if str(ref_image).startswith(("http://", "https://"))
            else str(
                _resolve_asset_input(
                    ctx,
                    str(ref_image),
                    allow_repo_reference=True,
                )
            )
        )
    if args.get("vlm_model"):
        kwargs["vlm_model"] = str(args["vlm_model"])

    with _owned_output_lock(kwargs["output_dir"], _require_workspace_root(ctx)):
        result = produce_validated_first_frame_adapter.produce_validated_first_frame(
            ctx.config, **kwargs
        )
    return result


# -------------------- produce_validated_sprite_action --------------------


def _produce_validated_sprite_action(
    args: dict[str, Any], ctx: ToolCtx
) -> dict[str, Any]:
    """End-to-end chain: first-frame orchestrator → video orchestrator."""
    video_user_content = args.get("video_user_content")
    output_dir = args.get("output_dir")
    first_frame_override = args.get("first_frame_override")

    if not (video_user_content and output_dir):
        raise ToolError(
            "INVALID_CONFIG",
            "`video_user_content` and `output_dir` are required.",
        )

    image_prompt = args.get("image_prompt")
    char_desc = args.get("character_description")
    if not first_frame_override and not (image_prompt and char_desc):
        raise ToolError(
            "INVALID_CONFIG",
            "`image_prompt` and `character_description` are required "
            "when `first_frame_override` is not provided.",
        )

    defaults = ctx.config.assets.sprite
    video_defaults = ctx.config.assets.seedance
    rmbg_defaults = ctx.config.assets.rmbg
    kwargs: dict[str, Any] = {
        "image_prompt": str(image_prompt or ""),
        "character_description": str(char_desc or ""),
        "video_user_content": str(video_user_content),
        "output_dir": _resolve_owned_output_dir(ctx, str(output_dir)),
        "safe_bg_color": str(args.get("safe_bg_color", defaults.safe_bg_color)),
        "image_size": str(args.get("image_size", defaults.image_size)),
        "image_quality": str(args.get("image_quality", defaults.image_quality)),
        "image_max_attempts": int(
            args.get("image_max_attempts", defaults.image_max_attempts)
        ),
        "image_extra_rubrics": args.get("image_extra_rubrics"),
        "duration": int(args.get("duration", defaults.duration)),
        "ratio": str(args.get("ratio", defaults.ratio)),
        "resolution": str(args.get("resolution", defaults.resolution)),
        "seedance_model": str(args.get("seedance_model", defaults.seedance_model)),
        "aug_preset": str(args.get("aug_preset", defaults.aug_preset)),
        "rmbg_fg_cutoff": int(args.get("rmbg_fg_cutoff", defaults.rmbg_fg_cutoff)),
        "gif_alpha_threshold": int(
            args.get("gif_alpha_threshold", defaults.gif_alpha_threshold)
        ),
        "align_feet_to_bottom": bool(
            args.get("align_feet_to_bottom", defaults.align_feet_to_bottom)
        ),
        "foot_margin_px": int(args.get("foot_margin_px", defaults.foot_margin_px)),
        "matte_workers": int(args.get("matte_workers", defaults.matte_workers)),
        "max_frames": int(args.get("max_frames", defaults.max_frames)),
        "drop_bg_leak": bool(args.get("drop_bg_leak", rmbg_defaults.drop_bg_leak)),
        "bg_leak_area_threshold": float(
            args.get("bg_leak_area_threshold", rmbg_defaults.bg_leak_area_threshold)
        ),
        "bg_leak_color_dist": int(
            args.get("bg_leak_color_dist", rmbg_defaults.bg_leak_color_dist)
        ),
        "bg_leak_alpha_threshold": int(
            args.get("bg_leak_alpha_threshold", rmbg_defaults.bg_leak_alpha_threshold)
        ),
        "bg_leak_keep_min_frames": int(
            args.get("bg_leak_keep_min_frames", rmbg_defaults.bg_leak_keep_min_frames)
        ),
        "bg_leak_alpha_weighted": bool(
            args.get("bg_leak_alpha_weighted", rmbg_defaults.bg_leak_alpha_weighted)
        ),
        "video_n_candidates": int(
            args.get(
                "video_n_candidates",
                video_defaults.max_quality_candidates,
            )
        ),
        "video_extra_rubrics": args.get("video_extra_rubrics"),
        "effects": bool(args.get("effects", defaults.effects)),
    }
    if first_frame_override:
        kwargs["first_frame_override"] = str(
            _resolve_asset_input(ctx, str(first_frame_override))
        )
    ref_image = args.get("ref_image")
    if ref_image:
        kwargs["ref_image"] = (
            str(ref_image)
            if str(ref_image).startswith(("http://", "https://"))
            else str(
                _resolve_asset_input(
                    ctx,
                    str(ref_image),
                    allow_repo_reference=True,
                )
            )
        )
    if args.get("bg_hex"):
        kwargs["bg_hex"] = str(args["bg_hex"])
    if args.get("seed_base") is not None:
        kwargs["seed_base"] = int(args["seed_base"])
    if args.get("vlm_model"):
        kwargs["vlm_model"] = str(args["vlm_model"])

    # Animation profile drives framing / alignment / rubric selection.
    # Runtime-only FX is global: the config default for ``effects`` must reach
    # the adapter for BOTH profiles instead of silently restoring character FX.
    kwargs["animation_profile"] = str(args.get("animation_profile", "character"))
    if "align_feet_to_bottom" not in args:
        kwargs.pop("align_feet_to_bottom", None)

    with _owned_output_lock(kwargs["output_dir"], _require_workspace_root(ctx)):
        result = (
            produce_validated_sprite_action_adapter.produce_validated_sprite_action(
                ctx.config, **kwargs
            )
        )
    return result


# -------------------- produce_validated_video --------------------


def _produce_validated_video(args: dict[str, Any], ctx: ToolCtx) -> dict[str, Any]:
    """Serial-retry sprite-action video: generate → matte → two-pass validate (framing on raw mp4 + content on matted preview), retry until it passes (cap = n_candidates)."""
    base_prompt = args.get("base_prompt")
    first_frame = args.get("first_frame")
    output_dir = args.get("output_dir")
    action_description = args.get("action_description")
    if not (base_prompt and first_frame and output_dir and action_description):
        raise ToolError(
            "INVALID_CONFIG",
            "`base_prompt`, `first_frame`, `output_dir`, and "
            "`action_description` are all required.",
        )

    first_frame_resolved = (
        str(first_frame)
        if str(first_frame).startswith(("http://", "https://"))
        else str(
            _resolve_asset_input(
                ctx,
                str(first_frame),
                allow_repo_reference=True,
            )
        )
    )

    defaults = ctx.config.assets.sprite
    video_defaults = ctx.config.assets.seedance
    rmbg_defaults = ctx.config.assets.rmbg
    kwargs: dict[str, Any] = {
        "base_prompt": str(base_prompt),
        "first_frame": first_frame_resolved,
        "output_dir": _resolve_owned_output_dir(ctx, str(output_dir)),
        "action_description": str(action_description),
        "duration": int(args.get("duration", defaults.duration)),
        "ratio": str(args.get("ratio", defaults.ratio)),
        "resolution": str(args.get("resolution", defaults.resolution)),
        "model": str(args.get("model", defaults.seedance_model)),
        "bg_hex": str(args.get("bg_hex", defaults.safe_bg_color)),
        "aug_preset": str(args.get("aug_preset", defaults.aug_preset)),
        "rmbg_fg_cutoff": int(args.get("rmbg_fg_cutoff", defaults.rmbg_fg_cutoff)),
        "gif_alpha_threshold": int(
            args.get("gif_alpha_threshold", defaults.gif_alpha_threshold)
        ),
        "align_feet_to_bottom": bool(
            args.get("align_feet_to_bottom", defaults.align_feet_to_bottom)
        ),
        "foot_margin_px": int(args.get("foot_margin_px", defaults.foot_margin_px)),
        "matte_workers": int(args.get("matte_workers", defaults.matte_workers)),
        "max_frames": int(args.get("max_frames", defaults.max_frames)),
        "drop_bg_leak": bool(args.get("drop_bg_leak", rmbg_defaults.drop_bg_leak)),
        "bg_leak_area_threshold": float(
            args.get("bg_leak_area_threshold", rmbg_defaults.bg_leak_area_threshold)
        ),
        "bg_leak_color_dist": int(
            args.get("bg_leak_color_dist", rmbg_defaults.bg_leak_color_dist)
        ),
        "bg_leak_alpha_threshold": int(
            args.get("bg_leak_alpha_threshold", rmbg_defaults.bg_leak_alpha_threshold)
        ),
        "bg_leak_keep_min_frames": int(
            args.get("bg_leak_keep_min_frames", rmbg_defaults.bg_leak_keep_min_frames)
        ),
        "bg_leak_alpha_weighted": bool(
            args.get("bg_leak_alpha_weighted", rmbg_defaults.bg_leak_alpha_weighted)
        ),
        "extra_rubrics": args.get("extra_rubrics"),
        "rubric_override": args.get("rubric_override"),
        "n_candidates": int(
            args.get(
                "n_candidates",
                video_defaults.max_quality_candidates,
            )
        ),
        "allow_unvalidated": bool(args.get("allow_unvalidated", False)),
    }
    if args.get("seed_base") is not None:
        kwargs["seed_base"] = int(args["seed_base"])
    if args.get("vlm_model"):
        kwargs["vlm_model"] = str(args["vlm_model"])

    with _owned_output_lock(kwargs["output_dir"], _require_workspace_root(ctx)):
        result = produce_validated_video_adapter.produce_validated_video(
            ctx.config, **kwargs
        )
    return result


# -------------------- extend_background --------------------


def _display_artifact_path(path: Path, workspace_root: Path) -> str:
    try:
        return path.relative_to(workspace_root).as_posix()
    except ValueError:
        return str(path)


def _background_source_lineage(
    source: Path,
    *,
    source_sha256: str,
    workspace_root: Path,
) -> list[Any]:
    lineage: list[Any] = []
    metadata_path = source.with_suffix(".asset.json")
    if metadata_path.is_file():
        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            metadata = None
        if (
            isinstance(metadata, dict)
            and metadata.get("artifact_sha256") == source_sha256
        ):
            inherited = metadata.get("lineage")
            if not isinstance(inherited, list):
                source_contract = metadata.get("source_contract")
                inherited = (
                    source_contract.get("lineage")
                    if isinstance(source_contract, dict)
                    else None
                )
            if isinstance(inherited, list):
                lineage.extend(inherited)
    lineage.append(
        {
            "artifact": _display_artifact_path(source, workspace_root),
            "sha256": source_sha256,
        }
    )
    return lineage


def _extend_background(args: dict[str, Any], ctx: ToolCtx) -> dict[str, Any]:
    input_path = args.get("input")
    output = args.get("output")
    if not input_path or not output:
        raise ToolError("INVALID_CONFIG", "`input` and `output` are required.")

    source = _resolve_asset_input(ctx, str(input_path))
    if not source.is_file():
        raise ToolError(
            "INPUT_FILE_NOT_FOUND",
            f"input image not found: {source}",
        )
    final_path = _resolve_static_asset_output(ctx, str(output))
    final_metadata = _resolve_asset_file_output(
        ctx,
        str(final_path.with_suffix(".asset.json")),
        expected_ending=".asset.json",
    )
    final_bbox = _resolve_asset_file_output(
        ctx,
        str(final_path.with_suffix(".bbox.json")),
        expected_ending=".bbox.json",
    )
    final_composite = _resolve_asset_file_output(
        ctx,
        str(final_path.with_name(f"{final_path.stem}-composite.png")),
        expected_ending=".png",
    )
    final_outpaint_full = _resolve_asset_file_output(
        ctx,
        str(final_path.with_name(f"{final_path.stem}-outpaint-full.png")),
        expected_ending=".png",
    )
    published_paths = {
        final_path,
        final_metadata,
        final_bbox,
        final_composite,
        final_outpaint_full,
    }
    if source in published_paths:
        raise ToolError(
            "INVALID_CONFIG",
            "extend_background input collides with an output path that the "
            "operation may publish or replace.",
        )
    workspace_root = _require_workspace_root(ctx)
    source_sha256 = hashlib.sha256(source.read_bytes()).hexdigest()
    lineage = _background_source_lineage(
        source,
        source_sha256=source_sha256,
        workspace_root=workspace_root,
    )

    with secure_file_lock(
        _static_output_lock_path(ctx, final_path),
        exclusive=True,
    ):
        staging = tempfile.TemporaryDirectory(prefix="gca-static-outpaint-")
        staged_path = Path(staging.name) / final_path.name
        staged_composite = staged_path.with_name(f"{staged_path.stem}-composite.png")
        staged_outpaint_full = staged_path.with_name(
            f"{staged_path.stem}-outpaint-full.png"
        )
        try:
            result = bg_extend.extend_right(
                ctx.config,
                input_path=source,
                output=staged_path,
                prompt=args.get("prompt"),
                size=args.get("size"),
                quality=str(
                    args.get("quality", ctx.config.assets.image.default_quality)
                ),
                review_output=staged_path,
            )
            if (
                Path(result["composite"]) != staged_composite
                or Path(result["outpaint_full"]) != staged_outpaint_full
            ):
                raise ToolError(
                    "INVALID_ARTIFACT",
                    "Background adapter returned unexpected review artifact paths.",
                )
            artifact_paths = {
                "composite": _display_artifact_path(
                    final_composite,
                    workspace_root,
                ),
                "outpaint_full": _display_artifact_path(
                    final_outpaint_full,
                    workspace_root,
                ),
            }
            review_artifact_sha256 = {
                "composite": hashlib.sha256(staged_composite.read_bytes()).hexdigest(),
                "outpaint_full": hashlib.sha256(
                    staged_outpaint_full.read_bytes()
                ).hexdigest(),
            }
            result.update(
                {
                    "composite": str(final_composite),
                    "outpaint_full": str(final_outpaint_full),
                    "intermediates": str(final_path.parent),
                    "source_sha256": source_sha256,
                    "lineage": lineage,
                    "artifact_paths": artifact_paths,
                    "review_artifact_sha256": review_artifact_sha256,
                }
            )
            provenance: dict[str, Any] = {
                "source": "generated",
                "producer": "gca-mcp.extend_background",
                "generation_method": "reference-outpaint",
            }
            if result.get("request_id"):
                provenance["request_id"] = str(result["request_id"])
            return _publish_staged_static_asset(
                ctx,
                staged_artifact=staged_path,
                final_artifact=final_path,
                staged_bbox=None,
                producer="extend_background",
                provenance=provenance,
                source_contract={
                    "sourceStrategy": "generated-image",
                    "assetType": "background",
                    "generationMode": "reference-outpaint",
                    "matting": "skipped-opaque",
                    "sourceArtifact": _display_artifact_path(
                        source,
                        workspace_root,
                    ),
                    "sourceSha256": source_sha256,
                    "lineage": lineage,
                    "reviewArtifacts": artifact_paths,
                    "reviewArtifactSha256": review_artifact_sha256,
                },
                result=result,
                staged_auxiliary=[
                    (staged_composite, final_composite),
                    (staged_outpaint_full, final_outpaint_full),
                ],
            )
        finally:
            staging.cleanup()


# -------------------- analyze_sprite_metadata --------------------


def _analyze_sprite_metadata(args: dict[str, Any], ctx: ToolCtx) -> dict[str, Any]:
    """VLM-based sprite facing + contentBbox analysis."""
    image = args.get("image")
    if not image:
        raise ToolError("INVALID_CONFIG", "`image` (spritesheet path) is required.")
    frame_width = args.get("frame_width")
    frame_height = args.get("frame_height")
    if not frame_width or not frame_height:
        raise ToolError(
            "INVALID_CONFIG", "`frame_width` and `frame_height` are required."
        )

    ref_image = args.get("reference_image")
    ref_path = (
        _resolve_asset_input(
            ctx,
            str(ref_image),
            allow_repo_reference=True,
        )
        if ref_image
        else None
    )

    defaults = ctx.config.assets.vlm
    vlm_model = str(args.get("vlm_model") or defaults.model)
    vlm_max_tokens = int(args.get("vlm_max_tokens", defaults.image_max_tokens))
    result = sprite_metadata_analyzer.analyze_sprite_metadata(
        ctx.config,
        image_path=_resolve_asset_input(ctx, str(image)),
        frame_width=int(frame_width),
        frame_height=int(frame_height),
        reference_image=Path(ref_path) if ref_path else None,
        description=args.get("description"),
        vlm_model=vlm_model,
        vlm_max_tokens=vlm_max_tokens,
        vlm_temperature=float(args.get("vlm_temperature", defaults.temperature)),
        vlm_timeout=float(args.get("vlm_timeout", defaults.image_timeout_seconds)),
    )
    return result


# -------------------- pack_spritesheet --------------------


def _pack_spritesheet(args: dict[str, Any], ctx: ToolCtx) -> dict[str, Any]:
    """Assemble matted frames into a Phaser spritesheet PNG + JSON."""
    frames_dir = args.get("frames_dir")
    output = args.get("output")
    if not frames_dir or not output:
        raise ToolError("INVALID_CONFIG", "`frames_dir` and `output` are required.")
    frame_width = args.get("frame_width")
    frame_height = args.get("frame_height")
    if not frame_width or not frame_height:
        raise ToolError(
            "INVALID_CONFIG", "`frame_width` and `frame_height` are required."
        )

    d = ctx.config.assets.sprite
    workspace_root = _require_workspace_root(ctx)
    output_path = _resolve_asset_output(ctx, str(output))
    if output_path.suffix != ".png":
        raise ToolError(
            "INVALID_CONFIG",
            "pack_spritesheet output must use a lowercase .png suffix.",
        )
    relative_output = output_path.relative_to(workspace_root)
    output_digest = hashlib.sha256(
        relative_output.as_posix().encode("utf-8")
    ).hexdigest()[:16]
    lock_path = workspace_lock_path(
        workspace_root,
        f"spritesheet-{output_digest}",
    )
    with secure_file_lock(lock_path, exclusive=True):
        return spritesheet_packer.pack_spritesheet(
            frames_dir=_resolve_asset_input(ctx, str(frames_dir)),
            output=output_path,
            frame_width=int(frame_width),
            frame_height=int(frame_height),
            fps=int(args.get("fps", d.fps)),
            max_sheet_width=int(args.get("max_sheet_width", d.max_sheet_width)),
            max_frames=int(args.get("max_frames", d.max_frames)),
            min_frame_dimension_px=int(
                args.get("min_frame_dimension_px", d.min_frame_dimension_px)
            ),
            facing=args.get("facing"),
            content_bbox=args.get("content_bbox"),
        )


# -------------------- content_bbox (props / tiles / UI) --------------------


def _content_bbox(args: dict[str, Any], ctx: ToolCtx) -> dict[str, Any]:
    """Alpha content bbox for a static asset (or each cell of a strip)."""
    image = args.get("image")
    if not image:
        raise ToolError("INVALID_CONFIG", "`image` is required.")
    out = args.get("output")
    ct = args.get("clean_threshold")
    return asset_bbox.compute_content_bbox(
        _resolve_asset_input(ctx, str(image)),
        cols=int(args.get("cols", 1)),
        rows=int(args.get("rows", 1)),
        alpha_threshold=int(args.get("alpha_threshold", 10)),
        clean_threshold=int(ct) if ct is not None else None,
        output=_resolve_asset_output(ctx, str(out)) if out else None,
    )


def _read_static_image_dimensions(artifact: Path) -> tuple[int, int]:
    """Reopen and verify a single-frame PNG, returning measured size."""
    if not artifact.is_file():
        raise ToolError("MISSING_ARTIFACT", f"Static asset not found: {artifact}")
    try:
        with Image.open(artifact) as image:
            width, height = image.size
            if image.format != "PNG":
                raise ToolError(
                    "INVALID_ARTIFACT",
                    "Runtime static assets must decode as PNG files.",
                )
            if int(getattr(image, "n_frames", 1)) != 1:
                raise ToolError(
                    "INVALID_ARTIFACT",
                    "Static assets must contain exactly one image frame.",
                )
            image.verify()
    except ToolError:
        raise
    except (OSError, ValueError) as exc:
        raise ToolError(
            "INVALID_ARTIFACT",
            f"Static asset is not a readable image: {artifact}",
        ) from exc
    if width < 1 or height < 1:
        raise ToolError("INVALID_ARTIFACT", "Static asset has invalid dimensions.")
    return width, height


def _seal_static_asset_metadata(
    ctx: ToolCtx,
    *,
    artifact: Path,
    canonical_artifact: Path | None = None,
    metadata_output: Path | None = None,
    producer: str,
    provenance: dict[str, Any],
    source_contract: dict[str, Any],
    result: dict[str, Any],
) -> dict[str, Any]:
    """Build one static metadata seal from staged pixels for pinned publication."""
    workspace_root = _require_workspace_root(ctx)
    final_artifact = _resolve_static_asset_output(
        ctx,
        str(canonical_artifact if canonical_artifact is not None else artifact),
    )
    width, height = _read_static_image_dimensions(artifact)
    if not isinstance(provenance, dict) or not provenance:
        raise ToolError(
            "INVALID_ARTIFACT",
            "Static asset metadata requires non-empty provenance.",
        )

    artifact_relative = final_artifact.relative_to(workspace_root).as_posix()
    canonical_source_contract = {
        "version": 1,
        **source_contract,
        "path": artifact_relative,
    }
    source_strategy = canonical_source_contract.get("sourceStrategy")
    if source_strategy not in {
        "generated-image",
        "supplied-image",
        "licensed-image",
    }:
        raise ToolError(
            "INVALID_ARTIFACT",
            "Static asset source contract has an invalid sourceStrategy.",
        )

    artifact_sha256 = hashlib.sha256(artifact.read_bytes()).hexdigest()
    metadata: dict[str, Any] = {
        "schema_version": 2,
        "producer": producer,
        "artifact": artifact_relative,
        "artifact_sha256": artifact_sha256,
        "width": width,
        "height": height,
        "provenance": provenance,
        "source_contract": canonical_source_contract,
    }
    for field in ("anchors", "slots"):
        if isinstance(result.get(field), list):
            metadata[field] = result[field]
    if isinstance(result.get("source_sha256"), str):
        metadata["source_sha256"] = result["source_sha256"]
    if isinstance(result.get("lineage"), list):
        metadata["lineage"] = result["lineage"]
    if isinstance(result.get("artifact_paths"), dict):
        metadata["artifact_paths"] = result["artifact_paths"]
    if isinstance(result.get("review_artifact_sha256"), dict):
        metadata["review_artifact_sha256"] = result["review_artifact_sha256"]
    metadata_path = _resolve_asset_file_output(
        ctx,
        str(final_artifact.with_suffix(".asset.json")),
        expected_ending=".asset.json",
    )
    atomic_write_json(metadata_output or metadata_path, metadata)

    result.update(
        {
            "artifact_sha256": artifact_sha256,
            "width": width,
            "height": height,
            "provenance": provenance,
            "source_contract": canonical_source_contract,
            "metadata_sidecar": str(metadata_path),
        }
    )
    return result


def _register_existing_static_asset(
    args: dict[str, Any], ctx: ToolCtx
) -> dict[str, Any]:
    """Seal an existing still image with producer-owned hash/dimension metadata."""
    image_value = args.get("image")
    if not image_value:
        raise ToolError("INVALID_CONFIG", "`image` is required.")
    provenance = args.get("provenance")
    if not isinstance(provenance, dict) or not provenance:
        raise ToolError(
            "INVALID_CONFIG",
            "`provenance` must be a non-empty object for an existing asset.",
        )
    source_strategy = args.get("source_strategy")
    if source_strategy is None:
        source_strategy = (
            "licensed-image" if provenance.get("licenseId") else "supplied-image"
        )
    if source_strategy not in {"supplied-image", "licensed-image"}:
        raise ToolError(
            "INVALID_CONFIG",
            "`source_strategy` must be `supplied-image` or `licensed-image`.",
        )
    if source_strategy == "licensed-image" and not provenance.get("licenseId"):
        raise ToolError(
            "INVALID_CONFIG",
            "Licensed assets require provenance.licenseId.",
        )
    image_path = _resolve_static_asset_output(ctx, str(image_value))
    _resolve_asset_file_output(
        ctx,
        str(image_path.with_suffix(".asset.json")),
        expected_ending=".asset.json",
    )
    _resolve_asset_file_output(
        ctx,
        str(image_path.with_suffix(".bbox.json")),
        expected_ending=".bbox.json",
    )
    if not image_path.is_file():
        raise ToolError("MISSING_ARTIFACT", f"Existing asset not found: {image_path}")
    workspace_root = _require_workspace_root(ctx)
    workspace = _require_pinned_static_workspace(ctx)
    relative_image = image_path.relative_to(workspace_root)

    with secure_file_lock(
        _static_output_lock_path(ctx, image_path),
        exclusive=True,
    ):
        with tempfile.TemporaryDirectory(prefix="gca-static-register-") as temp:
            staged_path = Path(temp) / image_path.name
            staged_bbox = staged_path.with_suffix(".bbox.json")
            workspace.copy_from_pinned(relative_image, staged_path)
            width, height = _read_static_image_dimensions(staged_path)
            result: dict[str, Any] = {
                "image": str(image_path),
                "width": width,
                "height": height,
                "artifact_sha256": hashlib.sha256(staged_path.read_bytes()).hexdigest(),
                "provenance": provenance,
                "source_strategy": source_strategy,
            }
            bbox_for_publish: Path | None = None
            if bool(args.get("collidable", False)):
                ct = args.get("clean_threshold", 24)
                bbox = asset_bbox.compute_content_bbox(
                    staged_path,
                    cols=int(args.get("cols", 1)),
                    rows=int(args.get("rows", 1)),
                    clean_threshold=int(ct) if ct is not None else None,
                    output=staged_bbox,
                    image_reference=image_path,
                )
                result["bbox"] = bbox.get("contentBbox") or bbox.get("cells") or bbox
                bbox_for_publish = staged_bbox

            for field in ("anchors", "slots"):
                if isinstance(args.get(field), list):
                    result[field] = args[field]
            return _publish_staged_static_asset(
                ctx,
                staged_artifact=staged_path,
                final_artifact=image_path,
                staged_bbox=bbox_for_publish,
                producer="register_existing_static_asset",
                provenance=provenance,
                source_contract={
                    "sourceStrategy": source_strategy,
                    "registration": "preserve-pixels",
                },
                result=result,
            )


def _reference_edit_contract_is_current(
    ctx: ToolCtx,
    *,
    provenance: dict[str, Any],
    source_contract: dict[str, Any],
) -> bool:
    """Verify a crop-seeded static producer still points at the sealed crop."""
    workspace_root = _require_workspace_root(ctx)
    reference_artifact = source_contract.get("referenceArtifact")
    reference_sha256 = source_contract.get("referenceArtifactSha256")
    if (
        not isinstance(reference_artifact, str)
        or not reference_artifact
        or Path(reference_artifact).is_absolute()
        or not isinstance(reference_sha256, str)
        or not re.fullmatch(r"[0-9a-f]{64}", reference_sha256)
        or provenance.get("generation_method") != "reference-edit"
        or provenance.get("referenceArtifact") != reference_artifact
        or provenance.get("referenceArtifactSha256") != reference_sha256
    ):
        return False
    try:
        reference_path = _resolve_asset_input(ctx, reference_artifact)
    except ToolError:
        return False
    if (
        reference_artifact != reference_path.relative_to(workspace_root).as_posix()
        or not reference_path.is_file()
    ):
        return False
    try:
        return (
            hashlib.sha256(reference_path.read_bytes()).hexdigest() == reference_sha256
        )
    except OSError:
        return False


def _finalize_asset_manifest(args: dict[str, Any], ctx: ToolCtx) -> dict[str, Any]:
    """Merge producer-owned static bbox sidecars into one manifest atomically."""
    workspace_root = _require_workspace_root(ctx)
    game_dir_arg = args.get("game_dir")
    if game_dir_arg:
        game_dir = _resolve_under_workspace(ctx, str(game_dir_arg))
    else:
        game_dir = workspace_root
    assert game_dir is not None
    game_dir = game_dir.resolve()
    if game_dir != workspace_root:
        raise ToolError(
            "INVALID_PATH",
            "finalize_asset_manifest only writes the active workspace; "
            "`game_dir`, when supplied, must resolve to that workspace root.",
        )

    manifest_path = _resolve_asset_file_output(
        ctx,
        "assets/asset-manifest.json",
        expected_ending="asset-manifest.json",
    )
    if not manifest_path.is_file():
        raise ToolError(
            "MISSING_ARTIFACT",
            f"Asset manifest does not exist: {manifest_path}",
        )

    with _asset_manifest_lock(manifest_path):
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ToolError(
                "INVALID_ARTIFACT",
                f"Asset manifest is not valid JSON: {manifest_path}",
            ) from exc
        entries = manifest.get("assets") if isinstance(manifest, dict) else None
        if not isinstance(entries, list):
            raise ToolError(
                "INVALID_ARTIFACT",
                "Asset manifest must use the canonical top-level `assets` array.",
            )

        collidable = {"prop", "tile", "tileset", "platform", "obstacle"}
        sprites = {"sprite", "character", "enemy", "boss", "npc", "projectile"}
        updated: list[str] = []
        missing: list[str] = []
        stale: list[str] = []
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            category = str(entry.get("category") or "").lower()
            if category in sprites:
                continue
            asset_path_value = entry.get("path")
            if not isinstance(asset_path_value, str) or not asset_path_value:
                missing.append(str(entry.get("key") or "<missing-key>"))
                continue
            if Path(asset_path_value).is_absolute():
                raise ToolError(
                    "INVALID_PATH",
                    f"Manifest asset path must be workspace-relative: {asset_path_value}",
                )
            asset_path = _resolve_static_asset_output(ctx, asset_path_value)
            metadata_sidecar = _resolve_asset_file_output(
                ctx,
                str(asset_path.with_suffix(".asset.json")),
                expected_ending=".asset.json",
            )
            if not asset_path.is_file() or not metadata_sidecar.is_file():
                missing.append(str(entry.get("key") or asset_path_value))
                continue
            if metadata_sidecar.stat().st_mtime_ns < asset_path.stat().st_mtime_ns:
                stale.append(str(entry.get("key") or asset_path_value))
                continue
            try:
                metadata = json.loads(metadata_sidecar.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                missing.append(str(entry.get("key") or asset_path_value))
                continue
            if not isinstance(metadata, dict):
                missing.append(str(entry.get("key") or asset_path_value))
                continue
            try:
                actual_width, actual_height = _read_static_image_dimensions(asset_path)
                digest = hashlib.sha256(asset_path.read_bytes()).hexdigest()
            except (ToolError, OSError):
                stale.append(str(entry.get("key") or asset_path_value))
                continue
            if metadata.get("artifact_sha256") != digest:
                stale.append(str(entry.get("key") or asset_path_value))
                continue
            sidecar_width = metadata.get("width")
            sidecar_height = metadata.get("height")
            if type(sidecar_width) is not int or type(sidecar_height) is not int:
                missing.append(str(entry.get("key") or asset_path_value))
                continue
            if (
                sidecar_width != actual_width
                or sidecar_height != actual_height
                or sidecar_width < 1
                or sidecar_height < 1
            ):
                stale.append(str(entry.get("key") or asset_path_value))
                continue
            expected_source = entry.get("sourceStrategy")
            source_contract = metadata.get("source_contract")
            expected_asset_type = _manifest_static_asset_type(category)
            producer = metadata.get("producer")
            if (
                expected_asset_type == "ui"
                and not _file_backed_ui_sizing_matches_manifest(
                    manifest_quality_profile=manifest.get("qualityProfile"),
                    entry=entry,
                    actual_width=actual_width,
                    actual_height=actual_height,
                )
            ):
                stale.append(str(entry.get("key") or asset_path_value))
                continue
            if expected_source == "generated-image":
                valid_producers = {"produce_static_asset"}
                if category == "background":
                    valid_producers.add("extend_background")
            else:
                valid_producers = {"register_existing_static_asset"}
            if (
                metadata.get("schema_version") != 2
                or producer not in valid_producers
                or metadata.get("artifact") != asset_path_value
                or not isinstance(metadata.get("provenance"), dict)
                or not metadata["provenance"]
                or not isinstance(source_contract, dict)
                or source_contract.get("version") != 1
                or source_contract.get("sourceStrategy") != expected_source
                or source_contract.get("path") != asset_path_value
                or (
                    expected_source == "generated-image"
                    and (
                        expected_asset_type is None
                        or source_contract.get("assetType") != expected_asset_type
                    )
                )
            ):
                stale.append(str(entry.get("key") or asset_path_value))
                continue
            provenance = metadata["provenance"]
            if producer == "produce_static_asset":
                generation_mode = source_contract.get("generationMode")
                reference_fields = {
                    "referenceArtifact",
                    "referenceArtifactSha256",
                }
                if generation_mode == "reference-edit":
                    if not _reference_edit_contract_is_current(
                        ctx,
                        provenance=provenance,
                        source_contract=source_contract,
                    ):
                        stale.append(str(entry.get("key") or asset_path_value))
                        continue
                elif generation_mode == "text-to-image":
                    if any(
                        field in source_contract or field in provenance
                        for field in reference_fields
                    ):
                        stale.append(str(entry.get("key") or asset_path_value))
                        continue
                else:
                    stale.append(str(entry.get("key") or asset_path_value))
                    continue
                if (
                    expected_asset_type == "ui"
                    and not _generated_ui_sizing_matches_manifest(
                        source_contract=source_contract,
                        manifest_quality_profile=manifest.get("qualityProfile"),
                        entry=entry,
                        actual_width=actual_width,
                        actual_height=actual_height,
                    )
                ):
                    stale.append(str(entry.get("key") or asset_path_value))
                    continue
            if producer == "extend_background":
                expected_artifact_paths = {
                    "composite": asset_path.with_name(
                        f"{asset_path.stem}-composite.png"
                    )
                    .relative_to(workspace_root)
                    .as_posix(),
                    "outpaint_full": asset_path.with_name(
                        f"{asset_path.stem}-outpaint-full.png"
                    )
                    .relative_to(workspace_root)
                    .as_posix(),
                }
                artifact_paths = metadata.get("artifact_paths")
                review_artifact_sha256 = metadata.get("review_artifact_sha256")
                source_sha256 = metadata.get("source_sha256")
                lineage = metadata.get("lineage")
                source_artifact = source_contract.get("sourceArtifact")
                source_path: Path | None = None
                if isinstance(source_artifact, str) and source_artifact:
                    try:
                        source_path = _resolve_asset_input(ctx, source_artifact)
                    except ToolError:
                        source_path = None
                resolved_review_paths = {
                    key: _resolve_asset_file_output(
                        ctx,
                        path,
                        expected_ending=".png",
                    )
                    for key, path in expected_artifact_paths.items()
                }
                lineage_valid = (
                    isinstance(lineage, list)
                    and bool(lineage)
                    and all(
                        isinstance(item, dict)
                        and isinstance(item.get("artifact"), str)
                        and bool(item["artifact"])
                        and isinstance(item.get("sha256"), str)
                        and re.fullmatch(r"[0-9a-f]{64}", item["sha256"])
                        for item in lineage
                    )
                    and lineage[-1]
                    == {
                        "artifact": source_artifact,
                        "sha256": source_sha256,
                    }
                )
                review_hashes_valid = (
                    isinstance(review_artifact_sha256, dict)
                    and set(review_artifact_sha256) == set(expected_artifact_paths)
                    and source_contract.get("reviewArtifactSha256")
                    == review_artifact_sha256
                    and all(
                        isinstance(review_artifact_sha256.get(key), str)
                        and re.fullmatch(
                            r"[0-9a-f]{64}",
                            review_artifact_sha256[key],
                        )
                        and hashlib.sha256(
                            resolved_review_paths[key].read_bytes()
                        ).hexdigest()
                        == review_artifact_sha256[key]
                        for key in expected_artifact_paths
                        if resolved_review_paths[key].is_file()
                    )
                    and all(path.is_file() for path in resolved_review_paths.values())
                )
                if (
                    artifact_paths != expected_artifact_paths
                    or source_contract.get("reviewArtifacts") != expected_artifact_paths
                    or source_contract.get("sourceSha256") != source_sha256
                    or not isinstance(source_sha256, str)
                    or not re.fullmatch(r"[0-9a-f]{64}", source_sha256)
                    or source_path is None
                    or not source_path.is_file()
                    or hashlib.sha256(source_path.read_bytes()).hexdigest()
                    != source_sha256
                    or not lineage_valid
                    or source_contract.get("lineage") != lineage
                    or not review_hashes_valid
                ):
                    stale.append(str(entry.get("key") or asset_path_value))
                    continue
            if expected_source in {"supplied-image", "licensed-image"} and metadata.get(
                "provenance"
            ) != entry.get("provenance"):
                stale.append(str(entry.get("key") or asset_path_value))
                continue
            entry["width"] = actual_width
            entry["height"] = actual_height
            for field in ("anchors", "slots"):
                if isinstance(metadata.get(field), list):
                    entry[field] = metadata[field]

            if category in collidable and entry.get("decorative") is not True:
                sidecar = _resolve_asset_file_output(
                    ctx,
                    str(asset_path.with_suffix(".bbox.json")),
                    expected_ending=".bbox.json",
                )
                if not sidecar.is_file():
                    missing.append(str(entry.get("key") or asset_path_value))
                    continue
                if sidecar.stat().st_mtime_ns < asset_path.stat().st_mtime_ns:
                    stale.append(str(entry.get("key") or asset_path_value))
                    continue
                try:
                    data = json.loads(sidecar.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    missing.append(str(entry.get("key") or asset_path_value))
                    continue
                bbox = data.get("contentBbox")
                cells = data.get("cells")
                if isinstance(cells, list) and len(cells) > 1:
                    entry["cells"] = cells
                    entry.pop("bbox", None)
                elif isinstance(bbox, dict) and all(
                    key in bbox for key in ("x", "y", "w", "h")
                ):
                    entry["bbox"] = {key: bbox[key] for key in ("x", "y", "w", "h")}
                    entry.pop("cells", None)
                else:
                    missing.append(str(entry.get("key") or asset_path_value))
                    continue
            updated.append(str(entry.get("key") or asset_path_value))

        if missing or stale:
            details = []
            if missing:
                details.append(f"missing/invalid asset metadata: {', '.join(missing)}")
            if stale:
                details.append(
                    f"stale/hash-mismatched asset metadata: {', '.join(stale)}"
                )
            raise ToolError(
                "MISSING_METADATA",
                "; ".join(details),
                hint=(
                    "Regenerate the asset and its producer sidecars after the final PNG, "
                    "then finalize again."
                ),
            )

        atomic_write_json(manifest_path, manifest)

    return {
        "manifest": str(manifest_path),
        "updated": updated,
        "updated_count": len(updated),
        "atomic": True,
    }


def _trim_transparent(args: dict[str, Any], ctx: ToolCtx) -> dict[str, Any]:
    """Crop a static asset to its alpha content bbox (drops transparent margin)."""
    image = args.get("image")
    if not image:
        raise ToolError("INVALID_CONFIG", "`image` is required.")
    out = args.get("output")
    ct = args.get("clean_threshold", 24)
    return asset_bbox.trim_to_content_bbox(
        _resolve_asset_output(ctx, str(image)),
        output=_resolve_asset_output(ctx, str(out)) if out else None,
        padding=int(args.get("padding", 2)),
        alpha_threshold=int(args.get("alpha_threshold", 10)),
        clean_threshold=int(ct) if ct is not None else None,
        min_trim_px=int(args.get("min_trim_px", 4)),
    )


def _scale_uniform(args: dict[str, Any], ctx: ToolCtx) -> dict[str, Any]:
    """Scale an RGBA PNG UNIFORMLY (aspect preserved) to a target scale."""
    image = args.get("image")
    if not image:
        raise ToolError("INVALID_CONFIG", "`image` is required.")
    th = args.get("target_height")
    tw = args.get("target_width")
    box = args.get("max_box")
    max_box = None
    if box is not None:
        if not (isinstance(box, (list, tuple)) and len(box) == 2):
            raise ToolError("INVALID_CONFIG", "`max_box` must be [width, height].")
        max_box = (int(box[0]), int(box[1]))
    out = args.get("output")
    return asset_bbox.scale_to_target(
        _resolve_asset_output(ctx, str(image)),
        target_height=int(th) if th is not None else None,
        target_width=int(tw) if tw is not None else None,
        max_box=max_box,
        allow_upscale=bool(args.get("allow_upscale", True)),
        output=_resolve_asset_output(ctx, str(out)) if out else None,
    )


# -------------------- produce_static_asset (atomic prop/tile/UI chain) --------------------


_UI_ALIAS_ASSET_TYPES = {
    "hud",
    "icon",
    "button",
    "bar",
    "panel",
    "badge",
    "gauge",
    "meter",
    "dashboard",
    "toolbar",
    "counter",
    "widget",
    "lettering",
    "logo",
    "wordmark",
    "art-word",
    "frame",
    "overlay",
}
_UI_STATIC_ASSET_TYPES = {"ui"}
_CANONICAL_STATIC_ASSET_TYPES = frozenset(
    {
        "background",
        "effect",
        "fx",
        "impact",
        "item",
        "map",
        "obstacle",
        "parallax",
        "platform",
        "prop",
        "tile",
        "tileset",
        "ui",
    }
)
_MANIFEST_UI_CATEGORIES = frozenset(_UI_STATIC_ASSET_TYPES | _UI_ALIAS_ASSET_TYPES)
_QUALITY_PROFILES = {"art_directed", "responsive", "pixel_art", "minimal"}
_MANIFEST_UI_SIZING_FIELDS = frozenset(
    {
        "targetWidth",
        "targetHeight",
        "maxBox",
        "logicalBox",
        "baseSize",
        "integerScales",
    }
)
_LEGACY_SOURCE_UI_SIZING_FIELDS = frozenset(
    {
        "targetWidth",
        "targetHeight",
        "maxBox",
        "logicalBox",
        "baseSize",
        "finalDimensions",
        "sizing",
        "resampler",
    }
)


def _manifest_static_asset_type(category: str) -> str | None:
    """Map manifest categories to the producer's canonical static asset type."""
    normalized = category.strip().lower()
    if normalized in _MANIFEST_UI_CATEGORIES:
        return "ui"
    if normalized in _CANONICAL_STATIC_ASSET_TYPES:
        return normalized
    return None


def _require_workspace_quality_profile(ctx: ToolCtx) -> str:
    """Read the authoritative routed profile; UI production fails closed without it."""
    state_path = _require_workspace_root(ctx) / ".playtest" / "state.json"
    if not state_path.is_file():
        raise ToolError(
            "STATE_JSON_MISSING",
            "Static UI production requires .playtest/state.json with quality_profile.",
        )
    try:
        payload = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ToolError(
            "INVALID_CONFIG",
            f"Could not read routed quality_profile from {state_path}: {exc}",
        ) from exc
    profile = payload.get("quality_profile") if isinstance(payload, dict) else None
    normalized = str(profile or "").strip().lower()
    if normalized not in _QUALITY_PROFILES:
        raise ToolError(
            "INVALID_CONFIG",
            "Static UI production requires a valid routed quality_profile: "
            + ", ".join(sorted(_QUALITY_PROFILES))
            + ".",
        )
    return normalized


def _positive_target_int(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ToolError("INVALID_CONFIG", f"`{field}` must be a positive integer.")
    return value


def _validate_static_target(
    args: dict[str, Any],
) -> tuple[
    int | None,
    int | None,
    tuple[int, int] | None,
    tuple[int, int] | None,
]:
    raw_height = args.get("target_height")
    raw_width = args.get("target_width")
    raw_box = args.get("max_box")
    raw_base_size = args.get("base_size")
    target_count = sum(
        value is not None for value in (raw_height, raw_width, raw_box, raw_base_size)
    )
    if target_count > 1:
        raise ToolError(
            "INVALID_CONFIG",
            "Pass at most ONE of target_height / target_width / max_box / base_size.",
        )
    target_height = (
        _positive_target_int(raw_height, "target_height")
        if raw_height is not None
        else None
    )
    target_width = (
        _positive_target_int(raw_width, "target_width")
        if raw_width is not None
        else None
    )
    max_box: tuple[int, int] | None = None
    if raw_box is not None:
        if not isinstance(raw_box, (list, tuple)) or len(raw_box) != 2:
            raise ToolError("INVALID_CONFIG", "`max_box` must be [width, height].")
        max_box = (
            _positive_target_int(raw_box[0], "max_box[0]"),
            _positive_target_int(raw_box[1], "max_box[1]"),
        )
    base_size: tuple[int, int] | None = None
    if raw_base_size is not None:
        if not isinstance(raw_base_size, (list, tuple)) or len(raw_base_size) != 2:
            raise ToolError(
                "INVALID_CONFIG",
                "`base_size` must contain exactly [width, height].",
            )
        base_size = (
            _positive_target_int(raw_base_size[0], "base_size[0]"),
            _positive_target_int(raw_base_size[1], "base_size[1]"),
        )
    return target_height, target_width, max_box, base_size


def _canonical_ui_sizing_contract(
    *,
    quality_profile: str,
    target_height: int | None,
    target_width: int | None,
    max_box: tuple[int, int] | None,
    base_size: tuple[int, int] | None,
    final_width: int,
    final_height: int,
) -> dict[str, Any]:
    """Build the one canonical, profile-routed UI sizing contract."""
    contract: dict[str, Any] = {
        "finalDimensions": {"width": final_width, "height": final_height},
        "aspectPreserved": True,
    }
    if quality_profile == "art_directed" and target_width is not None:
        contract.update(
            {
                "mode": "target-width",
                "targetWidth": target_width,
                "resampler": "lanczos",
            }
        )
    elif quality_profile == "art_directed" and target_height is not None:
        contract.update(
            {
                "mode": "target-height",
                "targetHeight": target_height,
                "resampler": "lanczos",
            }
        )
    elif quality_profile in {"responsive", "minimal"} and max_box is not None:
        contract.update(
            {
                "mode": "max-box",
                "maxBox": [max_box[0], max_box[1]],
                "resampler": "lanczos",
            }
        )
    elif quality_profile == "pixel_art" and base_size is not None:
        contract.update(
            {
                "mode": "base-size",
                "baseSize": [base_size[0], base_size[1]],
                "resampler": "nearest-neighbor",
            }
        )
    else:
        raise ToolError(
            "INVALID_CONFIG",
            f"Could not derive canonical UI sizing for {quality_profile}.",
        )
    return contract


def _positive_pair(value: Any) -> tuple[int, int] | None:
    if not isinstance(value, list) or len(value) != 2:
        return None
    if any(
        isinstance(item, bool) or not isinstance(item, int) or item <= 0
        for item in value
    ):
        return None
    return value[0], value[1]


def _file_backed_ui_sizing_matches_manifest(
    *,
    manifest_quality_profile: Any,
    entry: dict[str, Any],
    actual_width: int,
    actual_height: int,
) -> bool:
    """Validate authored UI sizing against pixels for every file source strategy."""
    present_fields = {field for field in _MANIFEST_UI_SIZING_FIELDS if field in entry}
    if manifest_quality_profile == "art_directed":
        if present_fields == {"targetWidth"}:
            target = entry.get("targetWidth")
            return type(target) is int and target > 0 and actual_width == target
        if present_fields == {"targetHeight"}:
            target = entry.get("targetHeight")
            return type(target) is int and target > 0 and actual_height == target
        return False

    if manifest_quality_profile in {"responsive", "minimal"}:
        fit_fields = present_fields & {"maxBox", "logicalBox"}
        if len(fit_fields) != 1 or present_fields != fit_fields:
            return False
        box = _positive_pair(entry.get(next(iter(fit_fields))))
        return box is not None and actual_width <= box[0] and actual_height <= box[1]

    if manifest_quality_profile == "pixel_art":
        base_size = _positive_pair(entry.get("baseSize"))
        integer_scales = entry.get("integerScales")
        return (
            present_fields == {"baseSize", "integerScales"}
            and base_size is not None
            and (actual_width, actual_height) == base_size
            and isinstance(integer_scales, list)
            and bool(integer_scales)
            and all(type(scale) is int and scale > 0 for scale in integer_scales)
        )

    return False


def _generated_ui_sizing_matches_manifest(
    *,
    source_contract: dict[str, Any],
    manifest_quality_profile: Any,
    entry: dict[str, Any],
    actual_width: int,
    actual_height: int,
) -> bool:
    """Validate producer-owned UI sizing without rewriting authored fields."""
    quality_profile = source_contract.get("qualityProfile")
    if (
        quality_profile not in _QUALITY_PROFILES
        or manifest_quality_profile != quality_profile
        or any(field in source_contract for field in _LEGACY_SOURCE_UI_SIZING_FIELDS)
    ):
        return False

    sizing = source_contract.get("sizingContract")
    if not isinstance(sizing, dict):
        return False
    final_dimensions = sizing.get("finalDimensions")
    if (
        not isinstance(final_dimensions, dict)
        or set(final_dimensions) != {"width", "height"}
        or type(final_dimensions.get("width")) is not int
        or type(final_dimensions.get("height")) is not int
        or final_dimensions.get("width") != actual_width
        or final_dimensions.get("height") != actual_height
        or sizing.get("aspectPreserved") is not True
    ):
        return False

    present_manifest_fields = {
        field for field in _MANIFEST_UI_SIZING_FIELDS if field in entry
    }
    mode = sizing.get("mode")
    if quality_profile == "art_directed":
        if mode == "target-width":
            target_field = "targetWidth"
            expected_keys = {
                "mode",
                target_field,
                "finalDimensions",
                "aspectPreserved",
                "resampler",
            }
            actual_axis = actual_width
        elif mode == "target-height":
            target_field = "targetHeight"
            expected_keys = {
                "mode",
                target_field,
                "finalDimensions",
                "aspectPreserved",
                "resampler",
            }
            actual_axis = actual_height
        else:
            return False
        target = sizing.get(target_field)
        manifest_target = entry.get(target_field)
        return (
            set(sizing) == expected_keys
            and sizing.get("resampler") == "lanczos"
            and isinstance(target, int)
            and not isinstance(target, bool)
            and target > 0
            and actual_axis == target
            and present_manifest_fields == {target_field}
            and type(manifest_target) is int
            and manifest_target == target
        )

    if quality_profile in {"responsive", "minimal"}:
        expected_keys = {
            "mode",
            "maxBox",
            "finalDimensions",
            "aspectPreserved",
            "resampler",
        }
        producer_box = _positive_pair(sizing.get("maxBox"))
        fit_fields = present_manifest_fields & {"maxBox", "logicalBox"}
        if len(fit_fields) != 1:
            return False
        manifest_field = next(iter(fit_fields))
        manifest_box = _positive_pair(entry.get(manifest_field))
        return (
            mode == "max-box"
            and set(sizing) == expected_keys
            and sizing.get("resampler") == "lanczos"
            and producer_box is not None
            and manifest_box == producer_box
            and present_manifest_fields == {manifest_field}
            and actual_width <= producer_box[0]
            and actual_height <= producer_box[1]
        )

    expected_keys = {
        "mode",
        "baseSize",
        "finalDimensions",
        "aspectPreserved",
        "resampler",
    }
    producer_base = _positive_pair(sizing.get("baseSize"))
    manifest_base = _positive_pair(entry.get("baseSize"))
    integer_scales = entry.get("integerScales")
    return (
        mode == "base-size"
        and set(sizing) == expected_keys
        and sizing.get("resampler") == "nearest-neighbor"
        and producer_base is not None
        and manifest_base == producer_base
        and present_manifest_fields == {"baseSize", "integerScales"}
        and isinstance(integer_scales, list)
        and bool(integer_scales)
        and all(type(scale) is int and scale > 0 for scale in integer_scales)
        and (actual_width, actual_height) == producer_base
    )


def _staged_png_path(final_path: Path) -> Path:
    return final_path.with_name(
        f".{final_path.stem}.{uuid.uuid4().hex}.tmp{final_path.suffix}"
    )


def _static_output_lock_path(ctx: ToolCtx, final_path: Path) -> Path:
    root = _require_workspace_root(ctx)
    relative = final_path.relative_to(root).as_posix()
    digest = hashlib.sha256(relative.encode("utf-8")).hexdigest()[:16]
    return workspace_lock_path(root, f"static-asset-{digest}")


def _publish_staged_static_asset(
    ctx: ToolCtx,
    *,
    staged_artifact: Path,
    final_artifact: Path,
    staged_bbox: Path | None,
    producer: str,
    provenance: dict[str, Any],
    source_contract: dict[str, Any],
    result: dict[str, Any],
    staged_auxiliary: list[tuple[Path, Path]] | None = None,
) -> dict[str, Any]:
    """Publish all static outputs through one pinned, rollback-safe transaction."""
    workspace_root = _require_workspace_root(ctx)
    workspace = _require_pinned_static_workspace(ctx)
    final_artifact = _resolve_static_asset_output(ctx, str(final_artifact))
    final_metadata = _resolve_asset_file_output(
        ctx,
        str(final_artifact.with_suffix(".asset.json")),
        expected_ending=".asset.json",
    )
    final_bbox = _resolve_asset_file_output(
        ctx,
        str(final_artifact.with_suffix(".bbox.json")),
        expected_ending=".bbox.json",
    )
    auxiliary_pairs = [
        (
            staged,
            _resolve_asset_file_output(
                ctx,
                str(final),
                expected_ending=".png",
            ),
        )
        for staged, final in list(staged_auxiliary or [])
    ]
    publish_pairs: list[tuple[Path, Path]] = [
        (staged_artifact, final_artifact),
        *auxiliary_pairs,
    ]
    protected = [
        final_artifact,
        final_metadata,
        final_bbox,
        *(final for _staged, final in auxiliary_pairs),
    ]
    if len(protected) != len(set(protected)):
        raise ToolError(
            "INVALID_PATH",
            "Static publication contains colliding artifact or sidecar paths.",
        )
    if staged_bbox is not None:
        try:
            bbox_payload = json.loads(staged_bbox.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ToolError(
                "INVALID_ARTIFACT",
                f"Staged bbox sidecar is not valid JSON: {staged_bbox}",
            ) from exc
        if not isinstance(bbox_payload, dict):
            raise ToolError(
                "INVALID_ARTIFACT",
                f"Staged bbox sidecar must contain an object: {staged_bbox}",
            )
        bbox_payload["image"] = str(final_artifact)
        atomic_write_json(staged_bbox, bbox_payload)
        publish_pairs.append((staged_bbox, final_bbox))

    result["output"] = str(final_artifact)
    if staged_bbox is not None:
        result["bbox_sidecar"] = str(final_bbox)
    else:
        result["bbox_sidecar"] = None

    with tempfile.TemporaryDirectory(prefix="gca-static-metadata-") as temp:
        staged_metadata = Path(temp) / final_metadata.name
        sealed = _seal_static_asset_metadata(
            ctx,
            artifact=staged_artifact,
            canonical_artifact=final_artifact,
            metadata_output=staged_metadata,
            producer=producer,
            provenance=provenance,
            source_contract=source_contract,
            result=result,
        )
        replacements = [
            *publish_pairs,
            (staged_metadata, final_metadata),
        ]
        workspace.commit_files_to_pinned(
            [
                (staged, final.relative_to(workspace_root))
                for staged, final in replacements
            ],
            remove=(
                [final_bbox.relative_to(workspace_root)] if staged_bbox is None else []
            ),
        )
        workspace.assert_identity()
        return sealed


def _produce_static_asset(args: dict[str, Any], ctx: ToolCtx) -> dict[str, Any]:
    output = args.get("output")
    if not output:
        raise ToolError("INVALID_CONFIG", "`prompt` and `output` are required.")
    final_path = _resolve_static_asset_output(ctx, str(output))
    _resolve_asset_file_output(
        ctx,
        str(final_path.with_suffix(".asset.json")),
        expected_ending=".asset.json",
    )
    _resolve_asset_file_output(
        ctx,
        str(final_path.with_suffix(".bbox.json")),
        expected_ending=".bbox.json",
    )
    with secure_file_lock(
        _static_output_lock_path(ctx, final_path),
        exclusive=True,
    ):
        staging = tempfile.TemporaryDirectory(prefix="gca-static-produce-")
        staged_path = Path(staging.name) / final_path.name
        try:
            return _produce_static_asset_locked(
                args,
                ctx,
                final_path=final_path,
                staged_path=staged_path,
            )
        finally:
            staging.cleanup()


def _produce_static_asset_locked(
    args: dict[str, Any],
    ctx: ToolCtx,
    *,
    final_path: Path,
    staged_path: Path,
) -> dict[str, Any]:
    """Generate a static asset and run the full configured matting chain.

    Welds ``generate_image → configured matting backend → trim_transparent
    (→ content_bbox when collidable)`` into a single atomic tool so an agent
    can never ship a prop/tile/UI widget that skipped background removal — the
    exact gap that left whole batches of UI assets as opaque RGB plates.

    When a concept crop is given via ``ref_image`` (or ``input``), Stage 1
    seeds generation from that crop with ``edit_image`` instead of text-only
    ``generate_image`` — so a crop-seeded UI / binding panel gets the SAME
    welded ``edit_image(crop) → configured matting backend →
    trim_transparent`` chain and can no longer skip trim (the gap that shipped
    half-empty panels).

    Background and map art is opaque by design and CANNOT be matted (there is no
    removable key color). Those skip the alpha chain: pass ``skip_matting=True``
    explicitly, OR rely on the auto-detect — any ``asset_type`` in
    ``OPAQUE_SCENE_ASSET_TYPES`` is treated as skip even if the flag was
    forgotten.

    Parallax is full-scene but not opaque. Its layers composite above the sky,
    so they are matted like any other asset and receive a chroma-key plate; only
    the bottom-most layer is genuinely opaque and says so with
    ``skip_matting=True``.
    """
    prompt = args.get("prompt")
    output = args.get("output")
    if not prompt or not output:
        raise ToolError("INVALID_CONFIG", "`prompt` and `output` are required.")

    asset_type = args.get("asset_type")
    at_norm = str(asset_type or "").strip().lower()
    if not at_norm:
        raise ToolError(
            "INVALID_CONFIG",
            "`asset_type` is required for static asset production.",
        )
    if at_norm in _UI_ALIAS_ASSET_TYPES:
        raise ToolError(
            "INVALID_CONFIG",
            f'`asset_type={at_norm}` is not canonical; use `asset_type="ui"` '
            "for UI widgets, lettering, logos, and wordmarks.",
        )
    if at_norm not in _CANONICAL_STATIC_ASSET_TYPES:
        raise ToolError(
            "INVALID_CONFIG",
            f"Unknown static `asset_type={at_norm}`; use one of: "
            + ", ".join(sorted(_CANONICAL_STATIC_ASSET_TYPES))
            + ".",
        )
    th, tw, max_box, base_size = _validate_static_target(args)
    target_count = sum(value is not None for value in (th, tw, max_box))
    workspace_root = _require_workspace_root(ctx)
    relative_output = final_path.relative_to(workspace_root)
    output_is_ui = (
        len(relative_output.parts) >= 2
        and relative_output.parts[0] == "assets"
        and relative_output.parts[1] == "ui"
    )
    if output_is_ui and at_norm not in _UI_STATIC_ASSET_TYPES:
        raise ToolError(
            "INVALID_CONFIG",
            "Static outputs under assets/ui/** require a canonical UI asset_type.",
        )
    is_ui = at_norm in _UI_STATIC_ASSET_TYPES or output_is_ui
    explicit_skip_matting = bool(args.get("skip_matting", False))
    if explicit_skip_matting and at_norm not in image_gen.FULL_SCENE_ASSET_TYPES:
        raise ToolError(
            "INVALID_CONFIG",
            "`skip_matting:true` is only valid for background/map/parallax assets.",
        )
    quality_profile = _require_workspace_quality_profile(ctx) if is_ui else None
    if base_size is not None and not is_ui:
        raise ToolError(
            "INVALID_CONFIG",
            "`base_size` is reserved for pixel_art UI assets.",
        )
    if quality_profile == "art_directed":
        if base_size is not None or max_box is not None or (th is None and tw is None):
            raise ToolError(
                "INVALID_CONFIG",
                "art_directed UI assets require exactly one authored §2E native "
                "dimension: target_height or target_width; max_box is reserved "
                "for responsive/minimal fit contracts.",
            )
    elif quality_profile == "pixel_art":
        if base_size is None or any(value is not None for value in (th, tw, max_box)):
            raise ToolError(
                "INVALID_CONFIG",
                "pixel_art UI assets require exactly one `base_size=[width, "
                "height]` and reject target_height, target_width, and max_box.",
            )
    elif quality_profile in {"responsive", "minimal"}:
        if max_box is None or any(value is not None for value in (th, tw, base_size)):
            raise ToolError(
                "INVALID_CONFIG",
                f"{quality_profile} UI assets require exactly one "
                "`max_box=[width, height]`.",
            )

    ref_image = args.get("ref_image")
    reference_path: Path | None = None
    reference_artifact: str | None = None
    reference_sha256: str | None = None
    if ref_image:
        raw_reference = str(ref_image)
        if re.match(r"^[a-z][a-z0-9+.-]*://", raw_reference, flags=re.IGNORECASE):
            raise ToolError(
                "INVALID_CONFIG",
                "`ref_image` must be a local workspace assets/** or design/** "
                "artifact; URLs are not accepted by produce_static_asset.",
            )
        reference_path = _resolve_asset_input(ctx, raw_reference)
        if not reference_path.is_file():
            raise ToolError(
                "INPUT_FILE_NOT_FOUND",
                f"Reference image not found: {reference_path}",
            )
        if reference_path in {
            final_path,
            final_path.with_suffix(".asset.json"),
            final_path.with_suffix(".bbox.json"),
        }:
            raise ToolError(
                "INVALID_CONFIG",
                "`ref_image` must remain distinct from the static asset outputs.",
            )
        reference_artifact = reference_path.relative_to(workspace_root).as_posix()
        reference_sha256 = hashlib.sha256(reference_path.read_bytes()).hexdigest()

    # Skip-matting: explicit flag OR opaque-scene asset_type (double safety so a
    # forgotten flag on a background can never trigger a meaningless matte).
    # The flag carries the parallax case, whose layers are overlays by default
    # and opaque only at the bottom of the stack.
    skip_matting = explicit_skip_matting or at_norm in image_gen.OPAQUE_SCENE_ASSET_TYPES

    img_defaults = ctx.config.assets.image
    quality = str(args.get("quality", img_defaults.default_quality))

    # ---- Stage 1: generate the raw image at `output` ----
    # crop-seeded (edit_image) when a ref crop is given, else text-only.
    safe_bg_color = args.get("safe_bg_color")
    if safe_bg_color is None and not skip_matting:
        # Matted asset with no explicit safe bg → fill with the configured
        # reserved chroma-key color so the selected backend can strip it. Opaque
        # scenes (skip_matting) keep None so the model paints the full canvas.
        safe_bg_color = image_gen.default_safe_bg_color(
            at_norm,
            img_defaults.safe_bg_color,
            # Any non-scene static asset that will be matted needs a removable
            # key even when an older caller omitted asset_type.
            default_when_unknown=True,
        )

    out_path = staged_path
    if reference_path is not None:
        gen = image_gen.edit_image(
            ctx.config,
            prompt=str(prompt),
            input_path=str(reference_path),
            output=out_path,
            size=str(args.get("size", img_defaults.edit_default_size)),
            quality=quality,
            safe_bg_color=safe_bg_color,
            enforce_safe_bg_avoidance=bool(
                args.get(
                    "enforce_safe_bg_avoidance",
                    img_defaults.enforce_safe_bg_avoidance,
                )
            ),
            framing_profile=args.get("framing_profile"),
            asset_type=at_norm,
        )
    else:
        gen = image_gen.generate_image(
            ctx.config,
            prompt=str(prompt),
            output=out_path,
            size=str(args.get("size", img_defaults.default_size)),
            quality=quality,
            max_retries=int(args.get("max_retries", img_defaults.max_retries)),
            retry_delay=float(
                args.get("retry_delay", img_defaults.retry_delay_seconds)
            ),
            safe_bg_color=safe_bg_color,
            enforce_safe_bg_avoidance=bool(
                args.get(
                    "enforce_safe_bg_avoidance", img_defaults.enforce_safe_bg_avoidance
                )
            ),
            framing_profile=args.get("framing_profile"),
            asset_type=at_norm,
            aspect=args.get("aspect"),
        )

    result: dict[str, Any] = {
        "output": str(final_path),
        "size": gen.get("size"),
        "asset_type": at_norm,
        "seeded_from_crop": reference_path is not None,
        "skipped_matting": skip_matting,
        "matted": False,
        "trimmed": False,
        "bbox": None,
        "bbox_sidecar": None,
    }
    provenance: dict[str, Any] = {
        "source": "generated",
        "producer": "gca-mcp.produce_static_asset",
        "generation_method": (
            "reference-edit" if reference_path is not None else "text-to-image"
        ),
    }
    if gen.get("request_id"):
        provenance["request_id"] = str(gen["request_id"])
    requested_matting_backend = str(ctx.config.assets.matting.backend)
    source_contract: dict[str, Any] = {
        "sourceStrategy": "generated-image",
        "assetType": at_norm,
        "generationMode": (
            "reference-edit" if reference_path is not None else "text-to-image"
        ),
        "matting": "skipped-opaque" if skip_matting else requested_matting_backend,
    }
    if reference_artifact is not None and reference_sha256 is not None:
        provenance["referenceArtifact"] = reference_artifact
        provenance["referenceArtifactSha256"] = reference_sha256
        source_contract["referenceArtifact"] = reference_artifact
        source_contract["referenceArtifactSha256"] = reference_sha256

    if not skip_matting:
        # ---- Stage 2: matting (in place on staging) ----
        matte_cfg = ctx.config.assets.matting

        def apply_color_key() -> None:
            """Apply the deterministic fallback without publishing partial output."""
            from ..runners.assets.color_key import remove_bg_color

            if not isinstance(safe_bg_color, str):
                raise ToolError(
                    "INVALID_CONFIG",
                    "color_key matting requires a configured safe background color.",
                )
            try:
                with Image.open(out_path) as staged:
                    matted = remove_bg_color(
                        staged,
                        key_color=safe_bg_color,
                        threshold=matte_cfg.color_key_threshold,
                        edge_threshold=matte_cfg.color_key_edge_threshold,
                    )
                matted.save(out_path, format="PNG")
            except (OSError, ValueError) as exc:
                raise ToolError(
                    "COLOR_KEY_MATTING_FAILED",
                    f"Color-key matting failed for {final_path}.",
                    hint=str(exc),
                ) from exc

        resolved_matting_backend: str
        fallback_error: ToolError | None = None
        rmbg_route = (
            matting_availability.acquire_rmbg_route(
                ctx.config,
                ctx.config.rmbg2.endpoint,
            )
            if requested_matting_backend == "auto"
            else None
        )
        if rmbg_route is not None and rmbg_route.action == "error":
            raise rmbg_route.as_error()
        if requested_matting_backend in {"auto", "rmbg2"}:
            rmbg_defaults = ctx.config.assets.rmbg
            if rmbg_route is not None and rmbg_route.action == "color_key":
                fallback_error = rmbg_route.as_error()
                apply_color_key()
                resolved_matting_backend = "color_key"
            else:
                try:
                    rmbg2.remove_background(
                        ctx.config,
                        input_path=out_path,
                        output=out_path,
                        bg_hex=args.get("bg_hex"),
                        aug_preset=str(
                            args.get("aug_preset", rmbg_defaults.aug_preset)
                        ),
                        aug_hard=args.get("aug_hard"),
                        aug_soft=args.get("aug_soft"),
                        rmbg_fg_cutoff=int(
                            args.get(
                                "rmbg_fg_cutoff",
                                rmbg_defaults.rmbg_fg_cutoff,
                            )
                        ),
                        max_size=args.get("max_size"),
                        max_workers=int(
                            args.get("max_workers", rmbg_defaults.workers)
                        ),
                        timeout=int(
                            args.get("timeout", rmbg_defaults.timeout_seconds)
                        ),
                    )
                    if rmbg_route is not None:
                        matting_availability.remember_rmbg_success(rmbg_route)
                    resolved_matting_backend = "rmbg2"
                except ToolError as exc:
                    if rmbg_route is not None:
                        if exc.code in {"INPUT_FILE_NOT_FOUND", "INVALID_CONFIG"}:
                            matting_availability.cancel_rmbg_probe(rmbg_route)
                        else:
                            matting_availability.remember_rmbg_error(
                                rmbg_route,
                                exc,
                            )
                    if (
                        requested_matting_backend != "auto"
                        or not is_transient_rmbg_error(exc)
                    ):
                        raise
                    fallback_error = exc
                    apply_color_key()
                    resolved_matting_backend = "color_key"
                except Exception:
                    if rmbg_route is not None:
                        matting_availability.cancel_rmbg_probe(rmbg_route)
                    raise
        elif requested_matting_backend == "color_key":
            apply_color_key()
            resolved_matting_backend = "color_key"
        else:
            raise ToolError(
                "INVALID_CONFIG",
                "Unsupported assets.matting.backend: "
                f"{requested_matting_backend!r}.",
            )

        source_contract["matting"] = resolved_matting_backend
        result["matting"] = resolved_matting_backend
        if requested_matting_backend == "auto":
            used_fallback = fallback_error is not None
            source_contract["mattingRequested"] = "auto"
            source_contract["mattingFallback"] = used_fallback
            provenance["mattingRequested"] = "auto"
            provenance["mattingFallback"] = used_fallback
            result["matting_requested"] = "auto"
            result["matting_fallback"] = used_fallback
            if rmbg_route is not None:
                source_contract["rmbgProbeCached"] = rmbg_route.cached
                provenance["rmbgProbeCached"] = rmbg_route.cached
                result["rmbg_probe_cached"] = rmbg_route.cached
            if fallback_error is not None:
                fallback_reason = (
                    f"{fallback_error.code}: {fallback_error.message}"
                )
                source_contract["mattingFallbackCode"] = fallback_error.code
                source_contract["mattingFallbackReason"] = fallback_reason
                provenance["mattingFallbackCode"] = fallback_error.code
                provenance["mattingFallbackReason"] = fallback_reason
                result["matting_fallback_code"] = fallback_error.code
                result["matting_fallback_reason"] = fallback_reason
        if (
            resolved_matting_backend == "color_key"
            and isinstance(safe_bg_color, str)
        ):
            provenance["mattingKeyColor"] = safe_bg_color
            source_contract["mattingKeyColor"] = safe_bg_color
            result["matting_key_color"] = safe_bg_color
        result["matted"] = True

        # ---- Stage 3: trim_transparent (drop the transparent margin) ----
        ct = args.get("clean_threshold", 24)
        trim = asset_bbox.trim_to_content_bbox(
            out_path,
            output=out_path,
            padding=int(args.get("padding", 2)),
            clean_threshold=int(ct) if ct is not None else None,
        )
        result["trimmed"] = True
        result["trim"] = {
            "before": trim.get("before"),
            "after": trim.get("after"),
            "trimmed_px": trim.get("trimmed_px"),
        }

        # Re-project binding anchors/slots from the pre-trim box onto the
        # post-trim box. The later uniform scale preserves these fractions.
        anchors_in = args.get("anchors")
        slots_in = args.get("slots")
        if (anchors_in or slots_in) and trim.get("before") and trim.get("after"):
            reproj = asset_bbox.reproject_anchors(
                anchors_in,
                slots_in,
                before=trim["before"],
                after=trim["after"],
                trimmed_px=trim.get("trimmed_px") or {},
            )
            result["anchors"] = reproj["anchors"]
            result["slots"] = reproj["slots"]
            if reproj["out_of_bounds"]:
                result["anchor_warnings"] = reproj["out_of_bounds"]

    # ---- Stage 3.5: scale_uniform (bring the PNG to its IN-GAME display size) ----
    # The "proportion-control" step on the asset side: scale the generated source
    # UNIFORMLY (aspect preserved — never squashed) to its authored display size.
    # The on-disk pixel size IS the display size.
    # Driven by EXACTLY ONE of target_height / target_width / max_box; omit all
    # three to skip (e.g. an asset whose generated size is already correct, or a
    # spritesheet/strip that must keep equal-sized cells).
    if base_size is not None:
        sized = asset_bbox.contain_pad_to_size(
            out_path,
            base_size=base_size,
            output=out_path,
        )
        result["scaled"] = sized["before"] != sized["content"]
        result["sizing"] = sized
        if result.get("anchors") is not None or result.get("slots") is not None:
            projected = asset_bbox.reproject_bindings_to_canvas(
                result.get("anchors"),
                result.get("slots"),
                content=sized["content"],
                canvas=sized["after"],
                padding=sized["padding"],
            )
            result["anchors"] = projected["anchors"]
            result["slots"] = projected["slots"]
        after = sized.get("after") or {}
        if (
            int(after.get("w", 0)) != base_size[0]
            or int(after.get("h", 0)) != base_size[1]
        ):
            raise ToolError(
                "INVALID_IMAGE",
                "Pixel-art contain/pad did not produce the exact base_size.",
            )
    elif target_count == 1:
        scaled = asset_bbox.scale_to_target(
            out_path,
            target_height=th,
            target_width=tw,
            max_box=max_box,
            allow_upscale=bool(args.get("allow_upscale", True)),
            output=out_path,
        )
        result["scaled"] = scaled.get("scaled", False)
        result["scale"] = {
            "before": scaled.get("before"),
            "after": scaled.get("after"),
            "factor": scaled.get("scale"),
        }
        after = scaled.get("after") or {}
        after_w = int(after.get("w", 0))
        after_h = int(after.get("h", 0))
        if (
            (th is not None and after_h != th)
            or (tw is not None and after_w != tw)
            or (max_box is not None and (after_w > max_box[0] or after_h > max_box[1]))
        ):
            raise ToolError(
                "INVALID_IMAGE",
                "Uniform scaling did not satisfy the authored §2E target: "
                f"produced {after_w}x{after_h}.",
            )

    if is_ui:
        final_width, final_height = _read_static_image_dimensions(out_path)
        assert quality_profile is not None
        source_contract["qualityProfile"] = quality_profile
        source_contract["sizingContract"] = _canonical_ui_sizing_contract(
            quality_profile=quality_profile,
            target_height=th,
            target_width=tw,
            max_box=max_box,
            base_size=base_size,
            final_width=final_width,
            final_height=final_height,
        )

    # ---- Stage 4 (optional): content_bbox for collidable/standable assets ----
    staged_bbox: Path | None = None
    if bool(args.get("collidable", False)):
        staged_bbox = out_path.with_suffix(".bbox.json")
        ct = args.get("clean_threshold", 24)
        bbox = asset_bbox.compute_content_bbox(
            out_path,
            cols=int(args.get("cols", 1)),
            rows=int(args.get("rows", 1)),
            clean_threshold=int(ct) if ct is not None else None,
            output=staged_bbox,
            image_reference=final_path,
        )
        result["bbox"] = bbox.get("contentBbox") or bbox.get("cells") or bbox
    return _publish_staged_static_asset(
        ctx,
        staged_artifact=out_path,
        final_artifact=final_path,
        staged_bbox=staged_bbox,
        producer="produce_static_asset",
        provenance=provenance,
        source_contract=source_contract,
        result=result,
    )


# -------------------- registry --------------------


_IMG_OUTPUT_DESC = (
    "Output PNG path under active-workspace assets/** or design/**. "
    "Resolved absolute paths and symlinks may not escape those subtrees."
)

_IMG_QUALITY_DESC = (
    "Provider render effort, and the dominant cost of an asset run. Measured on "
    "one 1024x1024 image: low 25s, medium 41s, high 115s. A batch generates "
    "dozens of assets and retries some, so `high` turns minutes of waiting into "
    "an hour. Leave it at the default unless the GDD asks for a hero asset whose "
    "detail a reviewer would actually notice; flat vector shapes, silhouettes, "
    "lettering, and chroma-plate art gain nothing from it."
)

_SLOT_RUBRIC_SCHEMA = {
    "type": "object",
    "properties": {
        "id": {"type": "string"},
        "name": {"type": "string"},
        "description": {"type": "string"},
    },
    "required": ["id", "name", "description"],
    "additionalProperties": False,
}
_SLOT_VALIDATION_ITEM_PROPERTIES = {
    "image": {
        "type": "string",
        "description": (
            "Exact final runtime artifact path from the manifest slot/action "
            "(packed spritesheet or final static PNG)."
        ),
    },
    "slot_key": {
        "type": "string",
        "description": "Required manifest runtime key.",
    },
    "action": {
        "type": "string",
        "description": (
            "Optional sprite action. Defaults to the manifest defaultAction."
        ),
    },
    "extra_rubrics": {
        "type": "array",
        "description": (
            "Optional checks appended to the immutable canonical S1-S5 rubric."
        ),
        "items": _SLOT_RUBRIC_SCHEMA,
    },
    "vlm_model": _PINNED_VLM_MODEL_SCHEMA,
    "vlm_max_tokens": {"type": "integer", "minimum": 1, "default": 1200},
    "vlm_temperature": {"type": "number", "default": 0.0},
    "vlm_timeout": {"type": "number", "minimum": 1, "default": 60},
    "attempts_per_model": {
        "type": "integer",
        "enum": [1],
        "default": 1,
        "description": "Pinned single-attempt VLM policy.",
    },
}
_SLOT_VALIDATION_ITEM_SCHEMA = {
    "type": "object",
    "properties": _SLOT_VALIDATION_ITEM_PROPERTIES,
    "required": ["image", "slot_key"],
    "additionalProperties": False,
}


TOOLS = [
    ToolDef(
        name="generate_image",
        description=(
            "Text-to-image via gpt-image-2. "
            "Returns {output, size, quality, request_id}. "
            "By default the prompt is wrapped with a hard color "
            "constraint forbidding pure magenta (#FF00FF) and pure "
            "chroma green (#00F000) anywhere in the asset — these "
            "are reserved for downstream chroma-key matting. Pass "
            "`safe_bg_color=#FF00FF`/`#00F000` to also request a "
            "solid bg of that color (ready for Seedance i2v + matting)."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "prompt": {"type": "string"},
                "output": {"type": "string", "description": _IMG_OUTPUT_DESC},
                "size": {
                    "type": "string",
                    "default": "auto",
                    "description": (
                        "Output size. Default 'auto' passes through to "
                        "gpt-image-2 so the MODEL picks the resolution from "
                        "the prompt (true adaptive sizing). To CAP resolution "
                        "(auto isn't minimal — it optimizes for content), "
                        "pass `asset_type`/`aspect` for a tuned size, or an "
                        "explicit 'WxH' (always wins)."
                    ),
                },
                "asset_type": {
                    "type": "string",
                    "description": (
                        "Asset category used to resolve the request canvas from "
                        "`assets.image.size_by_asset_type` in gca.config.yaml. "
                        "Unknown values do not invent a local fallback."
                    ),
                },
                "aspect": {
                    "type": "string",
                    "description": (
                        "Explicit aspect when size='auto' (overrides "
                        "`asset_type`), resolved from "
                        "`assets.image.size_by_aspect` in gca.config.yaml."
                    ),
                },
                "quality": {
                    "type": "string",
                    "enum": ["low", "medium", "high"],
                    "default": "low",
                    "description": _IMG_QUALITY_DESC,
                },
                "max_retries": {"type": "integer", "default": 3},
                "retry_delay": {"type": "number", "default": 10},
                "safe_bg_color": {
                    "type": "string",
                    "enum": ["#FF00FF", "#00F000"],
                    "description": (
                        "When set, asks the model to fill the canvas "
                        "with this solid bg color (matting-ready). "
                        "Must be #FF00FF (magenta) or #00F000 (green)."
                    ),
                },
                "enforce_safe_bg_avoidance": {
                    "type": "boolean",
                    "default": True,
                    "description": (
                        "When True (default), prepends a hard constraint "
                        "block forbidding only the selected safe-bg key in "
                        "the foreground; the alternate color remains allowed."
                    ),
                },
                "flat_2d": {
                    "type": "boolean",
                    "description": (
                        "When True, prepends a HARD flat-2D constraint "
                        "block (no 3D/isometric/perspective; straight "
                        "top-down or side-on; flat shapes, no thickness/"
                        "shadows). Use it when gpt-image-2 would otherwise "
                        "render inherently-3D subjects (dominoes, dice, blocks, "
                        "ramps) in 3D. Default: AUTO — True for concept mockups "
                        "under design/, else False. Pass explicitly to override."
                    ),
                },
                "subject_margin": {
                    "type": "boolean",
                    "description": (
                        "Injects the 'single centered subject with ≥8% "
                        "edge margin, no scene' block. Default: AUTO — "
                        "False for full-scene mockups under design/, True "
                        "otherwise. Set False for any full-canvas/scene "
                        "image; pass explicitly to override."
                    ),
                },
                "framing_profile": {
                    "type": "string",
                    "enum": ["grounded_sprite", "centered_asset", "full_scene"],
                    "description": (
                        "Explicit framing block (overrides the asset_type-"
                        "derived default): `grounded_sprite` = grounded "
                        "character first frame (feet on the bottom edge, i2v "
                        "swing margins) — ONLY for character/sprite types; "
                        "`centered_asset` = a single prop/UI/icon centered with "
                        "≥8% margin on every edge; `full_scene` = background/"
                        "map/parallax/mockup (no single-subject block). When "
                        "omitted the profile is derived from `asset_type` "
                        "(NOT from safe_bg_color) — grounded types → "
                        "grounded_sprite, opaque scene types → full_scene, "
                        "everything else → centered_asset."
                    ),
                },
            },
            "required": ["prompt", "output"],
            "additionalProperties": False,
        },
        handler=_generate_image,
        requires_workspace=True,
    ),
    ToolDef(
        name="edit_image",
        description=(
            "Reference-based image edit (gpt-image-2 edit endpoint). Use to "
            "derive a clean game-ready front reference from a user image, "
            "to extract stylized lettering/elements from a design crop, or to "
            "generate per-action first frames from a locked reference. "
            "Same selected-key safe-color constraint as generate_image. By "
            "DEFAULT (no asset_type) the edit lands on a removable #FF00FF "
            "background so it can be matted — pass asset_type=background/map/"
            "parallax to keep an OPAQUE full scene (e.g. map-base seeding)."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "prompt": {"type": "string"},
                "input": {
                    "type": "string",
                    "description": "Reference image — local path or HTTP(S) URL.",
                },
                "output": {"type": "string", "description": _IMG_OUTPUT_DESC},
                "size": {"type": "string", "default": "1024x1024"},
                "quality": {
                    "type": "string",
                    "enum": ["low", "medium", "high"],
                    "default": "low",
                    "description": _IMG_QUALITY_DESC,
                },
                "safe_bg_color": {
                    "type": "string",
                    "enum": ["#FF00FF", "#00F000"],
                },
                "enforce_safe_bg_avoidance": {
                    "type": "boolean",
                    "default": True,
                },
                "asset_type": {
                    "type": "string",
                    "description": (
                        "Set to an OPAQUE scene type (background/map/parallax) "
                        "to KEEP the full background with NO safe-color fill — "
                        "required for map-base seeding so the terrain isn't "
                        "turned into magenta. Omit for lettering / element "
                        "extraction / sprites: the output then defaults to a "
                        "removable #FF00FF background so matting works. Also "
                        "drives the framing default (opaque scene → full_scene, "
                        "grounded types → grounded_sprite, else centered_asset)."
                    ),
                },
                "framing_profile": {
                    "type": "string",
                    "enum": ["grounded_sprite", "centered_asset", "full_scene"],
                    "description": (
                        "Explicit framing block override (see generate_image). "
                        "Defaults from `asset_type` when omitted."
                    ),
                },
            },
            "required": ["prompt", "input", "output"],
            "additionalProperties": False,
        },
        handler=_edit_image,
        requires_workspace=True,
    ),
    ToolDef(
        name="generate_seedance_video",
        description=(
            "Seedance 2.0 i2v / t2v through ARK's native submit/poll API. "
            "Local frames are inlined as data URIs, while existing HTTP(S) "
            "inputs are passed through unchanged. Produces exactly one video; "
            "the global transient policy retries only when no usable video is "
            "returned, while download failures retry the same returned URL."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "prompt": {"type": "string"},
                "output": {"type": "string", "description": "MP4 output path."},
                "model": {
                    "type": "string",
                    "enum": ["fast", "standard"],
                    "default": "fast",
                },
                "model_id": {"type": "string"},
                "duration": {"type": ["integer", "string"], "default": 5},
                "ratio": {"type": "string", "default": "16:9"},
                "resolution": {"type": "string", "default": "720p"},
                "seed": {"type": ["integer", "string"]},
                "camerafixed": {"type": "boolean"},
                "watermark": {"type": "boolean"},
                "image": {
                    "type": ["array", "string"],
                    "items": {"type": "string"},
                    "description": "Reference image URL(s) or local path(s).",
                },
                "first_frame": {"type": "string"},
                "last_frame": {"type": "string"},
                "extra_params": {"type": "object"},
                "save_response": {"type": "string"},
                "server_timeout": {"type": "integer", "default": 600},
                "download_timeout": {"type": "integer", "default": 300},
                "print_url_only": {"type": "boolean", "default": False},
            },
            "required": ["prompt", "output"],
            "additionalProperties": False,
        },
        handler=_generate_seedance_video,
        requires_workspace=True,
    ),
    ToolDef(
        name="extend_background",
        description=(
            "Extend a background image to the right with seamless gpt-image-2 "
            "editing. Builds a provider-safe canvas from the source's right-side "
            "overlap plus one full source-width blank region, cover/crops any "
            "provider size drift without distortion, restores the overlap "
            "pixel-for-pixel, and emits an exact source-W×H runtime segment. "
            "Always keeps same-basename `<stem>-composite.png` and "
            "`<stem>-outpaint-full.png` review evidence in the same rollback-safe "
            "pinned publication as the runtime PNG. Output is a strict `.png` "
            "file below assets/** or design/**. The generated-image background "
            "`.asset.json` seals source, lineage, and review hashes for "
            "`finalize_asset_manifest`."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "input": {
                    "type": "string",
                    "description": (
                        "Source segment under the active workspace assets/** "
                        "or design/**; its path and SHA-256 are sealed into lineage."
                    ),
                },
                "output": {"type": "string"},
                "prompt": {"type": "string"},
                "size": {"type": "string"},
                "quality": {
                    "type": "string",
                    "enum": ["low", "medium", "high"],
                    "default": "low",
                    "description": _IMG_QUALITY_DESC,
                },
            },
            "required": ["input", "output"],
            "additionalProperties": False,
        },
        handler=_extend_background,
        requires_workspace=True,
    ),
    ToolDef(
        name="build_i2v_prompt",
        description=(
            "Wrap user content with the iter_05-validated i2v constraint "
            "block (background lock, hard-edged solid effects, no glow / "
            "fade / haze, no semi-transparent particles). The FRAMING half "
            "of the block is chosen by `animation_profile`: `character` "
            "(default) anchors the figure's **feet to the bottom edge, "
            "character never off-screen, no zoom or recompose**; "
            "`projectile` instead centers a free-flying bullet/arrow/"
            "fireball with margin on all four edges and NO ground contact. "
            "Returns a prompt ready to pass to `generate_seedance_video`. "
            "Use this before any character/projectile-body i2v call so the resulting "
            "frames matte cleanly via `rmbg2_matting` or "
            "`rmbg2_video_matting`. **Background MUST be #FF00FF (magenta) "
            "or #00F000 (chroma green)** — these are the only matting-safe "
            "colors. `reference_image` must already have one of those border "
            "colors; transparent/non-chroma frames must be staged first."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "user_content": {
                    "type": "string",
                    "description": (
                        "Plain-language description of what should happen "
                        "in the video, e.g. 'A frost druid casting an ice "
                        "spell, robes flowing, side view'."
                    ),
                },
                "preset": {
                    "type": "string",
                    "enum": ["magenta_bg", "green_bg"],
                    "description": (
                        "Tested bg + effect_palette combos. "
                        "`magenta_bg` (#FF00FF) — for foregrounds with no "
                        "magenta/pink (including green subjects). `green_bg` "
                        "(#00F000) — for foregrounds containing magenta/pink "
                        "that would conflict with magenta keying. Overrides `bg_hex` and "
                        "`effect_palette` when set."
                    ),
                },
                "bg_hex": {
                    "type": "string",
                    "enum": ["#FF00FF", "#00F000"],
                    "default": "#FF00FF",
                    "description": (
                        "Safe background color. MUST be #FF00FF (magenta) "
                        "or #00F000 (chroma green). Other values raise "
                        "INVALID_CONFIG."
                    ),
                },
                "effect_palette": {
                    "type": ["array", "string"],
                    "items": {"type": "string"},
                    "description": (
                        "List of allowed effect colors (hex strings). "
                        "Comma-separated string also accepted. Must all "
                        "be ≥100 RGB-distance from `bg_hex`. Defaults to "
                        "the preset palette matching the chosen bg."
                    ),
                },
                "foreground_recolor_instructions": {
                    "type": "string",
                    "description": (
                        "Only set when foreground has pixels closer than "
                        "100 RGB-distance to `bg_hex`. Example: 'white "
                        "robes → ice blue (#1E3A5F); silver beard → "
                        "blue-gray (#A8B5C0)'."
                    ),
                },
                "reference_image": {
                    "type": "string",
                    "description": (
                        "Optional path to the first-frame PNG. When "
                        "provided (and `preset` is not), detects the actual "
                        "#FF00FF / #00F000 border color. Transparent or "
                        "non-chroma images are rejected."
                    ),
                },
                "effects": {
                    "type": "boolean",
                    "default": False,
                    "description": (
                        "Default false because game FX are runtime particle "
                        "recipes. True is reserved for a non-runtime/custom video."
                    ),
                },
                "animation_profile": {
                    "type": "string",
                    "enum": ["character", "projectile"],
                    "default": "character",
                    "description": (
                        "Framing contract of the generated prompt. "
                        "`character` (default) = grounded figure that "
                        "stands on the bottom edge with anchored feet. "
                        "`projectile` = free-flying bullet/arrow/fireball/"
                        "bolt CENTERED with margin on all four edges, NOT "
                        "grounded and NOT touching the bottom edge, with no "
                        "character/feet language. MUST match the validator "
                        "rubric group used downstream — a projectile scored "
                        "with the character template gets contradictory "
                        "generation vs. validation instructions."
                    ),
                },
            },
            "required": ["user_content"],
            "additionalProperties": False,
        },
        handler=_build_i2v_prompt,
    ),
    ToolDef(
        name="rmbg2_video_matting",
        description=(
            "Per-frame video matting → RGBA PNG sequence + encoded transparent "
            "outputs (gif / webm / mov / mp4_preview). Honors "
            "`assets.matting.backend`: `auto` prefers RMBG-2.0 and remattes the "
            "entire clip with deterministic color-key on network, missing-route "
            "HTTP 404, or HTTP 5xx outages; "
            "explicit `rmbg2` and `color_key` are also supported. Pass `bg_hex` "
            "for color-key fallback and `aug_preset` for "
            "color-aug rescue (recover external particle effects RMBG "
            "would otherwise clip). Requires ffmpeg + ffprobe on PATH."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "input": {
                    "type": "string",
                    "description": "Source video (mp4 / mov / webm).",
                },
                "output_dir": {
                    "type": "string",
                    "description": (
                        "Where to write RGBA PNG frames + encoded outputs."
                    ),
                },
                "formats": {
                    "type": ["array", "string"],
                    "items": {
                        "type": "string",
                        "enum": ["gif", "webm", "mov", "mp4_preview"],
                    },
                    "default": ["gif"],
                    "description": (
                        "Output formats to encode. Comma-separated string "
                        "also accepted. `gif` = global-palette transparent "
                        "GIF; `webm` = VP9 with alpha; `mov` = QTRLE; "
                        "`mp4_preview` = alpha-on-black MPEG-4 for review."
                    ),
                },
                "endpoint": {
                    "type": "string",
                    "description": "Override RMBG2_ENDPOINT for this call.",
                },
                "bg_hex": {
                    "type": "string",
                    "description": (
                        "Safe background color (e.g. '#FF00FF'). When set, "
                        "color-aug rescue is enabled via `aug_preset` or "
                        "the explicit `aug_hard`/`aug_soft` overrides."
                    ),
                },
                "aug_preset": {
                    "type": "string",
                    "enum": [
                        "off",
                        "aggressive",
                        "medium",
                        "balanced",
                        "conservative",
                        "more-conservative",
                        "very-conservative",
                        "nearly-off",
                    ],
                    "default": "off",
                    "description": (
                        "Color-aug intensity preset (lower numbers = "
                        "more aggressive rescue). `off` = no rescue at "
                        "all → pure RMBG decision, zero risk of bg "
                        "residue from false-positive rescue. Safest "
                        "default. Bump up to `nearly-off` (130/170) or "
                        "lower presets only if external particle "
                        "effects are getting clipped."
                    ),
                },
                "aug_hard": {"type": "integer"},
                "aug_soft": {"type": "integer"},
                "rmbg_fg_cutoff": {
                    "type": "integer",
                    "default": 230,
                    "description": (
                        "RMBG soft→hard alpha cutoff (0-255). Higher = "
                        "stricter foreground = less bg-color residue at "
                        "the silhouette edge. 230 ≈ require ~90% model "
                        "confidence."
                    ),
                },
                "max_size": {
                    "type": "integer",
                    "default": 960,
                    "description": "Server-side longest-edge cap per frame.",
                },
                "workers": {
                    "type": "integer",
                    "default": 8,
                    "description": (
                        "Parallel HTTP workers (server uses SO_REUSEPORT "
                        "+ dynamic batching across multiple GPUs)."
                    ),
                },
                "gif_max_size": {"type": "integer", "default": 960},
                "gif_alpha_threshold": {
                    "type": "integer",
                    "default": 200,
                    "description": (
                        "Final 1-bit transparency cutoff applied during "
                        "GIF encoding (0-255). Higher = more borderline "
                        "pixels become fully transparent → cleaner edges, "
                        "no half-transparent magenta/green halo around "
                        "the silhouette."
                    ),
                },
                "keep_frames": {
                    "type": "boolean",
                    "default": True,
                    "description": (
                        "Keep the RGBA PNG sequence next to the encoded "
                        "outputs. Useful for downstream sprite-sheet builds."
                    ),
                },
                "timeout": {
                    "type": "integer",
                    "default": 60,
                    "description": "Per-frame HTTP timeout in seconds.",
                },
                "align_feet_to_bottom": {
                    "type": "boolean",
                    "default": False,
                    "description": (
                        "Safety net for seedance vertical drift: after "
                        "matting, translate every RGBA frame so its "
                        "alpha-bbox bottom edge sits on the canvas "
                        "bottom (or `foot_margin_px` above it). Only "
                        "vertical translation; horizontal composition "
                        "is preserved. Recommended for character "
                        "sprites where the prompt-level 'feet at "
                        "bottom' constraint isn't reliable enough."
                    ),
                },
                "foot_margin_px": {
                    "type": "integer",
                    "default": 0,
                    "description": (
                        "Pixels of empty space to leave below the "
                        "lowest opaque pixel when "
                        "`align_feet_to_bottom=True`. 0 = lowest "
                        "pixel touches the absolute bottom edge."
                    ),
                },
                "drop_bg_leak": {
                    "type": "boolean",
                    "default": True,
                    "description": (
                        "Drop matted frames where the safe background "
                        "color (`bg_hex`) leaked through as opaque "
                        "pixels — i.e. RMBG mis-classified a bright "
                        "effect as foreground and left the magenta/green "
                        "block intact. Only active when `bg_hex` is set. "
                        "Survivors are renumbered so downstream encoding "
                        "stays gap-free."
                    ),
                },
                "bg_leak_area_threshold": {
                    "type": "number",
                    "default": 0.005,
                    "description": (
                        "Drop a frame whose alpha-weighted leak score "
                        "(or, with alpha_weighted=False, fraction of "
                        "opaque-and-near-bg pixels) exceeds this value. "
                        "0.005 = strict default that catches "
                        "semi-transparent magenta floods (the common "
                        "'whole-canvas tinted magenta' failure mode) "
                        "and even mild whole-canvas magenta haze."
                    ),
                },
                "bg_leak_color_dist": {
                    "type": "integer",
                    "default": 120,
                    "description": (
                        "RGB euclidean distance threshold for 'near "
                        "`bg_hex`'. 120 ≈ catches both pure magenta "
                        "leaks AND near-magenta 'fading into bg' frames "
                        "(common for fx tails). Lower → only near-pure "
                        "leak counts; higher risks killing legitimate "
                        "purple smoke."
                    ),
                },
                "bg_leak_alpha_threshold": {
                    "type": "integer",
                    "default": 128,
                    "description": (
                        "Alpha cutoff (0-255) for counting a pixel as "
                        "opaque during leak detection. Pixels at or "
                        "below this alpha are treated as transparent."
                    ),
                },
                "bg_leak_keep_min_frames": {
                    "type": "integer",
                    "default": 2,
                    "description": (
                        "Safety floor: if dropping leak frames would "
                        "leave fewer than this many survivors (i.e. "
                        "matting failed across the whole clip), the "
                        "purge is skipped so the validator can rank "
                        "this candidate low instead of getting an "
                        "empty sequence."
                    ),
                },
                "bg_leak_alpha_weighted": {
                    "type": "boolean",
                    "default": True,
                    "description": (
                        "Use the alpha-weighted leak metric: each "
                        "near-bg pixel contributes alpha/255 to the "
                        "score instead of 0 or 1. This catches "
                        "semi-transparent magenta floods (alpha 30-127) "
                        "that the hard-cutoff metric misses. Set to "
                        "False to fall back to the legacy "
                        "(alpha > alpha_threshold) cutoff."
                    ),
                },
            },
            "required": ["input", "output_dir"],
            "additionalProperties": False,
        },
        handler=_rmbg2_video_matting,
        requires_workspace=True,
    ),
    ToolDef(
        name="validate_asset_video",
        description=(
            "Strict VLM check for a generated game-asset animation "
            "video (typically a Seedance i2v MP4 — pre- or post-matting). "
            "Verifies TWO hard constraints, both judged against the first "
            "frame: R1 in-frame (no body part / weapon / effect ever "
            "clipped by a canvas edge) and R2 scale-locked (the character "
            "does not balloon or shrink from a silent camera zoom — its "
            "on-screen size stays the same as the first frame). Returns "
            "per-rubric PASS/FAIL with concrete evidence on failures. "
            "Single VLM round-trip on doubao-seed-2-0-pro-260215; "
            "intended to run after `rmbg2_video_matting` so a failing "
            "take can be regenerated before it ships into the game. Pass "
            "`extra_rubrics` to append action-specific checks, or "
            "`rubric_override` to replace the default 2-rubric set entirely."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "video": {
                    "type": "string",
                    "description": (
                        "Path to the asset video. MP4 is preferred for VLM "
                        "fidelity; transparent GIF / WebM also accepted. "
                        "Relative paths resolve under the open workspace."
                    ),
                },
                "action_description": {
                    "type": "string",
                    "description": (
                        "Free-text description of what the video is "
                        "supposed to depict, e.g. 'The knight performs an "
                        "overhead sword attack — feet planted, shield "
                        "braced forward in the off-hand.' Context for the "
                        "validator when judging the two hard rubrics "
                        "(in-frame, scale-locked)."
                    ),
                },
                "extra_rubrics": {
                    "type": "array",
                    "description": (
                        "Optional list of action-specific rubrics to "
                        "append to the default 2. Each item: "
                        "{id: 'R3+', name, description}."
                    ),
                    "items": {
                        "type": "object",
                        "properties": {
                            "id": {"type": "string"},
                            "name": {"type": "string"},
                            "description": {"type": "string"},
                        },
                        "required": ["id", "name", "description"],
                        "additionalProperties": False,
                    },
                },
                "rubric_override": {
                    "type": "array",
                    "description": (
                        "Optional full replacement for the default rubric "
                        "set. When provided, the default 2 are dropped "
                        "and only these rubrics are checked."
                    ),
                    "items": {
                        "type": "object",
                        "properties": {
                            "id": {"type": "string"},
                            "name": {"type": "string"},
                            "description": {"type": "string"},
                        },
                        "required": ["id", "name", "description"],
                        "additionalProperties": False,
                    },
                },
                "vlm_model": _PINNED_VLM_MODEL_SCHEMA,
                "vlm_max_tokens": {"type": "integer", "default": 1500},
                "vlm_temperature": {"type": "number", "default": 0.0},
                "vlm_timeout": {"type": "number", "default": 180},
            },
            "required": ["video", "action_description"],
            "additionalProperties": False,
        },
        handler=_validate_asset_video,
        requires_workspace=True,
    ),
    ToolDef(
        name="produce_validated_video",
        description=(
            "Single-output-by-default sprite-action production: runs one chain of "
            "`generate_seedance_video` → `rmbg2_video_matting` → "
            "two-pass `validate_asset_video` at a time (fresh seed per "
            "attempt), and STOPS at the first take that clears BOTH "
            "validation passes — a FRAMING pass on the raw mp4 (R1 "
            "in-frame: nothing clipped off the canvas; R2 scale-locked: "
            "no silent zoom) and a CONTENT pass on the matted preview "
            "(R3 identity, R4 hand-prop attachment, R5 limb integrity, "
            "R6 loop coherence, R7 action fidelity). The global quality-candidate "
            "default is 1, so validation failure does NOT regenerate by default. "
            "Transient Seedance failures that return no usable video are retried "
            "independently. If `n_candidates` is explicitly raised, failed takes "
            "regenerate and re-check up to that cap. If "
            "none passes, the least-bad take (fewest rubric failures) "
            "ships as a best-effort winner rather than blocking. Each "
            "attempt's MP4 / matted GIF / RGBA frames / verdict.json "
            "land under `output_dir/candidate_NN/`; the winner is also "
            "mirrored to `output_dir/final/` so downstream callers don't "
            "have to guess. Use this as the default sprite-action "
            "production tool — `generate_seedance_video` + "
            "`rmbg2_video_matting` + `validate_asset_video` should only "
            "be called individually for debugging or non-standard "
            "pipelines."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "base_prompt": {
                    "type": "string",
                    "description": (
                        "The i2v prompt — typically the output of "
                        "`build_i2v_prompt(user_content=..., preset='magenta_bg')`. "
                        "All N candidates use this exact prompt; "
                        "variance comes from different seeds."
                    ),
                },
                "first_frame": {
                    "type": "string",
                    "description": (
                        "First-frame PNG (path or URL). Same first_frame is reused "
                        "across attempts — only the seed varies. It also defines the "
                        "reference framing the validator checks against (R2 scale-"
                        "locked). If R1/R2 (in-frame / scale) keep failing across all "
                        "attempts, regenerate the first_frame with `generate_image` "
                        "using `framing_profile='grounded_sprite'` and more empty "
                        "margin around the character, "
                        "and call this tool again. (The matted preview of each take is "
                        "additionally checked for identity / hand-prop / limb / loop.)"
                    ),
                },
                "output_dir": {
                    "type": "string",
                    "description": (
                        "Where to write attempt_01/ ... attempt_NN/ + final/ + "
                        "_orchestrator_summary.json."
                    ),
                },
                "action_description": {
                    "type": "string",
                    "description": (
                        "One-sentence description of the action, e.g. 'The knight "
                        "performs an overhead sword attack — feet planted, shield "
                        "braced forward.' Context for the validator's two hard "
                        "rubrics (in-frame, scale-locked)."
                    ),
                },
                "duration": {"type": "integer", "default": 5},
                "ratio": {"type": "string", "default": "1:1"},
                "resolution": {"type": "string", "default": "720p"},
                "model": {
                    "type": "string",
                    "enum": ["fast", "standard"],
                    "default": "fast",
                },
                "seed_base": {
                    "type": "integer",
                    "description": (
                        "Optional base seed; attempts use seed_base + n. Defaults to "
                        "`int(time.time()*1000)` so consecutive runs differ."
                    ),
                },
                "bg_hex": {
                    "type": "string",
                    "enum": ["#FF00FF", "#00F000"],
                    "default": "#FF00FF",
                },
                "aug_preset": {
                    "type": "string",
                    "enum": [
                        "off",
                        "aggressive",
                        "medium",
                        "balanced",
                        "conservative",
                        "more-conservative",
                        "very-conservative",
                        "nearly-off",
                    ],
                    "default": "off",
                },
                "rmbg_fg_cutoff": {"type": "integer", "default": 230},
                "gif_alpha_threshold": {"type": "integer", "default": 200},
                "align_feet_to_bottom": {"type": "boolean", "default": True},
                "foot_margin_px": {"type": "integer", "default": 2},
                "drop_bg_leak": {
                    "type": "boolean",
                    "default": True,
                    "description": (
                        "Drop matted frames where the safe bg color "
                        "(`bg_hex`) leaked through as opaque pixels "
                        "(matte failure on bright effects)."
                    ),
                },
                "bg_leak_area_threshold": {
                    "type": "number",
                    "default": 0.005,
                    "description": (
                        "Drop a frame when ≥ this fraction (0-1) of "
                        "the canvas is opaque AND near `bg_hex`."
                    ),
                },
                "bg_leak_color_dist": {"type": "integer", "default": 120},
                "bg_leak_alpha_threshold": {"type": "integer", "default": 128},
                "bg_leak_keep_min_frames": {"type": "integer", "default": 2},
                "bg_leak_alpha_weighted": {"type": "boolean", "default": True},
                "matte_workers": {"type": "integer", "default": 8},
                "vlm_model": _PINNED_VLM_MODEL_SCHEMA,
                "extra_rubrics": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "id": {"type": "string"},
                            "name": {"type": "string"},
                            "description": {"type": "string"},
                        },
                        "required": ["id", "name", "description"],
                        "additionalProperties": False,
                    },
                },
                "rubric_override": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "id": {"type": "string"},
                            "name": {"type": "string"},
                            "description": {"type": "string"},
                        },
                        "required": ["id", "name", "description"],
                        "additionalProperties": False,
                    },
                },
                "n_candidates": {
                    "type": "integer",
                    "default": 1,
                    "minimum": 1,
                    "description": (
                        "MAX number of serial attempts. Attempts run one "
                        "at a time (generate → matte → two-pass validate); "
                        "the loop STOPS at the first take that clears both "
                        "passes (framing: in-frame + scale-locked; content: "
                        "identity + hand-prop + limb + loop), so a clean "
                        "first take costs a single attempt (~5 min). Only "
                        "failing takes trigger a regeneration only when this "
                        "cap is explicitly above 1. Defaults to the global "
                        "`assets.seedance.max_quality_candidates` setting (1 in "
                        "the checked-in config), so validation does not regenerate "
                        "by default. No-output/network retries use the separate "
                        "global transient policy. If none passes within the cap, "
                        "the least-bad take ships as best-effort."
                    ),
                },
                "allow_unvalidated": {
                    "type": "boolean",
                    "default": False,
                    "description": (
                        "Escape hatch for when EVERY attempt failed to even "
                        "reach validation (VLM service / parse error — no "
                        "verdict at all). Default False ships NOTHING in that "
                        "case (validation_unavailable is NOT the same as a "
                        "least-bad judged take). Set True only to deliberately "
                        "allow an unjudged video through — the result is marked "
                        "validation_state=validation_unavailable."
                    ),
                },
            },
            "required": [
                "base_prompt",
                "first_frame",
                "output_dir",
                "action_description",
            ],
            "additionalProperties": False,
        },
        handler=_produce_validated_video,
        requires_workspace=True,
    ),
    ToolDef(
        name="validate_image",
        description=(
            "Strict VLM rubric check for a single sprite first-frame image. "
            "Pre-i2v gate: catches static-composition defects (notably "
            "props rendered floating beside the body with no visible "
            "holding hand) that the video-level validator misses because "
            "those defects don't *change* across frames. Run after "
            "`generate_image` and before `build_i2v_prompt` / "
            "`generate_seedance_video`. Single VLM call (~10-20 s). 8 "
            "default rubrics: I1 identity match, I2 hand-prop visible "
            "attachment, I3 limb integrity, I4 foot anchor, I5 in-frame, "
            "I6 background pure, I7 single character, I8 view consistency."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "image": {
                    "type": "string",
                    "description": (
                        "Path to the first-frame PNG. Relative paths "
                        "resolve under the open workspace."
                    ),
                },
                "character_description": {
                    "type": "string",
                    "description": (
                        "Free-text description of what the image is "
                        "supposed to show, e.g. 'A side-view stout knight "
                        "in gold-plated armor with a red plume, holding a "
                        "longsword raised over the shoulder and a heater "
                        "shield braced forward in the off-hand.' Used by "
                        "I1 (identity match) and I8 (view consistency)."
                    ),
                },
                "extra_rubrics": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "id": {"type": "string"},
                            "name": {"type": "string"},
                            "description": {"type": "string"},
                        },
                        "required": ["id", "name", "description"],
                        "additionalProperties": False,
                    },
                },
                "rubric_override": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "id": {"type": "string"},
                            "name": {"type": "string"},
                            "description": {"type": "string"},
                        },
                        "required": ["id", "name", "description"],
                        "additionalProperties": False,
                    },
                },
                "vlm_model": _PINNED_VLM_MODEL_SCHEMA,
                "vlm_max_tokens": {"type": "integer", "default": 1200},
                "vlm_temperature": {"type": "number", "default": 0.0},
                "vlm_timeout": {"type": "number", "default": 90},
            },
            "required": ["image", "character_description"],
            "additionalProperties": False,
        },
        handler=_validate_image,
        requires_workspace=True,
    ),
    ToolDef(
        name="validate_asset_slot",
        description=(
            "Strict VLM slot-fit validation for GENERATED final runtime "
            "artifacts. Pass `image` + `slot_key` for one slot, or pass `items` "
            "to validate the entire independent set concurrently with one "
            "worker per item (no lower concurrency cap). Hash-valid passed "
            "receipts are reused without another provider call. Every image "
            "uses exactly one pinned Doubao 2.0 request, with no fallback or "
            "retry. Spritesheets use a frame-zero crop while the "
            "receipt binds the complete runtime sheet. The canonical S1-S5 "
            "rubric cannot be replaced; `extra_rubrics` only appends."
        ),
        input_schema={
            "type": "object",
            "properties": {
                **_SLOT_VALIDATION_ITEM_PROPERTIES,
                "items": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": 128,
                    "description": (
                        "Independent slot requests. Every item starts "
                        "concurrently; order is preserved in the result."
                    ),
                    "items": _SLOT_VALIDATION_ITEM_SCHEMA,
                },
            },
            "additionalProperties": False,
        },
        handler=_validate_asset_slot,
        requires_workspace=True,
    ),
    ToolDef(
        name="produce_validated_first_frame",
        description=(
            "Closed-loop sprite first-frame production: runs "
            "`generate_image` → `validate_image`, and **retries on "
            "validator FAIL** up to `max_attempts` (default 3). Each "
            "retry folds the failure evidence from every previous "
            "attempt into a synthesised, rubric-grouped `STRICTLY AVOID` "
            "block appended to the prompt — duplicates collapsed, "
            "generalised actionable guidance attached per rubric "
            "category. If no attempt PASSes within the budget, the "
            "**last** attempt is selected. Use this as the default "
            "first-frame production tool — it catches composition "
            "defects (e.g. floating shield with no holding hand) "
            "BEFORE they reach Seedance i2v and corrupt all 121 "
            "downstream frames. Output: `output_dir/attempt_NN/` per "
            "attempt + `output_dir/final/` mirror of the chosen attempt."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "image_prompt": {
                    "type": "string",
                    "description": (
                        "User-content for `generate_image`. Will be wrapped "
                        "with the safe-bg + animation-profile framing block; "
                        "the synthesised STRICTLY AVOID addendum from "
                        "prior attempts is also appended."
                    ),
                },
                "character_description": {
                    "type": "string",
                    "description": (
                        "Description passed to `validate_image` for "
                        "I1/I8 checks. Should describe the intended "
                        "character + view + composition."
                    ),
                },
                "output_dir": {
                    "type": "string",
                    "description": ("Where to write attempt_NN/ and final/."),
                },
                "size": {"type": "string", "default": "1024x1024"},
                "quality": {
                    "type": "string",
                    "enum": ["low", "medium", "high"],
                    "default": "low",
                    "description": _IMG_QUALITY_DESC,
                },
                "safe_bg_color": {
                    "type": "string",
                    "enum": ["#FF00FF", "#00F000"],
                    "default": "#FF00FF",
                },
                "animation_profile": {
                    "type": "string",
                    "enum": ["character", "projectile"],
                    "default": "character",
                    "description": (
                        "Selects the complete first-frame contract. `character` "
                        "uses grounded framing + I* rubrics; `projectile` uses "
                        "centered free-flying framing + P* rubrics."
                    ),
                },
                "enforce_safe_bg_avoidance": {
                    "type": "boolean",
                    "default": True,
                },
                "image_max_retries": {"type": "integer", "default": 3},
                "image_retry_delay": {"type": "number", "default": 10},
                "ref_image": {
                    "type": "string",
                    "description": (
                        "Optional grounded concept crop (GDD §2 Ref Crop) — local "
                        "path (resolves under the workspace) or HTTP(S) URL. When "
                        "set, each first-frame attempt is produced via "
                        "edit_image(input=ref_image) so the generated frame is "
                        "locked to the approved concept; omit for text-only "
                        "generate_image (fully backward compatible)."
                    ),
                },
                "vlm_model": _PINNED_VLM_MODEL_SCHEMA,
                "extra_rubrics": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "id": {"type": "string"},
                            "name": {"type": "string"},
                            "description": {"type": "string"},
                        },
                        "required": ["id", "name", "description"],
                        "additionalProperties": False,
                    },
                },
                "rubric_override": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "id": {"type": "string"},
                            "name": {"type": "string"},
                            "description": {"type": "string"},
                        },
                        "required": ["id", "name", "description"],
                        "additionalProperties": False,
                    },
                },
                "max_attempts": {
                    "type": "integer",
                    "default": 3,
                    "minimum": 1,
                    "description": (
                        "Hard cap on (generate_image + validate_image) "
                        "iterations. Each attempt costs ~1-2 minutes."
                    ),
                },
            },
            "required": [
                "image_prompt",
                "character_description",
                "output_dir",
            ],
            "additionalProperties": False,
        },
        handler=_produce_validated_first_frame,
        requires_workspace=True,
    ),
    ToolDef(
        name="produce_validated_sprite_action",
        description=(
            "End-to-end sprite-action production in ONE call with "
            "**different validation strategies per stage**: chains "
            "`produce_validated_first_frame` (serial retry up to "
            "`image_max_attempts` with STRICTLY AVOID feedback "
            "injection — image gen actually responds to feedback) → "
            "`produce_validated_video` (one quality candidate by default; "
            "optional serial retry with regeneration "
            "up to `video_n_candidates` attempts — stops at the first "
            "take that clears both validation passes: framing "
            "[in-frame + scale-locked] on the raw mp4 and content "
            "[identity + hand-prop + limb + loop] on the matted "
            "preview). The image stage NEVER blocks the video "
            "stage — even if no image attempt PASSes, the last-tried "
            "image is forwarded. The i2v prompt is auto-built from "
            "`video_user_content` via `build_i2v_prompt`. This is the "
            "recommended **default** for producing sprite actions — "
            "single tool call, both stages validated."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "image_prompt": {
                    "type": "string",
                    "description": (
                        "First-frame image generation prompt — "
                        "describes character / pose / view / art style. "
                        "Will be wrapped with safe-bg + foot-anchor "
                        "blocks and any synthesised retry guidance "
                        "automatically (the image stage uses serial "
                        "retry with STRICTLY AVOID feedback injection)."
                    ),
                },
                "character_description": {
                    "type": "string",
                    "description": (
                        "Description for the IMAGE validator's I1/I8 "
                        "checks. Should describe what the first frame "
                        "should look like (species + view + pose + props)."
                    ),
                },
                "video_user_content": {
                    "type": "string",
                    "description": (
                        "Action description (context for the VIDEO "
                        "validator's two hard rubrics). Will be wrapped "
                        "via `build_i2v_prompt` to produce the full "
                        "Seedance prompt automatically. Example: 'The "
                        "knight performs an overhead sword attack — feet "
                        "planted, shield braced forward.'"
                    ),
                },
                "output_dir": {
                    "type": "string",
                    "description": (
                        "Top-level output directory. Will contain "
                        "`first_frame/` (image-stage subtree) and "
                        "`video/` (video-stage subtree)."
                    ),
                },
                "first_frame_override": {
                    "type": "string",
                    "description": (
                        "Path to a pre-generated reference image to use "
                        "directly as the Seedance first_frame. When set, "
                        "Stage 1 (image generation + validation) is "
                        "SKIPPED entirely. Use this to share ONE "
                        "reference across all actions of the same "
                        "character — generate the reference once, then "
                        "pass its path here for each action."
                    ),
                },
                "ref_image": {
                    "type": "string",
                    "description": (
                        "Optional grounded concept crop (GDD §2 Ref Crop) — local "
                        "path (resolves under the workspace) or HTTP(S) URL. When "
                        "set (and `first_frame_override` is not), the image stage "
                        "seeds each first-frame attempt via "
                        "edit_image(input=ref_image) so the sprite matches the "
                        "approved concept. Ignored when `first_frame_override` is "
                        "given (image stage is skipped). Omit for text-only."
                    ),
                },
                # ---- image-stage knobs ----
                "safe_bg_color": {
                    "type": "string",
                    "enum": ["#FF00FF", "#00F000"],
                    "default": "#FF00FF",
                },
                "image_size": {"type": "string", "default": "1024x1024"},
                "image_quality": {
                    "type": "string",
                    "enum": ["low", "medium", "high"],
                    "default": "low",
                    "description": _IMG_QUALITY_DESC,
                },
                "image_max_attempts": {
                    "type": "integer",
                    "default": 3,
                    "minimum": 1,
                    "description": (
                        "Cap on serial-retry attempts in the image "
                        "stage. Each attempt costs ~1-2 minutes; the "
                        "image stage uses STRICTLY AVOID feedback "
                        "injection between attempts (image gen actually "
                        "responds to feedback). Image stage NEVER "
                        "blocks — last attempt is forwarded to video "
                        "stage if no PASS."
                    ),
                },
                "image_extra_rubrics": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "id": {"type": "string"},
                            "name": {"type": "string"},
                            "description": {"type": "string"},
                        },
                        "required": ["id", "name", "description"],
                        "additionalProperties": False,
                    },
                },
                # ---- video-stage knobs ----
                "duration": {"type": "integer", "default": 5},
                "ratio": {"type": "string", "default": "1:1"},
                "resolution": {"type": "string", "default": "720p"},
                "seedance_model": {
                    "type": "string",
                    "enum": ["fast", "standard"],
                    "default": "fast",
                },
                "seed_base": {"type": "integer"},
                "bg_hex": {
                    "type": "string",
                    "enum": ["#FF00FF", "#00F000"],
                    "description": (
                        "Override matting bg_hex; defaults to safe_bg_color."
                    ),
                },
                "aug_preset": {
                    "type": "string",
                    "enum": [
                        "off",
                        "aggressive",
                        "medium",
                        "balanced",
                        "conservative",
                        "more-conservative",
                        "very-conservative",
                        "nearly-off",
                    ],
                    "default": "off",
                },
                "rmbg_fg_cutoff": {"type": "integer", "default": 230},
                "gif_alpha_threshold": {"type": "integer", "default": 200},
                "align_feet_to_bottom": {"type": "boolean", "default": True},
                "foot_margin_px": {"type": "integer", "default": 2},
                "drop_bg_leak": {
                    "type": "boolean",
                    "default": True,
                    "description": (
                        "Drop matted frames where the safe bg color "
                        "(`bg_hex`) leaked through as opaque pixels "
                        "(matte failure on bright effects)."
                    ),
                },
                "bg_leak_area_threshold": {
                    "type": "number",
                    "default": 0.005,
                    "description": (
                        "Drop a frame when ≥ this fraction (0-1) of "
                        "the canvas is opaque AND near `bg_hex`."
                    ),
                },
                "bg_leak_color_dist": {"type": "integer", "default": 120},
                "bg_leak_alpha_threshold": {"type": "integer", "default": 128},
                "bg_leak_keep_min_frames": {"type": "integer", "default": 2},
                "bg_leak_alpha_weighted": {"type": "boolean", "default": True},
                "matte_workers": {"type": "integer", "default": 8},
                "video_n_candidates": {
                    "type": "integer",
                    "default": 1,
                    "minimum": 1,
                    "description": (
                        "MAX serial attempts in the video stage (fresh "
                        "seedance seed each). Attempts run one at a time "
                        "and stop at the first take that clears both "
                        "validation passes (framing [in-frame + scale-"
                        "locked] + content [identity + hand-prop + limb + "
                        "loop]); defaults to the global "
                        "`assets.seedance.max_quality_candidates` setting (1 in "
                        "the checked-in config), so validation failure does not "
                        "regenerate by default. Transient no-output/network "
                        "failures are retried independently."
                    ),
                },
                "video_extra_rubrics": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "id": {"type": "string"},
                            "name": {"type": "string"},
                            "description": {"type": "string"},
                        },
                        "required": ["id", "name", "description"],
                        "additionalProperties": False,
                    },
                },
                "vlm_model": _PINNED_VLM_MODEL_SCHEMA,
                "effects": {
                    "type": "boolean",
                    "default": False,
                    "description": (
                        "Default false for every animation profile because FX "
                        "are runtime ParticleEmitter recipes. Set true only for "
                        "an explicitly non-runtime/custom video pipeline."
                    ),
                },
                "animation_profile": {
                    "type": "string",
                    "enum": ["character", "projectile"],
                    "default": "character",
                    "description": (
                        "Selects the framing / alignment / rubric contract. "
                        "`character` (default) = grounded figure: foot-anchor "
                        "first frame, feet-aligned matte, character I1..I8 + "
                        "R1..R7 rubrics (identity/hand-prop/limb/loop/action), body-only "
                        "animation. `projectile` = free-flying projectile BODY (bullet/"
                        "arrow/fireball/bolt): CENTERED (not grounded) first "
                        "frame, no feet alignment, projectile P1..P6 image + "
                        "PR1..PR6 video rubrics (NO hand-prop / limb / grounded "
                        "checks), also effects OFF. Use `projectile` for "
                        "any manifest entry whose role is a visible animated "
                        "projectile body; its trail/impact/explosion remain "
                        "runtime particle recipes."
                    ),
                },
            },
            "required": ["video_user_content", "output_dir"],
            "additionalProperties": False,
        },
        handler=_produce_validated_sprite_action,
        requires_workspace=True,
    ),
    ToolDef(
        name="analyze_sprite_metadata",
        description=(
            "Sprite analysis: contentBbox from alpha channel of `image` (pixel-precise), "
            "facing from VLM on `reference_image` (if provided) or `image`. "
            "Visualization is ALWAYS drawn on `image` (the transparent spritesheet). "
            "Call ONCE per entity after spritesheet generation — pass the idle-sheet.png "
            "as `image` and reference.png as `reference_image`. Use returned values "
            "for all action sheets of the same character."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "image": {
                    "type": "string",
                    "description": (
                        "Path to the spritesheet PNG (with transparency/alpha). "
                        "Used for contentBbox calculation AND bbox visualization."
                    ),
                },
                "frame_width": {
                    "type": "integer",
                    "description": "Width of a single frame in the spritesheet.",
                },
                "frame_height": {
                    "type": "integer",
                    "description": "Height of a single frame in the spritesheet.",
                },
                "reference_image": {
                    "type": "string",
                    "description": (
                        "Optional path to the reference image (typically 960x960, "
                        "un-matted). VLM judges facing from this image (clearer "
                        "single-character view). If omitted, VLM uses `image` instead."
                    ),
                },
                "description": {
                    "type": "string",
                    "description": (
                        "What this sprite is — e.g. 'enemy bullet projectile "
                        "flying left', 'player character holding blaster, "
                        "side-view platform game', 'round robot enemy facing "
                        "right'. Helps VLM judge facing direction accurately."
                    ),
                },
                "vlm_model": _PINNED_VLM_MODEL_SCHEMA,
                "vlm_max_tokens": {
                    "type": "integer",
                    "description": (
                        "Override VLM token budget. Defaults to "
                        "`assets.vlm.image_max_tokens` from gca.config.yaml."
                    ),
                },
                "vlm_temperature": {
                    "type": "number",
                    "description": (
                        "Override VLM sampling temperature. Defaults to "
                        "`assets.vlm.temperature` from gca.config.yaml."
                    ),
                },
                "vlm_timeout": {
                    "type": "number",
                    "description": (
                        "HTTP timeout in seconds. Defaults to "
                        "`assets.vlm.image_timeout_seconds` from gca.config.yaml."
                    ),
                },
            },
            "required": ["image", "frame_width", "frame_height"],
            "additionalProperties": False,
        },
        handler=_analyze_sprite_metadata,
        requires_workspace=True,
    ),
    ToolDef(
        name="pack_spritesheet",
        description=(
            "Assemble matted transparent frames (e.g. a "
            "`…/rmbg2/transparent_frames/` directory from "
            "`rmbg2_video_matting`) into a single Phaser-ready spritesheet "
            "PNG + `*-sheet.json`. Handles the full assembly: caps frame "
            "count to `max_frames`, resizes every frame to the GDD target "
            "`frame_width × frame_height`, and wraps into multiple rows so "
            "the sheet stays within `max_sheet_width` (GPU MAX_TEXTURE_SIZE "
            "safety, 4096px). This is the REQUIRED final step for every "
            "animated sprite action — do NOT hand-assemble sheets with "
            "Python/Node. Defaults are read from `assets.sprite.*` in "
            "gca.config.yaml; pass `facing`/`content_bbox` (from "
            "`analyze_sprite_metadata`) to record them in the JSON."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "frames_dir": {
                    "type": "string",
                    "description": (
                        "Directory of transparent RGBA frame PNGs (sorted by "
                        "filename). Relative paths resolve under the workspace."
                    ),
                },
                "output": {
                    "type": "string",
                    "pattern": r".+\.png$",
                    "description": (
                        "Output spritesheet PNG path, e.g. "
                        "`assets/sprites/player/idle-sheet.png`. The metadata "
                        "JSON is written next to it as `idle-sheet.json`."
                    ),
                },
                "frame_width": {
                    "type": "integer",
                    "description": "Target frame width (GDD Frame Size column).",
                },
                "frame_height": {
                    "type": "integer",
                    "description": "Target frame height (GDD Frame Size column).",
                },
                "fps": {
                    "type": "integer",
                    "description": (
                        "Playback FPS written to the JSON. Defaults to "
                        "`assets.sprite.fps`."
                    ),
                },
                "max_sheet_width": {
                    "type": "integer",
                    "description": (
                        "Per-axis pixel cap for the sheet (GPU safety). "
                        "Defaults to `assets.sprite.max_sheet_width` (4096)."
                    ),
                },
                "max_frames": {
                    "type": "integer",
                    "description": (
                        "Cap on frames packed (uniform down-sample if more). "
                        "Defaults to `assets.sprite.max_frames` (24)."
                    ),
                },
                "min_frame_dimension_px": {
                    "type": "integer",
                    "description": (
                        "Floor for the safety down-scale. Defaults to "
                        "`assets.sprite.min_frame_dimension_px` (64)."
                    ),
                },
                "facing": {
                    "type": "string",
                    "description": (
                        "Optional facing value (from analyze_sprite_metadata) "
                        "to record in the sheet JSON."
                    ),
                },
                "content_bbox": {
                    "type": "object",
                    "description": (
                        "Optional contentBbox (from analyze_sprite_metadata) "
                        "to record in the sheet JSON."
                    ),
                },
            },
            "required": ["frames_dir", "output", "frame_width", "frame_height"],
            "additionalProperties": False,
        },
        handler=_pack_spritesheet,
        requires_workspace=True,
    ),
    ToolDef(
        name="register_existing_static_asset",
        description=(
            "Validate and seal a user-supplied or licensed PNG without "
            "regenerating it. Both the path suffix and decoded Pillow format must "
            "be PNG. Writes an atomic hash/dimension/provenance/source-contract "
            "`.asset.json` sidecar and, "
            "for collidable art, a same-basename `.bbox.json` sidecar consumed "
            "by `finalize_asset_manifest`."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "image": {
                    "type": "string",
                    "description": (
                        "Existing `.png` path strictly below active-workspace "
                        "assets/** or design/**; decoded content must also be PNG."
                    ),
                },
                "provenance": {
                    "type": "object",
                    "description": (
                        "Non-empty source record; include licenseId for licensed art."
                    ),
                },
                "source_strategy": {
                    "type": "string",
                    "enum": ["supplied-image", "licensed-image"],
                    "description": (
                        "Manifest sourceStrategy. Defaults to licensed-image "
                        "when provenance has licenseId, otherwise supplied-image."
                    ),
                },
                "collidable": {"type": "boolean"},
                "cols": {"type": "integer", "minimum": 1},
                "rows": {"type": "integer", "minimum": 1},
                "clean_threshold": {"type": "integer", "minimum": 0, "maximum": 255},
                "anchors": {"type": "array", "items": {"type": "object"}},
                "slots": {"type": "array", "items": {"type": "object"}},
            },
            "required": ["image", "provenance"],
            "additionalProperties": False,
        },
        handler=_register_existing_static_asset,
        requires_workspace=True,
    ),
    ToolDef(
        name="finalize_asset_manifest",
        description=(
            "Single-writer reducer for the canonical asset manifest. After all "
            "parallel asset workers finish, reopens every static PNG and "
            "validates its measured dimensions against the atomic `.asset.json` "
            "producer/hash/dimension/provenance/source contract plus collidable "
            "assets' same-basename `.bbox.json`, "
            "binds generated source_contract.assetType to the manifest category "
            "(all UI/HUD/icon/button/bar/panel/frame/lettering aliases map to `ui`), "
            "verifies crop-reference hashes, validates actual profile sizing for "
            "every file-backed UI source strategy, and additionally verifies "
            "generated-UI producer sizing seals against authored manifest fields, "
            "accepts sealed `extend_background` outputs for background rows, "
            "rejects missing, stale, or manifest-mismatched metadata, and "
            "atomically merges dimensions, binding geometry, and bbox/cells "
            "into the active workspace's `assets/asset-manifest.json`. "
            "Parallel workers never mutate the shared manifest."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "game_dir": {
                    "type": "string",
                    "description": (
                        "Optional compatibility assertion; when supplied it "
                        "must resolve exactly to the active workspace root."
                    ),
                },
            },
            "additionalProperties": False,
        },
        handler=_finalize_asset_manifest,
        requires_workspace=True,
    ),
    ToolDef(
        name="content_bbox",
        description=(
            "Compute the opaque-content bounding box of a STATIC asset "
            "(prop / tile / UI / background sprite) from its alpha channel — "
            "the same pixel-precise method used for sprite contentBbox. For a "
            "single image returns top-level `contentBbox {x,y,w,h}` (frame-local "
            "px); for a tile strip/grid pass `cols`/`rows` to get a per-cell "
            "`cells[]` array. Record the result alongside the asset so "
            "implement-agent can size static collision bodies to the VISIBLE "
            "pixels (setSize/setOffset) instead of the full padded frame — this "
            "is what prevents players from standing in mid-air on a platform "
            "whose matted image has a large transparent margin. No VLM/network."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "image": {
                    "type": "string",
                    "description": "Path to the matted PNG (with transparency).",
                },
                "cols": {
                    "type": "integer",
                    "description": "Columns if this is a tile strip/grid (default 1).",
                },
                "rows": {
                    "type": "integer",
                    "description": "Rows if this is a tile strip/grid (default 1).",
                },
                "alpha_threshold": {
                    "type": "integer",
                    "description": "Alpha cutoff for 'opaque' (default 10).",
                },
                "clean_threshold": {
                    "type": "integer",
                    "description": (
                        "Optional: hard-zero pixels with alpha below this "
                        "before measuring, killing sparse rmbg2 edge specks "
                        "that would otherwise anchor the bbox to the canvas "
                        "edge. Omit to disable (default). ~24 is a safe value."
                    ),
                },
                "output": {
                    "type": "string",
                    "description": (
                        "Optional sidecar JSON path to write the result "
                        "(e.g. assets/props/tiles-island.bbox.json)."
                    ),
                },
            },
            "required": ["image"],
            "additionalProperties": False,
        },
        handler=_content_bbox,
        requires_workspace=True,
    ),
    ToolDef(
        name="trim_transparent",
        description=(
            "Crop a STATIC asset (prop / tile / UI widget / icon / badge / "
            "lettering / logo) to its alpha content bbox, dropping the "
            "transparent margin RMBG-2.0 left behind. Optional `padding` "
            "(default 2 px) preserves soft-edge halos. No-op when (a) the "
            "image has no opaque pixels, (b) it is already tight, or (c) the "
            "trim would remove < `min_trim_px` on every edge. DO NOT call "
            "this on: spritesheets / tile strips (per-frame must be "
            "equal-sized — call `pack_spritesheet`'s flow instead), value-less "
            "panels with `anchors`/`slots` (anchor coords are 0~1 fractions of "
            "the whole image — cropping shifts them), or full-canvas opaque "
            "backgrounds (no transparent area to trim). To bring a widget to "
            "its in-game display size, follow with `scale_uniform` (uniform, "
            "aspect-preserving — never a non-uniform resize, which squashes). "
            "Rewrites the file in place unless `output` is given. No VLM/network."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "image": {
                    "type": "string",
                    "description": "Path to the matted RGBA PNG to trim.",
                },
                "output": {
                    "type": "string",
                    "description": (
                        "Optional destination path. Defaults to overwriting "
                        "`image` in place."
                    ),
                },
                "padding": {
                    "type": "integer",
                    "description": (
                        "Pixels of transparent padding to keep on every edge "
                        "around the content bbox (default 2). Preserves "
                        "anti-aliased corners and soft halos."
                    ),
                },
                "alpha_threshold": {
                    "type": "integer",
                    "description": "Alpha cutoff for 'opaque' (default 10).",
                },
                "clean_threshold": {
                    "type": "integer",
                    "description": (
                        "Hard-zero pixels with alpha below this BEFORE "
                        "measuring the bbox, so sparse rmbg2 edge specks can't "
                        "anchor the box to the canvas edge and block the trim "
                        "(the 'only top/right got cut' bug). Measurement-only "
                        "— the saved crop keeps original soft edges. Default "
                        "24. Pass 0 to disable."
                    ),
                },
                "min_trim_px": {
                    "type": "integer",
                    "description": (
                        "If trim < this on every edge, skip (default 4). "
                        "Avoids needless rewrites for already-tight assets."
                    ),
                },
            },
            "required": ["image"],
            "additionalProperties": False,
        },
        handler=_trim_transparent,
        requires_workspace=True,
    ),
    ToolDef(
        name="scale_uniform",
        description=(
            "Scale an RGBA PNG UNIFORMLY (aspect ALWAYS preserved — never "
            "squashed) to its in-game display size. The asset-side proportion-control "
            "step brings a generated source to the authored size "
            "so the runtime draws it 1:1 — the on-disk "
            "pixel size becomes the single source of truth for display size "
            "(the runtime no longer guesses from the GDD). Pass EXACTLY ONE of: "
            "`target_height` (lock height, width follows — recommended for HUD "
            "icons/buttons/gauges from a role height), `target_width` (lock "
            "width), or `max_box=[w,h]` (contain-fit inside a box). Use after "
            "`trim_transparent`. Rewrites in place unless `output` is given. "
            "No VLM/network."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "image": {
                    "type": "string",
                    "description": "Path to the RGBA PNG to scale.",
                },
                "target_height": {
                    "type": "integer",
                    "minimum": 1,
                    "description": (
                        "Lock the displayed height (px); width is derived from "
                        "the source aspect. Recommended for HUD widgets."
                    ),
                },
                "target_width": {
                    "type": "integer",
                    "minimum": 1,
                    "description": (
                        "Lock the displayed width (px); height follows aspect."
                    ),
                },
                "max_box": {
                    "type": "array",
                    "items": {"type": "integer", "minimum": 1},
                    "minItems": 2,
                    "maxItems": 2,
                    "description": (
                        "[maxW, maxH] contain-fit box — scale = min(maxW/W, "
                        "maxH/H), aspect preserved."
                    ),
                },
                "allow_upscale": {
                    "type": "boolean",
                    "description": (
                        "When false, never enlarge (cap scale at 1.0). Default true."
                    ),
                },
                "output": {
                    "type": "string",
                    "description": (
                        "Optional destination path. Defaults to overwriting "
                        "`image` in place."
                    ),
                },
            },
            "required": ["image"],
            "additionalProperties": False,
        },
        handler=_scale_uniform,
        requires_workspace=True,
    ),
    ToolDef(
        name="produce_static_asset",
        description=(
            "RECOMMENDED DEFAULT for every static prop / tileset / functional "
            "UI widget (button, bar, panel, icon, badge). Welds the entire "
            "matting chain into ONE atomic call: `generate_image` (on a solid "
            "configured safe bg) → the `assets.matting.backend` (`auto` prefers "
            "RMBG-2.0 and falls back on network, missing-route HTTP 404, or HTTP "
            "5xx outages; explicit `rmbg2` and "
            "deterministic `color_key` are also supported) → "
            "`trim_transparent` (drop the leftover margin) → profile sizing "
            "(`scale_uniform` for target-axis/max-box contracts or exact "
            "nearest-neighbor contain/pad for pixel-art `base_size`) → optional "
            "`content_bbox` (when `collidable`). The color-key backend fails "
            "closed when the selected key removes no pixels or erases the whole "
            "foreground. This guarantees the shipped PNG has real transparency "
            "— you CANNOT accidentally generate a prop/UI asset and forget the "
            "matte (the #1 cause of opaque RGB plates rendering as solid "
            "rectangles in-game).\n\n"
            "BACKGROUND / MAP / PARALLAX art is opaque by design and has no "
            "removable key color — it must NOT be matted. Pass "
            "`skip_matting:true` (or set `asset_type` to background/map/"
            "parallax, which auto-skips). Opaque and matted branches both finish "
            "through the same pinned multi-file artifact/sidecar transaction. "
            "Both the `.png` path and decoded PNG format are enforced before the "
            "`.asset.json` hash/dimension/provenance/source-contract seal can be "
            "consumed by `finalize_asset_manifest`.\n\n"
            "For an art_directed UI/HUD asset, exactly one uniform target is "
            "required and comes from the row's authored GDD §2E sizing contract; "
            "gate thresholds never clamp or invent that design size. pixel_art "
            "UI requires exact `base_size=[w,h]` and uses nearest-neighbor "
            "contain/pad; every responsive/minimal UI asset, including decorative "
            "frames and overlays, requires `max_box`. The `.asset.json` seals one "
            "canonical `qualityProfile` + `sizingContract`, including final "
            "dimensions, aspect preservation, and resampler semantics.\n\n"
            "NOT for: animated sprites (use `produce_validated_sprite_action`, "
            "which is never trimmed — equal-sized frames). Crop-seeded still "
            "assets — styled lettering/logos, value-less binding panels, and "
            "large decorative frame/overlay layers — ALL belong here: pass the "
            "crop via `ref_image` (seeds `edit_image` internally) so they get "
            "the welded matte+trim+scale. Every still asset is trimmed and "
            "uniformly scaled the same way — a binding panel is NOT a special "
            "case: pass its `anchors`/`slots` and they are re-projected by the "
            "crop offsets so trimming can't float overlaid values off the "
            "widget. A decorative frame is just a large `decorative` UI image "
            "(trim is a no-op when its content already hugs the edges). Returns "
            "{output, matted, trimmed, trim, anchors, slots, bbox, "
            "bbox_sidecar, metadata_sidecar, artifact_sha256, width, height, "
            "provenance, source_contract, size, skipped_matting}."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "prompt": {
                    "type": "string",
                    "description": (
                        "Image generation prompt. For matted assets, describe "
                        "the prop/widget on a plain solid background; the safe "
                        "background fill + color-avoidance block are added "
                        "automatically."
                    ),
                },
                "output": {"type": "string", "description": _IMG_OUTPUT_DESC},
                "ref_image": {
                    "type": "string",
                    "description": (
                        "Optional local concept crop (GDD §2 Ref Crop), resolved "
                        "canonically under active-workspace assets/** or design/**. "
                        "URLs and repository-global references are rejected. When "
                        "set, Stage 1 seeds generation FROM the crop via "
                        "edit_image instead of text-only generate_image, then "
                        "runs the SAME configured-matting → trim_transparent chain. "
                        "The workspace-relative reference path and SHA-256 are "
                        "sealed into provenance and source_contract. "
                        "Use this for crop-seeded UI / binding panels so they "
                        "get the welded matte+trim and can't skip trim. Omit "
                        "for text-only assets."
                    ),
                },
                "asset_type": {
                    "type": "string",
                    "enum": sorted(_CANONICAL_STATIC_ASSET_TYPES),
                    "description": (
                        "Required canonical static category. Use `ui` for every "
                        "UI widget, HUD, icon, button/bar, panel/frame/overlay, "
                        "badge, lettering/art-word, logo, wordmark, dashboard, "
                        "toolbar, or counter; those "
                        "manifest UI categories map to source_contract.assetType "
                        "`ui`. Use prop/tile/tileset/etc. for other matted assets; "
                        "background / map / parallax "
                        "auto-skip matting (opaque scene art). Also drives the "
                        "framing default: matted still types (prop/ui/icon/…) → "
                        "`centered_asset` (centered with edge margin) — NEVER "
                        "the grounded-character block, even though a safe bg is "
                        "auto-filled; grounded character types → "
                        "`grounded_sprite`; background/map/parallax → "
                        "`full_scene`."
                    ),
                },
                "framing_profile": {
                    "type": "string",
                    "enum": ["grounded_sprite", "centered_asset", "full_scene"],
                    "description": (
                        "Explicit framing override (see generate_image). Rarely "
                        "needed — the `asset_type` default is correct for props/"
                        "UI/icons (centered_asset). Defaults from `asset_type`."
                    ),
                },
                "skip_matting": {
                    "type": "boolean",
                    "default": False,
                    "description": (
                        "Opaque scene art only. Accepted for background/map/"
                        "parallax and rejected for all matted asset types."
                    ),
                },
                "collidable": {
                    "type": "boolean",
                    "default": False,
                    "description": (
                        "True for props/tiles the player stands on / collides "
                        "with → also compute content_bbox and write a "
                        "`<output>.bbox.json` sidecar so collision bodies fit "
                        "the visible pixels. After all parallel workers join, "
                        "`finalize_asset_manifest` performs the only manifest merge."
                    ),
                },
                "cols": {
                    "type": "integer",
                    "description": "content_bbox: columns if a tile strip/grid (default 1).",
                },
                "rows": {
                    "type": "integer",
                    "description": "content_bbox: rows if a tile strip/grid (default 1).",
                },
                "anchors": {
                    "type": "array",
                    "items": {"type": "object"},
                    "description": (
                        "§2E.1 Binding Spec anchors ({id, xPct, yPct, fontPct, "
                        "binds}) measured on the mockup as fractions of the "
                        "WHOLE asset box. When given, they are re-projected "
                        "onto the post-trim box and returned as `anchors` — "
                        "write those back into the manifest. Uniform sizing "
                        "preserves fractions; pixel-art contain/pad reprojects "
                        "them into the padded base-size canvas."
                    ),
                },
                "slots": {
                    "type": "array",
                    "items": {"type": "object"},
                    "description": (
                        "Explicit §2E.1 slot rectangles ({id/action, xPct, yPct, "
                        "wPct, hPct}) as fractions of the whole asset box. "
                        "Re-projected onto the post-trim box like `anchors` and "
                        "returned as `slots`. Data-driven manifest bindings "
                        "({source, action}) are not accepted here; retain them "
                        "in the manifest/runtime contract."
                    ),
                },
                "size": {"type": "string"},
                "quality": {
                    "type": "string",
                    "enum": ["low", "medium", "high"],
                    "default": "low",
                    "description": _IMG_QUALITY_DESC,
                },
                "aspect": {
                    "type": "string",
                    "description": ("Configured key from assets.image.size_by_aspect."),
                },
                "safe_bg_color": {
                    "type": "string",
                    "enum": ["#FF00FF", "#00F000"],
                    "description": (
                        "Reserved staging background. Both colors are supported "
                        "by rmbg2 and color_key matting; choose the one absent "
                        "from the foreground."
                    ),
                },
                "enforce_safe_bg_avoidance": {"type": "boolean"},
                "retry_delay": {"type": "number", "minimum": 0},
                "bg_hex": {
                    "type": "string",
                    "description": (
                        "RMBG2-only color-augmentation hint (e.g. '#FF00FF'). "
                        "The color_key backend keys the actual safe_bg_color."
                    ),
                },
                "aug_preset": {"type": "string"},
                "aug_hard": {"type": "integer"},
                "aug_soft": {"type": "integer"},
                "rmbg_fg_cutoff": {"type": "integer"},
                "max_size": {"type": "integer", "minimum": 1},
                "max_workers": {"type": "integer", "minimum": 1},
                "timeout": {"type": "integer", "minimum": 1},
                "padding": {
                    "type": "integer",
                    "description": "trim_transparent padding in px (default 2).",
                },
                "clean_threshold": {
                    "type": "integer",
                    "description": (
                        "trim: hard-zero pixels with alpha below this before "
                        "measuring the content bbox, so sparse rmbg2 edge "
                        "specks can't block the trim. Default 24; pass 0 to "
                        "disable. Also applied to the collidable content_bbox."
                    ),
                },
                "target_height": {
                    "type": "integer",
                    "minimum": 1,
                    "description": (
                        "scale_uniform: lock the in-game display HEIGHT (px) "
                        "after trim — width follows the source aspect (never "
                        "squashed). Recommended for HUD widgets/icons sized "
                        "from a design role height. The on-disk size becomes "
                        "the display size. Pass at most ONE of target_height / "
                        "target_width / max_box / base_size. art_directed UI/HUD assets "
                        "require target_height or target_width; max_box is for "
                        "responsive/minimal fit contracts."
                    ),
                },
                "target_width": {
                    "type": "integer",
                    "minimum": 1,
                    "description": (
                        "scale_uniform: lock the in-game display WIDTH (px); "
                        "height follows aspect."
                    ),
                },
                "max_box": {
                    "type": "array",
                    "items": {"type": "integer", "minimum": 1},
                    "minItems": 2,
                    "maxItems": 2,
                    "description": (
                        "scale_uniform: [maxW, maxH] contain-fit box (aspect "
                        "preserved)."
                    ),
                },
                "base_size": {
                    "type": "array",
                    "items": {"type": "integer", "minimum": 1},
                    "minItems": 2,
                    "maxItems": 2,
                    "description": (
                        "pixel_art UI only: exact [width, height] runtime texture "
                        "box. Content is aspect-preserving contain-fit, centered "
                        "on transparent padding, and resized only with nearest-"
                        "neighbor. Mutually exclusive with target_height, "
                        "target_width, and max_box."
                    ),
                },
                "allow_upscale": {
                    "type": "boolean",
                    "description": (
                        "scale_uniform: when false, never enlarge (cap at 1.0). "
                        "Default true."
                    ),
                },
                "max_retries": {"type": "integer"},
            },
            "required": ["prompt", "output", "asset_type"],
            "additionalProperties": False,
        },
        handler=_produce_static_asset,
        requires_workspace=True,
    ),
]


_BUILD_MUTATION_TOOLS = {
    "generate_image",
    "edit_image",
    "generate_seedance_video",
    "extend_background",
    "rmbg2_video_matting",
    "produce_validated_video",
    "produce_validated_first_frame",
    "produce_validated_sprite_action",
    "pack_spritesheet",
    "register_existing_static_asset",
    "finalize_asset_manifest",
    "content_bbox",
    "trim_transparent",
    "scale_uniform",
    "produce_static_asset",
}
_PARALLEL_BUILD_MUTATION_TOOLS = {
    "rmbg2_video_matting",
    "produce_validated_video",
    "produce_validated_first_frame",
    "produce_validated_sprite_action",
    "pack_spritesheet",
    "register_existing_static_asset",
    "produce_static_asset",
}


def _guard_build_mutation(handler, *, allow_parallel: bool):
    """Prevent asset/build writes from racing an active verification epoch."""

    @wraps(handler)
    def guarded(args: dict[str, Any], ctx: ToolCtx):
        game_dir = _require_workspace_root(ctx)
        with build_mutation_flock(
            game_dir,
            exclusive=not allow_parallel,
        ):
            return handler(args, ctx)

    return guarded


TOOLS = [
    ToolDef(
        name=tool.name,
        description=tool.description,
        input_schema=tool.input_schema,
        handler=(
            _guard_build_mutation(
                tool.handler,
                allow_parallel=tool.name in _PARALLEL_BUILD_MUTATION_TOOLS,
            )
            if tool.name in _BUILD_MUTATION_TOOLS
            else tool.handler
        ),
        requires_workspace=tool.requires_workspace,
    )
    for tool in TOOLS
]
