"""Render a soft path-corridor mask from a tower-defense ``map-layout.json``.

The mask is a **soft hint** for ``edit_image`` (gpt-image-2): it shows where
the road network sits so the generated *terrain* can echo it (worn ground,
energy seams) WITHOUT the model baking a literal road. It is NOT a hard control
(there is no ControlNet in the stack), and gameplay correctness never depends on
how faithfully the model follows it — the game code renders the authoritative
road/props from the same layout, and the structural gates enforce the contract.

Pure ``Pillow`` + ``numpy`` (no OpenCV / scikit-image), matching the rest of the
adapters in this package.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ..errors import ToolError


def _points(waypoints: list[dict[str, Any]]) -> list[tuple[float, float]]:
    pts: list[tuple[float, float]] = []
    for wp in waypoints or []:
        try:
            pts.append((float(wp["x"]), float(wp["y"])))
        except (KeyError, TypeError, ValueError):
            continue
    return pts


def render_layout_mask(
    layout: dict[str, Any],
    output_path: Path,
    *,
    road_value: int = 150,
    bg_value: int = 24,
    blur: float = 6.0,
    draw_markers: bool = True,
) -> dict[str, Any]:
    """Render ``layout`` to a grayscale-on-RGB corridor mask at ``output_path``.

    Parameters
    ----------
    layout:
        Parsed ``map-layout.json`` (``canvas`` + ``paths[].waypoints`` required).
    road_value / bg_value:
        Mid-gray road intensity over a dark background — deliberately NOT pure
        white road on black, so the hint reads as a *corridor*, not a paint-by
        -numbers road the model should trace literally.
    blur:
        Gaussian blur radius (px) softening the corridor so it stays a hint.
    draw_markers:
        Also mark the core / spawns / fork nodes / build slots.
    """
    from PIL import Image, ImageDraw, ImageFilter

    canvas = layout.get("canvas") or {}
    try:
        width = int(canvas["width"])
        height = int(canvas["height"])
    except (KeyError, TypeError, ValueError) as e:
        raise ToolError(
            "INVALID_CONFIG",
            "map-layout.json must declare canvas.width / canvas.height.",
        ) from e
    if width <= 0 or height <= 0:
        raise ToolError("INVALID_CONFIG", f"Invalid canvas size {width}x{height}.")

    paths = layout.get("paths") or []
    if not paths:
        raise ToolError("INVALID_CONFIG", "map-layout.json has no paths to render.")

    road_w = max(2, int(layout.get("road_width", 48)))
    road_value = max(0, min(255, int(road_value)))
    bg_value = max(0, min(255, int(bg_value)))
    cap_r = road_w / 2.0

    img = Image.new("L", (width, height), bg_value)
    draw = ImageDraw.Draw(img)

    def _disc(cx: float, cy: float, r: float, value: int) -> None:
        draw.ellipse([cx - r, cy - r, cx + r, cy + r], fill=value)

    rendered = 0
    for p in paths:
        pts = _points(p.get("waypoints"))
        if len(pts) < 2:
            continue
        # Stroke the polyline with rounded joins, plus a disc at each vertex so
        # corners stay continuous (PIL square caps would leave notches).
        draw.line(pts, fill=road_value, width=road_w, joint="curve")
        for (x, y) in pts:
            _disc(x, y, cap_r, road_value)
        rendered += 1

    if rendered == 0:
        raise ToolError(
            "INVALID_CONFIG",
            "No path had >= 2 waypoints; nothing to render.",
        )

    if draw_markers:
        core = layout.get("core") or {}
        if "x" in core and "y" in core:
            _disc(float(core["x"]), float(core["y"]), road_w * 0.9,
                  min(255, road_value + 80))
        for s in layout.get("spawns") or []:
            if "x" in s and "y" in s:
                _disc(float(s["x"]), float(s["y"]), road_w * 0.7,
                      min(255, road_value + 50))
        for f in layout.get("fork_nodes") or []:
            if "x" in f and "y" in f:
                _disc(float(f["x"]), float(f["y"]), road_w * 0.45, road_value)
        for slot in layout.get("build_slots") or []:
            if "x" in slot and "y" in slot:
                sx, sy = float(slot["x"]), float(slot["y"])
                sr = road_w * 0.4
                draw.rectangle([sx - sr, sy - sr, sx + sr, sy + sr],
                               fill=max(0, road_value - 55))

    if blur and blur > 0:
        img = img.filter(ImageFilter.GaussianBlur(float(blur)))

    out = img.convert("RGB")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    out.save(output_path)

    return {
        "output": str(output_path),
        "size": {"w": width, "h": height},
        "paths_rendered": rendered,
        "road_width": road_w,
        "markers": bool(draw_markers),
        "note": (
            "Soft corridor hint for edit_image(input=mask). NOT a hard control; "
            "gameplay geometry is rendered by code from the same map-layout.json."
        ),
    }


__all__ = ["render_layout_mask"]
