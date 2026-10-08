"""RMBG-2.0 background-removal adapter — HTTP client to the remote service.

Replaces the standalone ``rmbg2_image_matting.py`` script. Wraps the
single ``POST /v1/images/remove-background`` endpoint and adds parallel
batch processing for directory inputs.

The service returns a lossless soft-alpha RGBA PNG. Optional ``bg_hex``
+ ``key_hard`` / ``key_soft`` (color-aug rescue) keeps particles /
external effects that pure RMBG would clip — this is the
``frost_druid_magenta_v3`` recipe, executed server-side.
"""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from io import BytesIO
from pathlib import Path
from typing import Any

from PIL import Image, UnidentifiedImageError

from ..config import Config
from ..errors import ToolError
from ..providers import get_provider

# Presets from frost_druid_magenta_v3/README.md §3.3 (key_hard, key_soft).
# Lower numbers = more aggressive rescue. ``balanced`` is the recommended
# starting point for magenta-background sprites.
AUG_PRESETS: dict[str, tuple[int, int] | None] = {
    "off":               None,
    "aggressive":        (15, 40),
    "medium":            (30, 60),
    "balanced":          (50, 80),
    "conservative":      (70, 100),
    "more-conservative": (90, 120),
    "very-conservative": (110, 140),
    "nearly-off":        (130, 170),
}

IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp"}
MAX_RMBG_OUTPUT_BYTES = 64 * 1024 * 1024
MAX_RMBG_OUTPUT_PIXELS = 64 * 1024 * 1024


def validate_rmbg_output(data: bytes) -> None:
    """Reject malformed or non-alpha provider responses before caching success."""

    if not data or len(data) > MAX_RMBG_OUTPUT_BYTES:
        raise ToolError(
            "RMBG2_FAILED",
            "RMBG2 returned an empty or oversized image response.",
        )
    try:
        with Image.open(BytesIO(data)) as image:
            if image.format != "PNG" or "A" not in image.getbands():
                raise ValueError("expected an alpha-bearing PNG")
            width, height = image.size
            if (
                width < 1
                or height < 1
                or width * height > MAX_RMBG_OUTPUT_PIXELS
            ):
                raise ValueError("decoded image dimensions exceed the safety limit")
            image.load()
    except (OSError, UnidentifiedImageError, ValueError) as exc:
        raise ToolError(
            "RMBG2_FAILED",
            "RMBG2 returned a malformed image response.",
            hint=str(exc),
        ) from exc


def resolve_aug(
    preset: str,
    hard: int | None,
    soft: int | None,
) -> tuple[int, int] | None:
    """Explicit ``hard``/``soft`` always wins over preset name."""
    if hard is not None and soft is not None:
        return (hard, soft)
    return AUG_PRESETS.get(preset)


def _process_one(
    src: Path,
    dst: Path,
    *,
    config: Config,
    endpoint: str | None,
    bg_hex: str | None,
    aug: tuple[int, int] | None,
    rmbg_fg_cutoff: int,
    max_size: int | None,
    timeout: int,
) -> dict[str, Any]:
    if not src.is_file():
        raise ToolError(
            "INPUT_FILE_NOT_FOUND", f"RMBG2 input not found: {src}"
        )
    img_bytes = src.read_bytes()
    t0 = time.time()
    out_bytes, meta = get_provider(config).rmbg_matte(
        config,
        img_bytes,
        endpoint=endpoint,
        bg_hex=bg_hex,
        aug=aug,
        rmbg_fg_cutoff=rmbg_fg_cutoff,
        max_size=max_size,
        timeout=timeout,
    )
    validate_rmbg_output(out_bytes)
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_bytes(out_bytes)
    meta["client_total_ms"] = int((time.time() - t0) * 1000)
    meta["input"] = str(src)
    meta["output"] = str(dst)
    return meta


