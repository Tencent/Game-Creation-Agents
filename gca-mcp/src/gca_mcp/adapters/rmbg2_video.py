"""Video-matting adapter with RMBG-2.0 and deterministic color-key support.

Per-frame parallel calls to the configured matting backend (the same one
``rmbg2_matting`` uses, via ``providers.get_provider().rmbg_matte``), then assembles the
RGBA PNG sequence and encodes the requested output formats via
``ffmpeg``. In ``auto`` mode, a fallback-eligible RMBG outage remattes the entire
clip with one color-key backend so a published animation never mixes edge methods.

ffmpeg is mandatory; cv2 is intentionally avoided (heavy dep). Frames
are extracted via ``ffmpeg -i in.mp4 frames/%04d.png`` and read back
with Pillow.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

from ..config import Config
from ..errors import ToolError, is_transient_rmbg_error
from ..providers import get_provider
from ..runners.assets.color_key import remove_bg_color
from . import matting_availability
from . import rmbg2 as rmbg2_adapter

SUPPORTED_FORMATS = ("gif", "webm", "mov", "mp4_preview")


def _require_ffmpeg() -> None:
    if shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None:
        raise ToolError(
            "FFMPEG_MISSING",
            "ffmpeg/ffprobe required for rmbg2_video_matting.",
            hint="Install ffmpeg (`apt-get install ffmpeg` or `brew install ffmpeg`).",
        )


def _probe_fps(video: Path) -> float:
    proc = subprocess.run(
        [
            "ffprobe", "-v", "error",
            "-select_streams", "v:0",
            "-show_entries", "stream=r_frame_rate",
            "-of", "json",
            str(video),
        ],
        capture_output=True, text=True, check=False,
    )
    if proc.returncode != 0:
        raise ToolError(
            "VLM_CALL_FAILED",
            f"ffprobe failed for {video.name}.",
            hint=proc.stderr[-300:],
        )
    try:
        rate = json.loads(proc.stdout)["streams"][0]["r_frame_rate"]
    except (KeyError, IndexError, ValueError) as e:
        raise ToolError(
            "BAD_RESPONSE", f"ffprobe returned no frame rate: {e}"
        ) from e
    if "/" in rate:
        num, den = rate.split("/")
        return float(num) / float(den) if float(den) else 0.0
    return float(rate)


def _extract_frames(
    video: Path, frames_dir: Path, *, max_frames: int | None = None
) -> list[Path]:
    """Extract frames from video. When *max_frames* is set, uniformly
    sample at most that many frames (reduces matting cost and sheet size).
    """
    frames_dir.mkdir(parents=True, exist_ok=True)
    cmd: list[str] = ["ffmpeg", "-y", "-loglevel", "error", "-i", str(video)]
    if max_frames and max_frames > 0:
        cmd += ["-vf", f"select='not(mod(n\\,{_calc_step(video, max_frames)}))',setpts=N/FRAME_RATE/TB"]
        cmd += ["-vsync", "vfr"]
    else:
        cmd += ["-vsync", "0"]
    cmd.append(str(frames_dir / "%04d.png"))
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise ToolError(
            "VLM_CALL_FAILED",
            f"ffmpeg frame extraction failed for {video.name}.",
            hint=proc.stderr[-300:],
        )
    return sorted(frames_dir.glob("*.png"))


def _calc_step(video: Path, max_frames: int) -> int:
    """Compute frame step N so that selecting every Nth frame yields ~max_frames."""
    total = _count_frames(video)
    if total <= max_frames:
        return 1
    return max(1, total // max_frames)


def _count_frames(video: Path) -> int:
    """Probe total frame count via ffprobe."""
    proc = subprocess.run(
        [
            "ffprobe", "-v", "error", "-select_streams", "v:0",
            "-count_frames", "-show_entries", "stream=nb_read_frames",
            "-of", "json", str(video),
        ],
        capture_output=True, text=True, check=False,
    )
    try:
        data = json.loads(proc.stdout)
        return int(data["streams"][0]["nb_read_frames"])
    except (ValueError, KeyError, IndexError):
        return 9999


def _matte_frame(
    src: Path,
    dst: Path,
    *,
    provider: Any,
    config: Config,
    endpoint: str | None,
    bg_hex: str | None,
    aug: tuple[int, int] | None,
    rmbg_fg_cutoff: int,
    max_size: int | None,
    timeout: int,
) -> None:
    """One frame: read PNG → configured matte operation → write RGBA PNG."""
    img_bytes = src.read_bytes()
    out_bytes, _meta = provider.rmbg_matte(
        config,
        img_bytes,
        endpoint=endpoint,
        bg_hex=bg_hex,
        aug=aug,
        rmbg_fg_cutoff=rmbg_fg_cutoff,
        max_size=max_size,
        timeout=timeout,
    )
    rmbg2_adapter.validate_rmbg_output(out_bytes)
    dst.write_bytes(out_bytes)


def _matte_frame_color_key(
    src: Path,
    dst: Path,
    *,
    key_color: str,
    threshold: int,
    edge_threshold: int,
) -> None:
    """Matte one extracted frame with the configured deterministic key."""
    from PIL import Image

    try:
        with Image.open(src) as image:
            matted = remove_bg_color(
                image,
                key_color=key_color,
                threshold=threshold,
                edge_threshold=edge_threshold,
            )
        dst.parent.mkdir(parents=True, exist_ok=True)
        matted.save(dst, format="PNG")
    except (OSError, ValueError) as exc:
        raise ToolError(
            "COLOR_KEY_MATTING_FAILED",
            f"Color-key video matting failed for {src.name}.",
            hint=str(exc),
        ) from exc


def _matte_frames_color_key(
    frames: list[Path],
    output_dir: Path,
    *,
    key_color: str | None,
    threshold: int,
    edge_threshold: int,
    workers: int,
) -> None:
    """Matte one whole clip atomically with color-key and fail closed."""
    if not isinstance(key_color, str) or not key_color:
        raise ToolError(
            "INVALID_CONFIG",
            "Color-key video fallback requires a configured `bg_hex`.",
        )
    if output_dir.exists():
        shutil.rmtree(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    def _job(src: Path) -> ToolError | None:
        try:
            _matte_frame_color_key(
                src,
                output_dir / src.name,
                key_color=key_color,
                threshold=threshold,
                edge_threshold=edge_threshold,
            )
            return None
        except ToolError as exc:
            return exc
        except Exception as exc:  # noqa: BLE001
            return ToolError(
                "INTERNAL_ERROR",
                f"Color-key video matting failed for {src.name}: {exc}",
            )

    failures: list[ToolError] = []
    with ThreadPoolExecutor(max_workers=max(1, workers)) as executor:
        for failure in executor.map(_job, frames):
            if failure is not None:
                failures.append(failure)
    if failures:
        shutil.rmtree(output_dir, ignore_errors=True)
        raise failures[0]


def align_rgba_frames_to_bottom(
    rgba_dir: Path,
    *,
    margin_px: int = 0,
    alpha_threshold: int = 8,
) -> dict[str, Any]:
    """Translate every RGBA PNG so its alpha-bbox bottom edge sits
    ``margin_px`` above the canvas bottom.

    Used as the post-matting "feet glue" pass: even when seedance
    drifts the character upward across frames, this pulls every
    matted frame back down so the lowest opaque pixel touches
    canvas bottom (with optional margin).

    Translation is purely vertical — horizontal composition is left
    untouched. Frames with no opaque content are skipped.

    Returns aggregate stats: how many frames shifted, max/median dy,
    how many frames were already aligned.
    """
    import numpy as np
    from PIL import Image

    pngs = sorted(rgba_dir.glob("*.png"))
    if not pngs:
        return {"frames": 0, "shifted": 0, "skipped_empty": 0}

    shifted = 0
    skipped_empty = 0
    dys: list[int] = []
    for p in pngs:
        im = Image.open(p).convert("RGBA")
        arr = np.array(im)
        a = arr[:, :, 3]
        H = arr.shape[0]

        rows_with_content = np.where((a > alpha_threshold).any(axis=1))[0]
        if len(rows_with_content) == 0:
            skipped_empty += 1
            continue

        bbox_bottom = int(rows_with_content.max())
        target_bottom = H - 1 - max(0, int(margin_px))
        dy = target_bottom - bbox_bottom

        if dy == 0:
            dys.append(0)
            continue

        out = np.zeros_like(arr)
        if dy > 0:
            # Shift content DOWN by dy: rows [0 .. H-1-dy] go to [dy .. H-1]
            out[dy:, :] = arr[: H - dy, :]
        else:
            # Shift content UP (rare — character was already below target)
            shift = -dy
            out[: H - shift, :] = arr[shift:, :]

        Image.fromarray(out).save(p)
        shifted += 1
        dys.append(dy)

    if not dys:
        return {
            "frames": len(pngs),
            "shifted": 0,
            "skipped_empty": skipped_empty,
        }

    abs_dys = sorted(abs(d) for d in dys)
    median = abs_dys[len(abs_dys) // 2]
    return {
        "frames": len(pngs),
        "shifted": shifted,
        "skipped_empty": skipped_empty,
        "max_abs_dy_px": int(max(abs_dys)),
        "median_abs_dy_px": int(median),
        "margin_px": int(margin_px),
    }


def _parse_hex_rgb(bg_hex: str) -> tuple[int, int, int]:
    """Parse ``#RRGGBB`` (or ``RRGGBB``) into an ``(r, g, b)`` tuple."""
    h = bg_hex.strip().lstrip("#")
    if len(h) != 6:
        raise ToolError(
            "INVALID_CONFIG",
            f"bg_hex must be #RRGGBB, got {bg_hex!r}.",
        )
    try:
        return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16))
    except ValueError as e:
        raise ToolError(
            "INVALID_CONFIG", f"bg_hex is not valid hex: {bg_hex!r}"
        ) from e


