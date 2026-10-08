"""Regression tests for seedance pick_safe_bg (SD-02)."""

from __future__ import annotations

from types import SimpleNamespace

import numpy as np
import pytest
from PIL import Image

from gca_mcp.adapters import seedance
from gca_mcp.errors import ToolError


def _write(tmp_path, arr, name="frame.png"):
    p = tmp_path / name
    Image.fromarray(arr).save(p)
    return p


def test_opaque_magenta_bg_is_preserved(tmp_path):
    # Magenta canvas with a brown character block — old code picked green.
    arr = np.zeros((256, 256, 3), dtype=np.uint8)
    arr[:, :] = (0xFF, 0x00, 0xFF)  # magenta everywhere
    arr[80:200, 90:170] = (139, 90, 43)  # brown character interior
    p = _write(tmp_path, arr)
    assert seedance.pick_safe_bg(p) == "#FF00FF"


def test_opaque_green_bg_is_preserved(tmp_path):
    arr = np.zeros((256, 256, 3), dtype=np.uint8)
    arr[:, :] = (0x00, 0xF0, 0x00)  # chroma green everywhere
    arr[80:200, 90:170] = (180, 60, 60)  # reddish character
    p = _write(tmp_path, arr)
    assert seedance.pick_safe_bg(p) == "#00F000"


def test_transparent_image_uses_alpha_foreground(tmp_path):
    # RGBA with transparent surround + a greenish foreground → pick magenta
    # (farther from the green subject).
    arr = np.zeros((256, 256, 4), dtype=np.uint8)
    arr[80:200, 90:170, :3] = (30, 200, 60)  # green-ish subject
    arr[80:200, 90:170, 3] = 255
    p = tmp_path / "matted.png"
    Image.fromarray(arr, mode="RGBA").save(p)
    assert seedance.pick_safe_bg(p) == "#FF00FF"
    assert seedance.detect_existing_safe_bg(p) is None


def test_standalone_prompt_rejects_non_chroma_reference(tmp_path):
    p = tmp_path / "white.png"
    Image.new("RGB", (64, 64), "white").save(p)
    with pytest.raises(ToolError, match="no detectable"):
        seedance.build_i2v_prompt("hover", reference_image=p)


def test_preset_must_match_reference_background(tmp_path):
    arr = np.zeros((64, 64, 3), dtype=np.uint8)
    arr[:, :] = (0x00, 0xF0, 0x00)
    p = _write(tmp_path, arr, "green.png")
    with pytest.raises(ToolError, match="actually uses"):
        seedance.build_i2v_prompt(
            "hover",
            preset="magenta_bg",
            reference_image=p,
        )


# -------------------- SD-02 follow-up: robust detection --------------------


def test_wide_grounded_base_keeps_magenta(tmp_path):
    # Magenta canvas with a WIDE grounded base spanning the full bottom — the
    # old border-MEAN detection drifted off-color and mis-picked; per-pixel
    # border coverage keeps magenta.
    arr = np.zeros((256, 256, 3), dtype=np.uint8)
    arr[:, :] = (0xFF, 0x00, 0xFF)
    arr[200:256, :] = (139, 90, 43)   # full-width brown base
    arr[80:200, 90:170] = (139, 90, 43)
    p = _write(tmp_path, arr, "widebase.png")
    assert seedance.pick_safe_bg(p) == "#FF00FF"


def test_single_transparent_pixel_stays_opaque_path(tmp_path):
    # Opaque magenta frame + ONE stray transparent pixel must NOT flip onto the
    # alpha path (which used to mis-pick the other safe color).
    arr = np.zeros((256, 256, 4), dtype=np.uint8)
    arr[:, :, :3] = (0xFF, 0x00, 0xFF)
    arr[:, :, 3] = 255
    arr[0, 0, 3] = 0  # single transparent pixel
    p = tmp_path / "onepixel.png"
    Image.fromarray(arr, mode="RGBA").save(p)
    assert seedance.pick_safe_bg(p) == "#FF00FF"