def remove_background(
    config: Config,
    *,
    input_path: Path | None = None,
    output: Path | None = None,
    input_dir: Path | None = None,
    output_dir: Path | None = None,
    endpoint: str | None = None,
    bg_hex: str | None = None,
    aug_preset: str = "off",
    aug_hard: int | None = None,
    aug_soft: int | None = None,
    rmbg_fg_cutoff: int = 230,
    max_size: int | None = None,
    max_workers: int = 8,
    timeout: int = 60,
) -> dict[str, Any]:
    """Remove background via remote RMBG-2.0 service.

    Exactly one of ``input_path`` / ``input_dir`` must be set.
    """
    if (input_path is None) == (input_dir is None):
        raise ToolError(
            "INVALID_CONFIG",
            "Specify exactly one of `input` or `input_dir`.",
        )

    if aug_preset not in AUG_PRESETS:
        raise ToolError(
            "INVALID_CONFIG",
            f"Unknown aug_preset: {aug_preset!r}",
            hint=f"One of: {', '.join(AUG_PRESETS)}",
        )

    aug = resolve_aug(aug_preset, aug_hard, aug_soft) if bg_hex else None
    ep = endpoint or config.rmbg2.endpoint

    # ---- single image ----
    if input_path is not None:
        if output is None:
            output = input_path.with_name(input_path.stem + "_matted.png")
        meta = _process_one(
            input_path,
            output,
            config=config,
            endpoint=ep,
            bg_hex=bg_hex,
            aug=aug,
            rmbg_fg_cutoff=rmbg_fg_cutoff,
            max_size=max_size,
            timeout=timeout,
        )
        return {
            "mode": "single",
            "endpoint": ep,
            "color_aug": bool(bg_hex and aug is not None),
            "aug": {"hard": aug[0], "soft": aug[1]} if aug else None,
            "output": str(output),
            "server_meta": meta,
        }

    # ---- batch dir ----
    assert input_dir is not None
    if not input_dir.is_dir():
        raise ToolError(
            "INPUT_FILE_NOT_FOUND",
            f"RMBG2 input_dir is not a directory: {input_dir}",
        )
    if output_dir is None:
        output_dir = input_dir.parent / (input_dir.name + "_matted")
    output_dir.mkdir(parents=True, exist_ok=True)

    sources = sorted(
        p for p in input_dir.iterdir()
        if p.is_file() and p.suffix.lower() in IMAGE_SUFFIXES
    )
    if not sources:
        raise ToolError(
            "INPUT_FILE_NOT_FOUND",
            f"No images ({', '.join(IMAGE_SUFFIXES)}) under {input_dir}.",
        )

    def _job(src: Path) -> dict[str, Any]:
        dst = output_dir / f"{src.stem}_matted.png"
        return _process_one(
            src,
            dst,
            config=config,
            endpoint=ep,
            bg_hex=bg_hex,
            aug=aug,
            rmbg_fg_cutoff=rmbg_fg_cutoff,
            max_size=max_size,
            timeout=timeout,
        )

    t0 = time.time()
    results: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=max(1, max_workers)) as ex:
        futs = {ex.submit(_job, s): s for s in sources}
        for fut in as_completed(futs):
            src = futs[fut]
            try:
                results.append(fut.result())
            except ToolError as e:
                failures.append({"input": str(src), "code": e.code, "message": e.message})
            except Exception as e:  # noqa: BLE001
                failures.append({"input": str(src), "code": "INTERNAL_ERROR", "message": str(e)})

    elapsed_s = round(time.time() - t0, 2)

    if failures and not results:
        raise ToolError(
            "RMBG2_FAILED",
            f"All {len(sources)} images failed RMBG2 matting.",
            hint=str(failures[0])[:300],
        )

    return {
        "mode": "batch",
        "endpoint": ep,
        "color_aug": bool(bg_hex and aug is not None),
        "aug": {"hard": aug[0], "soft": aug[1]} if aug else None,
        "input_dir": str(input_dir),
        "output_dir": str(output_dir),
        "total": len(sources),
        "succeeded": len(results),
        "failed": len(failures),
        "elapsed_s": elapsed_s,
        "failures": failures[:10],  # cap to keep payload small
    }


__all__ = [
    "AUG_PRESETS",
    "remove_background",
    "resolve_aug",
    "validate_rmbg_output",
]
