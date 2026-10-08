"""Regression tests for produce_validated_video (VID-01, VID-02, VID-03)."""

from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

from gca_mcp.adapters import asset_video_validator as avv
from gca_mcp.adapters import produce_validated_video as pvv
from gca_mcp.adapters import rmbg2_video, seedance
from gca_mcp.errors import ToolError


def _verdict(ids_to_verdict: dict[str, str], overall: str | None = None):
    results = [
        {"id": k, "name": k, "verdict": v, "evidence": None}
        for k, v in ids_to_verdict.items()
    ]
    failed = [r for r in results if r["verdict"] == "FAIL"]
    passed = [r for r in results if r["verdict"] == "PASS"]
    unknown = [r for r in results if r["verdict"] is None]
    if overall is None:
        overall = "PASS" if not failed and not unknown else "FAIL"
    return {
        "verdict": overall,
        "overall_verdict_reported": overall,
        "passed": overall == "PASS",
        "rubric_results": results,
        "failures": failed,
        "n_passed": len(passed),
        "n_failed": len(failed),
        "n_unknown": len(unknown),
        "duplicate_ids": [],
        "unexpected_ids": [],
        "raw_response": "raw",
    }


@pytest.fixture(autouse=True)
def _stub_preview_encoder(monkeypatch):
    def fake_preview(frames_dir, output, **kw):
        Path(output).parent.mkdir(parents=True, exist_ok=True)
        Path(output).write_bytes(b"matted-preview")
        return str(output)

    monkeypatch.setattr(rmbg2_video, "encode_preview_from_frames", fake_preview)


def _visible_frame(size=(32, 32)):
    """An RGBA frame with real opaque content (a filled square) — a HEALTHY
    matte, unlike a fully-transparent (alpha=0) frame."""
    from PIL import Image

    im = Image.new("RGBA", size, (0, 0, 0, 0))
    # paint an opaque 20x20 block so visible-alpha health check passes
    block = Image.new("RGBA", (20, 20), (200, 40, 40, 255))
    im.paste(block, (6, 6))
    return im


def _write_matte(matte_dir: Path):
    matte_dir.mkdir(parents=True, exist_ok=True)
    # Real transparent PNG frame set (what pack_spritesheet consumes) with
    # visible opaque content — a healthy matte …
    frames = matte_dir / "transparent_frames"
    frames.mkdir(parents=True, exist_ok=True)
    for i in range(1, 4):
        _visible_frame().save(frames / f"{i:04d}.png")
    gif = matte_dir / "transparent.gif"
    _visible_frame().save(gif)
    return {
        "outputs": {"gif": str(gif), "mp4_preview": None},
        "frames_total": 3,
        "frames_failed": 0,
        "bg_leak_stats": None,
    }


def _write_matte_gif_softfail(matte_dir: Path):
    """Matte succeeds (healthy PNG frames present) but the GIF encode soft-failed."""
    frames = matte_dir / "transparent_frames"
    frames.mkdir(parents=True, exist_ok=True)
    for i in range(1, 4):
        _visible_frame().save(frames / f"{i:04d}.png")
    # No transparent.gif written; outputs carries a gif_error instead.
    return {
        "outputs": {"gif_error": "boom", "mp4_preview": None},
        "frames_total": 3,
        "frames_failed": 0,
        "bg_leak_stats": None,
    }


def _write_matte_all_transparent(matte_dir: Path):
    """Matte 'succeeded' but every frame is fully alpha-zero (blank) — an
    UNHEALTHY matte that must NOT be reported usable (PR #123 review #5)."""
    from PIL import Image

    frames = matte_dir / "transparent_frames"
    frames.mkdir(parents=True, exist_ok=True)
    for i in range(1, 4):
        Image.new("RGBA", (32, 32), (0, 0, 0, 0)).save(frames / f"{i:04d}.png")
    return {
        "outputs": {"gif_error": "boom", "mp4_preview": None},
        "frames_total": 3,
        "frames_failed": 0,
        "bg_leak_stats": None,
    }


# -------------------- VID-02: rubric completeness --------------------


