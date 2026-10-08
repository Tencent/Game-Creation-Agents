from __future__ import annotations

import shutil
import subprocess
import threading
import time
from dataclasses import replace
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

from gca_mcp.adapters import matting_availability, rmbg2, rmbg2_video
from gca_mcp.adapters.rmbg2_video import encode_preview_from_frames
from gca_mcp.config import Config
from gca_mcp.errors import ToolError


@pytest.fixture(autouse=True)
def _reset_rmbg_availability():
    matting_availability.reset_rmbg_availability()
    yield
    matting_availability.reset_rmbg_availability()


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg required")
def test_preview_composites_transparency_onto_black(tmp_path: Path) -> None:
    frames = tmp_path / "frames"
    frames.mkdir()
    for index in (1, 2):
        image = Image.new("RGBA", (32, 32), (255, 0, 255, 0))
        for x in range(10, 22):
            for y in range(10, 22):
                image.putpixel((x, y), (220, 20, 20, 255))
        image.save(frames / f"{index:04d}.png")

    preview = tmp_path / "preview.mp4"
    assert encode_preview_from_frames(frames, preview, fps=2) == str(preview)

    decoded = tmp_path / "decoded.png"
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-loglevel",
            "error",
            "-i",
            str(preview),
            "-frames:v",
            "1",
            str(decoded),
        ],
        check=True,
    )
    with Image.open(decoded).convert("RGB") as frame:
        corner = frame.getpixel((0, 0))
        subject = frame.getpixel((16, 16))
    assert max(corner) < 20
    assert subject[0] > 120
    assert subject[0] > subject[1] * 3


def _stub_video_io(monkeypatch, *, plate_color=(255, 0, 255, 255)) -> None:
    monkeypatch.setattr(rmbg2_video, "_require_ffmpeg", lambda: None)
    monkeypatch.setattr(rmbg2_video, "_probe_fps", lambda _video: 12.0)
    monkeypatch.setattr(
        rmbg2_video,
        "_encode_outputs",
        lambda *_args, **_kwargs: {},
    )

    def extract(_video, frames_dir, **_kwargs):
        frames_dir.mkdir(parents=True, exist_ok=True)
        paths = []
        for index in range(1, 4):
            frame = Image.new("RGBA", (16, 16), plate_color)
            if plate_color == (255, 0, 255, 255):
                for x in range(5, 11):
                    for y in range(5, 11):
                        frame.putpixel((x, y), (30 * index, 80, 180, 255))
            path = frames_dir / f"{index:04d}.png"
            frame.save(path)
            paths.append(path)
        return paths

    monkeypatch.setattr(rmbg2_video, "_extract_frames", extract)


def _png_bytes(color=(20, 30, 40, 255)) -> bytes:
    buffer = BytesIO()
    Image.new("RGBA", (16, 16), color).save(buffer, format="PNG")
    return buffer.getvalue()