def drop_bg_leak_frames(
    rgba_dir: Path,
    bg_hex: str,
    *,
    area_threshold: float = 0.005,
    alpha_threshold: int = 128,
    color_dist: int = 120,
    keep_min_frames: int = 2,
    alpha_weighted: bool = True,
) -> dict[str, Any]:
    """Delete matted frames where the background color leaked through.

    Two failure modes are detected by the same metric:
      1. **Hard leak** — RMBG kept large opaque regions of the safe
         color (e.g. magenta tail frames where the whole canvas is
         opaque #FF00FF).
      2. **Soft / semi-transparent leak** — translucent FX (alpha
         30-127) drag in lots of magenta-ish color that, when
         composited in-engine, paints the whole frame magenta-tinted.
         A pure ``alpha > alpha_threshold`` cutoff misses this.

    With ``alpha_weighted=True`` (default) each near-bg pixel is
    counted as ``alpha / 255`` instead of 0/1. Hard fully-opaque
    leaks contribute their full 1.0; semi-transparent magenta floods
    contribute proportionally; truly transparent regions contribute
    0. This single metric handles both modes without false-positives
    on edge spill.

    With ``alpha_weighted=False`` the old hard-cutoff metric is used
    (count pixels where ``alpha > alpha_threshold`` AND near bg) for
    parity with older calibrations.

    Frames whose score exceeds ``area_threshold`` are deleted, and
    survivors are renumbered to a contiguous ``%04d.png`` sequence.

    Safety: if dropping would leave fewer than ``keep_min_frames``
    frames, the purge is skipped entirely so the validator can rank
    a degraded clip low rather than getting an empty sequence.
    """
    import numpy as np
    from PIL import Image

    pngs = sorted(rgba_dir.glob("*.png"))
    if not pngs:
        return {"frames": 0, "dropped": 0, "kept": 0, "skipped": "no_frames"}

    br, bg, bb = _parse_hex_rgb(bg_hex)
    dist_sq_cutoff = float(color_dist) ** 2

    fractions: dict[str, float] = {}
    to_drop: list[Path] = []
    for p in pngs:
        arr = np.asarray(Image.open(p).convert("RGBA"), dtype=np.int32)
        a = arr[:, :, 3]
        total = a.shape[0] * a.shape[1]
        dist_sq = (
            (arr[:, :, 0] - br) ** 2
            + (arr[:, :, 1] - bg) ** 2
            + (arr[:, :, 2] - bb) ** 2
        )
        near = dist_sq <= dist_sq_cutoff
        if alpha_weighted:
            # Each near-bg pixel contributes alpha/255 to the score.
            score = float((a * near).sum()) / 255.0 / float(max(1, total))
        else:
            opaque = a > alpha_threshold
            score = float((opaque & near).sum()) / float(max(1, total))
        fractions[p.name] = round(score, 4)
        if score > area_threshold:
            to_drop.append(p)

    kept_count = len(pngs) - len(to_drop)
    if kept_count < keep_min_frames:
        return {
            "frames": len(pngs),
            "dropped": 0,
            "kept": len(pngs),
            "skipped": "would_empty",
            "area_threshold": area_threshold,
            "alpha_weighted": alpha_weighted,
            "max_leak_fraction": max(fractions.values()) if fractions else 0.0,
            "leak_fractions": fractions,
        }

    drop_set = {p.name for p in to_drop}
    for p in to_drop:
        p.unlink()

    survivors = sorted(p for p in rgba_dir.glob("*.png"))
    staged: list[Path] = []
    for p in survivors:
        tmp = p.with_suffix(p.suffix + ".tmp")
        p.rename(tmp)
        staged.append(tmp)
    for i, tmp in enumerate(staged, start=1):
        tmp.rename(rgba_dir / f"{i:04d}.png")

    return {
        "frames": len(pngs),
        "dropped": len(to_drop),
        "kept": kept_count,
        "dropped_frames": sorted(drop_set),
        "area_threshold": area_threshold,
        "alpha_threshold": alpha_threshold,
        "color_dist": color_dist,
        "alpha_weighted": alpha_weighted,
        "max_leak_fraction": max(fractions.values()) if fractions else 0.0,
        "leak_fractions": fractions,
    }


