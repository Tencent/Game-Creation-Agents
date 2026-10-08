"""Local in-process background removal.

Runs RMBG-2.0 (BRIA) when a Hugging Face token grants access, else falls back
to BiRefNet (same architecture, permissive license). Heavy deps (torch,
transformers) are lazy-imported and only required when this path is actually
used — install with ``uv sync --extra local-rmbg``. The model is loaded once
and cached per process.

Returns ``(rgba_png_bytes, meta)`` — the same shape the remote RMBG service
returns to callers — so it is a drop-in behind the provider client's matting API.
"""

from __future__ import annotations

import io
import os
import threading
from typing import Any

from ..errors import ToolError

_LOCK = threading.Lock()
_STATE: dict[str, Any] = {}

# Candidate weights, in preference order. RMBG-2.0 is gated (needs HF token +
# accepted license); BiRefNet is the permissive fallback.
_RMBG_MODEL = "briaai/RMBG-2.0"
_BIREFNET_MODEL = "ZhengPeng7/BiRefNet"
_INPUT_SIZE = (1024, 1024)


def _load_model() -> tuple[Any, str]:
    """Load (and cache) the segmentation model. Returns (model, model_id)."""
    with _LOCK:
        if _STATE.get("model") is not None:
            return _STATE["model"], _STATE["model_id"]
        try:
            import torch
            from transformers import AutoModelForImageSegmentation
        except ImportError as e:  # pragma: no cover - optional stack
            raise ToolError(
                "MISSING_DEPENDENCY",
                "Local matting needs torch + transformers.",
                hint="Install the extra: `uv sync --extra local-rmbg`.",
            ) from e

        token = os.environ.get("HF_TOKEN") or None
        last_err: Exception | None = None
        for model_id in (_RMBG_MODEL, _BIREFNET_MODEL):
            try:
                model = AutoModelForImageSegmentation.from_pretrained(
                    model_id, trust_remote_code=True, token=token
                )
                model.eval()
                try:
                    torch.set_float32_matmul_precision("high")
                except Exception:  # noqa: BLE001
                    pass
                _STATE.update({"model": model, "model_id": model_id, "torch": torch})
                return model, model_id
            except Exception as e:  # noqa: BLE001 - fall back to next candidate
                last_err = e
        raise ToolError(
            "RMBG2_FAILED",
            "Could not load a local matting model (RMBG-2.0 or BiRefNet).",
            hint=str(last_err)[:300] if last_err else None,
        )


def _alpha_from_mask(image: Any, mask: Any) -> Any:
    """Attach a single-channel PIL mask to ``image`` as its alpha channel."""
    resized = mask.resize(image.size)
    out = image.convert("RGBA")
    out.putalpha(resized)
    return out


def _apply_color_aug(rgba: Any, bg_hex: str, aug: tuple[int, int]) -> Any:
    """Best-effort particle rescue: force pixels far from ``bg_hex`` opaque.

    Approximates the remote service's color-aug: pixels whose RGB distance to
    the safe background exceeds ``key_soft`` are forced fully opaque (rescued),
    so bright external effects RMBG would clip are retained. ``key_hard`` is the
    near-bg cutoff kept transparent. This is a simplified local port.
    """
    import numpy as np

    key_hard, key_soft = aug
    h = bg_hex.strip().lstrip("#")
    try:
        br, bg, bb = (int(h[i:i + 2], 16) for i in (0, 2, 4))
    except (ValueError, IndexError):
        return rgba
    arr = np.asarray(rgba, dtype=np.int32)
    dist = np.sqrt(
        (arr[:, :, 0] - br) ** 2 + (arr[:, :, 1] - bg) ** 2 + (arr[:, :, 2] - bb) ** 2
    )
    a = arr[:, :, 3]
    a = np.where(dist >= key_soft, 255, a)
    a = np.where(dist <= key_hard, 0, a)
    arr[:, :, 3] = a
    from PIL import Image

    return Image.fromarray(arr.astype("uint8"), "RGBA")


def remove_background_local(
    image_bytes: bytes,
    *,
    bg_hex: str | None = None,
    aug: tuple[int, int] | None = None,
    rmbg_fg_cutoff: int = 230,
    max_size: int | None = None,
) -> tuple[bytes, dict[str, Any]]:
    """Matte ``image_bytes`` locally; returns ``(rgba_png_bytes, meta)``."""
    from PIL import Image
    try:
        from torchvision import transforms
    except ImportError as e:
        raise ToolError(
            "MISSING_DEPENDENCY",
            "Local matting needs torchvision.",
            hint="Install the extra: `uv sync --extra local-rmbg`.",
        ) from e

    model, model_id = _load_model()
    torch = _STATE["torch"]

    image = Image.open(io.BytesIO(image_bytes)).convert("RGB")
    if max_size and max(image.size) > max_size:
        scale = max_size / max(image.size)
        image = image.resize((int(image.size[0] * scale), int(image.size[1] * scale)))

    transform = transforms.Compose(
        [
            transforms.Resize(_INPUT_SIZE),
            transforms.ToTensor(),
            transforms.Normalize([0.485, 0.456, 0.406], [0.229, 0.224, 0.225]),
        ]
    )
    tensor = transform(image).unsqueeze(0)
    with torch.no_grad():
        preds = model(tensor)[-1].sigmoid().cpu()
    pred = preds[0].squeeze()
    mask = transforms.ToPILImage()(pred)

    # Hard-cutoff the soft alpha at rmbg_fg_cutoff for a clean silhouette edge.
    if rmbg_fg_cutoff:
        mask = mask.point(lambda v: 255 if v >= rmbg_fg_cutoff else v)

    rgba = _alpha_from_mask(image, mask)
    if bg_hex and aug is not None:
        rgba = _apply_color_aug(rgba, bg_hex, aug)

    buf = io.BytesIO()
    rgba.save(buf, format="PNG")
    meta = {"backend": "local", "model": model_id}
    return buf.getvalue(), meta


__all__ = ["remove_background_local"]
