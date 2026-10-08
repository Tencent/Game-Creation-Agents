"""Tower-defense path/layout tools — render a soft corridor mask from the
gameplay-authoritative ``map-layout.json`` so a terrain-only map base can echo
the road network without baking it.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..adapters import map_layout_mask
from ..errors import ToolError
from ..server import ToolCtx, ToolDef
from ..verify_epoch import build_mutation_flock
from ..workspace import validated_workspace_root


def _resolve_under_workspace(ctx: ToolCtx, p: str | None) -> Path | None:
    """Resolve one path inside the active workspace, rejecting symlink escapes."""
    if not p:
        return None
    if ctx.state.workspace is None:
        raise ToolError("NO_WORKSPACE_OPEN", "No active workspace.")
    root = validated_workspace_root(ctx.state.workspace)
    path = Path(str(p)).expanduser()
    resolved = (path if path.is_absolute() else root / path).resolve()
    if not resolved.is_relative_to(root):
        raise ToolError(
            "INVALID_PATH",
            f"Path escapes the active workspace: {resolved}",
        )
    return resolved


def _render_layout_mask(args: dict[str, Any], ctx: ToolCtx) -> dict[str, Any]:
    layout_arg = args.get("layout")
    if not layout_arg:
        raise ToolError(
            "INVALID_CONFIG",
            "`layout` is required (path to levels/map-layout.json).",
        )
    output = args.get("output")
    if not output:
        raise ToolError("INVALID_CONFIG", "`output` is required (mask PNG path).")

    layout_path = _resolve_under_workspace(ctx, str(layout_arg))
    if layout_path is None or not layout_path.is_file():
        raise ToolError(
            "INPUT_FILE_NOT_FOUND",
            f"map-layout.json not found: {layout_path}",
        )
    try:
        layout = json.loads(layout_path.read_text(encoding="utf-8"))
    except Exception as e:  # noqa: BLE001
        raise ToolError(
            "INVALID_CONFIG", f"Cannot parse {layout_path}: {e}"
        ) from e

    out_path = _resolve_under_workspace(ctx, str(output))
    assert out_path is not None
    workspace_root = validated_workspace_root(ctx.state.workspace)
    relative_output = out_path.relative_to(workspace_root)
    if not relative_output.parts or relative_output.parts[0] not in {
        "assets",
        "design",
    }:
        raise ToolError(
            "INVALID_PATH",
            "Layout-mask output must be under workspace assets/** or design/**.",
        )
    with build_mutation_flock(workspace_root):
        return map_layout_mask.render_layout_mask(
            layout,
            out_path,
            road_value=int(args.get("road_value", 150)),
            bg_value=int(args.get("bg_value", 24)),
            blur=float(args.get("blur", 6.0)),
            draw_markers=bool(args.get("draw_markers", True)),
        )


TOOLS = [
    ToolDef(
        name="render_layout_mask",
        description=(
            "TOWER DEFENSE — render a soft path-corridor mask PNG from a "
            "gameplay-authoritative `levels/map-layout.json` (road polylines + "
            "core/spawn/build-slot markers). Feed the result to "
            "`edit_image(input=<mask>)` so a TERRAIN-ONLY map base echoes where "
            "the road sits (worn ground / energy seams) WITHOUT baking a literal "
            "road. This is a soft hint only — the game code renders the "
            "authoritative road/props from the same layout, so gameplay alignment "
            "does not depend on how faithfully the model follows the mask. "
            "Relative paths resolve under the open workspace."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "layout": {
                    "type": "string",
                    "description": "Path to map-layout.json (default location levels/map-layout.json).",
                },
                "output": {
                    "type": "string",
                    "description": "Output mask PNG path (relative resolves under the workspace).",
                },
                "road_value": {
                    "type": "integer",
                    "default": 150,
                    "description": "Road corridor gray intensity (0-255).",
                },
                "bg_value": {
                    "type": "integer",
                    "default": 24,
                    "description": "Background gray intensity (0-255).",
                },
                "blur": {
                    "type": "number",
                    "default": 6.0,
                    "description": "Gaussian blur radius (px) — softens the corridor so it stays a hint.",
                },
                "draw_markers": {
                    "type": "boolean",
                    "default": True,
                    "description": "Also mark core / spawns / fork nodes / build slots.",
                },
            },
            "required": ["layout", "output"],
            "additionalProperties": False,
        },
        handler=_render_layout_mask,
        requires_workspace=True,
    ),
]
