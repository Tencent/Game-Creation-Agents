"""External API clients for image, video, VLM, and matting calls.

Adapters keep their orchestration responsibilities (prompt wrapping, sizing,
retry policy, and downloads) while this module owns provider endpoints,
authentication, request payloads, and response parsing.
"""

from __future__ import annotations

import base64
import os
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Protocol, runtime_checkable

import requests

from .config import DOUBAO_VLM_MODEL
from .errors import ToolError


class VlmAttemptError(Exception):
    """A single VLM attempt failed, with explicit retry classification."""

    def __init__(
        self,
        reason: str,
        *,
        rate_limited: bool = False,
        retryable: bool = True,
        status_code: int | None = None,
    ) -> None:
        super().__init__(reason)
        self.reason = reason
        self.rate_limited = rate_limited
        self.retryable = retryable
        self.status_code = status_code


def _is_rate_limited(status_code: int | None, body_text: str) -> bool:
    if status_code == 429:
        return True
    low = (body_text or "").lower()
    return "rate" in low or "exhausted" in low or "quota" in low


def _is_retryable_vlm_status(status_code: int) -> bool:
    """Return whether retrying the same VLM endpoint can plausibly recover."""
    return status_code in {408, 409, 425, 429} or status_code >= 500


def _is_retryable_vlm_message(message: str) -> bool:
    """Classify HTTP-200 API error envelopes without retrying bad requests."""
    low = (message or "").lower()
    terminal_markers = (
        "auth",
        "credential",
        "forbidden",
        "invalid argument",
        "invalid request",
        "not found",
        "permission",
        "unsupported",
    )
    return not any(marker in low for marker in terminal_markers)


