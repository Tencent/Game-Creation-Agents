"""Sprite metadata analyzer — uses Doubao VLM to determine facing direction
and content bounding box from a spritesheet image.

Single VLM round-trip (~5-15s). Called by assets-agent after spritesheet
generation to populate `facing` and `contentBbox` fields in `*-sheet.json`
with accurate values from actual pixel analysis rather than heuristic estimation.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from ..config import Config
from ..errors import ToolError
from .iterate_vlm import call_vlm_on_image

ANALYZE_PROMPT = """You are analyzing a game spritesheet image. The image shows a horizontal strip of animation frames (all frames of the same character/effect in a single row).

{context_hint}


Look at the FIRST FRAME (leftmost) and determine:

1. **FACING**: Which direction does the character's BODY (torso/chest/face) point?
   - "right" = the character's chest/face points toward the right side of the image
   - "left" = the character's chest/face points toward the left side of the image
   - "up" = the character's head/face points toward the top of the image
   - "down" = the character's head/face points toward the bottom of the image
   - "none" = the sprite is non-directional (explosion, particle, symmetric effect)

   IMPORTANT: Judge by the TORSO/CHEST/HEAD direction, NOT weapons, shields, or appendages held outward. For top-down sprites (fish, vehicles seen from above), report the direction the head/nose points.

2. **CONTENT_BBOX**: Estimate the bounding box of the opaque (non-transparent) content within the FIRST FRAME only.
   - The full frame size is {frame_width}x{frame_height} pixels
   - Report the rectangle {{x, y, w, h}} in pixels where the actual character/content sits
   - x,y = top-left corner of the content area within the frame
   - w,h = width and height of the content area

Respond in EXACTLY this JSON format (no other text):
```json
{{"facing": "right", "contentBbox": {{"x": 50, "y": 30, "w": 150, "h": 200}}}}
```

Replace values with your actual measurements. Be as accurate as possible."""


def _draw_bbox_visualization(
    image_path: Path,
    frame_width: int,
    frame_height: int,
    bbox: dict,
    facing: str,
) -> Path:
    """Draw contentBbox rectangle + facing arrow on the first frame, save as *_bbox_viz.png."""
    from PIL import Image, ImageDraw

    img = Image.open(image_path)
    # Crop first frame only
    first_frame = img.crop((0, 0, frame_width, frame_height)).convert("RGBA")

    # Scale up for visibility if frame is small
    scale = max(1, 256 // max(frame_width, frame_height))
    if scale > 1:
        first_frame = first_frame.resize(
            (frame_width * scale, frame_height * scale), Image.NEAREST
        )

    draw = ImageDraw.Draw(first_frame)
    bx = bbox["x"] * scale
    by = bbox["y"] * scale
    bw = bbox["w"] * scale
    bh = bbox["h"] * scale

    # Draw bbox rectangle (green)
    draw.rectangle([bx, by, bx + bw, by + bh], outline=(0, 255, 0, 255), width=2)

    # Draw facing arrow (red)
    center_x = bx + bw // 2
    center_y = by + bh // 2
    arrow_len = min(bw, bh) // 3
    tip = 6 * scale
    if facing == "right":
        draw.line([(center_x - arrow_len, center_y), (center_x + arrow_len, center_y)], fill=(255, 0, 0, 255), width=2)
        draw.polygon([(center_x + arrow_len, center_y), (center_x + arrow_len - tip, center_y - tip//2), (center_x + arrow_len - tip, center_y + tip//2)], fill=(255, 0, 0, 255))
    elif facing == "left":
        draw.line([(center_x + arrow_len, center_y), (center_x - arrow_len, center_y)], fill=(255, 0, 0, 255), width=2)
        draw.polygon([(center_x - arrow_len, center_y), (center_x - arrow_len + tip, center_y - tip//2), (center_x - arrow_len + tip, center_y + tip//2)], fill=(255, 0, 0, 255))
    elif facing == "down":
        draw.line([(center_x, center_y - arrow_len), (center_x, center_y + arrow_len)], fill=(255, 0, 0, 255), width=2)
        draw.polygon([(center_x, center_y + arrow_len), (center_x - tip//2, center_y + arrow_len - tip), (center_x + tip//2, center_y + arrow_len - tip)], fill=(255, 0, 0, 255))
    elif facing == "up":
        draw.line([(center_x, center_y + arrow_len), (center_x, center_y - arrow_len)], fill=(255, 0, 0, 255), width=2)
        draw.polygon([(center_x, center_y - arrow_len), (center_x - tip//2, center_y - arrow_len + tip), (center_x + tip//2, center_y - arrow_len + tip)], fill=(255, 0, 0, 255))

    # Add text label
    label = f"facing: {facing}  bbox: {bbox['x']},{bbox['y']} {bbox['w']}x{bbox['h']}"
    draw.text((4, 4), label, fill=(255, 255, 0, 255))

    # Save next to original
    viz_path = image_path.parent / f"{image_path.stem}_bbox_viz.png"
    first_frame.save(viz_path)
    return viz_path


def _compute_bbox_from_alpha(image_path: Path, frame_width: int, frame_height: int, alpha_threshold: int = 10) -> dict:
    """Compute precise contentBbox from the first frame's alpha channel."""
    import numpy as np
    from PIL import Image

    img = Image.open(image_path).convert("RGBA")
    first_frame = img.crop((0, 0, frame_width, frame_height))
    alpha = np.array(first_frame)[:, :, 3]

    rows = np.any(alpha > alpha_threshold, axis=1)
    cols = np.any(alpha > alpha_threshold, axis=0)

    if not rows.any() or not cols.any():
        return {"x": 0, "y": 0, "w": frame_width, "h": frame_height}

    y_min, y_max = int(np.where(rows)[0][0]), int(np.where(rows)[0][-1])
    x_min, x_max = int(np.where(cols)[0][0]), int(np.where(cols)[0][-1])

    return {
        "x": x_min,
        "y": y_min,
        "w": x_max - x_min + 1,
        "h": y_max - y_min + 1,
    }