def test_green_bg_with_sparse_transparency_stays_green(tmp_path):
    # A few scattered transparent pixels (< the area ratio) on an opaque green
    # frame stays on the opaque path and keeps green.
    arr = np.zeros((256, 256, 4), dtype=np.uint8)
    arr[:, :, :3] = (0x00, 0xF0, 0x00)
    arr[:, :, 3] = 255
    arr[0, 0:5, 3] = 0  # 5 transparent pixels of 65536 → far below 2%
    p = tmp_path / "sparse.png"
    Image.fromarray(arr, mode="RGBA").save(p)
    assert seedance.pick_safe_bg(p) == "#00F000"


# -------------------- PR #123 review #1 / #8: projectile i2v prompt --------------------


def test_projectile_prompt_has_no_ground_or_foot_language():
    # A projectile body must get the free-flying template — never the grounded
    # character template that anchors feet to the bottom edge (which contradicts
    # the projectile validator's "must NOT touch the bottom edge").
    p = seedance.build_i2v_prompt(
        "a fireball flies to the right",
        bg_hex="#FF00FF",
        effects=False,
        animation_profile="projectile",
    )
    low = p.lower()
    # No grounded-character framing: feet must not be ANCHORED to the bottom.
    assert "feet must stay anchored" not in low
    assert "feet remain in contact with the bottom" not in low
    assert "stands on the bottom edge" not in low
    # It must NOT forbid the projectile itself (the character no-FX block does).
    assert "no projectiles" not in low
    # It must assert the centered / no-ground-contact contract.
    assert "free-flying projectile" in low
    assert "not rest on" in low or "does not rest on" in low


def test_character_prompt_still_grounded():
    p = seedance.build_i2v_prompt(
        "a knight walks", bg_hex="#FF00FF", animation_profile="character"
    )
    assert "feet" in p.lower()


def test_projectile_no_effects_block_forbids_separate_fx_only():
    # effects=False on a projectile forbids SEPARATE runtime VFX (muzzle flash,
    # sparks) but must not forbid "projectiles" (the subject).
    p = seedance.build_i2v_prompt(
        "an arrow", bg_hex="#FF00FF", effects=False, animation_profile="projectile"
    ).lower()
    assert "muzzle flash" in p
    assert "no projectiles" not in p


def test_invalid_animation_profile_raises_in_prompt():
    import pytest

    from gca_mcp.errors import ToolError

    with pytest.raises(ToolError):
        seedance.build_i2v_prompt("x", bg_hex="#FF00FF", animation_profile="bogus")


def test_generate_video_delegates_transport_to_provider(tmp_path, monkeypatch):
    captured = {}

    class FakeChannel:
        @staticmethod
        def resolve_video_model(_config, *, model, model_id):
            assert model == "fast"
            assert model_id is None
            return "doubao-seedance-2-0-fast-260128"

        @staticmethod
        def ensure_image_url(_config, path):
            return f"https://uploaded.example/{path.split('/')[-1]}"

        @staticmethod
        def request_video(_config, **kwargs):
            captured.update(kwargs)
            return {
                "video_url": "https://video.example/result.mp4",
                "model_id": kwargs["model_id"],
                "protocol": "ark_native",
                "raw_response": {"code": 0, "answer": []},
            }

        @staticmethod
        def video_verify_ssl(_config):
            return True

    monkeypatch.setattr(seedance, "get_provider", lambda _config: FakeChannel())
    result = seedance.generate_video(
        SimpleNamespace(),
        prompt="bird flaps",
        output=tmp_path / "result.mp4",
        first_frame="/tmp/first.png",
        duration=5,
        ratio="1:1",
        resolution="480p",
        server_timeout=600,
        print_url_only=True,
    )

    assert captured["prompt"] == "bird flaps"
    assert captured["first_frame_url"] == "https://uploaded.example/first.png"
    assert captured["server_timeout"] == 600
    assert result["protocol"] == "ark_native"
    assert "raw_response" not in result
    assert not (tmp_path / "result.mp4").exists()