def test_strict_pass_for_ids_blocks_unknown_and_missing():
    framing = [r["id"] for r in avv.rubrics_as_dicts(avv.FRAMING_RUBRICS)]
    v_ok = _verdict({rid: "PASS" for rid in framing})
    assert pvv._strict_pass_for_ids(v_ok, framing)
    # UNKNOWN one rubric (validator overall forced PASS) → blocked
    d = {rid: "PASS" for rid in framing}
    d[framing[0]] = None
    v_unknown = _verdict(d, overall="PASS")
    assert not pvv._strict_pass_for_ids(v_unknown, framing)
    # missing rubric → blocked
    v_missing = _verdict({framing[0]: "PASS"})
    assert not pvv._strict_pass_for_ids(v_missing, framing)
    # Folded all-PASS results still fail when parser metadata saw a duplicate.
    v_duplicate = _verdict({rid: "PASS" for rid in framing})
    v_duplicate["duplicate_ids"] = [framing[0]]
    assert not pvv._strict_pass_for_ids(v_duplicate, framing)


def test_video_with_unknown_rubric_is_not_passed(tmp_path, monkeypatch):
    def fake_gen(config, *, prompt, output, **kw):
        Path(output).parent.mkdir(parents=True, exist_ok=True)
        Path(output).write_bytes(b"fakevideo")

    def fake_matte(config, *, input_video, output_dir, **kw):
        return _write_matte(Path(output_dir))

    def fake_validate(config, *, video_path, rubric_override, **kw):
        ids = [r["id"] for r in rubric_override]
        d = {rid: "PASS" for rid in ids}
        d[ids[0]] = None  # one UNKNOWN — validator still says overall PASS
        return _verdict(d, overall="PASS")

    monkeypatch.setattr(seedance, "generate_video", fake_gen)
    monkeypatch.setattr(rmbg2_video, "matte_video", fake_matte)
    monkeypatch.setattr(avv, "validate_asset_video", fake_validate)

    out = pvv.produce_validated_video(
        None,
        base_prompt="walk",
        first_frame=str(tmp_path / "ff.png"),
        output_dir=tmp_path / "out",
        action_description="knight walks",
        n_candidates=1,
    )
    assert out["passed"] is False


def test_video_summary_propagates_resolved_matting_backend(
    tmp_path,
    monkeypatch,
):
    def fake_gen(config, *, prompt, output, **kw):
        Path(output).parent.mkdir(parents=True, exist_ok=True)
        Path(output).write_bytes(b"fakevideo")

    def fake_matte(config, *, input_video, output_dir, **kw):
        result = _write_matte(Path(output_dir))
        result.update(
            {
                "matting": "color_key",
                "matting_requested": "auto",
                "matting_fallback": True,
                "matting_fallback_code": "RMBG2_FAILED",
                "matting_fallback_reason": (
                    "RMBG2_FAILED: RMBG2 service returned HTTP 502."
                ),
                "matting_key_color": "#FF00FF",
                "rmbg_failures_before_fallback": 3,
            }
        )
        return result

    def fake_validate(config, *, video_path, rubric_override, **kw):
        ids = [rubric["id"] for rubric in rubric_override]
        return _verdict({rubric_id: "PASS" for rubric_id in ids})

    monkeypatch.setattr(seedance, "generate_video", fake_gen)
    monkeypatch.setattr(rmbg2_video, "matte_video", fake_matte)
    monkeypatch.setattr(avv, "validate_asset_video", fake_validate)

    out = pvv.produce_validated_video(
        None,
        base_prompt="walk",
        first_frame=str(tmp_path / "ff.png"),
        output_dir=tmp_path / "out",
        action_description="knight walks",
        n_candidates=1,
    )

    assert out["passed"] is True
    assert out["matting"] == "color_key"
    assert out["matting_requested"] == "auto"
    assert out["matting_fallback"] is True
    assert out["matting_fallback_code"] == "RMBG2_FAILED"
    assert out["matting_key_color"] == "#FF00FF"
    assert out["candidates"][0]["rmbg_failures_before_fallback"] == 3


