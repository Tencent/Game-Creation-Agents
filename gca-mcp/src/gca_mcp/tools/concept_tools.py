"""Concept-art tools for the multimodal GDD (visual backbone).

Three tools, all operating on the full-scene concept mockups design-agent
generates under ``design/``:

- ``fit_image_to_resolution`` — resize a mockup to an EXACT W×H so the embedded
  reference matches the real game canvas.
- ``ground_crop`` — VLM-ground the mockup into per-element reference crops that
  assets-agent seeds from via ``edit_image`` / ``ref_image`` (the *looks-like*
  axis). Its boxes are loose art-seed crops, NOT element positions.
- ``detect_layout`` — VLM position detection (the *lays-out-like* axis): a TIGHT,
  canvas-normalized box + role + anchor for every element, written to
  ``design/<scene>.detect.json``. This is the position source the design-agent
  turns into the GDD §2.0.2 Scene Layout Design. Distinct from ``ground_crop``.

The heavy lifting lives in :mod:`gca_mcp.adapters.concept_ground` and
:mod:`gca_mcp.adapters.detect_layout`; this module is the thin MCP surface
(schema + workspace-relative path resolution).
"""

from __future__ import annotations

import json
import tempfile
from functools import wraps
from pathlib import Path
from typing import Any

from ..adapters import concept_ground, detect_layout, iterate_vlm
from ..errors import ToolError
from ..server import ToolCtx, ToolDef
from ..verify_epoch import build_mutation_flock
from ..workspace import validated_workspace_root


def _resolve_under_workspace(ctx: ToolCtx, p: str | None) -> Path | None:
    """Resolve one path inside the active workspace, rejecting symlink escapes."""
    if not p:
        return None
    workspace = ctx.state.workspace
    if workspace is None:
        raise ToolError("NO_WORKSPACE_OPEN", "No active workspace.")
    root = validated_workspace_root(workspace)
    path = Path(p).expanduser()
    resolved = (path if path.is_absolute() else root / path).resolve()
    staging_root = getattr(ctx, "_concept_staging_root", None)
    if isinstance(staging_root, Path) and resolved.is_relative_to(staging_root):
        return resolved
    if not resolved.is_relative_to(root):
        raise ToolError(
            "INVALID_PATH",
            f"Concept path escapes the active workspace: {resolved}",
        )
    return resolved


def _positive_int_arg(
    args: dict[str, Any],
    name: str,
    default: int,
) -> int:
    value = args.get(name, default)
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ToolError("INVALID_CONFIG", f"`{name}` must be a positive integer.")
    return value


def _concept_paths_overlap(left: Path, right: Path) -> bool:
    return (
        left == right
        or left.is_relative_to(right)
        or right.is_relative_to(left)
    )


def _validate_ground_crop_paths(
    *,
    input_ref: str,
    output_dir_ref: str,
    detect_ref: str | None,
) -> None:
    output_dir = Path(output_dir_ref)
    if (
        len(output_dir.parts) != 3
        or output_dir.parts[:2] != ("design", "crops")
        or output_dir.parts[2] in {"", ".", ".."}
    ):
        raise ToolError(
            "INVALID_PATH",
            "ground_crop output_dir must be exactly "
            "design/crops/<scene-or-screen> with one non-empty leaf segment.",
        )
    for label, value in (("input", input_ref), ("detect", detect_ref)):
        if value is None:
            continue
        if _concept_paths_overlap(output_dir, Path(value)):
            raise ToolError(
                "INVALID_PATH",
                f"ground_crop output_dir dangerously overlaps `{label}`: {value}.",
            )


def _validate_detect_layout_paths(*, input_ref: str, output_ref: str) -> None:
    output = Path(output_ref)
    if (
        len(output.parts) != 2
        or output.parts[0] != "design"
        or output.name == ".detect.json"
        or not output.name.endswith(".detect.json")
    ):
        raise ToolError(
            "INVALID_PATH",
            "detect_layout output must be a strict file descendant of design/ "
            "with canonical `<name>.detect.json` form.",
        )
    if _concept_paths_overlap(Path(input_ref), output):
        raise ToolError(
            "INVALID_PATH",
            "detect_layout output must not equal or overlap its image input.",
        )


# -------------------- fit_image_to_resolution --------------------


