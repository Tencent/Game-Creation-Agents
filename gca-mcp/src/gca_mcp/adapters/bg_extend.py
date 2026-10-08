"""Seamless background extension adapter.

The provider sees the right edge of the source as an overlap followed by one
full source-width blank region.  This keeps a normal 16:9 request below the
provider's 3:1 aspect limit while still producing a complete next segment.
The provider is not trusted to preserve its seed: the source overlap is pasted
back pixel-for-pixel before the full review artifact is saved or cropped.
"""

from __future__ import annotations

import tempfile
import uuid
from math import ceil
from pathlib import Path
from typing import Any

from PIL import Image

from ..config import Config
from ..errors import ToolError
from . import image_gen


DEFAULT_PROMPT = (
    "Fill only the blank white region on the right as a seamless horizontal "
    "continuation of the game-background overlap on the left. Preserve the "
    "left overlap exactly: do not repaint, crop, resize, blur, shift, or "
    "change any pixel in it. Continue the same perspective, "
    "major guide lines, lighting, art style, color palette, and horizon "
    "alignment into the blank region. Do not add characters, text, labels, "
    "UI, or borders. The result should look like one continuous "
    "side-scrolling game background."
)


def _overlap_width(width: int, height: int) -> int:
    """Use half a segment when possible while keeping the canvas at most 3:1."""
    maximum_for_provider = 3 * height - width
    if maximum_for_provider < 1:
        raise ToolError(
            "INVALID_IMAGE",
            "Background source is too wide to outpaint within the provider's "
            "3:1 canvas limit.",
        )
    return min(max(1, width // 2), maximum_for_provider)


def _make_overlap_composite(
    src: Path, dst: Path, blank_color: tuple[int, int, int, int]
) -> tuple[Image.Image, int, int, int]:
    with Image.open(src) as opened:
        image = opened.convert("RGBA")
    width, height = image.size
    if width < 1 or height < 1:
        raise ToolError("INVALID_IMAGE", f"Degenerate image size {width}x{height}.")
    overlap_width = _overlap_width(width, height)
    overlap = image.crop((width - overlap_width, 0, width, height))
    canvas = Image.new("RGBA", (overlap_width + width, height), blank_color)
    canvas.paste(overlap, (0, 0))
    dst.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(dst, format="PNG")
    return overlap, width, height, overlap_width


def _cover_crop(image: Image.Image, target: tuple[int, int]) -> Image.Image:
    """Aspect-preserving cover resize followed by a centered crop."""
    target_width, target_height = target
    if image.size == target:
        return image.convert("RGBA")
    source_width, source_height = image.size
    if source_width < 1 or source_height < 1:
        raise ToolError("INVALID_IMAGE", "Provider returned a degenerate image.")
    scale = max(target_width / source_width, target_height / source_height)
    resized_width = max(target_width, ceil(source_width * scale))
    resized_height = max(target_height, ceil(source_height * scale))
    resized = image.convert("RGBA").resize(
        (resized_width, resized_height),
        Image.Resampling.LANCZOS,
    )
    left = (resized_width - target_width) // 2
    top = (resized_height - target_height) // 2
    return resized.crop((left, top, left + target_width, top + target_height))


def extend_right(
    config: Config,
    *,
    input_path: Path,
    output: Path,
    prompt: str | None = None,
    size: str | None = None,
    quality: str = "low",
    blank_color: tuple[int, int, int, int] = (255, 255, 255, 255),
    review_output: Path | None = None,
) -> dict[str, Any]:
    if not input_path.is_file():
        raise ToolError("INPUT_FILE_NOT_FOUND", f"input image not found: {input_path}")

    review_base = review_output or output
    composite = review_base.with_name(f"{review_base.stem}-composite.png")
    outpaint_full = review_base.with_name(f"{review_base.stem}-outpaint-full.png")
    composite.parent.mkdir(parents=True, exist_ok=True)
    output.parent.mkdir(parents=True, exist_ok=True)

    overlap, src_w, src_h, overlap_width = _make_overlap_composite(
        input_path,
        composite,
        blank_color,
    )
    canvas_size = (overlap_width + src_w, src_h)
    api_size = size or f"{canvas_size[0]}x{canvas_size[1]}"

    with tempfile.TemporaryDirectory(prefix="bg-outpaint-provider-") as temp_dir:
        provider_output = Path(temp_dir) / f"{uuid.uuid4().hex}.png"
        provider_result = image_gen.edit_image(
            config,
            prompt=prompt or DEFAULT_PROMPT,
            input_path=str(composite),
            output=provider_output,
            size=api_size,
            quality=quality,
            enforce_safe_bg_avoidance=False,
            subject_margin=False,
            framing_profile="full_scene",
            asset_type="background",
        )
        if not provider_output.is_file():
            raise ToolError(
                "INVALID_IMAGE",
                "Background edit provider did not produce an output image.",
            )
        try:
            with Image.open(provider_output) as opened:
                normalized = _cover_crop(opened, canvas_size)
        except OSError as exc:
            raise ToolError(
                "INVALID_IMAGE",
                "Background edit provider returned an unreadable image.",
            ) from exc

    # Never trust prompt-only preservation. Restore the exact source overlap
    # before either review evidence or the runtime crop is persisted.
    normalized.paste(overlap, (0, 0))
    normalized.save(outpaint_full, format="PNG")
    runtime = normalized.crop((overlap_width, 0, overlap_width + src_w, src_h))
    runtime.save(output, format="PNG")

    result = {
        "output": str(output),
        "input": str(input_path),
        "size": f"{src_w}x{src_h}",
        "api_size": api_size,
        "canvas_size": f"{canvas_size[0]}x{canvas_size[1]}",
        "overlap_width": overlap_width,
        "composite": str(composite),
        "outpaint_full": str(outpaint_full),
        "intermediates": str(composite.parent),
    }
    if isinstance(provider_result, dict) and provider_result.get("request_id"):
        result["request_id"] = str(provider_result["request_id"])
    return result


__all__ = ["DEFAULT_PROMPT", "extend_right"]