def test_framing_and_content_validation_overlap_and_merge_in_order(
    tmp_path,
    monkeypatch,
):
    def fake_gen(config, *, prompt, output, **kw):
        Path(output).parent.mkdir(parents=True, exist_ok=True)
        Path(output).write_bytes(b"fakevideo")

    def fake_matte(config, *, input_video, output_dir, **kw):
        return _write_matte(Path(output_dir))

    started = threading.Barrier(2)
    content_finished = threading.Event()
    call_ids: list[list[str]] = []

    def fake_validate(config, *, video_path, rubric_override, **kw):
        ids = [rubric["id"] for rubric in rubric_override]
        call_ids.append(ids)
        started.wait(timeout=2)
        if ids[0] == "R1":
            assert content_finished.wait(timeout=2)
        else:
            content_finished.set()
        return _verdict({rubric_id: "PASS" for rubric_id in ids})

    monkeypatch.setattr(seedance, "generate_video", fake_gen)
    monkeypatch.setattr(rmbg2_video, "matte_video", fake_matte)
    monkeypatch.setattr(avv, "validate_asset_video", fake_validate)

    out = pvv.produce_validated_video(
        None,
        base_prompt="walk",
        first_frame=str(tmp_path / "ff.png"),
        output_dir=tmp_path / "out",
        action_description="knight walks",
        n_candidates=1,
    )
    verdict = json.loads(
        (tmp_path / "out/candidate_01/verdict.json").read_text()
    )

    assert out["passed"] is True
    assert len(call_ids) == 2
    assert [item["id"] for item in verdict["rubric_results"]] == [
        "R1",
        "R2",
        "R3",
        "R4",
        "R5",
        "R6",
        "R7",
    ]
    assert list(verdict["passes"]) == ["framing", "content"]


# -------------------- VID-03: don't ship unvalidated --------------------


def test_validation_unavailable_ships_nothing(tmp_path, monkeypatch):
    def fake_gen(config, *, prompt, output, **kw):
        Path(output).parent.mkdir(parents=True, exist_ok=True)
        Path(output).write_bytes(b"fakevideo")

    def fake_matte(config, *, input_video, output_dir, **kw):
        return _write_matte(Path(output_dir))

    def boom_validate(config, **kw):
        raise ToolError("VLM_ERROR", "service down")

    monkeypatch.setattr(seedance, "generate_video", fake_gen)
    monkeypatch.setattr(rmbg2_video, "matte_video", fake_matte)
    monkeypatch.setattr(avv, "validate_asset_video", boom_validate)

    out = pvv.produce_validated_video(
        None,
        base_prompt="walk",
        first_frame=str(tmp_path / "ff.png"),
        output_dir=tmp_path / "out",
        action_description="knight walks",
        n_candidates=2,
    )
    assert out["validation_state"] == "validation_unavailable"
    assert out["passed"] is False
    assert out["usable"] is False
    assert out["winner_candidate"] is None
    assert out["final_transparent_gif"] is None
    assert not (tmp_path / "out" / "final").exists()


def test_allow_unvalidated_escape_hatch(tmp_path, monkeypatch):
    def fake_gen(config, *, prompt, output, **kw):
        Path(output).parent.mkdir(parents=True, exist_ok=True)
        Path(output).write_bytes(b"fakevideo")

    def fake_matte(config, *, input_video, output_dir, **kw):
        return _write_matte(Path(output_dir))

    def boom_validate(config, **kw):
        raise ToolError("VLM_ERROR", "service down")

    monkeypatch.setattr(seedance, "generate_video", fake_gen)
    monkeypatch.setattr(rmbg2_video, "matte_video", fake_matte)
    monkeypatch.setattr(avv, "validate_asset_video", boom_validate)

    out = pvv.produce_validated_video(
        None,
        base_prompt="walk",
        first_frame=str(tmp_path / "ff.png"),
        output_dir=tmp_path / "out",
        action_description="knight walks",
        n_candidates=1,
        allow_unvalidated=True,
    )
    assert out["validation_state"] == "validation_unavailable"
    assert out["winner_candidate"] is not None
    assert out["passed"] is False


# -------------------- VID-01: stale rmbg2 not reused --------------------


def test_stale_final_cleared_when_nothing_ships(tmp_path, monkeypatch):
    out_dir = tmp_path / "out"
    # Pre-seed a stale final/ from a previous run.
    stale = out_dir / "final" / "rmbg2"
    stale.mkdir(parents=True)
    (stale / "transparent.gif").write_bytes(b"OLD")

    def fake_gen(config, *, prompt, output, **kw):
        raise ToolError("SEEDANCE_NO_OUTPUT", "down")

    monkeypatch.setattr(seedance, "generate_video", fake_gen)

    out = pvv.produce_validated_video(
        None,
        base_prompt="walk",
        first_frame=str(tmp_path / "ff.png"),
        output_dir=out_dir,
        action_description="knight walks",
        n_candidates=1,
    )
    assert out["usable"] is False
    assert out["final_transparent_gif"] is None
    assert not (out_dir / "final").exists()


