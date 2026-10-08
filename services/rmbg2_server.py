"""RMBG-2.0 high-concurrency server — multi-GPU, single port (8080), FastAPI + uvicorn.

One model replica is loaded per GPU. The GPU set auto-adapts to the machine:
by default every CUDA device visible to the process is used (override with
RMBG_GPU_IDS). Every incoming request is auto-dispatched to whichever GPU
currently has the most free inference slots (semaphore-based least-busy
scheduling), so a single :8080 endpoint transparently spreads load across all
detected cards. Scales from 1 GPU to as many as the host exposes.

Endpoint: POST /v1/images/remove-background
  Body: {"image": "<base64>",
         "max_size": 1024,          # optional, resize longest edge before infer
         "bg_hex": "#FF00FF",        # optional, enables color-aug rescue + decontam
         "key_hard": 50,             # optional, with key_soft → rescue ramp
         "key_soft": 80,
         "rmbg_fg_cutoff": 230,      # soft→hard alpha cutoff (edge residue control)
         "decontaminate": true}      # optional, unmultiply bg spill from edge RGB
  Response: {"image": "<base64_rgba_png>", "width", "height", "gpu", "elapsed_ms"}

The matting decision:
  1. RMBG-2.0 produces a soft-alpha mask (default path, color-aug OFF → returned as-is).
  2. When bg_hex + key_hard/key_soft are given, pixels far from the safe bg color
     are *rescued* into the foreground (recovers particles / glow RMBG would clip).
  3. rmbg_fg_cutoff hardens low-confidence RMBG alpha at the silhouette edge to
     suppress magenta/green residue (higher = stricter = cleaner edge).
  4. decontaminate (color decontamination / unmultiply): for partially-transparent
     edge pixels, solve C = a*F + (1-a)*bg for the true foreground color F and
     write F back to RGB. This removes the magenta/green spill that adjusting
     alpha alone cannot — the colored fringe on wispy edges (ink tendrils, fur,
     particles) is pulled back toward the real foreground color.

Start (from within the project's Python env):
  python rmbg2_server.py

Env overrides:
  RMBG_GPU_IDS="0,1,2,3,4,5,6,7"   # which GPUs to use (default: all visible)
  RMBG_SLOTS_PER_GPU=4             # concurrent inference tasks queued per GPU
  RMBG_PORT=8080
  RMBG_MODEL_PATH=briaai/RMBG-2.0  # HF hub id or local model dir
"""

import asyncio
import base64
import io
import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager

import numpy as np
import torch
from fastapi import FastAPI, HTTPException
from PIL import Image
from pydantic import BaseModel, Field
from transformers import AutoModelForImageSegmentation
from torchvision import transforms

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("rmbg2")

MODEL_PATH = os.environ.get("RMBG_MODEL_PATH", "briaai/RMBG-2.0")


def _resolve_gpu_ids() -> list[int]:
    env = os.environ.get("RMBG_GPU_IDS", "").strip()
    if env:
        return [int(x) for x in env.replace(" ", "").split(",") if x != ""]
    n = torch.cuda.device_count()
    if n == 0:
        raise RuntimeError("No CUDA devices visible — cannot start RMBG-2.0 server.")
    return list(range(n))


GPU_IDS = _resolve_gpu_ids()
MAX_CONCURRENT_PER_GPU = int(os.environ.get("RMBG_SLOTS_PER_GPU", "4"))
PORT = int(os.environ.get("RMBG_PORT", "8080"))

models: dict[int, object] = {}
semaphores: dict[int, asyncio.Semaphore] = {}
# One thread per (gpu, slot) so concurrent inferences never queue behind the GIL
# waiting for a free worker — the semaphore is the real concurrency gate.
executor = ThreadPoolExecutor(max_workers=len(GPU_IDS) * MAX_CONCURRENT_PER_GPU)

transform_image = transforms.Compose([
    transforms.Resize((1024, 1024)),
    transforms.ToTensor(),
    transforms.Normalize([0.485, 0.456, 0.406], [0.229, 0.224, 0.225]),
])