def _fit_image_to_resolution(args: dict[str, Any], ctx: ToolCtx) -> dict[str, Any]:
    input_path = args.get("input")
    output = args.get("output")
    width = args.get("width")
    height = args.get("height")
    if not input_path or not output or width is None or height is None:
        raise ToolError(
            "INVALID_CONFIG",
            "`input`, `output`, `width`, and `height` are required.",
        )
    resolved_in = _resolve_under_workspace(ctx, str(input_path))
    resolved_out = _resolve_under_workspace(ctx, str(output))
    # Design-only tool. Running it on assets/** letterbox-pads + flattens alpha,
    # baking opaque black bars into a transparent asset (the #1 cause of black
    # borders on UI titles). Block it at the boundary.
    for label, rp in (("input", resolved_in), ("output", resolved_out)):
        if rp is not None and "assets" in Path(rp).parts:
            raise ToolError(
                "INVALID_CONFIG",
                f"fit_image_to_resolution is for design mockups (design/**) only — "
                f"refusing to run on an assets/ path ({label}={rp}). It center-pads "
                f"to the target box and flattens alpha, which bakes opaque "
                f"letterbox bars into the asset. For an asset whose aspect drifted "
                f"from its source crop, regenerate it at the crop's aspect via "
                f"edit_image, or uniformly scale with TRANSPARENT padding; "
                f"implement-agent's applyUiDisplay sizes it at runtime.",
            )
    return concept_ground.fit_to_resolution(
        resolved_in,
        resolved_out,
        width=int(width),
        height=int(height),
        mode=str(args.get("mode", "contain")),
    )


# -------------------- ground_crop --------------------


def _ground_crop(args: dict[str, Any], ctx: ToolCtx) -> dict[str, Any]:
    input_path = args.get("input")
    output_dir = args.get("output_dir")
    if not input_path or not output_dir:
        raise ToolError("INVALID_CONFIG", "`input` and `output_dir` are required.")
    source_ref = _concept_path_ref(ctx, "input", input_path)
    out_dir_ref = _concept_path_ref(ctx, "output_dir", output_dir)
    detect_arg = args.get("detect")
    detect_ref = (
        _concept_path_ref(ctx, "detect", detect_arg)
        if detect_arg is not None
        else None
    )
    _validate_ground_crop_paths(
        input_ref=source_ref,
        output_dir_ref=out_dir_ref,
        detect_ref=detect_ref,
    )
    canonical_output_dir = (
        validated_workspace_root(ctx.state.workspace) / out_dir_ref
    )
    if canonical_output_dir.exists() and not canonical_output_dir.is_dir():
        raise ToolError(
            "INVALID_PATH",
            f"ground_crop output_dir is not a directory path: {out_dir_ref}.",
        )
    resolved_input = _resolve_under_workspace(ctx, str(input_path))
    resolved_output_dir = _resolve_under_workspace(ctx, str(output_dir))

    # Detect-driven mode (preferred): reuse the detect_layout pass so every crop is
    # key-bound to a detected/layout element + the background is covered. No VLM.
    if detect_arg is not None:
        detect_path = _resolve_under_workspace(ctx, str(detect_arg))
        if detect_path is None or not detect_path.is_file():
            raise ToolError(
                "INPUT_FILE_NOT_FOUND",
                f"`detect` file not found: {detect_arg}. Run detect_layout first, "
                f"then pass its design/<scene>.detect.json here for detect-driven crops.",
            )
        return concept_ground.crop_from_detect(
            image_path=resolved_input,
            detect_path=detect_path,
            out_dir=resolved_output_dir,
            padding=float(args.get("padding", 0.02)),
            min_area_frac=float(args.get("min_area_frac", 0.0003)),
            add_background=bool(args.get("add_background", True)),
            source_ref=source_ref,
            detect_ref=detect_ref,
            out_dir_ref=out_dir_ref,
        )

    elements = args.get("elements")
    if isinstance(elements, str):
        elements = [elements]
    vlm_model = str(args.get("model", iterate_vlm.DEFAULT_VLM_MODEL))
    vlm_max_tokens = _positive_int_arg(
        args,
        "vlm_max_tokens",
        ctx.config.assets.vlm.image_max_tokens,
    )
    result = concept_ground.ground_and_crop(
        ctx.config,
        image_path=resolved_input,
        out_dir=resolved_output_dir,
        elements=[str(e) for e in elements] if elements else None,
        model=vlm_model,
        padding=float(args.get("padding", 0.04)),
        min_area_frac=float(args.get("min_area_frac", 0.0008)),
        max_elements=_positive_int_arg(args, "max_elements", 24),
        vlm_max_tokens=vlm_max_tokens,
        add_background=bool(args.get("add_background", True)),
        vlm_timeout=float(
            args.get("vlm_timeout", ctx.config.assets.vlm.image_timeout_seconds)
        ),
        source_ref=source_ref,
        out_dir_ref=out_dir_ref,
    )
    return result