def _make_global_palette_gif(
    rgba_dir: Path,
    out_gif: Path,
    fps: int,
    *,
    alpha_threshold: int = 128,
    max_size: int = 960,
) -> None:
    """Pillow-based global-palette transparent GIF (one palette across frames)."""
    from PIL import Image  # local — Pillow already a dep

    pngs = sorted(rgba_dir.glob("*.png"))
    if not pngs:
        raise ToolError("RMBG2_FAILED", "No matted frames available for GIF encoding.")

    first = Image.open(pngs[0])
    if max(first.size) > max_size:
        s = max_size / max(first.size)
        target = (int(first.size[0] * s), int(first.size[1] * s))
    else:
        target = first.size

    frames_rgb, masks = [], []
    for p in pngs:
        img = Image.open(p)
        if img.mode != "RGBA":
            img = img.convert("RGBA")
        if img.size != target:
            img = img.resize(target, Image.LANCZOS)
        rgb = img.convert("RGB")
        a = img.split()[-1]
        m = a.point(lambda v: 255 if v >= alpha_threshold else 0, mode="L")
        frames_rgb.append(rgb)
        masks.append(m)

    W, H = target
    N = len(frames_rgb)
    KEY = (255, 0, 254)
    canvas = Image.new("RGB", (W, H * N), KEY)
    for i, (rgb, mask) in enumerate(zip(frames_rgb, masks)):
        canvas.paste(rgb, (0, i * H), mask)
    paletted = canvas.convert(
        "P", palette=Image.Palette.ADAPTIVE, colors=255, dither=Image.Dither.NONE
    )
    palette = list(paletted.getpalette() or [])
    while len(palette) < 256 * 3:
        palette.append(0)
    key_index = min(
        range(256),
        key=lambda i: (palette[i*3] - KEY[0]) ** 2
        + (palette[i*3+1] - KEY[1]) ** 2
        + (palette[i*3+2] - KEY[2]) ** 2,
    )
    if key_index != 0:
        lut = list(range(256))
        lut[0], lut[key_index] = key_index, 0
        paletted = Image.frombytes(
            "P", paletted.size, paletted.tobytes().translate(bytes(lut))
        )
        for ch in range(3):
            palette[ch], palette[key_index*3+ch] = palette[key_index*3+ch], palette[ch]
    palette[0:3] = [255, 255, 255]
    paletted.putpalette(palette)
    out_frames = [paletted.crop((0, i * H, W, (i + 1) * H)) for i in range(N)]
    duration_ms = max(20, int(1000 / fps))
    out_gif.parent.mkdir(parents=True, exist_ok=True)
    out_frames[0].save(
        out_gif,
        format="GIF",
        save_all=True,
        append_images=out_frames[1:],
        duration=duration_ms,
        loop=0,
        disposal=2,
        transparency=0,
        background=0,
        optimize=False,
    )