def test_rmbg_output_validation_decodes_pixels() -> None:
    valid = _png_bytes()
    rmbg2.validate_rmbg_output(valid)

    with pytest.raises(ToolError, match="malformed image response"):
        rmbg2.validate_rmbg_output(valid[: len(valid) // 2])


@pytest.mark.parametrize("status", [404, 502])
def test_auto_video_fallback_remattes_entire_clip_without_mixed_frames(
    tmp_path,
    monkeypatch,
    status,
):
    _stub_video_io(monkeypatch)
    input_video = tmp_path / "input.mp4"
    input_video.write_bytes(b"video")

    class FlakyProvider:
        def __init__(self):
            self.calls = 0
            self.lock = threading.Lock()

        def rmbg_matte(self, *_args, **_kwargs):
            with self.lock:
                self.calls += 1
                call = self.calls
            if call == 1:
                return _png_bytes(), {"backend": "remote"}
            raise ToolError(
                "RMBG2_FAILED",
                f"RMBG2 service returned HTTP {status}.",
            )

    provider = FlakyProvider()
    monkeypatch.setattr(rmbg2_video, "get_provider", lambda _config: provider)

    result = rmbg2_video.matte_video(
        Config.from_env(),
        input_video=input_video,
        output_dir=tmp_path / "matte",
        formats=["gif"],
        bg_hex="#FF00FF",
        drop_bg_leak=False,
    )

    frames = sorted((tmp_path / "matte/transparent_frames").glob("*.png"))
    assert [frame.name for frame in frames] == [
        "0001.png",
        "0002.png",
        "0003.png",
    ]
    assert result["matting"] == "color_key"
    assert result["matting_requested"] == "auto"
    assert result["matting_fallback"] is True
    assert result["matting_fallback_code"] == "RMBG2_FAILED"
    assert result["rmbg_failures_before_fallback"] == 2
    assert result["frames_failed"] == 0
    for frame in frames:
        with Image.open(frame) as image:
            assert image.getchannel("A").getextrema() == (0, 255)


def test_auto_video_probes_once_then_reuses_cached_fallback(
    tmp_path,
    monkeypatch,
):
    _stub_video_io(monkeypatch)
    input_video = tmp_path / "input.mp4"
    input_video.write_bytes(b"video")

    class UnavailableProvider:
        channel = "test"

        def __init__(self):
            self.calls = 0

        def rmbg_matte(self, *_args, **_kwargs):
            self.calls += 1
            raise ToolError(
                "RMBG2_FAILED",
                "RMBG2 service returned HTTP 502.",
            )

    provider = UnavailableProvider()
    monkeypatch.setattr(rmbg2_video, "get_provider", lambda _config: provider)
    config = Config.from_env()

    first = rmbg2_video.matte_video(
        config,
        input_video=input_video,
        output_dir=tmp_path / "first",
        formats=["gif"],
        bg_hex="#FF00FF",
        drop_bg_leak=False,
    )
    second = rmbg2_video.matte_video(
        config,
        input_video=input_video,
        output_dir=tmp_path / "second",
        formats=["gif"],
        bg_hex="#FF00FF",
        drop_bg_leak=False,
    )

    assert provider.calls == 1
    assert first["rmbg_failures_before_fallback"] == 1
    assert first["rmbg_probe_cached"] is False
    assert second["rmbg_failures_before_fallback"] == 0
    assert second["rmbg_probe_cached"] is True
    assert second["matting"] == "color_key"


def test_local_frame_io_failure_does_not_poison_rmbg_cache(
    tmp_path,
    monkeypatch,
):
    _stub_video_io(monkeypatch)
    input_video = tmp_path / "input.mp4"
    input_video.write_bytes(b"video")
    config = Config.from_env()
    monkeypatch.setattr(
        rmbg2_video,
        "_matte_frame",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            OSError("local disk write failed")
        ),
    )

    with pytest.raises(ToolError, match="Local RMBG frame I/O failed"):
        rmbg2_video.matte_video(
            config,
            input_video=input_video,
            output_dir=tmp_path / "matte",
            formats=["gif"],
            bg_hex="#FF00FF",
            drop_bg_leak=False,
        )

    route = matting_availability.acquire_rmbg_route(
        config,
        config.rmbg2.endpoint,
    )
    assert route.action == "probe"
    matting_availability.cancel_rmbg_probe(route)


def test_malformed_provider_image_is_cached_for_auto_fallback(
    tmp_path,
    monkeypatch,
):
    _stub_video_io(monkeypatch)
    input_video = tmp_path / "input.mp4"
    input_video.write_bytes(b"video")
    config = Config.from_env()
    provider = SimpleNamespace(
                rmbg_matte=lambda *_args, **_kwargs: (
            b"not-a-png",
            {"backend": "remote"},
        ),
    )
    monkeypatch.setattr(rmbg2_video, "get_provider", lambda _config: provider)

    result = rmbg2_video.matte_video(
        config,
        input_video=input_video,
        output_dir=tmp_path / "matte",
        formats=["gif"],
        bg_hex="#FF00FF",
        drop_bg_leak=False,
    )

    cached = matting_availability.acquire_rmbg_route(
        config,
        config.rmbg2.endpoint,
    )
    assert cached.action == "color_key"
    assert result["matting"] == "color_key"
    assert result["matting_fallback"] is True


def test_provider_failure_is_remembered_independent_of_local_error_order(
    tmp_path,
    monkeypatch,
):
    _stub_video_io(monkeypatch)
    input_video = tmp_path / "input.mp4"
    input_video.write_bytes(b"video")
    config = Config.from_env()

    def mixed_matte(src, dst, **_kwargs):
        if src.name == "0001.png":
            shutil.copy2(src, dst)
            return
        if src.name == "0002.png":
            raise OSError("local disk read failed")
        time.sleep(0.05)
        raise ToolError("RMBG2_FAILED", "RMBG2 returned HTTP 502.")

    monkeypatch.setattr(rmbg2_video, "_matte_frame", mixed_matte)

    with pytest.raises(ToolError, match="Local RMBG frame I/O failed"):
        rmbg2_video.matte_video(
            config,
            input_video=input_video,
            output_dir=tmp_path / "matte",
            formats=["gif"],
            bg_hex="#FF00FF",
            drop_bg_leak=False,
        )

    cached = matting_availability.acquire_rmbg_route(
        config,
        config.rmbg2.endpoint,
    )
    assert cached.action == "color_key"


def test_auto_video_fallback_does_not_hide_auth_failure(
    tmp_path,
    monkeypatch,
):
    _stub_video_io(monkeypatch)
    input_video = tmp_path / "input.mp4"
    input_video.write_bytes(b"video")
    provider = SimpleNamespace(
        rmbg_matte=lambda *_args, **_kwargs: (_ for _ in ()).throw(
            ToolError("AUTH_FAILED", "RMBG2 rejected HTTP 401.")
        )
    )
    monkeypatch.setattr(rmbg2_video, "get_provider", lambda _config: provider)

    with pytest.raises(ToolError) as exc_info:
        rmbg2_video.matte_video(
            Config.from_env(),
            input_video=input_video,
            output_dir=tmp_path / "matte",
            formats=["gif"],
            bg_hex="#FF00FF",
            drop_bg_leak=False,
        )

    assert exc_info.value.code == "AUTH_FAILED"
    assert not (tmp_path / "matte/transparent_frames").exists()


def test_explicit_rmbg2_video_backend_keeps_fail_closed_behavior(
    tmp_path,
    monkeypatch,
):
    _stub_video_io(monkeypatch)
    input_video = tmp_path / "input.mp4"
    input_video.write_bytes(b"video")
    provider = SimpleNamespace(
        rmbg_matte=lambda *_args, **_kwargs: (_ for _ in ()).throw(
            ToolError(
                "RMBG2_FAILED",
                "RMBG2 service returned HTTP 502.",
            )
        )
    )
    monkeypatch.setattr(rmbg2_video, "get_provider", lambda _config: provider)
    config = Config.from_env()
    config = replace(
        config,
        assets=replace(
            config.assets,
            matting=replace(config.assets.matting, backend="rmbg2"),
        ),
    )

    with pytest.raises(ToolError) as exc_info:
        rmbg2_video.matte_video(
            config,
            input_video=input_video,
            output_dir=tmp_path / "matte",
            formats=["gif"],
            bg_hex="#FF00FF",
            drop_bg_leak=False,
        )

    assert exc_info.value.code == "RMBG2_FAILED"


def test_auto_video_color_key_fallback_fails_closed_on_invalid_plate(
    tmp_path,
    monkeypatch,
):
    _stub_video_io(monkeypatch, plate_color=(255, 255, 255, 255))
    input_video = tmp_path / "input.mp4"
    input_video.write_bytes(b"video")
    provider = SimpleNamespace(
        rmbg_matte=lambda *_args, **_kwargs: (_ for _ in ()).throw(
            ToolError("NETWORK_ERROR", "RMBG2 request timed out.")
        )
    )
    monkeypatch.setattr(rmbg2_video, "get_provider", lambda _config: provider)

    with pytest.raises(ToolError) as exc_info:
        rmbg2_video.matte_video(
            Config.from_env(),
            input_video=input_video,
            output_dir=tmp_path / "matte",
            formats=["gif"],
            bg_hex="#FF00FF",
            drop_bg_leak=False,
        )

    assert exc_info.value.code == "COLOR_KEY_MATTING_FAILED"
    assert not (tmp_path / "matte/transparent_frames").exists()