# -------------------- detect_layout --------------------


def _detect_layout(args: dict[str, Any], ctx: ToolCtx) -> dict[str, Any]:
    input_path = args.get("input")
    if not input_path:
        raise ToolError("INVALID_CONFIG", "`input` (concept mockup path) is required.")
    output = args.get("output")
    input_ref = _concept_path_ref(ctx, "input", input_path)
    output_ref = (
        _concept_path_ref(ctx, "output", output)
        if output
        else (
            Path(input_ref).parent / f"{Path(input_ref).stem}.detect.json"
        ).as_posix()
    )
    _validate_detect_layout_paths(input_ref=input_ref, output_ref=output_ref)
    canonical_output = validated_workspace_root(ctx.state.workspace) / output_ref
    if canonical_output.exists() and not canonical_output.is_file():
        raise ToolError(
            "INVALID_PATH",
            f"detect_layout output is not a file path: {output_ref}.",
        )
    canvas: tuple[int, int] | None = None
    cw, ch = args.get("canvas_width"), args.get("canvas_height")
    if (cw is None) != (ch is None):
        raise ToolError(
            "INVALID_CONFIG",
            "`canvas_width` and `canvas_height` must be supplied together.",
        )
    if cw is not None:
        canvas = (
            _positive_int_arg(args, "canvas_width", 0),
            _positive_int_arg(args, "canvas_height", 0),
        )
    vlm_model = str(args.get("model", iterate_vlm.DEFAULT_VLM_MODEL))
    vlm_max_tokens = _positive_int_arg(
        args,
        "vlm_max_tokens",
        ctx.config.assets.vlm.detect_max_tokens,
    )
    result = detect_layout.detect_layout(
        ctx.config,
        image_path=_resolve_under_workspace(ctx, str(input_path)),
        out_path=_resolve_under_workspace(ctx, str(output) if output else output_ref),
        scene=str(args["scene"]) if args.get("scene") else None,
        canvas=canvas,
        model=vlm_model,
        min_area_frac=float(args.get("min_area_frac", 0.0003)),
        max_elements=_positive_int_arg(args, "max_elements", 32),
        vlm_max_tokens=vlm_max_tokens,
        vlm_timeout=float(
            args.get("vlm_timeout", ctx.config.assets.vlm.image_timeout_seconds)
        ),
        source_ref=input_ref,
        out_path_ref=output_ref,
    )
    return result


# -------------------- registry --------------------