def _make_preview_on_solid_gif(
    rgba_dir: Path,
    out_gif: Path,
    fps: int,
    *,
    bg_rgb: tuple[int, int, int] = (255, 255, 255),
    alpha_threshold: int = 128,
    max_size: int = 960,
) -> None:
    """Composite every RGBA frame onto a solid background and write a GIF.

    Used as a sanity-check preview alongside ``transparent.gif``. The
    transparent GIF can be misleading when a viewer ignores alpha (or
    when the RGBA PNG keeps RGB data under alpha=0) — magenta RGB
    bleeds visually even though the pixel composites as background
    in-game. Compositing onto a solid color shows the user *exactly*
    what the sprite looks like in-game, with no alpha-rendering
    quirks.
    """
    from PIL import Image

    pngs = sorted(rgba_dir.glob("*.png"))
    if not pngs:
        raise ToolError(
            "RMBG2_FAILED",
            "No matted frames available for preview encoding.",
        )

    first = Image.open(pngs[0])
    if max(first.size) > max_size:
        s = max_size / max(first.size)
        target = (int(first.size[0] * s), int(first.size[1] * s))
    else:
        target = first.size

    composited: list[Image.Image] = []
    for p in pngs:
        img = Image.open(p)
        if img.mode != "RGBA":
            img = img.convert("RGBA")
        if img.size != target:
            img = img.resize(target, Image.LANCZOS)
        # Hard 1-bit alpha so the preview matches what GIF encoding
        # would actually keep (rather than blending soft edges with
        # the bg color, which would make leaks look milder than they
        # really are).
        a = img.split()[-1].point(
            lambda v: 255 if v >= alpha_threshold else 0, mode="L"
        )
        bg = Image.new("RGB", target, bg_rgb)
        bg.paste(img.convert("RGB"), mask=a)
        composited.append(bg)

    duration_ms = max(20, int(1000 / fps))
    out_gif.parent.mkdir(parents=True, exist_ok=True)
    composited[0].save(
        out_gif,
        format="GIF",
        save_all=True,
        append_images=composited[1:],
        duration=duration_ms,
        loop=0,
        optimize=False,
    )


def _encode_outputs(
    rgba_dir: Path,
    output_dir: Path,
    fps: float,
    formats: list[str],
    *,
    gif_max_size: int = 960,
    gif_alpha_threshold: int = 128,
) -> dict[str, str]:
    """Encode the requested formats; soft-fail individual encoders."""
    fps_int = max(1, round(fps))
    pattern = str(rgba_dir / "%04d.png")
    out: dict[str, str] = {}
    output_dir.mkdir(parents=True, exist_ok=True)

    if "webm" in formats:
        webm = output_dir / "transparent.webm"
        proc = subprocess.run(
            [
                "ffmpeg", "-y", "-loglevel", "error",
                "-framerate", str(fps_int), "-i", pattern,
                "-c:v", "libvpx-vp9", "-pix_fmt", "yuva420p",
                "-b:v", "3M", "-auto-alt-ref", "0",
                str(webm),
            ],
            capture_output=True, text=True, check=False,
        )
        if proc.returncode == 0:
            out["webm"] = str(webm)

    if "mov" in formats:
        mov = output_dir / "transparent.mov"
        proc = subprocess.run(
            [
                "ffmpeg", "-y", "-loglevel", "error",
                "-framerate", str(fps_int), "-i", pattern,
                "-c:v", "qtrle",
                str(mov),
            ],
            capture_output=True, text=True, check=False,
        )
        if proc.returncode == 0:
            out["mov"] = str(mov)

    if "mp4_preview" in formats:
        from PIL import Image

        prev = output_dir / "preview_on_black.mp4"
        first_frame = next(iter(sorted(rgba_dir.glob("*.png"))), None)
        if first_frame is not None:
            with Image.open(first_frame) as frame:
                width, height = frame.size
            proc = subprocess.run(
                [
                    "ffmpeg", "-y", "-loglevel", "error",
                    "-framerate", str(fps_int), "-i", pattern,
                    "-f", "lavfi", "-i",
                    f"color=c=black:s={width}x{height}:r={fps_int}",
                    "-filter_complex",
                    "[1:v][0:v]overlay=shortest=1:format=auto,format=yuv420p",
                    "-c:v", "mpeg4", "-q:v", "5", "-shortest",
                    str(prev),
                ],
                capture_output=True, text=True, check=False,
            )
            if proc.returncode == 0:
                out["mp4_preview"] = str(prev)

    if "gif" in formats:
        gif = output_dir / "transparent.gif"
        try:
            _make_global_palette_gif(
                rgba_dir, gif, fps_int,
                alpha_threshold=gif_alpha_threshold,
                max_size=gif_max_size,
            )
            out["gif"] = str(gif)
        except ToolError:
            raise
        except Exception as e:  # noqa: BLE001
            # Soft-fail: GIF encode shouldn't kill the whole call if
            # other formats already succeeded.
            out["gif_error"] = str(e)

        # Sanity-check preview: composite onto solid white so a viewer
        # that ignores alpha (or one that renders alpha=0 pixels' RGB)
        # cannot fool you about how the matte actually looks in-game.
        # White was chosen because magenta-on-white reveals leaks far
        # more clearly than magenta-on-black.
        preview = output_dir / "preview_on_white.gif"
        try:
            _make_preview_on_solid_gif(
                rgba_dir, preview, fps_int,
                bg_rgb=(255, 255, 255),
                alpha_threshold=gif_alpha_threshold,
                max_size=gif_max_size,
            )
            out["preview_on_white"] = str(preview)
        except Exception as e:  # noqa: BLE001
            out["preview_on_white_error"] = str(e)

    return out


