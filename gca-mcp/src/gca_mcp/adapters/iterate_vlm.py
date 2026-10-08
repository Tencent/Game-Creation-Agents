"""Single-shot VLM helper calls (image / video → text) through ARK.

Provides ``call_vlm_on_image`` / ``call_vlm_on_video``, which send one media
block plus a prompt to the pinned Doubao 2.0 model and return the model's text.
"""

from __future__ import annotations

import base64
import time
from pathlib import Path
from typing import Any

from ..config import DOUBAO_VLM_MODEL, Config
from ..errors import ToolError
from ..providers import VlmAttemptError, get_provider

DEFAULT_VLM_MODEL = DOUBAO_VLM_MODEL


def _validate_requested_model(model: str | None) -> None:
    if model is None:
        return
    if model != DOUBAO_VLM_MODEL:
        raise ToolError(
            "INVALID_CONFIG",
            f"Unsupported VLM model {model!r}; only {DOUBAO_VLM_MODEL!r} is allowed.",
        )


def _endpoint_trace(ep: Any) -> dict[str, str]:
    return {
        "provider": str(getattr(ep, "provider", "unknown")),
        "model": str(
            getattr(ep, "bare_model", None)
            or getattr(ep, "model", None)
            or "unknown"
        ),
    }


def _post_vlm_detailed(
    *,
    config: Config,
    content: list[dict[str, Any]],
    max_tokens: int,
    temperature: float,
    timeout: float,
    attempts_per_model: int | None = None,
) -> dict[str, Any]:
    """Call the pinned VLM once and return endpoint/timing metadata."""
    provider = get_provider(config)
    chain = provider.vlm_chain(config)
    if len(chain) != 1:
        raise ToolError(
            "INVALID_CONFIG",
            "VLM policy requires exactly one configured model.",
        )
    configured_attempts, _backoff, _rate_limit_backoff = provider.vlm_retry(config)
    requested_attempts = (
        configured_attempts
        if attempts_per_model is None
        else int(attempts_per_model)
    )
    if configured_attempts != 1 or requested_attempts != 1:
        raise ToolError("INVALID_CONFIG", "VLM policy requires exactly one attempt.")

    ep = chain[0]
    endpoint = _endpoint_trace(ep)
    call_started = time.monotonic()
    attempt_started = time.monotonic()
    try:
        text = provider.vlm_attempt(
            config,
            ep,
            content=content,
            max_tokens=max_tokens,
            temperature=temperature,
            timeout=timeout,
        )
    except VlmAttemptError as exc:
        elapsed_ms = round((time.monotonic() - attempt_started) * 1000)
        raise ToolError(
            "VLM_CALL_FAILED",
            "Pinned Doubao 2.0 VLM request failed.",
            hint=(
                f"model={endpoint['model']} | elapsed_ms={elapsed_ms} | "
                f"status_code={exc.status_code} | reason={exc.reason}"
            ),
        ) from exc
    elapsed_ms = round((time.monotonic() - attempt_started) * 1000)
    return {
        "text": text,
        "selected": endpoint,
        "attempts": [
            {
                **endpoint,
                "attempt": 1,
                "status": "passed",
                "elapsed_ms": elapsed_ms,
            }
        ],
        "elapsed_ms": round((time.monotonic() - call_started) * 1000),
    }


def _post_vlm(
    *,
    config: Config,
    content: list[dict[str, Any]],
    max_tokens: int,
    temperature: float,
    timeout: float,
) -> str:
    """Compatibility wrapper returning only the model text."""
    return str(
        _post_vlm_detailed(
            config=config,
            content=content,
            max_tokens=max_tokens,
            temperature=temperature,
            timeout=timeout,
        )["text"]
    )


def call_vlm_on_video(
    *,
    config: Config,
    video_path: Path,
    prompt: str,
    model: str | None = None,
    max_tokens: int = 800,
    temperature: float = 0.0,
    timeout: float = 120.0,
) -> str:
    """One pinned Doubao VLM call: video bytes → text.

    ``model`` is accepted for backward compatibility but ignored — selection
    is fixed by the runtime policy.
    """
    _validate_requested_model(model)
    body = video_path.read_bytes()
    data_uri = f"data:video/mp4;base64,{base64.b64encode(body).decode()}"
    return _post_vlm(
        config=config,
        content=[
            {"type": "text", "text": prompt},
            {"type": "video_url", "video_url": {"url": data_uri}},
        ],
        max_tokens=max_tokens,
        temperature=temperature,
        timeout=timeout,
    )


def call_vlm_on_image(
    *,
    config: Config,
    image_path: Path,
    prompt: str,
    model: str | None = None,
    max_tokens: int = 800,
    temperature: float = 0.0,
    timeout: float = 120.0,
) -> str:
    """One pinned Doubao VLM call on a single image.

    ``model`` is accepted for backward compatibility but ignored.
    """
    _validate_requested_model(model)
    body = image_path.read_bytes()
    suffix = image_path.suffix.lower().lstrip(".") or "png"
    if suffix == "jpg":
        suffix = "jpeg"
    data_uri = f"data:image/{suffix};base64,{base64.b64encode(body).decode()}"
    return _post_vlm(
        config=config,
        content=[
            {"type": "text", "text": prompt},
            {"type": "image_url", "image_url": {"url": data_uri}},
        ],
        max_tokens=max_tokens,
        temperature=temperature,
        timeout=timeout,
    )


def call_vlm_on_image_detailed(
    *,
    config: Config,
    image_path: Path,
    prompt: str,
    model: str | None = None,
    max_tokens: int = 800,
    temperature: float = 0.0,
    timeout: float = 120.0,
    attempts_per_model: int | None = None,
) -> dict[str, Any]:
    """Image VLM call with endpoint, retry, and latency trace metadata."""
    _validate_requested_model(model)
    body = image_path.read_bytes()
    suffix = image_path.suffix.lower().lstrip(".") or "png"
    if suffix == "jpg":
        suffix = "jpeg"
    data_uri = f"data:image/{suffix};base64,{base64.b64encode(body).decode()}"
    return _post_vlm_detailed(
        config=config,
        content=[
            {"type": "text", "text": prompt},
            {"type": "image_url", "image_url": {"url": data_uri}},
        ],
        max_tokens=max_tokens,
        temperature=temperature,
        timeout=timeout,
        attempts_per_model=attempts_per_model,
    )


__all__ = [
    "DEFAULT_VLM_MODEL",
    "call_vlm_on_image",
    "call_vlm_on_image_detailed",
    "call_vlm_on_video",
]