def _compute_bbox_avg_across_frames(
    image_path: Path,
    frame_width: int,
    frame_height: int,
    alpha_threshold: int = 10,
) -> dict[str, Any]:
    """Average contentBbox across every non-empty frame in the spritesheet.

    Sprites animate — the foreground silhouette shifts each frame (a sword
    swing extends the arm; a jump tucks the legs). Sizing the collision body
    from the FIRST frame alone leaves the body too small or too large for
    most other frames, so projectiles miss / over-hit. Averaging the
    per-frame alpha bbox produces a body that fits the typical pose.

    Frames whose alpha is fully transparent (e.g. matte failures at the tail
    of an i2v sequence) are skipped so they don't pull the average toward
    the empty-frame fallback (whole frame). If every frame is empty, falls
    back to the full frame.

    Returns:
        dict with: x, y, w, h (averaged, rounded to int px in cell-local coords)
        plus 'sample_size' (frames counted) and 'first_frame_bbox' (for diff).
    """
    import numpy as np
    from PIL import Image

    img = np.array(Image.open(image_path).convert("RGBA"))
    sheet_h, sheet_w = img.shape[:2]
    cols = max(1, sheet_w // frame_width)
    rows = max(1, sheet_h // frame_height)

    boxes: list[tuple[int, int, int, int]] = []
    first_box: dict[str, int] | None = None
    for r in range(rows):
        for c in range(cols):
            sub_alpha = img[
                r * frame_height : (r + 1) * frame_height,
                c * frame_width : (c + 1) * frame_width,
                3,
            ]
            ys = np.any(sub_alpha > alpha_threshold, axis=1)
            xs = np.any(sub_alpha > alpha_threshold, axis=0)
            if not ys.any() or not xs.any():
                continue
            y_min, y_max = int(np.where(ys)[0][0]), int(np.where(ys)[0][-1])
            x_min, x_max = int(np.where(xs)[0][0]), int(np.where(xs)[0][-1])
            box = (x_min, y_min, x_max - x_min + 1, y_max - y_min + 1)
            boxes.append(box)
            if first_box is None:
                first_box = {"x": box[0], "y": box[1], "w": box[2], "h": box[3]}

    if not boxes:
        return {
            "x": 0,
            "y": 0,
            "w": frame_width,
            "h": frame_height,
            "sample_size": 0,
            "first_frame_bbox": None,
        }

    arr = np.array(boxes)
    return {
        "x": int(round(float(arr[:, 0].mean()))),
        "y": int(round(float(arr[:, 1].mean()))),
        "w": int(round(float(arr[:, 2].mean()))),
        "h": int(round(float(arr[:, 3].mean()))),
        "sample_size": len(boxes),
        "first_frame_bbox": first_box,
    }


def analyze_sprite_metadata(
    config: Config,
    *,
    image_path: Path,
    frame_width: int,
    frame_height: int,
    reference_image: Path | None = None,
    description: str | None = None,
    vlm_model: str | None = None,
    vlm_max_tokens: int | None = None,
    vlm_temperature: float | None = None,
    vlm_timeout: float | None = None,
) -> dict[str, Any]:
    """Analyze a spritesheet to determine facing (VLM) and contentBbox (pixel-precise).

    - contentBbox: computed from alpha channel of `image_path` (pixel-level accuracy)
    - facing: determined by VLM from `reference_image` (if provided) or `image_path`
    - visualization: always drawn on `image_path` (the transparent spritesheet)

    Args:
        config: App config with VLM credentials.
        image_path: Path to the spritesheet PNG (must have transparency/alpha).
            Used for contentBbox calculation AND bbox visualization.
        frame_width: Width of a single frame in pixels.
        frame_height: Height of a single frame in pixels.
        reference_image: Optional path to the reference image (typically 960x960,
            un-matted). When provided, VLM judges facing from this image (clearer
            single-character view) instead of the spritesheet. The bbox and
            visualization are still computed/drawn from `image_path`.
        description: What this sprite is (e.g. "enemy bullet projectile flying left",
            "player character holding blaster, side-view platform game").
            Helps VLM make more accurate facing judgments.
        vlm_model: Override VLM model.
        vlm_max_tokens: Override VLM token budget.
        vlm_temperature: Override VLM sampling temperature.
        vlm_timeout: HTTP timeout for VLM call.

    Returns:
        dict with keys: facing (str), contentBbox (dict with x,y,w,h)
    """
    if not image_path.is_file():
        raise ToolError(
            "INPUT_FILE_NOT_FOUND",
            f"Spritesheet not found: {image_path}",
        )

    # --- contentBbox: pixel-precise from alpha channel, averaged across all
    # non-empty frames of the spritesheet (sprites animate — first-frame-only
    # bbox underfits poses that extend the silhouette). The first-frame box is
    # kept under `firstFrameBbox` for forensics. ---
    bbox_avg = _compute_bbox_avg_across_frames(
        image_path, frame_width, frame_height
    )
    sample_size = int(bbox_avg.pop("sample_size", 0))
    first_frame_bbox = bbox_avg.pop("first_frame_bbox", None)
    bbox = {"x": bbox_avg["x"], "y": bbox_avg["y"], "w": bbox_avg["w"], "h": bbox_avg["h"]}

    # --- facing: VLM semantic analysis ---
    # Use reference_image for VLM if provided (clearer single-character view),
    # otherwise fall back to the spritesheet itself.
    vlm_image = image_path
    vlm_frame_w = frame_width
    vlm_frame_h = frame_height
    if reference_image and reference_image.is_file():
        vlm_image = reference_image
        from PIL import Image as _PILImage
        with _PILImage.open(reference_image) as ref_img:
            vlm_frame_w, vlm_frame_h = ref_img.size

    context_hint = ""
    if description:
        context_hint = f"CONTEXT: This sprite is: {description}. Use this information to help judge facing direction accurately."

    prompt = ANALYZE_PROMPT.format(
        frame_width=vlm_frame_w,
        frame_height=vlm_frame_h,
        context_hint=context_hint,
    )

    vlm_defaults = config.assets.vlm
    model = vlm_model or vlm_defaults.model
    max_tokens = vlm_max_tokens or vlm_defaults.image_max_tokens
    temperature = (
        vlm_defaults.temperature
        if vlm_temperature is None
        else vlm_temperature
    )
    timeout = vlm_timeout or vlm_defaults.image_timeout_seconds
    raw = call_vlm_on_image(
        config=config,
        image_path=vlm_image,
        prompt=prompt,
        model=model,
        max_tokens=max_tokens,
        temperature=temperature,
        timeout=timeout,
    )

    # Parse JSON from response (may be wrapped in ```json ... ```)
    result = None
    code_match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", raw, re.DOTALL)
    if code_match:
        try:
            result = json.loads(code_match.group(1))
        except json.JSONDecodeError:
            pass

    if result is None:
        for m in re.finditer(r"\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}", raw):
            try:
                candidate = json.loads(m.group())
                if "facing" in candidate:
                    result = candidate
                    break
            except json.JSONDecodeError:
                continue

    if result is None:
        raise ToolError(
            "VLM_PARSE_FAILED",
            f"Could not parse JSON from VLM response: {raw[:300]}",
        )

    facing = result.get("facing")
    if facing not in ("right", "left", "up", "down", "none"):
        raise ToolError(
            "VLM_PARSE_FAILED",
            f"Invalid facing value: {facing!r}. Expected 'right', 'left', 'up', 'down', or 'none'.",
        )

    # Generate visualization on the SPRITESHEET (not reference) with pixel-precise bbox
    viz_path = _draw_bbox_visualization(
        image_path, frame_width, frame_height, bbox, facing
    )

    return {
        "facing": facing,
        "contentBbox": bbox,
        "firstFrameBbox": first_frame_bbox,
        "bboxFrameSamples": sample_size,
        "visualization": str(viz_path),
        "vlm_model": model,
        "bbox_source": "alpha_avg_across_frames",
        "facing_source": f"vlm (from {'reference' if reference_image and reference_image.is_file() else 'spritesheet'})",
        "raw_response": raw[:500],
    }


__all__ = ["analyze_sprite_metadata"]