# -------------------- VID-03: UNKNOWN cannot win fallback --------------------


def test_complete_for_ids_distinguishes_judged_from_unknown():
    ids = ["R1", "R2"]
    # fully judged with a real FAIL → complete
    assert pvv._complete_for_ids(_verdict({"R1": "FAIL", "R2": "PASS"}), ids)
    # 0 FAIL but UNKNOWNs → NOT complete (can't enter judged fallback pool)
    assert not pvv._complete_for_ids(
        _verdict({"R1": None, "R2": None}, overall="PASS"), ids
    )
    # missing rubric → not complete
    assert not pvv._complete_for_ids(_verdict({"R1": "PASS"}), ids)


def test_unknown_candidate_does_not_beat_judged_fail(tmp_path, monkeypatch):
    # Candidate 1: 0 FAIL / 2 UNKNOWN (incomplete). Candidate 2: 1 FAIL / 0
    # UNKNOWN (fully judged). The judged FAIL must win the fallback, not the
    # UNKNOWN-riddled take — and it must NOT be reported as a clean pass.
    calls = {"n": 0}

    def fake_gen(config, *, prompt, output, **kw):
        Path(output).parent.mkdir(parents=True, exist_ok=True)
        Path(output).write_bytes(b"fakevideo")

    def fake_matte(config, *, input_video, output_dir, **kw):
        return _write_matte(Path(output_dir))

    def fake_validate(config, *, video_path, rubric_override, **kw):
        # Count validator invocations; two passes per attempt.
        calls["n"] += 1
        ids = [r["id"] for r in rubric_override]
        attempt = (calls["n"] + 1) // 2  # 1,1 → attempt1 ; 2,2 → attempt2
        if attempt == 1:
            d = {rid: None for rid in ids}  # all UNKNOWN
            return _verdict(d, overall="PASS")
        d = {rid: "PASS" for rid in ids}
        d[ids[0]] = "FAIL"  # one real FAIL, fully judged
        return _verdict(d)

    monkeypatch.setattr(seedance, "generate_video", fake_gen)
    monkeypatch.setattr(rmbg2_video, "matte_video", fake_matte)
    monkeypatch.setattr(avv, "validate_asset_video", fake_validate)

    out = pvv.produce_validated_video(
        None,
        base_prompt="walk",
        first_frame=str(tmp_path / "ff.png"),
        output_dir=tmp_path / "out",
        action_description="knight walks",
        n_candidates=2,
    )
    assert out["passed"] is False
    assert out["validation_state"] == "validation_failed"
    # winner is the fully-judged candidate (2), never the UNKNOWN one (1)
    assert out["winner_candidate"] == 2
    assert out["n_judged"] == 1
    assert out["n_incomplete"] == 1


def test_all_incomplete_ships_nothing_by_default(tmp_path, monkeypatch):
    def fake_gen(config, *, prompt, output, **kw):
        Path(output).parent.mkdir(parents=True, exist_ok=True)
        Path(output).write_bytes(b"fakevideo")

    def fake_matte(config, *, input_video, output_dir, **kw):
        return _write_matte(Path(output_dir))

    def fake_validate(config, *, video_path, rubric_override, **kw):
        ids = [r["id"] for r in rubric_override]
        return _verdict({rid: None for rid in ids}, overall="PASS")

    monkeypatch.setattr(seedance, "generate_video", fake_gen)
    monkeypatch.setattr(rmbg2_video, "matte_video", fake_matte)
    monkeypatch.setattr(avv, "validate_asset_video", fake_validate)

    out = pvv.produce_validated_video(
        None,
        base_prompt="walk",
        first_frame=str(tmp_path / "ff.png"),
        output_dir=tmp_path / "out",
        action_description="knight walks",
        n_candidates=2,
    )
    assert out["validation_state"] == "validation_incomplete"
    assert out["passed"] is False
    assert out["usable"] is False
    assert out["winner_candidate"] is None