def load_model(gpu_id: int):
    logger.info(f"Loading RMBG-2.0 on GPU {gpu_id}...")
    device = torch.device(f"cuda:{gpu_id}")
    model = AutoModelForImageSegmentation.from_pretrained(
        MODEL_PATH,
        trust_remote_code=True,
    )
    model.to(device)
    model.eval()
    # RMBG-2.0 (BiRefNet) supports half precision — halves memory & speeds infer.
    try:
        model.half()
        dtype = torch.float16
    except Exception:  # noqa: BLE001
        dtype = torch.float32
    torch.cuda.empty_cache()
    logger.info(f"GPU {gpu_id} model loaded (dtype={dtype}).")
    return model, dtype


@asynccontextmanager
async def lifespan(app: FastAPI):
    for gid in GPU_IDS:
        models[gid] = load_model(gid)
        semaphores[gid] = asyncio.Semaphore(MAX_CONCURRENT_PER_GPU)
    logger.info(
        f"Server ready — {len(GPU_IDS)} GPU(s) {GPU_IDS}, "
        f"{MAX_CONCURRENT_PER_GPU} slots each, port {PORT}"
    )
    yield
    models.clear()


app = FastAPI(title=f"RMBG-2.0 Service ({len(GPU_IDS)}-GPU)", lifespan=lifespan)


class RemoveBgRequest(BaseModel):
    image: str
    max_size: int | None = Field(default=None, description="Resize longest edge before inference")
    bg_hex: str | None = None
    key_hard: int | None = None
    key_soft: int | None = None
    rmbg_fg_cutoff: int = 230
    # Color decontamination (unmultiply bg spill from semi-transparent edge RGB).
    # None → auto: enabled whenever color-aug is active (bg_hex + keys present).
    decontaminate: bool | None = None
    # Only decontaminate pixels with alpha below this (0..1); fully-opaque body
    # interior is never touched. 1.0 = consider every non-opaque pixel.
    decontam_max_alpha: float = 0.95
    # Chroma despill: clamp the residual bg hue (the magenta/green fringe that
    # unmultiply cannot remove because RMBG hardened/rescued it to full opacity).
    # None → auto: enabled whenever a bg_hex is provided.
    despill: bool | None = None
    # How much the bg-hue channel(s) may exceed the neutral channel before being
    # clamped. 0 = fully neutralise (cleanest, slightly flat); ~20 = gentle
    # (keeps a touch of natural colour transition). Default tuned on ink sprites.
    despill_limit: int = 20


class RemoveBgResponse(BaseModel):
    image: str
    width: int
    height: int
    gpu: int
    elapsed_ms: int


def _hex_to_rgb(hex_str: str) -> tuple[int, int, int]:
    s = hex_str.lstrip("#")
    return (int(s[0:2], 16), int(s[2:4], 16), int(s[4:6], 16))


def _apply_alpha_postproc(
    rgb: np.ndarray,          # HxWx3 uint8 (original-resolution RGB)
    mask: np.ndarray,         # HxW uint8 (RMBG soft alpha, 0..255)
    *,
    bg_hex: str | None,
    key_hard: int | None,
    key_soft: int | None,
    rmbg_fg_cutoff: int,
) -> np.ndarray:
    """Return final HxW uint8 alpha. Pure RMBG when color-aug is disabled."""
    alpha = mask.astype(np.float32)

    color_aug = bg_hex is not None and key_hard is not None and key_soft is not None
    if not color_aug:
        return np.clip(alpha, 0, 255).astype(np.uint8)

    # --- rmbg_fg_cutoff: harden low-confidence RMBG alpha at the edge ---
    cutoff = float(max(1, min(255, rmbg_fg_cutoff)))
    hard = np.where(
        alpha >= cutoff,
        255.0,
        255.0 * (alpha / cutoff) ** 2,
    )

    # --- color-aug rescue: force pixels far from the safe bg color to foreground ---
    bg = np.array(_hex_to_rgb(bg_hex), dtype=np.float32)
    dist = np.sqrt(((rgb.astype(np.float32) - bg) ** 2).sum(axis=2))  # 0..~441
    lo, hi = float(min(key_hard, key_soft)), float(max(key_hard, key_soft))
    if hi <= lo:
        hi = lo + 1.0
    rescue = np.clip((dist - lo) / (hi - lo), 0.0, 1.0) * 255.0

    final = np.maximum(hard, rescue)
    return np.clip(final, 0, 255).astype(np.uint8)