TOOLS = [
    ToolDef(
        name="fit_image_to_resolution",
        description=(
            "Resize a concept mockup to an EXACT W×H. DESIGN MOCKUPS ONLY "
            "(design/**) — NEVER run on assets/** (it center-pads to the box and "
            "flattens alpha, baking opaque letterbox bars into the asset; the call "
            "is rejected). gpt-image-2 takes flexible sizes (snapped to /16 via "
            "normalize_size), so prefer generating the mockup at the game "
            "resolution directly; use this for the exact final size. mode: contain "
            "(default — pad; transparent for RGBA sources, never crops content), "
            "cover (scale + center-crop, can drop top/bottom), stretch (distort). "
            "Returns {output, width, height, mode, source_size}."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "input": {"type": "string", "description": "Concept mockup path."},
                "output": {
                    "type": "string",
                    "description": (
                        "Output PNG path. Relative paths resolve under the open "
                        "workspace; absolute paths are honored as-is."
                    ),
                },
                "width": {"type": "integer"},
                "height": {"type": "integer"},
                "mode": {
                    "type": "string",
                    "enum": ["cover", "contain", "stretch"],
                    "default": "contain",
                },
            },
            "required": ["input", "output", "width", "height"],
            "additionalProperties": False,
        },
        handler=_fit_image_to_resolution,
    ),
    ToolDef(
        name="ground_crop",
        description=(
            "Crop a concept mockup into per-element reference sub-images (art seeds "
            "for assets-agent's `ref_image` production tools) under `output_dir`; writes "
            "crops.json. TWO modes:\n"
            "• DETECT-DRIVEN (preferred) — pass `detect` = the design/<scene>.detect.json "
            "from detect_layout. Deterministic (no VLM): crops EVERY detected element "
            "by its box, reusing the SAME key (crop label == detect key == §2.0.2 "
            "layout key, a real binding) and ALWAYS adds a full-mockup `background` "
            "crop for explicit coverage/reference accounting. It seeds §2C only when "
            "clean enough; otherwise design waives it as reference-only. Run detect_layout first.\n"
            "• VLM-GROUNDED (legacy) — omit `detect`: a separate VLM grounding invents "
            "its own labels (use `elements` to constrain, or omit to auto-detect); "
            "labels are NOT bound to layout keys. It now emits the same crop fields "
            "as detect-driven mode and honors `add_background`.\n"
            "Returns {image, source_sha256, detect, detection_sha256, found, "
            "missed, crops, manifest, n_found, n_grounded, n_rejected}. Synthetic "
            "background contributes to n_found but never n_grounded. Detect-driven "
            "crops fail if the mockup no longer matches the source SHA-256 recorded "
            "by detect_layout; crops.json records both the mockup and exact "
            "detection SHA-256. Manifest paths are stable, workspace-relative "
            "paths, and every crop entry includes the SHA-256 of its exact PNG. "
            "Each crop's `box_px` is a source-mockup art/visual-diff frame, NOT an "
            "in-game size/position."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "input": {"type": "string", "description": "Concept mockup path."},
                "output_dir": {
                    "type": "string",
                    "description": (
                        "Dedicated owned directory exactly at "
                        "design/crops/<scene-or-screen>; writes crops + crops.json."
                    ),
                },
                "detect": {
                    "type": "string",
                    "description": (
                        "PREFERRED: path to design/<scene>.detect.json (from "
                        "detect_layout). Enables detect-driven, key-bound cropping "
                        "(+ full-mockup background); no VLM. Omit for legacy VLM grounding."
                    ),
                },
                "add_background": {
                    "type": "boolean",
                    "default": True,
                    "description": (
                        "Both modes: add a full-mockup `background` crop after at "
                        "least one usable non-background element is grounded. This "
                        "is not a background-only waiver."
                    ),
                },
                "elements": {
                    "type": ["array", "string"],
                    "items": {"type": "string"},
                    "description": "Legacy VLM mode only: element labels to locate. Omit to auto-detect all.",
                },
                "model": {"type": "string"},
                "padding": {
                    "type": "number",
                    "minimum": 0,
                    "maximum": 1,
                    "default": 0.02,
                },
                "min_area_frac": {
                    "type": "number",
                    "minimum": 0,
                    "maximum": 1,
                    "default": 0.0003,
                },
                "max_elements": {"type": "integer", "minimum": 1, "default": 24},
                "vlm_max_tokens": {"type": "integer", "minimum": 1},
                "vlm_timeout": {"type": "number", "exclusiveMinimum": 0},
            },
            "required": ["input", "output_dir"],
            "additionalProperties": False,
        },
        handler=_ground_crop,
    ),
    ToolDef(
        name="detect_layout",
        description=(
            "POSITION DETECTION on a concept mockup (the lays-out-like axis) — "
            "distinct from `ground_crop`. Runs a precision VLM pass that returns a "
            "TIGHT, canvas-normalized box + role + suggested anchor/origin/kind for "
            "EVERY visible element (HUD widgets, lettering, actors, props, "
            "decorative regions). Crops NOTHING. Writes `design/<scene>.detect.json` "
            "(next to the mockup unless `output` is given). This is the raw "
            "measurement the design-agent turns into the GDD §2.0.2 Scene Layout "
            "Design (then scaffold -> layout-contract.json -> gateLayoutAlignment). "
            "The record binds its workspace-relative source path to the mockup's "
            "SHA-256. `canvas_width` and `canvas_height` are positive and must be "
            "provided together. "
            "Returns {image, source_sha256, scene, out_path, canvas, n_detected, "
            "n_rejected, elements:[{key, role, box:[x,y,w,h] (0..1), "
            "box_canvas_px, center, origin, anchorTo, kind}]}. "
            "`box_canvas_px` is canvas-space convenience for human review only; "
            "programmatic consumers must use normalized `box`. Use "
            "`ground_crop` (NOT this) when you need an art-seed crop PNG."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "input": {"type": "string", "description": "Concept mockup path (design/<scene>.png)."},
                "output": {
                    "type": "string",
                    "description": (
                        "Optional explicit output path. Defaults to "
                        "`<input_dir>/<scene>.detect.json` (resolves under workspace)."
                    ),
                },
                "scene": {
                    "type": "string",
                    "description": "Scene name for the record (defaults to the mockup file stem).",
                },
                "canvas_width": {
                    "type": "integer",
                    "minimum": 1,
                    "description": "§1 game canvas width (for box_canvas_px convenience). Defaults to the mockup width.",
                },
                "canvas_height": {
                    "type": "integer",
                    "minimum": 1,
                    "description": "§1 game canvas height. Defaults to the mockup height.",
                },
                "model": {"type": "string"},
                "min_area_frac": {
                    "type": "number",
                    "minimum": 0,
                    "maximum": 1,
                    "default": 0.0003,
                },
                "max_elements": {"type": "integer", "minimum": 1, "default": 32},
                "vlm_max_tokens": {
                    "type": "integer",
                    "minimum": 1,
                    "description": (
                        "Override the layout-detection budget. Defaults to "
                        "`assets.vlm.detect_max_tokens` from gca.config.yaml."
                    ),
                },
                "vlm_timeout": {"type": "number", "exclusiveMinimum": 0},
            },
            "required": ["input"],
            "dependentRequired": {
                "canvas_width": ["canvas_height"],
                "canvas_height": ["canvas_width"],
            },
            "additionalProperties": False,
        },
        handler=_detect_layout,
    ),
]