def encode_preview_from_frames(
    frames_dir: Path,
    output: Path,
    *,
    fps: float = 12.0,
) -> str | None:
    """Build an alpha-on-black preview MP4 directly from a transparent PNG
    frame set — the exact frames that ship — for content validation.

    PR #123 review #5: when ``matte_video`` fails to encode ``mp4_preview``,
    content rubrics must STILL be judged on the MATTED output, never on the raw
    Seedance MP4 (raw video cannot show defects matting introduces — an erased
    limb, a background leak). This rebuilds a preview deterministically from the
    surviving ``transparent_frames/*.png`` so the content pass always sees the
    real matte. Returns the output path, or ``None`` if there are no frames or
    ffmpeg is unavailable / fails (caller then marks the content pass
    incomplete — it must not fall back to raw video).

    The frames may be non-contiguously numbered (leak-drop renumbers, but be
    defensive): they are symlinked/copied into a temp dir as a gap-free
    ``%04d.png`` sequence before encoding, since ffmpeg's image demuxer needs a
    contiguous pattern.
    """
    pngs = sorted(frames_dir.glob("*.png")) if frames_dir.is_dir() else []
    if not pngs:
        return None
    if shutil.which("ffmpeg") is None:
        return None

    fps_int = max(1, round(fps))
    output.parent.mkdir(parents=True, exist_ok=True)
    from PIL import Image

    with Image.open(pngs[0]) as first_frame:
        width, height = first_frame.size
    with tempfile.TemporaryDirectory(prefix="preview_seq_") as tmp:
        seq_dir = Path(tmp)
        for i, src in enumerate(pngs, start=1):
            shutil.copy2(src, seq_dir / f"{i:04d}.png")
        proc = subprocess.run(
            [
                "ffmpeg", "-y", "-loglevel", "error",
                "-framerate", str(fps_int),
                "-i", str(seq_dir / "%04d.png"),
                "-f", "lavfi", "-i",
                f"color=c=black:s={width}x{height}:r={fps_int}",
                "-filter_complex",
                "[1:v][0:v]overlay=shortest=1:format=auto,format=yuv420p",
                "-c:v", "mpeg4", "-q:v", "5", "-shortest",
                str(output),
            ],
            capture_output=True, text=True, check=False,
        )
    return str(output) if proc.returncode == 0 and output.is_file() else None


