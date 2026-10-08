"""Post-processing tools: preview composition, seam fixing, and image matting.

Each tool is a thin wrapper around the bundled Python scripts under
``gca_mcp/runners/assets/`` — they're invoked via subprocess with the
same CLI signature, but with paths resolved against the open workspace
and JSON-friendly result reporting.

Also hosts ``rmbg2_matting`` and deterministic ``color_key_matting``.

The CPU-only ``postprocess`` subcommands never remove backgrounds. Use
``rmbg2_matting`` or ``color_key_matting`` for still-image transparency, and
``rmbg2_video_matting`` for videos.
"""

from __future__ import annotations

import hashlib
import json
from functools import wraps
from pathlib import Path
from typing import Any

from ..adapters import rmbg2 as rmbg2_adapter
from ..adapters.script_runner import run_python_script, runner_path
from ..errors import ToolError
from ..evidence import atomic_write_json
from ..runners.assets.color_key import validate_settings
from ..server import ToolCtx, ToolDef
from ..verify_epoch import build_mutation_flock
from ..workspace import validated_workspace_root
from .asset_tools import _resolve_asset_input, _resolve_asset_output


def _resolve_input(ctx: ToolCtx, p: str | None) -> str | None:
    if p is None:
        return None
    return str(_resolve_asset_input(ctx, str(p)))


def _resolve_output(ctx: ToolCtx, p: str | None) -> str | None:
    if p is None:
        return None
    return str(_resolve_asset_output(ctx, str(p)))


def _push_flag(out: list[str], key: str, value: Any) -> None:
    if value is None:
        return
    if isinstance(value, bool):
        if value:
            out.append(f"--{key}")
        return
    out.extend([f"--{key}", str(value)])


# -------------------- postprocess (sub-commands) --------------------