# -------------------- VID (GIF soft-fail): usable from PNG frames --------------------


def test_gif_softfail_still_usable_from_png_frames(tmp_path, monkeypatch):
    # Matting + validation succeed, but the GIF encode soft-fails. The valid
    # transparent PNG frames must still make the take usable (they are the
    # exact inputs pack_spritesheet needs) — the old code gated `usable` on the
    # GIF's presence and wrongly marked it unusable.
    def fake_gen(config, *, prompt, output, **kw):
        Path(output).parent.mkdir(parents=True, exist_ok=True)
        Path(output).write_bytes(b"fakevideo")

    def fake_matte(config, *, input_video, output_dir, **kw):
        return _write_matte_gif_softfail(Path(output_dir))

    def fake_validate(config, *, video_path, rubric_override, **kw):
        ids = [r["id"] for r in rubric_override]
        return _verdict({rid: "PASS" for rid in ids})

    monkeypatch.setattr(seedance, "generate_video", fake_gen)
    monkeypatch.setattr(rmbg2_video, "matte_video", fake_matte)
    monkeypatch.setattr(avv, "validate_asset_video", fake_validate)

    out = pvv.produce_validated_video(
        None,
        base_prompt="walk",
        first_frame=str(tmp_path / "ff.png"),
        output_dir=tmp_path / "out",
        action_description="knight walks",
        n_candidates=1,
    )
    assert out["passed"] is True
    assert out["usable"] is True
    assert out["final_transparent_gif"] is None  # GIF genuinely absent
    assert out["final_transparent_frames_dir"] is not None
    assert out["final_frame_count"] == 3


# -------------------- PR #123 review #5: matte-health gate --------------------


def test_all_transparent_frames_not_usable(tmp_path, monkeypatch):
    # Matte 'succeeds' with frames present, but every frame is fully alpha-zero.
    # The publication gate must NOT report this usable — visible-alpha health
    # check fails. (The old gate shipped it because one PNG existed.)
    def fake_gen(config, *, prompt, output, **kw):
        Path(output).parent.mkdir(parents=True, exist_ok=True)
        Path(output).write_bytes(b"fakevideo")

    def fake_matte(config, *, input_video, output_dir, **kw):
        return _write_matte_all_transparent(Path(output_dir))

    def fake_validate(config, *, video_path, rubric_override, **kw):
        ids = [r["id"] for r in rubric_override]
        return _verdict({rid: "PASS" for rid in ids})

    monkeypatch.setattr(seedance, "generate_video", fake_gen)
    monkeypatch.setattr(rmbg2_video, "matte_video", fake_matte)
    monkeypatch.setattr(avv, "validate_asset_video", fake_validate)

    out = pvv.produce_validated_video(
        None,
        base_prompt="walk",
        first_frame=str(tmp_path / "ff.png"),
        output_dir=tmp_path / "out",
        action_description="knight walks",
        n_candidates=1,
    )
    assert out["usable"] is False
    assert out["matte_health"] is not None
    assert out["matte_health"]["has_visible_alpha"] is False
    assert out["passed"] is False
    assert out["validation_state"] == "matte_failed"
    assert not (tmp_path / "out" / "final").exists()


def test_two_surviving_frames_out_of_120_is_unhealthy(tmp_path):
    frames = tmp_path / "frames"
    frames.mkdir()
    _visible_frame().save(frames / "0001.png")
    _visible_frame().save(frames / "0002.png")
    health = rmbg2_video.assess_matte_health(
        frames,
        frames_total=120,
        frames_failed=0,
        bg_leak_stats={"dropped": 118, "kept": 2},
    )
    assert health["healthy"] is False
    assert health["success_ratio"] < 0.02


def test_fully_opaque_frames_are_not_a_healthy_matte(tmp_path):
    from PIL import Image

    frames = tmp_path / "opaque-frames"
    frames.mkdir()
    for index in (1, 2, 3):
        Image.new("RGBA", (32, 32), (20, 40, 60, 255)).save(
            frames / f"{index:04d}.png"
        )
    health = rmbg2_video.assess_matte_health(
        frames,
        frames_total=3,
        frames_failed=0,
    )
    assert health["healthy"] is False
    assert health["transparent_frames_ok"] is False