def assess_matte_health(
    frames_dir: Path,
    *,
    frames_total: int,
    frames_failed: int,
    bg_leak_stats: dict[str, Any] | None = None,
    min_frames: int = 2,
    min_success_ratio: float = 0.5,
    min_visible_alpha_ratio: float = 0.01,
    min_visible_frame_ratio: float = 0.9,
    min_transparent_area_ratio: float = 0.05,
    min_transparent_border_ratio: float = 0.5,
    alpha_threshold: int = 16,
) -> dict[str, Any]:
    """Judge whether a matted frame set is HEALTHY enough to ship.

    PR #123 review #5: the publication gate used to treat an asset as usable
    whenever ONE PNG existed in ``transparent_frames/`` — even a fully
    alpha-zero (blank) frame, a 1-of-120 survival rate, or a ``would_empty``
    leak state. This computes explicit health signals so the orchestrator can
    reject those cases:

      - ``enough_frames``      — at least ``min_frames`` frames survived.
      - ``success_ratio_ok``   — kept/total ≥ ``min_success_ratio``; this counts
                                 RMBG failures and leak-filter drops.
      - ``visible_frames_ok``  — nearly every kept frame has a meaningful
                                 visible-alpha area; one good frame cannot hide
                                 an otherwise empty animation.
      - ``transparent_frames_ok`` — nearly every frame retains a meaningful
                                    transparent area AND transparent canvas
                                    border; an opaque plate with a small clear
                                    hole is not a successful matte.
      - ``leak_ok``            — the leak purge did not bail out with
                                 ``would_empty`` (which means matting effectively
                                 failed across the whole clip).

    ``healthy`` is the AND of all four. Returns the flags + measured numbers so
    the caller can log exactly why a take was rejected.
    """
    import numpy as np
    from PIL import Image

    pngs = sorted(frames_dir.glob("*.png")) if frames_dir.is_dir() else []
    n_kept = len(pngs)
    enough_frames = n_kept >= min_frames

    success_ratio = n_kept / frames_total if frames_total > 0 else 0.0
    success_ratio_ok = success_ratio >= min_success_ratio

    # The large majority of kept frames must contain a meaningful subject.
    max_alpha_ratio = 0.0
    visible_frame_count = 0
    transparent_frame_count = 0
    transparent_border_frame_count = 0
    for p in pngs:
        try:
            with Image.open(str(p)) as im:
                a = np.array(im.convert("RGBA"))[:, :, 3]
        except Exception:  # noqa: BLE001
            continue
        if a.size:
            ratio = float((a > alpha_threshold).sum()) / float(a.size)
            max_alpha_ratio = max(max_alpha_ratio, ratio)
            if ratio >= min_visible_alpha_ratio:
                visible_frame_count += 1
            transparent_ratio = float((a <= alpha_threshold).sum()) / float(
                a.size
            )
            if transparent_ratio >= min_transparent_area_ratio:
                transparent_frame_count += 1
            border = np.concatenate(
                [a[0, :], a[-1, :], a[:, 0], a[:, -1]]
            )
            border_transparent_ratio = float(
                (border <= alpha_threshold).sum()
            ) / float(max(1, border.size))
            if border_transparent_ratio >= min_transparent_border_ratio:
                transparent_border_frame_count += 1
    visible_frame_ratio = (
        visible_frame_count / n_kept if n_kept > 0 else 0.0
    )
    visible_frames_ok = visible_frame_ratio >= min_visible_frame_ratio
    has_visible_alpha = visible_frame_count > 0
    transparent_frame_ratio = (
        transparent_frame_count / n_kept if n_kept > 0 else 0.0
    )
    transparent_frames_ok = (
        transparent_frame_ratio >= min_visible_frame_ratio
    )
    transparent_border_frame_ratio = (
        transparent_border_frame_count / n_kept if n_kept > 0 else 0.0
    )
    transparent_borders_ok = (
        transparent_border_frame_ratio >= min_visible_frame_ratio
    )

    leak_skip = (bg_leak_stats or {}).get("skipped")
    leak_ok = leak_skip != "would_empty"

    healthy = (
        enough_frames
        and success_ratio_ok
        and visible_frames_ok
        and transparent_frames_ok
        and transparent_borders_ok
        and leak_ok
    )
    return {
        "healthy": healthy,
        "n_kept": n_kept,
        "enough_frames": enough_frames,
        "success_ratio": round(success_ratio, 3),
        "success_ratio_ok": success_ratio_ok,
        "frames_failed": frames_failed,
        "max_visible_alpha_ratio": round(max_alpha_ratio, 4),
        "has_visible_alpha": has_visible_alpha,
        "visible_frame_count": visible_frame_count,
        "visible_frame_ratio": round(visible_frame_ratio, 3),
        "visible_frames_ok": visible_frames_ok,
        "transparent_frame_count": transparent_frame_count,
        "transparent_frame_ratio": round(transparent_frame_ratio, 3),
        "transparent_frames_ok": transparent_frames_ok,
        "transparent_border_frame_count": transparent_border_frame_count,
        "transparent_border_frame_ratio": round(
            transparent_border_frame_ratio, 3
        ),
        "transparent_borders_ok": transparent_borders_ok,
        "leak_ok": leak_ok,
        "leak_skipped": leak_skip,
    }


