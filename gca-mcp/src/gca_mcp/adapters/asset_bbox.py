"""Alpha content bounding-box for static assets (props / tiles / UI).

Uses the SAME alpha-projection method as the sprite analyzer
(``sprite_metadata_analyzer._compute_bbox_from_alpha``) but exposed for
non-sprite assets, so collision bodies can be sized to the *visible*
pixels instead of the full (transparent-padded) frame. This is what
stops platforms/props from being "stood on" in mid-air when the matted
image keeps a large transparent margin around the art.

Supports a single image (``cols=rows=1``) or a grid/strip (e.g. a 1x3
tile strip → per-cell bounding boxes in cell-local coordinates).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..errors import ToolError


def _bbox_in_cell(
    alpha: Any, x0: int, y0: int, w: int, h: int, threshold: int
) -> dict[str, int] | None:
    """Tight bbox of opaque pixels inside the cell at (x0,y0,w,h), cell-local."""
    import numpy as np

    sub = alpha[y0 : y0 + h, x0 : x0 + w]
    rows = np.any(sub > threshold, axis=1)
    cols = np.any(sub > threshold, axis=0)
    if not rows.any() or not cols.any():
        return None
    y_min, y_max = int(np.where(rows)[0][0]), int(np.where(rows)[0][-1])
    x_min, x_max = int(np.where(cols)[0][0]), int(np.where(cols)[0][-1])
    return {"x": x_min, "y": y_min, "w": x_max - x_min + 1, "h": y_max - y_min + 1}


def compute_content_bbox(
    image_path: Path,
    *,
    cols: int = 1,
    rows: int = 1,
    alpha_threshold: int = 10,
    clean_threshold: int | None = None,
    output: Path | None = None,
    image_reference: Path | None = None,
) -> dict[str, Any]:
    """Compute the opaque-content bbox of an image (or each cell of a strip).

    Returns per-cell bboxes (cell-local coords) plus, for a single cell,
    a convenience top-level ``contentBbox``. Optionally writes the result
    to ``output`` as a sidecar JSON.

    ``clean_threshold`` (optional): before measuring the bbox, hard-zero any
    pixel whose alpha is below this value. RMBG-2.0 frequently leaves sparse
    low-alpha stray pixels along an edge; because the bbox is anchored by ANY
    pixel with ``alpha > alpha_threshold``, a single stray point at (e.g.)
    the bottom-left pushes the box to the canvas edge and defeats trimming.
    Zeroing sub-``clean_threshold`` pixels FIRST kills those specks so the box
    snaps to the real content. Keep it well below the genuine anti-aliased
    soft edge (typically alpha ≥ 60) so real edges are not eaten — this only
    affects the MEASUREMENT; callers that also crop still crop the original
    (un-cleaned) pixels, preserving soft edges. ``None`` (default) = no
    cleaning, exact legacy behaviour.
    """
    import numpy as np
    from PIL import Image, UnidentifiedImageError

    if not image_path.is_file():
        raise ToolError("INPUT_FILE_NOT_FOUND", f"Image not found: {image_path}")
    try:
        img = Image.open(image_path).convert("RGBA")
    except (UnidentifiedImageError, OSError) as e:
        raise ToolError("INVALID_IMAGE", f"Cannot open {image_path}: {e}") from e

    width, height = img.size
    cols = max(1, int(cols))
    rows = max(1, int(rows))
    frame_w = width // cols
    frame_h = height // rows
    if frame_w == 0 or frame_h == 0:
        raise ToolError(
            "INVALID_CONFIG",
            f"cols/rows ({cols}x{rows}) exceed image size {width}x{height}.",
        )

    alpha = np.array(img)[:, :, 3]
    # Kill sparse low-alpha stray pixels (rmbg2 edge specks) BEFORE measuring so
    # they cannot anchor the bbox to the canvas edge. Measurement-only — the
    # caller's crop still uses the original pixels, so soft edges survive.
    if clean_threshold is not None and clean_threshold > 0:
        alpha = np.where(alpha < int(clean_threshold), 0, alpha)
    cells: list[dict[str, Any]] = []
    for r in range(rows):
        for c in range(cols):
            bb = _bbox_in_cell(
                alpha, c * frame_w, r * frame_h, frame_w, frame_h, alpha_threshold
            )
            cells.append(
                {
                    "index": r * cols + c,
                    "row": r,
                    "col": c,
                    # fall back to the full cell if it's entirely opaque/empty
                    "contentBbox": bb or {"x": 0, "y": 0, "w": frame_w, "h": frame_h},
                    "empty": bb is None,
                }
            )

    result: dict[str, Any] = {
        "image": str(image_reference or image_path),
        "size": {"w": width, "h": height},
        "frameWidth": frame_w,
        "frameHeight": frame_h,
        "cols": cols,
        "rows": rows,
        "alpha_threshold": alpha_threshold,
        "clean_threshold": clean_threshold,
        "cells": cells,
        # convenience for the common single-image prop/UI case
        "contentBbox": cells[0]["contentBbox"] if len(cells) == 1 else None,
    }

    if output is not None:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(result, indent=2), encoding="utf-8")
        result["output"] = str(output)
    return result


__all__ = [
    "compute_content_bbox",
    "contain_pad_to_size",
    "reproject_anchors",
    "reproject_bindings_to_canvas",
    "scale_to_target",
    "trim_to_content_bbox",
]


def reproject_anchors(
    anchors: list[dict] | None,
    slots: list[dict] | None,
    *,
    before: dict[str, int],
    after: dict[str, int],
    trimmed_px: dict[str, int],
) -> dict[str, Any]:
    """Re-express fractional anchor/slot positions after a trim crop.

    Binding-panel anchors (§2E.1 Binding Spec) are recorded as ``xPct/yPct``
    fractions of the WHOLE asset box, measured on the design mockup where the
    asset frame still carries its transparent margin. ``trim_to_content_bbox``
    crops that margin, which moves the (0, 0) origin and shrinks w/h — so the
    same fraction now points at a different physical pixel and the overlaid
    value ("100/100", a card cost) would float off the widget.

    This maps each fraction from the PRE-trim box onto the POST-trim box, using
    the deterministic crop offsets ``trim_to_content_bbox`` already returned::

        px_before = pct_before * before_dim
        px_after  = px_before − trim_(left|top)
        pct_after = px_after / after_dim

    So the anchor keeps pointing at the same physical spot on the art. This is
    the whole reason a binding panel can be trimmed like every other UI asset
    instead of being a ``skip_trim`` special case: measure once on the mockup,
    then re-project by the known crop — never re-detect on the final PNG (the
    value the anchor marks was deliberately NOT baked into the art, so there is
    nothing there to detect).

    ``wPct/hPct`` on slots scale by the box ratio (a width fraction of a wider
    pre-trim box maps to a larger fraction of the narrower post-trim box).
    Positions outside [0, 1] after mapping are clamped and flagged in
    ``out_of_bounds`` (usually means the anchor sat in the trimmed-away margin,
    i.e. it was mis-measured).

    Returns ``{anchors, slots, out_of_bounds}`` with NEW fraction dicts (inputs
    are not mutated). A no-op trim (``after == before`` and zero offsets)
    returns the inputs unchanged.
    """
    bw, bh = int(before.get("w", 0)), int(before.get("h", 0))
    aw, ah = int(after.get("w", 0)), int(after.get("h", 0))
    tl = int(trimmed_px.get("left", 0))
    tt = int(trimmed_px.get("top", 0))

    out_of_bounds: list[str] = []

    def _clamp(v: float, tag: str) -> float:
        if v < 0.0 or v > 1.0:
            out_of_bounds.append(tag)
        return min(1.0, max(0.0, v))

    def _map_x(px_pct: float, tag: str) -> float:
        if aw <= 0 or bw <= 0:
            return px_pct
        return _clamp((px_pct * bw - tl) / aw, tag)

    def _map_y(px_pct: float, tag: str) -> float:
        if ah <= 0 or bh <= 0:
            return px_pct
        return _clamp((px_pct * bh - tt) / ah, tag)

    def _scale_w(wpct: float) -> float:
        if aw <= 0 or bw <= 0:
            return wpct
        return min(1.0, max(0.0, wpct * bw / aw))

    def _scale_h(hpct: float) -> float:
        if ah <= 0 or bh <= 0:
            return hpct
        return min(1.0, max(0.0, hpct * bh / ah))

    new_anchors: list[dict] | None = None
    if anchors:
        new_anchors = []
        for i, a in enumerate(anchors):
            if not isinstance(a, dict):
                new_anchors.append(a)
                continue
            na = dict(a)
            aid = str(a.get("id") or a.get("binds") or a.get("bind") or i)
            if "xPct" in a:
                na["xPct"] = round(_map_x(float(a["xPct"]), f"anchor:{aid}:x"), 5)
            if "yPct" in a:
                na["yPct"] = round(_map_y(float(a["yPct"]), f"anchor:{aid}:y"), 5)
            # fontPct is a height fraction → scales with the vertical box ratio.
            if "fontPct" in a:
                na["fontPct"] = round(_scale_h(float(a["fontPct"])), 5)
            new_anchors.append(na)

    new_slots: list[dict] | None = None
    if slots:
        new_slots = []
        for i, s in enumerate(slots):
            if not isinstance(s, dict):
                new_slots.append(s)
                continue
            ns = dict(s)
            sid = str(s.get("id") or s.get("action") or i)
            if "xPct" in s:
                ns["xPct"] = round(_map_x(float(s["xPct"]), f"slot:{sid}:x"), 5)
            if "yPct" in s:
                ns["yPct"] = round(_map_y(float(s["yPct"]), f"slot:{sid}:y"), 5)
            if "wPct" in s:
                ns["wPct"] = round(_scale_w(float(s["wPct"])), 5)
            if "hPct" in s:
                ns["hPct"] = round(_scale_h(float(s["hPct"])), 5)
            new_slots.append(ns)

    return {
        "anchors": new_anchors if new_anchors is not None else anchors,
        "slots": new_slots if new_slots is not None else slots,
        "out_of_bounds": out_of_bounds,
    }


def reproject_bindings_to_canvas(
    anchors: list[dict] | None,
    slots: list[dict] | None,
    *,
    content: dict[str, int],
    canvas: dict[str, int],
    padding: dict[str, int],
) -> dict[str, Any]:
    """Map binding fractions from contained content into its padded canvas."""
    content_width = int(content.get("w", 0))
    content_height = int(content.get("h", 0))
    canvas_width = int(canvas.get("w", 0))
    canvas_height = int(canvas.get("h", 0))
    left = int(padding.get("left", 0))
    top = int(padding.get("top", 0))
    if min(content_width, content_height, canvas_width, canvas_height) <= 0:
        return {"anchors": anchors, "slots": slots}

    def _x(value: float) -> float:
        return round((left + value * content_width) / canvas_width, 5)

    def _y(value: float) -> float:
        return round((top + value * content_height) / canvas_height, 5)

    def _w(value: float) -> float:
        return round(value * content_width / canvas_width, 5)

    def _h(value: float) -> float:
        return round(value * content_height / canvas_height, 5)

    projected_anchors: list[dict] | None = None
    if anchors is not None:
        projected_anchors = []
        for anchor in anchors:
            if not isinstance(anchor, dict):
                projected_anchors.append(anchor)
                continue
            projected = dict(anchor)
            if "xPct" in projected:
                projected["xPct"] = _x(float(projected["xPct"]))
            if "yPct" in projected:
                projected["yPct"] = _y(float(projected["yPct"]))
            if "fontPct" in projected:
                projected["fontPct"] = _h(float(projected["fontPct"]))
            projected_anchors.append(projected)

    projected_slots: list[dict] | None = None
    if slots is not None:
        projected_slots = []
        for slot in slots:
            if not isinstance(slot, dict):
                projected_slots.append(slot)
                continue
            projected = dict(slot)
            if "xPct" in projected:
                projected["xPct"] = _x(float(projected["xPct"]))
            if "yPct" in projected:
                projected["yPct"] = _y(float(projected["yPct"]))
            if "wPct" in projected:
                projected["wPct"] = _w(float(projected["wPct"]))
            if "hPct" in projected:
                projected["hPct"] = _h(float(projected["hPct"]))
            projected_slots.append(projected)

    return {
        "anchors": projected_anchors if projected_anchors is not None else anchors,
        "slots": projected_slots if projected_slots is not None else slots,
    }


def trim_to_content_bbox(
    image_path: Path,
    *,
    output: Path | None = None,
    padding: int = 2,
    alpha_threshold: int = 10,
    clean_threshold: int | None = 24,
    min_trim_px: int = 4,
) -> dict[str, Any]:
    """Crop ``image_path`` to its alpha content bbox (with optional padding).

    Eliminates the transparent margin RMBG-2.0 leaves around static assets
    (props/tiles/UI/icons) — a margin that would otherwise (a) inflate the
    apparent size at `add.image`, (b) put collision bodies in empty space
    when sized from the full frame, and (c) make the asset look mis-aligned
    when stacked against other UI.

    Parameters
    ----------
    image_path :
        RGBA PNG to trim. The file is rewritten in place unless ``output``
        is set.
    output :
        Where to write the trimmed image. Defaults to overwriting
        ``image_path``.
    padding :
        Pixels of transparent padding to keep on every edge around the
        content bbox. Default 2 — preserves soft-edge halos (button glow,
        anti-aliased corners) that would otherwise look chopped.
    alpha_threshold :
        Pixels with alpha ≤ this value are considered fully transparent.
        Same default (10) as :func:`compute_content_bbox`.
    clean_threshold :
        Hard-zero pixels with alpha below this BEFORE measuring the content
        bbox, so sparse rmbg2 edge specks cannot anchor the box to the canvas
        edge and defeat the trim (the "trim only cut top+right, left/bottom
        stayed padded" bug). Measurement-only: the crop still saves the
        ORIGINAL pixels, so genuine anti-aliased soft edges (alpha ≥ 60) are
        preserved untouched. Default 24 — above stray-speck alpha, well below
        real soft edges. Pass ``None`` to disable (exact legacy behaviour).
    min_trim_px :
        If the would-be trim removes < this many pixels on EVERY edge,
        skip the crop (no-op). Avoids rewriting an already-tight asset
        and prevents floating-point churn for assets that are basically
        edge-to-edge already.

    Returns
    -------
    dict with::

        {
          "input":   str,
          "output":  str,
          "before":  {"w", "h"},
          "after":   {"w", "h"},
          "trimmed_px": {"top","bottom","left","right"},
          "trimmed":  bool,                   # False → no-op
          "reason":   str (only when trimmed=False),
        }

    Edge cases
    ----------
    - Image has NO opaque pixels (alpha all <= threshold) → no-op,
      reason "empty_alpha".
    - Bbox already fills the canvas (content-to-edge) → no-op,
      reason "already_tight".
    - Trim margin on every edge < ``min_trim_px`` → no-op,
      reason "below_min_trim".
    """
    from PIL import Image, UnidentifiedImageError

    if not image_path.is_file():
        raise ToolError("INPUT_FILE_NOT_FOUND", f"Image not found: {image_path}")
    try:
        img = Image.open(image_path).convert("RGBA")
    except (UnidentifiedImageError, OSError) as e:
        raise ToolError("INVALID_IMAGE", f"Cannot open {image_path}: {e}") from e

    w, h = img.size
    # Measure the bbox on a CLEANED alpha (stray specks zeroed) so the box snaps
    # to real content, but crop the ORIGINAL `img` below so soft edges survive.
    info = compute_content_bbox(
        image_path,
        alpha_threshold=alpha_threshold,
        clean_threshold=clean_threshold,
    )
    bbox = info.get("contentBbox")

    # Empty alpha → leave the file alone so the failure is visible upstream.
    if not bbox or info.get("cells", [{}])[0].get("empty"):
        return {
            "input": str(image_path),
            "output": str(image_path),
            "before": {"w": w, "h": h},
            "after": {"w": w, "h": h},
            "trimmed_px": {"top": 0, "bottom": 0, "left": 0, "right": 0},
            "trimmed": False,
            "reason": "empty_alpha",
        }

    x, y, bw, bh = bbox["x"], bbox["y"], bbox["w"], bbox["h"]

    # Expand by padding, clipped to canvas.
    left = max(0, x - padding)
    top = max(0, y - padding)
    right = min(w, x + bw + padding)
    bottom = min(h, y + bh + padding)

    trim_left = left
    trim_top = top
    trim_right = w - right
    trim_bottom = h - bottom

    new_w = right - left
    new_h = bottom - top

    # Already tight enough on every edge → skip the write.
    if (
        trim_left < min_trim_px
        and trim_right < min_trim_px
        and trim_top < min_trim_px
        and trim_bottom < min_trim_px
    ):
        return {
            "input": str(image_path),
            "output": str(image_path),
            "before": {"w": w, "h": h},
            "after": {"w": w, "h": h},
            "trimmed_px": {
                "top": trim_top,
                "bottom": trim_bottom,
                "left": trim_left,
                "right": trim_right,
            },
            "trimmed": False,
            "reason": "below_min_trim",
        }

    cropped = img.crop((left, top, right, bottom))
    out = output if output is not None else image_path
    out.parent.mkdir(parents=True, exist_ok=True)
    cropped.save(out, format="PNG")

    return {
        "input": str(image_path),
        "output": str(out),
        "before": {"w": w, "h": h},
        "after": {"w": new_w, "h": new_h},
        "trimmed_px": {
            "top": trim_top,
            "bottom": trim_bottom,
            "left": trim_left,
            "right": trim_right,
        },
        "trimmed": True,
        "clean_threshold": clean_threshold,
    }


def scale_to_target(
    image_path: Path,
    *,
    target_height: int | None = None,
    target_width: int | None = None,
    max_box: tuple[int, int] | None = None,
    allow_upscale: bool = True,
    output: Path | None = None,
) -> dict[str, Any]:
    """Scale an RGBA PNG UNIFORMLY (aspect preserved) to a target scale.

    This is the asset-side "proportion-control" step: after generation + matting + trim,
    bring the PNG to the size it should render at IN GAME, so the runtime can
    draw it 1:1 (``scale=1``) and never has to guess a display size from the
    GDD. The on-disk pixel size becomes the display size — a single source of
    truth.

    A SINGLE scale factor is computed from whichever constraint is given and
    applied to BOTH axes, so the aspect ratio is never changed (no squashing).
    Exactly one of ``target_height`` / ``target_width`` / ``max_box`` drives it:

    - ``target_height``  → ``scale = target_height / h`` (lock the height,
      width follows). Recommended for HUD widgets/icons where design authors a
      role height (icon → 56, button → 64, gauge → 100…).
    - ``target_width``   → ``scale = target_width / w`` (lock the width).
    - ``max_box=(w,h)``  → ``scale = min(w/W, h/H)`` (contain-fit inside a box,
      aspect preserved — the art touches the box on its limiting axis).

    Parameters
    ----------
    image_path :
        Source RGBA PNG (already trimmed to content).
    target_height, target_width :
        Lock one dimension; the other is derived from the source aspect.
    max_box :
        ``(maxW, maxH)`` contain-fit box. Mutually exclusive with the locks.
    allow_upscale :
        When False, never enlarge (scale is capped at 1.0) — useful so a small
        source isn't blurred up. Default True (a generated 816px asset is
        normally being shrunk, so this rarely bites).
    output :
        Destination path. Defaults to overwriting ``image_path``.

    Returns
    -------
    dict with ``input``, ``output``, ``before {w,h}``, ``after {w,h}``,
    ``scale`` (the uniform factor applied), and ``aspect`` (preserved w/h).
    """
    from PIL import Image, UnidentifiedImageError

    given = [v is not None for v in (target_height, target_width, max_box)]
    if sum(given) != 1:
        raise ToolError(
            "INVALID_CONFIG",
            "scale_to_target needs EXACTLY ONE of target_height / "
            "target_width / max_box.",
        )

    if not image_path.is_file():
        raise ToolError("INPUT_FILE_NOT_FOUND", f"Image not found: {image_path}")

    try:
        img = Image.open(image_path).convert("RGBA")
    except (UnidentifiedImageError, OSError) as e:
        raise ToolError("INVALID_IMAGE", f"Cannot open {image_path}: {e}") from e

    w, h = img.size
    if w <= 0 or h <= 0:
        raise ToolError("INVALID_IMAGE", f"Degenerate image size {w}x{h}")

    if target_height is not None:
        if target_height <= 0:
            raise ToolError("INVALID_CONFIG", "target_height must be positive.")
        scale = target_height / h
    elif target_width is not None:
        if target_width <= 0:
            raise ToolError("INVALID_CONFIG", "target_width must be positive.")
        scale = target_width / w
    else:
        bw, bh = max_box  # type: ignore[misc]
        if bw <= 0 or bh <= 0:
            raise ToolError("INVALID_CONFIG", "max_box dims must be positive.")
        scale = min(bw / w, bh / h)

    if not allow_upscale:
        scale = min(scale, 1.0)

    # Round to whole pixels, keeping both axes ≥ 1.
    new_w = max(1, round(w * scale))
    new_h = max(1, round(h * scale))

    out = output if output is not None else image_path

    # No meaningful change → skip the rewrite (avoid resample churn/blur).
    if new_w == w and new_h == h:
        if output is not None and output != image_path:
            img.save(out, format="PNG")
        return {
            "input": str(image_path),
            "output": str(out),
            "before": {"w": w, "h": h},
            "after": {"w": w, "h": h},
            "scale": 1.0,
            "aspect": round(w / h, 4),
            "scaled": False,
        }

    scaled = img.resize((new_w, new_h), Image.LANCZOS)
    out.parent.mkdir(parents=True, exist_ok=True)
    scaled.save(out, format="PNG")

    return {
        "input": str(image_path),
        "output": str(out),
        "before": {"w": w, "h": h},
        "after": {"w": new_w, "h": new_h},
        "scale": round(scale, 6),
        "aspect": round(w / h, 4),
        "scaled": True,
    }


def contain_pad_to_size(
    image_path: Path,
    *,
    base_size: tuple[int, int],
    output: Path | None = None,
) -> dict[str, Any]:
    """Contain-fit and center-pad an RGBA image using nearest-neighbor only.

    Pixel-art UI needs an exact logical texture box without distorting the
    artwork. The content is therefore scaled uniformly to fit ``base_size``
    with nearest-neighbor sampling, then alpha-composited onto a transparent
    canvas of the exact requested dimensions.
    """
    from PIL import Image, UnidentifiedImageError

    if not image_path.is_file():
        raise ToolError("INPUT_FILE_NOT_FOUND", f"Image not found: {image_path}")
    base_width, base_height = base_size
    if base_width <= 0 or base_height <= 0:
        raise ToolError("INVALID_CONFIG", "base_size dims must be positive.")
    try:
        image = Image.open(image_path).convert("RGBA")
    except (UnidentifiedImageError, OSError) as exc:
        raise ToolError("INVALID_IMAGE", f"Cannot open {image_path}: {exc}") from exc

    width, height = image.size
    if width <= 0 or height <= 0:
        raise ToolError("INVALID_IMAGE", f"Degenerate image size {width}x{height}")

    scale = min(base_width / width, base_height / height)
    content_width = min(base_width, max(1, round(width * scale)))
    content_height = min(base_height, max(1, round(height * scale)))
    if (content_width, content_height) == image.size:
        resized = image
    else:
        resized = image.resize(
            (content_width, content_height),
            Image.Resampling.NEAREST,
        )

    left = (base_width - content_width) // 2
    top = (base_height - content_height) // 2
    canvas = Image.new("RGBA", (base_width, base_height), (0, 0, 0, 0))
    canvas.alpha_composite(resized, dest=(left, top))
    out = output if output is not None else image_path
    out.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(out, format="PNG")

    return {
        "input": str(image_path),
        "output": str(out),
        "before": {"w": width, "h": height},
        "content": {"w": content_width, "h": content_height},
        "after": {"w": base_width, "h": base_height},
        "scale": round(scale, 6),
        "padding": {
            "left": left,
            "top": top,
            "right": base_width - content_width - left,
            "bottom": base_height - content_height - top,
        },
        "resampler": "nearest-neighbor",
        "aspect_preserved": True,
    }