def test_one_percent_transparent_hole_does_not_make_matte_healthy(tmp_path):
    from PIL import Image

    frames = tmp_path / "near-opaque-frames"
    frames.mkdir()
    for index in (1, 2, 3):
        image = Image.new("RGBA", (100, 100), (20, 40, 60, 255))
        for x in range(10):
            for y in range(10):
                image.putpixel((x, y), (0, 0, 0, 0))
        image.save(frames / f"{index:04d}.png")
    health = rmbg2_video.assess_matte_health(
        frames,
        frames_total=3,
        frames_failed=0,
    )
    assert health["healthy"] is False
    assert health["transparent_frames_ok"] is False


def test_interior_transparent_hole_cannot_fake_a_matted_border(tmp_path):
    from PIL import Image

    frames = tmp_path / "hole-frames"
    frames.mkdir()
    for index in (1, 2, 3):
        image = Image.new("RGBA", (100, 100), (20, 40, 60, 255))
        for x in range(38, 63):
            for y in range(38, 63):
                image.putpixel((x, y), (0, 0, 0, 0))
        image.save(frames / f"{index:04d}.png")
    health = rmbg2_video.assess_matte_health(
        frames,
        frames_total=3,
        frames_failed=0,
    )
    assert health["transparent_frames_ok"] is True
    assert health["transparent_borders_ok"] is False
    assert health["healthy"] is False


def test_validation_failure_does_not_regenerate_by_default(tmp_path, monkeypatch):
    generation_calls = 0

    def fake_gen(config, *, prompt, output, **kw):
        nonlocal generation_calls
        generation_calls += 1
        Path(output).parent.mkdir(parents=True, exist_ok=True)
        Path(output).write_bytes(b"fakevideo")

    def fake_matte(config, *, input_video, output_dir, **kw):
        return _write_matte(Path(output_dir))

    def fake_validate(config, *, video_path, rubric_override, **kw):
        ids = [rubric["id"] for rubric in rubric_override]
        return _verdict({rubric_id: "FAIL" for rubric_id in ids})

    monkeypatch.setattr(seedance, "generate_video", fake_gen)
    monkeypatch.setattr(rmbg2_video, "matte_video", fake_matte)
    monkeypatch.setattr(avv, "validate_asset_video", fake_validate)

    out = pvv.produce_validated_video(
        None,
        base_prompt="walk",
        first_frame=str(tmp_path / "ff.png"),
        output_dir=tmp_path / "out",
        action_description="knight walks",
    )

    assert generation_calls == 1
    assert out["n_candidates"] == 1
    assert out["n_attempts"] == 1


def test_unhealthy_matte_retries_and_never_publishes_first_take(
    tmp_path, monkeypatch
):
    calls = {"matte": 0}

    def fake_gen(config, *, prompt, output, **kw):
        Path(output).parent.mkdir(parents=True, exist_ok=True)
        Path(output).write_bytes(b"fakevideo")

    def fake_matte(config, *, input_video, output_dir, **kw):
        calls["matte"] += 1
        if calls["matte"] == 1:
            return _write_matte_all_transparent(Path(output_dir))
        return _write_matte(Path(output_dir))

    def fake_validate(config, *, video_path, rubric_override, **kw):
        ids = [rubric["id"] for rubric in rubric_override]
        return _verdict({rubric_id: "PASS" for rubric_id in ids})

    monkeypatch.setattr(seedance, "generate_video", fake_gen)
    monkeypatch.setattr(rmbg2_video, "matte_video", fake_matte)
    monkeypatch.setattr(avv, "validate_asset_video", fake_validate)
    out = pvv.produce_validated_video(
        None,
        base_prompt="walk",
        first_frame=str(tmp_path / "ff.png"),
        output_dir=tmp_path / "out",
        action_description="knight walks",
        n_candidates=2,
    )
    assert out["passed"] is True
    assert out["usable"] is True
    assert out["winner_candidate"] == 2
    assert out["n_attempts"] == 2


