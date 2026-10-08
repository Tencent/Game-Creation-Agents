"""Spritesheet packer.

Assemble a directory of transparent RGBA frames (typically
``…/rmbg2/transparent_frames/`` produced by ``rmbg2_video_matting``) into
a single Phaser-ready spritesheet PNG plus its metadata JSON.

This encapsulates the full assembly algorithm so the assets-agent never
has to spawn Python/Pillow itself:

1. **Frame-count cap** — uniformly sample down to ``max_frames`` if the
   matted sequence has more frames than the sheet budget.
2. **Resize to the GDD target frame size** — every frame is scaled to
   ``frame_width × frame_height`` (the intended on-screen display size),
   never left at raw Seedance resolution.
3. **Multi-row wrap under the GPU texture limit** — columns are chosen so
   the sheet width stays within ``max_sheet_width`` (WebGL
   ``MAX_TEXTURE_SIZE`` floor, 4096px); remaining frames wrap to extra
   rows. Phaser auto-computes columns from ``sheet_width / frameWidth``,
   so multi-row sheets load without extra config.
4. **Safety down-scale** — in the pathological case where even the
   wrapped grid would exceed ``max_sheet_width`` on the vertical axis,
   frames are shrunk (Lanczos) toward ``min_frame_dimension_px`` until
   both axes fit.
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

from PIL import Image, UnidentifiedImageError

from ..errors import ToolError


def _uniform_sample(items: list[Path], k: int) -> list[Path]:
    """Pick ``k`` items spread uniformly across ``items`` (keeps endpoints)."""
    n = len(items)
    if k <= 0 or k >= n:
        return list(items)
    step = n / k
    return [items[min(n - 1, int(i * step))] for i in range(k)]


def pack_spritesheet(
    *,
    frames_dir: Path,
    output: Path,
    frame_width: int,
    frame_height: int,
    fps: int = 12,
    max_sheet_width: int = 4096,
    max_frames: int | None = 24,
    min_frame_dimension_px: int = 64,
    facing: str | None = None,
    content_bbox: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Assemble ``frames_dir`` into ``output`` (PNG) + ``output``.json.

    Returns the written metadata plus the final sheet dimensions.
    """
    if frame_width <= 0 or frame_height <= 0:
        raise ToolError(
            "INVALID_CONFIG", "`frame_width` and `frame_height` must be positive."
        )
    if not frames_dir.is_dir():
        raise ToolError(
            "INPUT_FILE_NOT_FOUND", f"frames_dir not found: {frames_dir}"
        )

    frames = sorted(frames_dir.glob("*.png"))
    if not frames:
        raise ToolError(
            "POSTPROCESS_FAILED",
            f"No PNG frames found in {frames_dir}.",
            hint="Expected matted frames from rmbg2_video_matting (keep_frames=true).",
        )

    original_count = len(frames)
    sampled = False
    if max_frames and max_frames > 0 and original_count > max_frames:
        frames = _uniform_sample(frames, max_frames)
        sampled = True

    fw, fh = int(frame_width), int(frame_height)
    count = len(frames)

    # Columns are bounded by the per-axis pixel budget; wrap to rows.
    cols = max(1, min(count, max_sheet_width // fw))
    rows = math.ceil(count / cols)

    # Safety: if the wrapped grid is still too tall, shrink frames toward
    # the minimum floor until both axes fit under the texture limit.
    while rows * fh > max_sheet_width and (fw > min_frame_dimension_px or fh > min_frame_dimension_px):
        scale = math.sqrt(max_sheet_width / (rows * fh))
        fw = max(min_frame_dimension_px, int(fw * scale))
        fh = max(min_frame_dimension_px, int(fh * scale))
        cols = max(1, min(count, max_sheet_width // fw))
        rows = math.ceil(count / cols)

    sheet = Image.new("RGBA", (cols * fw, rows * fh), (0, 0, 0, 0))
    for i, f in enumerate(frames):
        try:
            with Image.open(f) as im:
                tile = im.convert("RGBA").resize((fw, fh), Image.LANCZOS)
        except (UnidentifiedImageError, OSError) as e:
            raise ToolError("INVALID_IMAGE", f"Could not read frame {f.name}: {e}") from e
        col, row = i % cols, i // cols
        sheet.paste(tile, (col * fw, row * fh))

    output.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(output)

    meta: dict[str, Any] = {
        "frameWidth": fw,
        "frameHeight": fh,
        "frameCount": count,
        "columns": cols,
        "rows": rows,
        "fps": int(fps),
    }
    if sampled:
        meta["originalFrameCount"] = original_count
    if facing is not None:
        meta["facing"] = facing
    if content_bbox is not None:
        meta["contentBbox"] = content_bbox

    json_path = output.with_suffix(".json")
    json_path.write_text(json.dumps(meta, indent=2), encoding="utf-8")

    return {
        "sheet": str(output),
        "json": str(json_path),
        "sheet_width": cols * fw,
        "sheet_height": rows * fh,
        "sampled": sampled,
        **meta,
    }


__all__ = ["pack_spritesheet"]