def _guard_build_mutation(handler):
    @wraps(handler)
    def guarded(args: dict[str, Any], ctx: ToolCtx):
        if ctx.state.workspace is None:
            raise ToolError("NO_WORKSPACE_OPEN", "No active workspace.")
        workspace = ctx.state.workspace
        with build_mutation_flock(validated_workspace_root(workspace)):
            if not all(
                callable(getattr(workspace, name, None))
                for name in (
                    "copy_from_pinned",
                    "commit_to_pinned",
                    "commit_tree_to_pinned",
                    "assert_identity",
                )
            ):
                return handler(args, ctx)
            with tempfile.TemporaryDirectory(prefix="gca-concept-") as temp:
                stage_root = Path(temp).resolve()
                staged_args, commits, aliases, canonical_refs = _stage_concept_args(
                    handler.__name__,
                    args,
                    ctx,
                    stage_root,
                )
                setattr(ctx, "_concept_staging_root", stage_root)
                setattr(ctx, "_concept_canonical_refs", canonical_refs)
                try:
                    result = handler(staged_args, ctx)
                finally:
                    delattr(ctx, "_concept_staging_root")
                    delattr(ctx, "_concept_canonical_refs")
                workspace.assert_identity()
                _rewrite_staged_json_paths(commits, aliases, validated_workspace_root(workspace))
                for source, relative, is_tree in commits:
                    if is_tree:
                        if not source.is_dir():
                            raise ToolError(
                                "OUTPUT_NOT_FOUND",
                                f"Concept output directory was not produced: {source}.",
                            )
                        workspace.commit_tree_to_pinned(source, relative)
                    else:
                        if not source.is_file():
                            raise ToolError(
                                "OUTPUT_NOT_FOUND",
                                f"Concept output file was not produced: {source}.",
                            )
                        workspace.commit_to_pinned(source, relative)
                workspace.assert_identity()
                return _canonicalize_staged_result(result, aliases)

    return guarded


def _concept_relative(ctx: ToolCtx, value: Any) -> Path:
    root = validated_workspace_root(ctx.state.workspace)
    raw = Path(str(value)).expanduser()
    resolved = (raw if raw.is_absolute() else root / raw).resolve()
    if not resolved.is_relative_to(root):
        raise ToolError("INVALID_PATH", f"Concept path escapes workspace: {resolved}.")
    return resolved.relative_to(root)


def _concept_path_ref(ctx: ToolCtx, key: str, value: Any) -> str:
    canonical_refs = getattr(ctx, "_concept_canonical_refs", None)
    if isinstance(canonical_refs, dict) and key in canonical_refs:
        return canonical_refs[key]
    return _concept_relative(ctx, value).as_posix()