def test_content_pass_never_falls_back_to_raw(tmp_path, monkeypatch):
    # When no preview is available, the content pass must be judged on a preview
    # REBUILT from the matted frames — never on the raw mp4. Assert the content
    # pass input is a matted (rebuilt) source, not the raw video.
    seen_inputs: list[str] = []

    def fake_gen(config, *, prompt, output, **kw):
        Path(output).parent.mkdir(parents=True, exist_ok=True)
        Path(output).write_bytes(b"fakevideo")

    def fake_matte(config, *, input_video, output_dir, **kw):
        return _write_matte_gif_softfail(Path(output_dir))

    def fake_validate(config, *, video_path, rubric_override, **kw):
        seen_inputs.append(str(video_path))
        ids = [r["id"] for r in rubric_override]
        return _verdict({rid: "PASS" for rid in ids})

    monkeypatch.setattr(seedance, "generate_video", fake_gen)
    monkeypatch.setattr(rmbg2_video, "matte_video", fake_matte)
    monkeypatch.setattr(avv, "validate_asset_video", fake_validate)

    out = pvv.produce_validated_video(
        None,
        base_prompt="walk",
        first_frame=str(tmp_path / "ff.png"),
        output_dir=tmp_path / "out",
        action_description="knight walks",
        n_candidates=1,
    )
    # framing pass sees the raw video.mp4; content pass must NOT — it sees the
    # rebuilt-from-frames preview.
    content_inputs = [p for p in seen_inputs if "video.mp4" not in p]
    assert content_inputs, seen_inputs
    assert all("preview_from_frames" in p for p in content_inputs), seen_inputs
    assert out["passed"] is True


def test_custom_override_is_judged_on_matted_input(tmp_path, monkeypatch):
    seen_inputs: list[str] = []

    def fake_gen(config, *, prompt, output, **kw):
        Path(output).parent.mkdir(parents=True, exist_ok=True)
        Path(output).write_bytes(b"fakevideo")

    def fake_matte(config, *, input_video, output_dir, **kw):
        return _write_matte(Path(output_dir))

    def fake_validate(config, *, video_path, rubric_override, **kw):
        seen_inputs.append(str(video_path))
        ids = [rubric["id"] for rubric in rubric_override]
        return _verdict({rubric_id: "PASS" for rubric_id in ids})

    monkeypatch.setattr(seedance, "generate_video", fake_gen)
    monkeypatch.setattr(rmbg2_video, "matte_video", fake_matte)
    monkeypatch.setattr(avv, "validate_asset_video", fake_validate)
    out = pvv.produce_validated_video(
        None,
        base_prompt="walk",
        first_frame=str(tmp_path / "ff.png"),
        output_dir=tmp_path / "out",
        action_description="knight walks",
        rubric_override=[
            {"id": "custom", "name": "Custom", "description": "Judge matte"}
        ],
        n_candidates=1,
    )
    assert out["passed"] is True
    assert len(seen_inputs) == 1
    assert "preview_from_frames" in seen_inputs[0]


def test_extra_rubrics_run_once_on_matted_content(tmp_path, monkeypatch):
    seen: list[tuple[str, list[str]]] = []

    def fake_gen(config, *, prompt, output, **kw):
        Path(output).parent.mkdir(parents=True, exist_ok=True)
        Path(output).write_bytes(b"fakevideo")

    def fake_matte(config, *, input_video, output_dir, **kw):
        return _write_matte(Path(output_dir))

    def fake_validate(
        config, *, video_path, rubric_override, extra_rubrics=None, **kw
    ):
        ids = [rubric["id"] for rubric in rubric_override]
        ids += [rubric["id"] for rubric in (extra_rubrics or [])]
        seen.append((str(video_path), ids))
        return _verdict({rubric_id: "PASS" for rubric_id in ids})

    monkeypatch.setattr(seedance, "generate_video", fake_gen)
    monkeypatch.setattr(rmbg2_video, "matte_video", fake_matte)
    monkeypatch.setattr(avv, "validate_asset_video", fake_validate)
    out = pvv.produce_validated_video(
        None,
        base_prompt="walk",
        first_frame=str(tmp_path / "ff.png"),
        output_dir=tmp_path / "out",
        action_description="knight walks",
        extra_rubrics=[
            {"id": "EX1", "name": "Extra", "description": "extra content"}
        ],
        n_candidates=1,
    )
    assert out["passed"] is True
    assert "EX1" not in seen[0][1]
    assert "video.mp4" not in seen[1][0]
    assert seen[1][1].count("EX1") == 1