def matte_video(
    config: Config,
    *,
    input_video: Path,
    output_dir: Path,
    formats: list[str] | None = None,
    endpoint: str | None = None,
    bg_hex: str | None = None,
    aug_preset: str = "off",
    aug_hard: int | None = None,
    aug_soft: int | None = None,
    rmbg_fg_cutoff: int = 230,
    max_size: int | None = 960,
    workers: int = 8,
    gif_max_size: int = 960,
    gif_alpha_threshold: int = 200,
    keep_frames: bool = True,
    timeout: int = 60,
    align_feet_to_bottom: bool = False,
    foot_margin_px: int = 0,
    max_frames: int | None = None,
    drop_bg_leak: bool = True,
    bg_leak_area_threshold: float = 0.005,
    bg_leak_color_dist: int = 120,
    bg_leak_alpha_threshold: int = 128,
    bg_leak_keep_min_frames: int = 2,
    bg_leak_alpha_weighted: bool = True,
) -> dict[str, Any]:
    """Matte every frame with one resolved backend and encode outputs.

    When ``align_feet_to_bottom=True``, every matted RGBA frame is
    vertically translated so its alpha-bbox bottom equals
    ``H - 1 - foot_margin_px``. This is the safety net for cases
    where seedance drifts the character upward across frames despite
    the prompt-level "feet at bottom" constraint.
    """
    if not input_video.is_file():
        raise ToolError(
            "INPUT_FILE_NOT_FOUND", f"Video not found: {input_video}"
        )

    formats = formats or ["gif"]
    bad = [f for f in formats if f not in SUPPORTED_FORMATS]
    if bad:
        raise ToolError(
            "INVALID_CONFIG",
            f"Unknown format(s): {bad}",
            hint=f"Allowed: {', '.join(SUPPORTED_FORMATS)}",
        )

    if aug_preset not in rmbg2_adapter.AUG_PRESETS:
        raise ToolError(
            "INVALID_CONFIG",
            f"Unknown aug_preset: {aug_preset!r}",
            hint=f"One of: {', '.join(rmbg2_adapter.AUG_PRESETS)}",
        )
    aug = (
        rmbg2_adapter.resolve_aug(aug_preset, aug_hard, aug_soft)
        if bg_hex else None
    )
    ep = endpoint or config.rmbg2.endpoint
    requested_matting = str(config.assets.matting.backend)
    if requested_matting not in {"auto", "rmbg2", "color_key"}:
        raise ToolError(
            "INVALID_CONFIG",
            f"Unsupported assets.matting.backend: {requested_matting!r}.",
        )

    _require_ffmpeg()
    output_dir.mkdir(parents=True, exist_ok=True)

    timing: dict[str, float] = {}
    with tempfile.TemporaryDirectory(prefix="rmbg2_video_", dir=output_dir) as tmp:
        tmp_path = Path(tmp)
        rgb_dir = tmp_path / "rgb_in"

        # 1. Extract RGB frames with ffmpeg (uniformly sampled if max_frames set)
        t0 = time.time()
        rgb_frames = _extract_frames(input_video, rgb_dir, max_frames=max_frames)
        if not rgb_frames:
            raise ToolError(
                "RMBG2_FAILED",
                f"ffmpeg produced no frames from {input_video.name}.",
            )
        fps = _probe_fps(input_video)
        timing["extract_s"] = round(time.time() - t0, 2)

        # 2. Resolve exactly one backend for the whole clip. Auto mode never
        # publishes a mixed RMBG/color-key sequence: any fallback-eligible service
        # failure wipes partial RMBG output and remattes every extracted frame.
        rgba_dir = output_dir / "transparent_frames"
        if rgba_dir.exists():
            shutil.rmtree(rgba_dir)
        rgba_dir.mkdir(parents=True, exist_ok=True)

        t1 = time.time()
        failures: list[str] = []
        fallback_error: ToolError | None = None
        rmbg_failures_before_fallback = 0
        resolved_matting: str
        rmbg_route = (
            matting_availability.acquire_rmbg_route(config, ep)
            if requested_matting == "auto"
            else None
        )
        if rmbg_route is not None and rmbg_route.action == "error":
            raise rmbg_route.as_error()

        if requested_matting == "color_key":
            matte_cfg = config.assets.matting
            _matte_frames_color_key(
                rgb_frames,
                rgba_dir,
                key_color=bg_hex,
                threshold=matte_cfg.color_key_threshold,
                edge_threshold=matte_cfg.color_key_edge_threshold,
                workers=workers,
            )
            resolved_matting = "color_key"
        elif rmbg_route is not None and rmbg_route.action == "color_key":
            fallback_error = rmbg_route.as_error()
            matte_cfg = config.assets.matting
            _matte_frames_color_key(
                rgb_frames,
                rgba_dir,
                key_color=bg_hex,
                threshold=matte_cfg.color_key_threshold,
                edge_threshold=matte_cfg.color_key_edge_threshold,
                workers=workers,
            )
            resolved_matting = "color_key"
        else:
            provider = get_provider(config)

            def _job(src: Path) -> tuple[Path, ToolError | None, bool]:
                dst = rgba_dir / src.name
                try:
                    _matte_frame(
                        src,
                        dst,
                        provider=provider,
                        config=config,
                        endpoint=ep,
                        bg_hex=bg_hex,
                        aug=aug,
                        rmbg_fg_cutoff=rmbg_fg_cutoff,
                        max_size=max_size,
                        timeout=timeout,
                    )
                    return src, None, False
                except ToolError as exc:
                    return src, exc, True
                except Exception as exc:  # noqa: BLE001
                    return (
                        src,
                        ToolError(
                            "INTERNAL_ERROR",
                            f"Local RMBG frame I/O failed for {src.name}: {exc}",
                        ),
                        False,
                    )

            rmbg_errors: list[tuple[Path, ToolError, bool]] = []
            remaining_frames = list(rgb_frames)
            if rmbg_route is not None and rmbg_route.action == "probe":
                probe_frame = remaining_frames.pop(0)
                _src, probe_error, provider_error = _job(probe_frame)
                if probe_error is None:
                    matting_availability.remember_rmbg_success(rmbg_route)
                else:
                    if provider_error:
                        matting_availability.remember_rmbg_error(
                            rmbg_route,
                            probe_error,
                        )
                    else:
                        matting_availability.cancel_rmbg_probe(rmbg_route)
                    rmbg_errors.append(
                        (probe_frame, probe_error, provider_error)
                    )

            if not rmbg_errors and remaining_frames:
                with ThreadPoolExecutor(max_workers=max(1, workers)) as executor:
                    futures = {
                        executor.submit(_job, frame): frame
                        for frame in remaining_frames
                    }
                    for future in as_completed(futures):
                        src, error, provider_error = future.result()
                        if error is not None:
                            rmbg_errors.append((src, error, provider_error))

            if requested_matting == "auto" and rmbg_errors:
                rmbg_errors.sort(key=lambda item: item[0].name)
                provider_errors = [
                    item for item in rmbg_errors if item[2]
                ]
                local_errors = [
                    item for item in rmbg_errors if not item[2]
                ]
                blocking_error = next(
                    (
                        error
                        for _src, error, _provider_error in provider_errors
                        if not is_transient_rmbg_error(error)
                    ),
                    None,
                )
                if blocking_error is not None:
                    if rmbg_route is not None:
                        matting_availability.remember_rmbg_error(
                            rmbg_route,
                            blocking_error,
                        )
                    shutil.rmtree(rgba_dir, ignore_errors=True)
                    raise blocking_error
                if provider_errors and rmbg_route is not None:
                    matting_availability.remember_rmbg_error(
                        rmbg_route,
                        provider_errors[0][1],
                    )
                if local_errors:
                    shutil.rmtree(rgba_dir, ignore_errors=True)
                    raise local_errors[0][1]
                fallback_error = provider_errors[0][1]
                rmbg_failures_before_fallback = len(provider_errors)
                fallback_started = time.time()
                matte_cfg = config.assets.matting
                _matte_frames_color_key(
                    rgb_frames,
                    rgba_dir,
                    key_color=bg_hex,
                    threshold=matte_cfg.color_key_threshold,
                    edge_threshold=matte_cfg.color_key_edge_threshold,
                    workers=workers,
                )
                timing["color_key_fallback_s"] = round(
                    time.time() - fallback_started, 2
                )
                resolved_matting = "color_key"
            else:
                failures = [
                    f"{src.name}: {error.code}: {error.message}"
                    for src, error, _provider_error in rmbg_errors
                ]
                resolved_matting = "rmbg2"
        timing["matte_s"] = round(time.time() - t1, 2)

        if not list(rgba_dir.glob("*.png")):
            raise ToolError(
                "RMBG2_FAILED",
                f"All {len(rgb_frames)} frames failed RMBG2 matting.",
                hint=failures[0] if failures else None,
            )

        # 3a. Drop frames where the safe background color leaked through
        #     as opaque pixels (matte failure on bright effects). Done
        #     BEFORE feet-align and encoding because both consume the
        #     contiguous %04d.png sequence; this step renumbers the
        #     survivors so downstream stages stay gap-free.
        bg_leak_stats: dict[str, Any] | None = None
        if drop_bg_leak and bg_hex:
            t_leak = time.time()
            bg_leak_stats = drop_bg_leak_frames(
                rgba_dir,
                bg_hex,
                area_threshold=bg_leak_area_threshold,
                alpha_threshold=bg_leak_alpha_threshold,
                color_dist=bg_leak_color_dist,
                keep_min_frames=bg_leak_keep_min_frames,
                alpha_weighted=bg_leak_alpha_weighted,
            )
            timing["drop_bg_leak_s"] = round(time.time() - t_leak, 2)

        # 3b. Optional: post-matting feet-glue pass.
        #    Even with the prompt-level "feet at bottom" constraint,
        #    seedance can drift the character upward across frames.
        #    This pulls every matted frame back down so the lowest
        #    opaque pixel touches the canvas bottom (with optional
        #    margin). Pure vertical translation — composition isn't
        #    altered horizontally.
        align_stats: dict[str, Any] | None = None
        if align_feet_to_bottom:
            t_align = time.time()
            align_stats = align_rgba_frames_to_bottom(
                rgba_dir, margin_px=foot_margin_px
            )
            timing["align_feet_s"] = round(time.time() - t_align, 2)

        # 4. Encode requested formats
        t2 = time.time()
        encoded = _encode_outputs(
            rgba_dir, output_dir, fps, formats,
            gif_max_size=gif_max_size,
            gif_alpha_threshold=gif_alpha_threshold,
        )
        timing["encode_s"] = round(time.time() - t2, 2)

        if not keep_frames:
            shutil.rmtree(rgba_dir, ignore_errors=True)

    return {
        "input": str(input_video),
        "output_dir": str(output_dir),
        "endpoint": ep,
        "matting": resolved_matting,
        "matting_requested": requested_matting,
        "matting_fallback": fallback_error is not None,
        "matting_fallback_code": (
            fallback_error.code if fallback_error is not None else None
        ),
        "matting_fallback_reason": (
            f"{fallback_error.code}: {fallback_error.message}"
            if fallback_error is not None
            else None
        ),
        "rmbg_probe_cached": (
            rmbg_route.cached if rmbg_route is not None else False
        ),
        "matting_key_color": bg_hex if resolved_matting == "color_key" else None,
        "rmbg_failures_before_fallback": rmbg_failures_before_fallback,
        "color_aug": bool(
            resolved_matting == "rmbg2" and bg_hex and aug is not None
        ),
        "aug": {"hard": aug[0], "soft": aug[1]} if aug else None,
        "fps": round(fps, 3),
        "frames_total": len(rgb_frames),
        "frames_failed": len(failures),
        "frames_kept": keep_frames,
        "drop_bg_leak": bool(drop_bg_leak and bg_hex),
        "bg_leak_stats": bg_leak_stats,
        "align_feet_to_bottom": bool(align_feet_to_bottom),
        "align_stats": align_stats,
        "outputs": encoded,
        "timing": timing,
        "failures": failures[:10],
    }


__all__ = [
    "SUPPORTED_FORMATS",
    "align_rgba_frames_to_bottom",
    "assess_matte_health",
    "drop_bg_leak_frames",
    "encode_preview_from_frames",
    "matte_video",
]