def _decontaminate_rgb(
    rgb: np.ndarray,          # HxWx3 uint8
    alpha_u8: np.ndarray,     # HxW uint8 (final alpha)
    *,
    bg_hex: str,
    max_alpha: float,
) -> np.ndarray:
    """Remove background colour spill from semi-transparent edge pixels.

    Observed colour is a composite C = a*F + (1-a)*bg. Solve for the true
    foreground colour F = (C - (1-a)*bg) / a and write it back, but only for
    partially-transparent pixels (0 < a < max_alpha). Fully-opaque body
    interior keeps its original RGB untouched.
    """
    a = (alpha_u8.astype(np.float32) / 255.0)[:, :, None]   # HxWx1 in 0..1
    bg = np.array(_hex_to_rgb(bg_hex), dtype=np.float32)[None, None, :]
    C = rgb.astype(np.float32)

    # F = (C - (1-a)*bg) / a ; guard tiny alpha to avoid blowing up noise.
    a_safe = np.clip(a, 1e-3, 1.0)
    F = (C - (1.0 - a) * bg) / a_safe
    F = np.clip(F, 0.0, 255.0)

    # Blend mask: only touch pixels that are translucent AND have meaningful
    # coverage (a>0). Opaque (a>=max_alpha) and fully transparent (a==0) untouched.
    touch = ((a[:, :, 0] > 0.0) & (a[:, :, 0] < max_alpha))[:, :, None]
    out = np.where(touch, F, C)
    return np.clip(out, 0, 255).astype(np.uint8)


def _despill_rgb(
    rgb: np.ndarray,          # HxWx3 uint8
    alpha_u8: np.ndarray,     # HxW uint8 (final alpha)
    *,
    bg_hex: str,
    limit: int,
) -> np.ndarray:
    """Clamp the residual background-hue fringe (despill / chroma suppression).

    Unmultiply only fixes *semi-transparent* mixed pixels. Pixels that RMBG
    hardened or color-aug rescued to full opacity but that still carry the bg
    hue (the magenta / green rim on wispy edges) keep that hue in RGB. Despill
    detects the bg hue and clamps the offending channel(s) down toward the
    neutral channel.

    The bg colour determines the axis automatically:
      magenta (#FF00FF): R and B high, G low → clamp R,B down toward G.
      green   (#00F000): G high, R and B low → clamp G down toward max(R,B).
    Generic fallback: clamp every channel that the bg is "high" on toward the
    mean of the channels it is "low" on.

    `limit` is how far above the neutral level a channel may stay before being
    clamped (0 = fully neutralise; ~20 = keep a little natural transition).
    Only opaque-enough pixels (alpha>0) are touched.
    """
    arr = rgb.astype(np.float32).copy()
    R, G, B = arr[:, :, 0], arr[:, :, 1], arr[:, :, 2]
    br, bg_, bb = _hex_to_rgb(bg_hex)
    lim = float(limit)
    visible = alpha_u8 > 0

    # Classify each channel of the bg as "high" (≥128) or "low" (<128).
    bg_ch = {"R": br, "G": bg_, "B": bb}
    high = [k for k, v in bg_ch.items() if v >= 128]
    low = [k for k, v in bg_ch.items() if v < 128]
    chan = {"R": R, "G": G, "B": B}

    if not high or not low:
        return np.clip(arr, 0, 255).astype(np.uint8)

    # Neutral reference = mean of the channels the bg is low on (the "clean" ones).
    neutral = sum(chan[k] for k in low) / len(low)
    cap = neutral + lim

    # Spill = a high-channel rising clearly above the neutral reference.
    spill = visible.copy()
    for k in high:
        spill &= (chan[k] > cap)
    for k in high:
        ch = chan[k]
        ch[spill] = np.minimum(ch[spill], cap[spill])

    arr[:, :, 0], arr[:, :, 1], arr[:, :, 2] = R, G, B
    return np.clip(arr, 0, 255).astype(np.uint8)