def _sha256_file(path: Path) -> str | None:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def _reseal_postprocessed_asset(
    output_path: Path,
    *,
    command: str,
    pre_sha256: str | None,
) -> dict[str, Any] | None:
    """Carry a static-asset seal across an in-pipeline transform.

    ``make-seamless`` rewrites the bytes of an already-sealed runtime asset. That
    breaks ``artifact_sha256`` while leaving provenance true: the pixels came from
    the same generation and were then changed by a known, deterministic step of
    this pipeline. Treating that as a broken seal left no way forward, because the
    only other writer of a valid seal is ``produce_static_asset``, which generates
    from a prompt rather than sealing bytes. Regenerating produced an image that
    still needed the same transform, so the loop never converged and its only exit
    was shipping a visible seam.

    The seal is refreshed rather than reissued: hash and dimensions catch up to the
    shipped bytes, the prior state joins ``lineage``, and provenance stays as it
    was, so the asset still traces to its original generation with the transform
    recorded in between.

    Only a seal that provably described these bytes beforehand is touched --
    ``pre_sha256`` must match what the sidecar recorded. Anything else is left
    alone, so this cannot give a foreign PNG generated provenance.
    """
    if pre_sha256 is None:
        return None
    sidecar = output_path.with_suffix(".asset.json")
    if not sidecar.is_file():
        return None
    try:
        metadata = json.loads(sidecar.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(metadata, dict) or metadata.get("artifact_sha256") != pre_sha256:
        return None

    post_sha256 = _sha256_file(output_path)
    if post_sha256 is None or post_sha256 == pre_sha256:
        return None

    try:
        from PIL import Image

        with Image.open(output_path) as im:
            width, height = im.size
    except Exception:  # noqa: BLE001 - an unreadable output is the runner's failure
        return None

    lineage = metadata.get("lineage")
    if not isinstance(lineage, list):
        lineage = []
    lineage.append(
        {
            "artifact": metadata.get("artifact"),
            "sha256": pre_sha256,
            "transform": command,
        }
    )

    metadata["artifact_sha256"] = post_sha256
    metadata["width"] = width
    metadata["height"] = height
    metadata["lineage"] = lineage
    # Written after the PNG so the finalizer's freshness check sees a sidecar at
    # least as new as the artifact it describes.
    atomic_write_json(sidecar, metadata)
    return {
        "sidecar": sidecar.name,
        "previous_sha256": pre_sha256,
        "artifact_sha256": post_sha256,
        "width": width,
        "height": height,
    }


def _postprocess(args: dict[str, Any], ctx: ToolCtx) -> dict[str, Any]:
    """Dispatch to ``postprocess.py {command}`` with the right flags.

    ``command`` ∈ {compose-preview, make-seamless}.
    """
    cmd = args.get("command")
    if cmd not in ("compose-preview", "make-seamless"):
        raise ToolError(
            "INVALID_CONFIG",
            f"Unknown postprocess command: {cmd!r}",
            hint="One of compose-preview / make-seamless.",
        )

    script = runner_path("assets", "postprocess.py")
    cli: list[str] = [cmd]

    # Command inputs are read-only; all generated files stay under assets/design.
    for key in ("input", "base", "placements"):
        v = args.get(key)
        if v is None:
            continue
        flag = key.replace("_", "-")
        cli.extend([f"--{flag}", _resolve_input(ctx, str(v))])
    for key in ("output", "report"):
        v = args.get(key)
        if v is None:
            continue
        flag = key.replace("_", "-")
        cli.extend([f"--{flag}", _resolve_output(ctx, str(v))])

    # Pass-through scalar flags for supported CPU-only subcommands.
    passthrough = {"feather"}
    for key in passthrough:
        if args.get(key) is not None:
            _push_flag(cli, key.replace("_", "-"), args.get(key))

    boolean_flags = {"both_axes"}
    for key in boolean_flags:
        if args.get(key):
            cli.append(f"--{key.replace('_', '-')}")

    # Read the artifact's state before the runner rewrites it; afterwards there is
    # no way to tell an intact seal from one that was already stale.
    output_arg = args.get("output")
    output_path = (
        Path(_resolve_output(ctx, str(output_arg))) if output_arg is not None else None
    )
    pre_sha256 = (
        _sha256_file(output_path)
        if output_path is not None and output_path.is_file()
        else None
    )

    proc = run_python_script(script, cli, check=False)
    if proc["returncode"] != 0:
        raise ToolError(
            "POSTPROCESS_FAILED",
            f"postprocess {cmd} failed.",
            hint=proc["stderr"][-500:],
        )
    result: dict[str, Any] = {
        "command": cmd,
        "output": output_arg,
        "stdout": proc["stdout"][-2000:],
    }
    if output_path is not None:
        resealed = _reseal_postprocessed_asset(
            output_path,
            command=cmd,
            pre_sha256=pre_sha256,
        )
        if resealed is not None:
            result["resealed"] = resealed
    return result


# -------------------- color_key_matting --------------------


def _color_key_matting(args: dict[str, Any], ctx: ToolCtx) -> dict[str, Any]:
    """Pure-computation safe-color matte (RMBG-2.0-free fallback)."""
    defaults = ctx.config.assets.matting
    key_color = args.get("key_color", ctx.config.assets.image.safe_bg_color)
    threshold = args.get("threshold", defaults.color_key_threshold)
    edge_threshold = args.get(
        "edge_threshold",
        defaults.color_key_edge_threshold,
    )
    try:
        validate_settings(key_color, threshold, edge_threshold)
    except ValueError as exc:
        raise ToolError(
            "INVALID_CONFIG",
            f"Invalid color_key_matting settings: {exc}",
        ) from exc
    script = runner_path("assets", "color_key.py")
    cli: list[str] = []
    _push_flag(cli, "input", _resolve_input(ctx, args.get("input")))
    _push_flag(cli, "output", _resolve_output(ctx, args.get("output")))
    _push_flag(cli, "key-color", key_color)
    _push_flag(cli, "threshold", threshold)
    _push_flag(cli, "edge-threshold", edge_threshold)

    proc = run_python_script(script, cli, check=False)
    if proc["returncode"] != 0:
        raise ToolError(
            "COLOR_KEY_MATTING_FAILED",
            f"color_key_matting failed for {args.get('input')}.",
            hint=proc["stderr"][-500:],
        )
    output = proc["stdout"].strip().splitlines()[-1] if proc["stdout"].strip() else None
    return {"output": output or args.get("output")}




# -------------------- rmbg2_matting --------------------


def _resolve_input_path(ctx: ToolCtx, p: str | None) -> Path | None:
    if p is None:
        return None
    raw = _resolve_input(ctx, p)
    return Path(raw) if raw is not None else None


def _resolve_output_path(ctx: ToolCtx, p: str | None) -> Path | None:
    if p is None:
        return None
    raw = _resolve_output(ctx, p)
    return Path(raw) if raw is not None else None


def _rmbg2_matting(args: dict[str, Any], ctx: ToolCtx) -> dict[str, Any]:
    """Single-image or batch background removal via remote RMBG-2.0."""
    if args.get("input") and args.get("input_dir"):
        raise ToolError(
            "INVALID_CONFIG",
            "Specify exactly one of `input` or `input_dir`.",
        )
    if not args.get("input") and not args.get("input_dir"):
        raise ToolError(
            "INVALID_CONFIG",
            "One of `input` (single image) or `input_dir` (batch) is required.",
        )

    defaults = ctx.config.assets.rmbg
    result = rmbg2_adapter.remove_background(
        ctx.config,
        input_path=_resolve_input_path(ctx, args.get("input")),
        output=_resolve_output_path(ctx, args.get("output")),
        input_dir=_resolve_input_path(ctx, args.get("input_dir")),
        output_dir=_resolve_output_path(ctx, args.get("output_dir")),
        endpoint=args.get("endpoint"),
        bg_hex=args.get("bg_hex"),
        aug_preset=str(args.get("aug_preset", defaults.aug_preset)),
        aug_hard=args.get("aug_hard"),
        aug_soft=args.get("aug_soft"),
        rmbg_fg_cutoff=int(args.get("rmbg_fg_cutoff", defaults.rmbg_fg_cutoff)),
        max_size=args.get("max_size"),
        max_workers=int(args.get("max_workers", defaults.workers)),
        timeout=int(args.get("timeout", defaults.timeout_seconds)),
    )
    return result


TOOLS = [
    ToolDef(
        name="postprocess",
        description=(
            "CPU-only asset utility. `command` selects the sub-task: "
            "compose-preview (layered preview from base + placements), "
            "make-seamless (offset-blend a middle/repeatable tile so TileSprite "
            "repeats it without a visible seam). It does NOT remove backgrounds "
            "or split grids — use rmbg2_matting or color_key_matting for still "
            "transparency and the i2v / produce_static_asset pipelines to "
            "generate individual assets."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "enum": ["compose-preview", "make-seamless"],
                },
                "input": {"type": "string"},
                "output": {"type": "string"},
                "base": {"type": "string"},
                "placements": {"type": "string"},
                "report": {"type": "string"},
                "feather": {
                    "type": "integer",
                    "description": "make-seamless: cross-fade band half-width (px) across the seam. Default 16.",
                },
                "both_axes": {
                    "type": "boolean",
                    "description": "make-seamless: also make vertical edges seamless (default: horizontal only).",
                },
            },
            "required": ["command"],
            "additionalProperties": False,
        },
        handler=_postprocess,
        requires_workspace=True,
    ),
    ToolDef(
        name="color_key_matting",
        description=(
            "Pure-computation background removal for #FF00FF or #00F000 staging "
            "backgrounds — a deterministic CPU-only fallback for environments "
            "with NO RMBG-2.0 deployment. No model, no API. Clears pixels near "
            "the selected key globally, then flood-fills background-connected "
            "key pixels from the image borders, preserving near-key detail inside the "
            "subject. RMBG-2.0 (`rmbg2_matting`) is the default and produces "
            "cleaner soft-alpha edges — prefer it whenever a local or remote "
            "endpoint is available. The tool fails when no key pixels are removed "
            "or the whole foreground disappears. Set "
            "`assets.matting.backend: color_key` to make produce_static_asset use "
            "this route by default."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "input": {
                    "type": "string",
                    "description": "Input image on a supported solid key background.",
                },
                "output": {
                    "type": "string",
                    "description": "Output RGBA PNG path.",
                },
                "key_color": {
                    "type": "string",
                    "enum": ["#FF00FF", "#00F000"],
                    "description": (
                        "Staging background color. Defaults to "
                        "assets.image.safe_bg_color."
                    ),
                },
                "threshold": {
                    "type": "integer",
                    "minimum": 0,
                    "maximum": 255,
                    "description": (
                        "Global key-color distance threshold; 0 disables the "
                        "global pass. Defaults to "
                        "assets.matting.color_key_threshold."
                    ),
                },
                "edge_threshold": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 255,
                    "description": (
                        "Border flood-fill key-color threshold; must be >= "
                        "threshold. Defaults to "
                        "assets.matting.color_key_edge_threshold."
                    ),
                },
            },
            "required": ["input", "output"],
            "additionalProperties": False,
        },
        handler=_color_key_matting,
        requires_workspace=True,
    ),
    ToolDef(
        name="rmbg2_matting",
        description=(
            "AI background removal via the remote RMBG-2.0 service. Returns "
            "a lossless soft-alpha RGBA PNG. This is the preferred background "
            "removal method; color_key_matting is the deterministic still-image "
            "fallback when no RMBG-2.0 deployment is available. "
            "Set `bg_hex` (e.g. '#FF00FF') with `aug_preset` to enable "
            "color-aug rescue: pixels far from the safe background color "
            "are forced to foreground, recovering particle effects RMBG "
            "would otherwise clip. Pass `input` for a single image, or "
            "`input_dir` for batch (parallelised across `max_workers` "
            "threads). The service endpoint defaults to `RMBG_BASE_URL` env var."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "input": {
                    "type": "string",
                    "description": "Single input image path (PNG/JPG/WebP).",
                },
                "output": {
                    "type": "string",
                    "description": (
                        "Output RGBA PNG. Defaults to "
                        "`<input_stem>_matted.png` next to the input."
                    ),
                },
                "input_dir": {
                    "type": "string",
                    "description": "Batch input directory.",
                },
                "output_dir": {
                    "type": "string",
                    "description": (
                        "Batch output directory. Defaults to "
                        "`<input_dir>_matted/`."
                    ),
                },
                "endpoint": {
                    "type": "string",
                    "description": "Override RMBG_BASE_URL for this call.",
                },
                "bg_hex": {
                    "type": "string",
                    "description": (
                        "Safe background color for color-aug rescue, e.g. "
                        "'#FF00FF'. When set, `aug_preset` (or explicit "
                        "`aug_hard`/`aug_soft`) takes effect."
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
                        "Color-aug intensity preset. `off` = no rescue "
                        "at all (pure RMBG, zero risk of bg residue) "
                        "and is the safest default. `nearly-off` "
                        "(130/170) barely touches RMBG; lower presets "
                        "rescue more particles but risk pulling in "
                        "bg-adjacent pixels."
                    ),
                },
                "aug_hard": {
                    "type": "integer",
                    "description": (
                        "Manual override for preset's key_hard. Must be "
                        "set together with `aug_soft`."
                    ),
                },
                "aug_soft": {
                    "type": "integer",
                    "description": "Manual override for preset's key_soft.",
                },
                "rmbg_fg_cutoff": {
                    "type": "integer",
                    "default": 230,
                    "description": (
                        "RMBG soft→hard alpha cutoff (0-255). Higher = "
                        "stricter foreground = less bg-color residue at "
                        "the silhouette edge."
                    ),
                },
                "max_size": {
                    "type": "integer",
                    "description": "Cap output longest-side. Default: original.",
                },
                "max_workers": {
                    "type": "integer",
                    "default": 8,
                    "description": "Thread pool size for batch mode.",
                },
                "timeout": {
                    "type": "integer",
                    "default": 60,
                    "description": "Per-image HTTP timeout in seconds.",
                },
            },
            "additionalProperties": False,
        },
        handler=_rmbg2_matting,
        requires_workspace=True,
    ),
]


def _guard_build_mutation(handler):
    @wraps(handler)
    def guarded(args: dict[str, Any], ctx: ToolCtx):
        if ctx.state.workspace is None:
            raise ToolError("NO_WORKSPACE_OPEN", "No active workspace.")
        with build_mutation_flock(validated_workspace_root(ctx.state.workspace)):
            return handler(args, ctx)

    return guarded


TOOLS = [
    ToolDef(
        name=tool.name,
        description=tool.description,
        input_schema=tool.input_schema,
        handler=(
            _guard_build_mutation(tool.handler)
            if tool.name in {"postprocess", "color_key_matting", "rmbg2_matting"}
            else tool.handler
        ),
        requires_workspace=tool.requires_workspace,
    )
    for tool in TOOLS
]