def _stage_concept_args(
    handler_name: str,
    args: dict[str, Any],
    ctx: ToolCtx,
    stage_root: Path,
) -> tuple[
    dict[str, Any],
    list[tuple[Path, Path, bool]],
    dict[str, str],
    dict[str, str],
]:
    workspace = ctx.state.workspace
    root = validated_workspace_root(workspace)
    staged = dict(args)
    commits: list[tuple[Path, Path, bool]] = []
    aliases: dict[str, str] = {}
    canonical_refs: dict[str, str] = {}
    input_relatives: dict[str, Path] = {}
    inputs = ["input"]
    if handler_name == "_ground_crop" and args.get("detect") is not None:
        inputs.append("detect")
    output_files = ["output"] if handler_name in {
        "_fit_image_to_resolution",
        "_detect_layout",
    } and args.get("output") else []
    output_dirs = ["output_dir"] if handler_name == "_ground_crop" else []

    for key in inputs:
        relative = _concept_relative(ctx, args[key])
        input_relatives[key] = relative
        canonical_refs[key] = relative.as_posix()
        target = stage_root / "inputs" / key / relative
        workspace.copy_from_pinned(relative, target)
        staged[key] = str(target)
        aliases[str(target)] = str(root / relative)
    for key in output_files:
        relative = _concept_relative(ctx, args[key])
        target = stage_root / "outputs" / key / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        canonical_refs[key] = relative.as_posix()
        staged[key] = str(target)
        commits.append((target, relative, False))
        aliases[str(target)] = str(root / relative)
    for key in output_dirs:
        relative = _concept_relative(ctx, args[key])
        target = stage_root / "outputs" / key
        target.mkdir(parents=True, exist_ok=True)
        canonical_refs[key] = relative.as_posix()
        staged[key] = str(target)
        commits.append((target, relative, True))
        aliases[str(target)] = str(root / relative)
    if handler_name == "_detect_layout" and not args.get("output"):
        source = input_relatives["input"]
        relative = source.parent / f"{source.stem}.detect.json"
        target = stage_root / "outputs" / "output" / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        canonical_refs["output"] = relative.as_posix()
        staged["output"] = str(target)
        commits.append((target, relative, False))
        aliases[str(target)] = str(root / relative)
    return staged, commits, aliases, canonical_refs


def _rewrite_staged_json_paths(
    commits: list[tuple[Path, Path, bool]],
    aliases: dict[str, str],
    workspace_root: Path,
) -> None:
    """Remove temporary staging paths from JSON before committing evidence."""
    relative_aliases: dict[str, str] = {}
    root = workspace_root.resolve()
    for staged, canonical in aliases.items():
        canonical_path = Path(canonical).resolve()
        if canonical_path.is_relative_to(root):
            relative_aliases[staged] = canonical_path.relative_to(root).as_posix()

    ordered_aliases = sorted(
        relative_aliases.items(),
        key=lambda item: -len(item[0]),
    )

    def convert(value: Any) -> Any:
        if isinstance(value, dict):
            return {key: convert(entry) for key, entry in value.items()}
        if isinstance(value, list):
            return [convert(entry) for entry in value]
        if isinstance(value, str):
            for staged, canonical in ordered_aliases:
                if value == staged or value.startswith(staged + "/"):
                    return canonical + value[len(staged) :]
        return value

    json_paths: list[Path] = []
    for source, _relative, is_tree in commits:
        if is_tree and source.is_dir():
            json_paths.extend(source.rglob("*.json"))
        elif source.suffix == ".json" and source.is_file():
            json_paths.append(source)
    for path in json_paths:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ToolError(
                "GROUNDING_FAILED",
                f"Concept evidence JSON is not readable before commit: {path}: {exc}",
            ) from exc
        path.write_text(json.dumps(convert(payload), indent=2), encoding="utf-8")


def _canonicalize_staged_result(value: Any, aliases: dict[str, str]) -> Any:
    """Replace transient staging paths in tool results with canonical paths."""
    ordered_aliases = sorted(aliases.items(), key=lambda item: -len(item[0]))

    def convert(item: Any) -> Any:
        if isinstance(item, dict):
            return {key: convert(entry) for key, entry in item.items()}
        if isinstance(item, list):
            return [convert(entry) for entry in item]
        if isinstance(item, tuple):
            return tuple(convert(entry) for entry in item)
        if isinstance(item, str):
            for staged, canonical in ordered_aliases:
                if item == staged or item.startswith(staged + "/"):
                    return canonical + item[len(staged) :]
        return item

    return convert(value)


TOOLS = [
    ToolDef(
        name=tool.name,
        description=tool.description,
        input_schema=tool.input_schema,
        handler=_guard_build_mutation(tool.handler),
        requires_workspace=tool.requires_workspace,
    )
    for tool in TOOLS
]
