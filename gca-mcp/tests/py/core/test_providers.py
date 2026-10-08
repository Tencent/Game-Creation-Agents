from __future__ import annotations

import base64
from types import SimpleNamespace

import pytest

from gca_mcp import providers
from gca_mcp.adapters import rmbg2_local
from gca_mcp.config import DOUBAO_VLM_MODEL


def test_rmbg_uses_configured_remote_service(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}
    output = b"remote-rgba-png"

    class Response:
        status_code = 200
        text = ""

        @staticmethod
        def json() -> dict[str, object]:
            return {
                "image": base64.b64encode(output).decode(),
                "model": "RMBG-2.0",
            }

    def fake_post(
        url: str,
        *,
        json: dict[str, object],
        timeout: int,
    ) -> Response:
        captured.update(url=url, payload=json, timeout=timeout)
        return Response()

    monkeypatch.setattr(providers.requests, "post", fake_post)

    result, metadata = providers.ProviderClient().rmbg_matte(
        object(),
        b"source-image",
        endpoint="https://rmbg.example/v1/images/remove-background",
        bg_hex="#FF00FF",
        aug=(50, 80),
        rmbg_fg_cutoff=230,
        max_size=1024,
        timeout=17,
    )

    assert result == output
    assert metadata == {"model": "RMBG-2.0", "backend": "remote"}
    assert captured["url"] == "https://rmbg.example/v1/images/remove-background"
    assert captured["timeout"] == 17
    payload = captured["payload"]
    assert isinstance(payload, dict)
    assert base64.b64decode(str(payload["image"])) == b"source-image"
    assert payload["bg_hex"] == "#FF00FF"
    assert payload["key_hard"] == 50
    assert payload["key_soft"] == 80
    assert payload["rmbg_fg_cutoff"] == 230
    assert payload["max_size"] == 1024


def test_rmbg_without_endpoint_keeps_local_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_local(image_bytes: bytes, **kwargs: object) -> tuple[bytes, dict[str, str]]:
        assert image_bytes == b"source-image"
        assert kwargs["max_size"] == 512
        return b"local-rgba-png", {"backend": "local"}

    monkeypatch.setattr(rmbg2_local, "remove_background_local", fake_local)
    monkeypatch.setattr(
        providers.requests,
        "post",
        lambda *_args, **_kwargs: pytest.fail("remote service should not be called"),
    )

    result, metadata = providers.ProviderClient().rmbg_matte(
        object(),
        b"source-image",
        endpoint=None,
        bg_hex=None,
        aug=None,
        rmbg_fg_cutoff=230,
        max_size=512,
        timeout=17,
    )

    assert result == b"local-rgba-png"
    assert metadata == {"backend": "local"}


def test_vlm_identity_matches_pinned_doubao(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "DOUBAO_API_ENDPOINT",
        "http://gateway.example/v1/chat/completions",
    )
    config = SimpleNamespace(
        vlm_policy=SimpleNamespace(
            attempts_per_model=1,
            backoff_seconds=0.0,
            rate_limit_backoff_seconds=0.0,
        )
    )

    endpoint = providers.ProviderClient().vlm_chain(config)[0]

    assert endpoint.provider == "ark-doubao"
    assert endpoint.bare_model == DOUBAO_VLM_MODEL


def test_get_provider_returns_external_provider_singleton() -> None:
    assert providers.get_provider() is providers.get_provider()
    assert isinstance(providers.get_provider(), providers.ProviderClient)