def vlm_roundtrip(
    url: str, headers: dict[str, str], payload: dict[str, Any],
    timeout: float,
) -> str:
    """Run one Doubao VLM request and parse its OpenAI-compatible response."""
    try:
        rsp = requests.post(url, headers=headers, json=payload, timeout=timeout)
    except requests.exceptions.RequestException as e:
        raise VlmAttemptError(f"request failed: {e}") from e
    if rsp.status_code >= 400:
        rate_limited = _is_rate_limited(rsp.status_code, rsp.text)
        raise VlmAttemptError(
            f"HTTP {rsp.status_code}: {rsp.text[:200]}",
            rate_limited=rate_limited,
            retryable=(
                rate_limited or _is_retryable_vlm_status(rsp.status_code)
            ),
            status_code=rsp.status_code,
        )
    try:
        data = rsp.json()
    except ValueError as e:
        raise VlmAttemptError(f"non-JSON body: {e}") from e
    if isinstance(data, dict) and data.get("error"):
        err = data["error"]
        msg = err.get("message", "") if isinstance(err, dict) else str(err)
        rate_limited = _is_rate_limited(None, msg)
        raise VlmAttemptError(
            f"api error: {msg}",
            rate_limited=rate_limited,
            retryable=rate_limited or _is_retryable_vlm_message(msg),
        )
    text = ((data.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
    if not text.strip():
        raise VlmAttemptError("empty response text")
    return text


@runtime_checkable
class ProviderTransport(Protocol):
    """Transport surface shared by the provider-backed adapters."""

    # ---- image (gpt-image / OpenAI Images) ----
    def image_output_size(self, config: Any, size: str) -> str: ...
    def generate_image(
        self, config: Any, *, prompt: str, size: str, quality: str,
        max_retries: int, retry_delay: float,
    ) -> dict[str, Any]: ...
    def edit_image(
        self, config: Any, *, prompt: str, size: str, quality: str, input_path: str,
    ) -> dict[str, Any]: ...

    # ---- video (ARK Seedance transport) ----
    def video_endpoint(self, config: Any) -> str: ...
    def video_verify_ssl(self, config: Any) -> bool: ...
    def video_auth_headers(
        self, config: Any, *, request_id: str, model: str, timeout_s: int,
    ) -> dict[str, str]: ...
    def resolve_video_model(self, config: Any, *, model: str, model_id: str | None) -> str: ...
    def ensure_image_url(self, config: Any, path: str) -> str: ...
    def request_video(
        self,
        config: Any,
        *,
        prompt: str,
        model_id: str,
        duration: int | str,
        ratio: str,
        resolution: str,
        seed: int | str | None,
        camerafixed: bool | None,
        watermark: bool | None,
        image_urls: list[str],
        first_frame_url: str | None,
        last_frame_url: str | None,
        extra_params: dict[str, Any] | None,
        server_timeout: int,
    ) -> dict[str, Any]: ...

    # ---- VLM ----
    def vlm_chain(self, config: Any) -> list[Any]: ...
    def vlm_retry(self, config: Any) -> tuple[int, float, float]: ...
    def vlm_attempt(
        self, config: Any, ep: Any, *, content: list[dict[str, Any]],
        max_tokens: int, temperature: float, timeout: float,
    ) -> str: ...

    # ---- matting ----
    def rmbg_matte(
        self, config: Any, image_bytes: bytes, *, endpoint: str | None,
        bg_hex: str | None, aug: tuple[int, int] | None, rmbg_fg_cutoff: int,
        max_size: int | None, timeout: int,
    ) -> tuple[bytes, dict[str, Any]]: ...


# ==========================================================================
# External provider helpers.
# ==========================================================================


def _require_env(name: str) -> str:
    value = (os.environ.get(name) or "").strip()
    if not value:
        raise ToolError(
            "MISSING_CREDENTIALS",
            f"{name} is not set.",
            hint="Fill your public .env (copy .env.example).",
        )
    return value


def _call_remote_rmbg(
    image_bytes: bytes,
    *,
    endpoint: str,
    bg_hex: str | None,
    aug: tuple[int, int] | None,
    rmbg_fg_cutoff: int,
    max_size: int | None,
    timeout: int,
) -> tuple[bytes, dict[str, Any]]:
    """Call a configured public RMBG service."""
    payload: dict[str, Any] = {"image": base64.b64encode(image_bytes).decode()}
    if bg_hex and aug is not None:
        payload.update(
            {
                "bg_hex": bg_hex,
                "key_hard": aug[0],
                "key_soft": aug[1],
                "rmbg_fg_cutoff": rmbg_fg_cutoff,
            }
        )
    if max_size:
        payload["max_size"] = max_size

    try:
        response = requests.post(endpoint, json=payload, timeout=timeout)
    except requests.exceptions.RequestException as exc:
        raise ToolError(
            "NETWORK_ERROR",
            f"RMBG2 request failed: {exc}",
            hint=f"Check that the service is reachable: {endpoint}",
        ) from exc

    if response.status_code in (401, 403):
        raise ToolError(
            "AUTH_FAILED",
            f"RMBG2 service rejected the request (HTTP {response.status_code}).",
        )
    if response.status_code >= 400:
        raise ToolError(
            "RMBG2_FAILED",
            f"RMBG2 service returned HTTP {response.status_code}.",
            hint=response.text[:300],
        )

    try:
        data = response.json()
    except ValueError as exc:
        raise ToolError(
            "BAD_RESPONSE",
            f"RMBG2 returned non-JSON body: {exc}",
        ) from exc
    if not isinstance(data, dict) or "image" not in data:
        raise ToolError(
            "BAD_RESPONSE",
            "RMBG2 response missing `image` field.",
            hint=str(data)[:300],
        )
    try:
        output = base64.b64decode(data["image"])
    except (ValueError, TypeError) as exc:
        raise ToolError(
            "BAD_RESPONSE",
            f"RMBG2 `image` field is not valid base64: {exc}",
        ) from exc

    metadata = {key: value for key, value in data.items() if key != "image"}
    metadata.setdefault("backend", "remote")
    return output, metadata


def _vlm_endpoint(model: str) -> SimpleNamespace:
    """Resolve the one supported VLM model to the ARK transport."""
    model = model.strip()
    if model != DOUBAO_VLM_MODEL:
        raise ToolError(
            "INVALID_CONFIG",
            f"Unsupported VLM model {model!r}; only "
            f"{DOUBAO_VLM_MODEL!r} is allowed.",
        )
    return SimpleNamespace(
        provider="ark-doubao",
        bare_model=DOUBAO_VLM_MODEL,
    )


class ProviderClient:
    """OpenAI Images and Volcengine ARK transport."""

    # ---- image ----
    def image_output_size(self, config: Any, size: str) -> str:
        """OpenAI gpt-image only accepts {1024x1024, 1536x1024, 1024x1536, auto}.

        Bucket an arbitrary ``WxH`` to the nearest allowed shape by orientation.
        (Behavior difference #1 — public only; downstream scale_uniform/fit still
        normalize to the game's real size.)
        """
        s = (size or "auto").strip().lower()
        if s == "auto":
            return "auto"
        try:
            w, h = (int(x) for x in s.split("x"))
        except (ValueError, AttributeError):
            return "1024x1024"
        if w > h:
            return "1536x1024"
        if h > w:
            return "1024x1536"
        return "1024x1024"

    def _image_base(self, config: Any) -> str:
        return (config.image.base_url or "https://api.openai.com/v1").rstrip("/")

    def _image_model(self, config: Any) -> str:
        return config.image.model_id or "gpt-image-2"

    @staticmethod
    def _decode_image_response(data: Any) -> bytes:
        if not isinstance(data, dict):
            raise ToolError("BAD_RESPONSE", f"OpenAI image response was not an object: {str(data)[:200]}")
        items = data.get("data") or []
        if not items:
            raise ToolError("BAD_RESPONSE", f"OpenAI image response had no data: {str(data)[:300]}")
        item = items[0] or {}
        if item.get("b64_json"):
            return base64.b64decode(item["b64_json"])
        if item.get("url"):
            resp = requests.get(item["url"], timeout=60)
            resp.raise_for_status()
            return resp.content
        raise ToolError("BAD_RESPONSE", "OpenAI image item had neither b64_json nor url.")

    def generate_image(
        self, config: Any, *, prompt: str, size: str, quality: str,
        max_retries: int, retry_delay: float,
    ) -> dict[str, Any]:
        key = _require_env("OPENAI_API_KEY")
        url = f"{self._image_base(config)}/images/generations"
        headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
        payload: dict[str, Any] = {
            "model": self._image_model(config), "prompt": prompt, "size": size, "n": 1,
        }
        if quality and quality != "auto":
            payload["quality"] = quality

        last = ""
        for attempt in range(1, max_retries + 1):
            try:
                resp = requests.post(
                    url, headers=headers, json=payload,
                    timeout=config.assets.image.request_timeout_seconds,
                )
            except requests.exceptions.RequestException as e:
                if attempt < max_retries:
                    time.sleep(retry_delay)
                    continue
                raise ToolError("NETWORK_ERROR", f"image gen request failed: {e}") from e
            if resp.status_code == 429 and attempt < max_retries:
                time.sleep(retry_delay)
                continue
            try:
                data = resp.json()
            except ValueError as e:
                raise ToolError("BAD_RESPONSE", f"non-JSON image response: {e}") from e
            if resp.status_code in (401, 403):
                raise ToolError("AUTH_FAILED", f"OpenAI image auth failed: {str(data)[:200]}")
            if resp.status_code >= 400:
                last = str(data)[:300]
                if attempt < max_retries:
                    time.sleep(retry_delay)
                    continue
                raise ToolError("IMAGE_GEN_FAILED", f"image gen failed: {last}")
            return {
                "bytes": self._decode_image_response(data),
                "request_id": resp.headers.get("x-request-id"),
            }
        raise ToolError(
            "IMAGE_RATE_LIMITED",
            f"image gen rate-limited after {max_retries} attempts: {last}",
        )

    def edit_image(
        self, config: Any, *, prompt: str, size: str, quality: str, input_path: str,
    ) -> dict[str, Any]:
        key = _require_env("OPENAI_API_KEY")
        if input_path.startswith(("http://", "https://")):
            resp = requests.get(input_path, timeout=60)
            resp.raise_for_status()
            img_bytes, fname = resp.content, "image.png"
        else:
            path = Path(input_path)
            if not path.is_file():
                raise ToolError("IMAGE_INPUT_NOT_FOUND", f"Reference image not found: {input_path}")
            img_bytes, fname = path.read_bytes(), path.name

        url = f"{self._image_base(config)}/images/edits"
        headers = {"Authorization": f"Bearer {key}"}
        # OpenAI image edits are a multipart upload.
        files: dict[str, Any] = {
            "image": (fname, img_bytes, "image/png"),
            "model": (None, self._image_model(config)),
            "prompt": (None, prompt),
            "size": (None, size),
            "n": (None, "1"),
        }
        if quality and quality != "auto":
            files["quality"] = (None, quality)
        try:
            resp = requests.post(
                url, headers=headers, files=files,
                timeout=config.assets.image.request_timeout_seconds,
            )
            data = resp.json()
        except requests.exceptions.RequestException as e:
            raise ToolError("NETWORK_ERROR", f"image edit request failed: {e}") from e
        except ValueError as e:
            raise ToolError("BAD_RESPONSE", f"non-JSON image edit response: {e}") from e
        if resp.status_code in (401, 403):
            raise ToolError("AUTH_FAILED", f"OpenAI image edit auth failed: {str(data)[:200]}")
        if resp.status_code >= 400:
            raise ToolError("IMAGE_EDIT_FAILED", f"image edit failed: {str(data)[:300]}")
        return {
            "bytes": self._decode_image_response(data),
            "request_id": resp.headers.get("x-request-id"),
        }

    # ---- video (Seedance async task via ARK) ----
    def video_endpoint(self, config: Any) -> str:
        return config.seedance.url

    def video_verify_ssl(self, config: Any) -> bool:
        return config.seedance.verify_ssl

    def video_auth_headers(
        self, config: Any, *, request_id: str, model: str, timeout_s: int,
    ) -> dict[str, str]:
        return {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {_require_env('ARK_API_KEY')}",
            "X-Request-ID": request_id,
        }

    def resolve_video_model(self, config: Any, *, model: str, model_id: str | None) -> str:
        if model_id:
            return model_id
        models = config.seedance.models
        return models.get(model) or models.get("fast") or model

    def ensure_image_url(self, config: Any, path: str) -> str:
        s = str(path)
        if s.startswith(("http://", "https://", "data:")):
            return s
        p = Path(path)
        mime = "image/png" if p.suffix.lower() == ".png" else "image/jpeg"
        return f"data:{mime};base64," + base64.b64encode(p.read_bytes()).decode()

    def request_video(
        self,
        config: Any,
        *,
        prompt: str,
        model_id: str,
        duration: int | str,
        ratio: str,
        resolution: str,
        seed: int | str | None,
        camerafixed: bool | None,
        watermark: bool | None,
        image_urls: list[str],
        first_frame_url: str | None,
        last_frame_url: str | None,
        extra_params: dict[str, Any] | None,
        server_timeout: int,
    ) -> dict[str, Any]:
        """Use the public ARK native submit/poll protocol."""
        from .adapters.seedance import _request_native_video

        return _request_native_video(
            config,
            provider=self,
            prompt=prompt,
            model_id=model_id,
            duration=duration,
            ratio=ratio,
            resolution=resolution,
            seed=seed,
            camerafixed=camerafixed,
            watermark=watermark,
            image_urls=image_urls,
            first_frame_url=first_frame_url,
            last_frame_url=last_frame_url,
            extra_params=extra_params,
            server_timeout=server_timeout,
        )

    # ---- VLM (ARK Doubao chat) ----
    def vlm_chain(self, config: Any) -> list[Any]:
        return [_vlm_endpoint(DOUBAO_VLM_MODEL)]

    def vlm_retry(self, config: Any) -> tuple[int, float, float]:
        policy = config.vlm_policy
        return (
            policy.attempts_per_model,
            policy.backoff_seconds,
            policy.rate_limit_backoff_seconds,
        )

    def vlm_attempt(
        self, config: Any, ep: Any, *, content: list[dict[str, Any]],
        max_tokens: int, temperature: float, timeout: float,
    ) -> str:
        req_id = f"gca-vlm-{int(time.time() * 1000)}"
        if ep.bare_model != DOUBAO_VLM_MODEL or ep.provider != "ark-doubao":
            raise ToolError("INVALID_CONFIG", "VLM endpoint violates the Doubao 2.0 policy.")
        url = (
            config.doubao.endpoint
            or "https://ark.cn-beijing.volces.com/api/v3/chat/completions"
        )
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {_require_env('ARK_API_KEY')}",
            "X-Request-ID": req_id,
        }
        payload = {
            "model": DOUBAO_VLM_MODEL,
            "messages": [{"role": "user", "content": content}],
            "max_tokens": max_tokens,
            "temperature": temperature,
            "stream": False,
        }
        return vlm_roundtrip(url, headers, payload, timeout)

    # ---- matting (configured service, with local fallback) ----
    def rmbg_matte(
        self, config: Any, image_bytes: bytes, *, endpoint: str | None = None,
        bg_hex: str | None, aug: tuple[int, int] | None, rmbg_fg_cutoff: int,
        max_size: int | None, timeout: int,
    ) -> tuple[bytes, dict[str, Any]]:
        remote_endpoint = (endpoint or "").strip()
        if remote_endpoint:
            return _call_remote_rmbg(
                image_bytes,
                endpoint=remote_endpoint,
                bg_hex=bg_hex,
                aug=aug,
                rmbg_fg_cutoff=rmbg_fg_cutoff,
                max_size=max_size,
                timeout=timeout,
            )

        from .adapters.rmbg2_local import remove_background_local

        return remove_background_local(
            image_bytes,
            bg_hex=bg_hex,
            aug=aug,
            rmbg_fg_cutoff=rmbg_fg_cutoff,
            max_size=max_size,
        )


_PROVIDER_CLIENT = ProviderClient()


def get_provider(config: Any = None) -> ProviderTransport:
    """Return the process-wide external-provider client."""
    return _PROVIDER_CLIENT


__all__ = [
    "ProviderClient",
    "ProviderTransport",
    "VlmAttemptError",
    "get_provider",
    "vlm_roundtrip",
]