@pytest.mark.parametrize(
    ("code", "message"),
    [
        ("NETWORK_ERROR", "connection reset"),
        ("SEEDANCE_TIMEOUT", "generation timed out"),
        ("SEEDANCE_NO_OUTPUT", "missing video_url"),
        ("BAD_RESPONSE", "malformed provider response"),
        ("SEEDANCE_FAILED", "seedance returned HTTP 502"),
        ("SEEDANCE_FAILED", "upstream request failed: timeout"),
    ],
)
def test_generate_video_retries_only_transient_no_output_failures(
    tmp_path,
    monkeypatch,
    code,
    message,
):
    calls = 0

    class FakeChannel:
        @staticmethod
        def resolve_video_model(_config, *, model, model_id):
            return model_id or model

        @staticmethod
        def ensure_image_url(_config, path):
            return path

        @staticmethod
        def request_video(_config, **_kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise ToolError(code, message)
            return {"video_url": "https://video.example/result.mp4"}

    config = SimpleNamespace(
        assets=SimpleNamespace(
            seedance=SimpleNamespace(
                transient_max_attempts=3,
                transient_retry_delay_seconds=0,
            )
        )
    )
    monkeypatch.setattr(seedance, "get_provider", lambda _config: FakeChannel())

    result = seedance.generate_video(
        config,
        prompt="bird flaps",
        output=tmp_path / "result.mp4",
        print_url_only=True,
    )

    assert calls == 2
    assert result["generation_attempts"] == 2


@pytest.mark.parametrize(
    ("code", "message"),
    [
        ("AUTH_FAILED", "bad credentials"),
        ("INVALID_CONFIG", "unknown model"),
        ("SEEDANCE_FAILED", "content policy violation"),
        ("SEEDANCE_FAILED", "seedance returned HTTP 400"),
    ],
)
def test_generate_video_does_not_retry_terminal_failures(
    tmp_path,
    monkeypatch,
    code,
    message,
):
    calls = 0

    class FakeChannel:
        @staticmethod
        def resolve_video_model(_config, *, model, model_id):
            return model_id or model

        @staticmethod
        def ensure_image_url(_config, path):
            return path

        @staticmethod
        def request_video(_config, **_kwargs):
            nonlocal calls
            calls += 1
            raise ToolError(code, message)

    config = SimpleNamespace(
        assets=SimpleNamespace(
            seedance=SimpleNamespace(
                transient_max_attempts=3,
                transient_retry_delay_seconds=0,
            )
        )
    )
    monkeypatch.setattr(seedance, "get_provider", lambda _config: FakeChannel())

    with pytest.raises(ToolError, match=message):
        seedance.generate_video(
            config,
            prompt="bird flaps",
            output=tmp_path / "result.mp4",
            print_url_only=True,
        )

    assert calls == 1


def test_video_download_retries_same_url_without_regeneration(tmp_path, monkeypatch):
    generation_calls = 0
    download_calls = 0

    class FakeChannel:
        @staticmethod
        def resolve_video_model(_config, *, model, model_id):
            return model_id or model

        @staticmethod
        def ensure_image_url(_config, path):
            return path

        @staticmethod
        def request_video(_config, **_kwargs):
            nonlocal generation_calls
            generation_calls += 1
            return {"video_url": "https://video.example/result.mp4"}

        @staticmethod
        def video_verify_ssl(_config):
            return True

    class DownloadResponse:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        @staticmethod
        def raise_for_status():
            return None

        @staticmethod
        def iter_content(*, chunk_size):
            assert chunk_size == 1024 * 1024
            yield b"video"

    def fake_get(*_args, **_kwargs):
        nonlocal download_calls
        download_calls += 1
        if download_calls == 1:
            raise seedance.requests.exceptions.ConnectionError("reset")
        return DownloadResponse()

    config = SimpleNamespace(
        assets=SimpleNamespace(
            seedance=SimpleNamespace(
                transient_max_attempts=3,
                transient_retry_delay_seconds=0,
            )
        )
    )
    monkeypatch.setattr(seedance, "get_provider", lambda _config: FakeChannel())
    monkeypatch.setattr(seedance.requests, "get", fake_get)
    output = tmp_path / "result.mp4"

    result = seedance.generate_video(
        config,
        prompt="bird flaps",
        output=output,
    )

    assert generation_calls == 1
    assert download_calls == 2
    assert output.read_bytes() == b"video"
    assert result["generation_attempts"] == 1
    assert result["download_attempts"] == 2


def test_native_poll_fails_fast_on_non_retryable_http(monkeypatch):
    calls = 0

    class Response:
        status_code = 404
        text = '{"error":{"code":"ResourceNotFound"}}'

    def fake_get(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        return Response()

    monkeypatch.setattr(seedance.requests, "get", fake_get)
    config = SimpleNamespace(
        seedance=SimpleNamespace(poll_interval_seconds=10.0)
    )
    provider = SimpleNamespace(
        video_auth_headers=lambda *_args, **_kwargs: {},
    )

    with pytest.raises(ToolError, match="HTTP 404"):
        seedance._poll_task(
            config,
            provider=provider,
            endpoint="https://ark.example/tasks",
            verify_ssl=True,
            task_id="missing-task",
            native_model="seedance-model",
            server_timeout=600,
        )

    assert calls == 1


def test_ark_native_video_transport_submits_and_polls(monkeypatch):
    captured = {}

    class SubmitResponse:
        status_code = 200
        text = ""

        @staticmethod
        def json():
            return {"id": "task-123"}

    class PollResponse:
        status_code = 200
        text = ""

        @staticmethod
        def json():
            return {
                "status": "succeeded",
                "content": {"video_url": "https://video.example/native.mp4"},
                "resolution": "480p",
            }

    def fake_post(url, *, headers, json, timeout, verify):
        captured.update(
            url=url,
            headers=headers,
            payload=json,
            timeout=timeout,
            verify=verify,
        )
        return SubmitResponse()

    monkeypatch.setattr(seedance.requests, "post", fake_post)
    monkeypatch.setattr(
        seedance.requests,
        "get",
        lambda *_args, **_kwargs: PollResponse(),
    )
    config = SimpleNamespace(
        seedance=SimpleNamespace(poll_interval_seconds=0.01)
    )
    provider = SimpleNamespace(
        video_endpoint=lambda _config: "https://ark.example/tasks",
        video_verify_ssl=lambda _config: True,
        video_auth_headers=lambda *_args, **_kwargs: {
            "Authorization": "Bearer test"
        },
    )

    result = seedance._request_native_video(
        config,
        provider=provider,
        prompt="bird flaps",
        model_id="doubao-seedance-2-0-fast-260128",
        duration=5,
        ratio="1:1",
        resolution="480p",
        seed=7,
        camerafixed=True,
        watermark=False,
        image_urls=[],
        first_frame_url="data:image/png;base64,AAAA",
        last_frame_url=None,
        extra_params=None,
        server_timeout=600,
    )

    assert captured["url"] == "https://ark.example/tasks"
    assert captured["payload"]["model"] == "doubao-seedance-2-0-fast-260128"
    content = captured["payload"]["content"]
    assert "--dur 5" in content[0]["text"]
    assert content[1] == {
        "type": "image_url",
        "image_url": {"url": "data:image/png;base64,AAAA"},
        "role": "first_frame",
    }
    assert result["task_id"] == "task-123"
    assert result["protocol"] == "ark_native_task"