def _infer_sync(
    img_bytes: bytes,
    gpu_id: int,
    max_size: int | None,
    *,
    bg_hex: str | None,
    key_hard: int | None,
    key_soft: int | None,
    rmbg_fg_cutoff: int,
    decontaminate: bool | None,
    decontam_max_alpha: float,
    despill: bool | None,
    despill_limit: int,
) -> tuple[bytes, int, int]:
    """Run inference synchronously on the specified GPU (called from thread pool)."""
    device = torch.device(f"cuda:{gpu_id}")
    model, dtype = models[gpu_id]

    img = Image.open(io.BytesIO(img_bytes)).convert("RGB")
    orig_w, orig_h = img.size

    if max_size and max(orig_w, orig_h) > max_size:
        ratio = max_size / max(orig_w, orig_h)
        img = img.resize((int(orig_w * ratio), int(orig_h * ratio)), Image.LANCZOS)
    proc_w, proc_h = img.size

    input_tensor = transform_image(img).unsqueeze(0).to(device=device, dtype=dtype)

    with torch.no_grad():
        preds = model(input_tensor)[-1].sigmoid().float().cpu()

    pred = preds[0].squeeze()
    mask = (pred * 255).numpy().astype(np.uint8)
    mask_img = Image.fromarray(mask, mode="L").resize((proc_w, proc_h), Image.LANCZOS)

    rgb_arr = np.asarray(img.convert("RGB"))
    alpha_arr = _apply_alpha_postproc(
        rgb_arr,
        np.asarray(mask_img),
        bg_hex=bg_hex,
        key_hard=key_hard,
        key_soft=key_soft,
        rmbg_fg_cutoff=rmbg_fg_cutoff,
    )

    # Color decontamination — auto-on whenever a safe bg colour is provided.
    do_decontam = decontaminate if decontaminate is not None else (bg_hex is not None)
    if do_decontam and bg_hex is not None:
        rgb_arr = _decontaminate_rgb(
            rgb_arr, alpha_arr, bg_hex=bg_hex, max_alpha=decontam_max_alpha
        )

    # Chroma despill — auto-on whenever a safe bg colour is provided. Runs after
    # unmultiply: it clamps the residual bg-hue fringe that unmultiply can't fix.
    do_despill = despill if despill is not None else (bg_hex is not None)
    if do_despill and bg_hex is not None:
        rgb_arr = _despill_rgb(
            rgb_arr, alpha_arr, bg_hex=bg_hex, limit=despill_limit
        )

    rgba = Image.fromarray(rgb_arr, mode="RGB").convert("RGBA")
    rgba.putalpha(Image.fromarray(alpha_arr, mode="L"))

    if (proc_w, proc_h) != (orig_w, orig_h):
        rgba = rgba.resize((orig_w, orig_h), Image.LANCZOS)

    buf = io.BytesIO()
    rgba.save(buf, format="PNG", compress_level=1)
    return buf.getvalue(), orig_w, orig_h


def pick_gpu() -> int:
    """Pick the GPU with the most available slots (least busy)."""
    best_gpu = GPU_IDS[0]
    best_free = -1
    for gid in GPU_IDS:
        free = semaphores[gid]._value
        if free > best_free:
            best_free = free
            best_gpu = gid
    return best_gpu


@app.post("/v1/images/remove-background", response_model=RemoveBgResponse)
async def remove_background(req: RemoveBgRequest):
    try:
        img_bytes = base64.b64decode(req.image)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid base64 image data")

    gpu_id = pick_gpu()
    sem = semaphores[gpu_id]

    async with sem:
        t0 = time.time()
        loop = asyncio.get_event_loop()
        try:
            png_bytes, w, h = await loop.run_in_executor(
                executor,
                lambda: _infer_sync(
                    img_bytes,
                    gpu_id,
                    req.max_size,
                    bg_hex=req.bg_hex,
                    key_hard=req.key_hard,
                    key_soft=req.key_soft,
                    rmbg_fg_cutoff=req.rmbg_fg_cutoff,
                    decontaminate=req.decontaminate,
                    decontam_max_alpha=req.decontam_max_alpha,
                    despill=req.despill,
                    despill_limit=req.despill_limit,
                ),
            )
        except Exception as e:
            logger.error(f"Inference error on GPU {gpu_id}: {e}")
            raise HTTPException(status_code=500, detail=f"Inference failed: {e}")
        elapsed_ms = int((time.time() - t0) * 1000)

    out_b64 = base64.b64encode(png_bytes).decode()
    return RemoveBgResponse(image=out_b64, width=w, height=h, gpu=gpu_id, elapsed_ms=elapsed_ms)


@app.get("/health")
async def health():
    return {
        "status": "ok",
        "gpus": len(GPU_IDS),
        "gpu_ids": GPU_IDS,
        "slots_per_gpu": MAX_CONCURRENT_PER_GPU,
        "available_slots": {gid: semaphores[gid]._value for gid in GPU_IDS},
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "rmbg2_server:app",
        host="0.0.0.0",
        port=PORT,
        workers=1,  # single process; all GPUs driven via the thread pool
        log_level="info",
    )
