"""Regression tests for produce_validated_first_frame (FF-01, FF-02, FF-03)."""

from __future__ import annotations

from pathlib import Path

from gca_mcp.adapters import asset_image_validator, image_gen
from gca_mcp.adapters import produce_validated_first_frame as pvff
from gca_mcp.errors import ToolError


def _write_png(path: Path) -> None:
    from PIL import Image

    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (64, 64), (10, 20, 30)).save(path)


def _verdict(rubric_verdicts: dict[str, str], overall: str | None = None):
    """Build a validator-style verdict dict. ``rubric_verdicts`` maps id→PASS/FAIL/None."""
    results = [
        {"id": rid, "name": rid, "verdict": v, "evidence": None}
        for rid, v in rubric_verdicts.items()
    ]
    failed = [r for r in results if r["verdict"] == "FAIL"]
    passed = [r for r in results if r["verdict"] == "PASS"]
    unknown = [r for r in results if r["verdict"] is None]
    if overall is None:
        overall = "PASS" if not failed and not unknown else "FAIL"
    return {
        "verdict": overall,
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


# -------------------- FF-03: strict pass gate --------------------


def test_strict_passed_requires_all_pass_no_unknown():
    assert pvff._strict_passed(_verdict({"I1": "PASS", "I2": "PASS"}))
    # UNKNOWN blocks even if validator says overall PASS
    assert not pvff._strict_passed(
        _verdict({"I1": "PASS", "I2": None}, overall="PASS")
    )
    # a FAIL blocks
    assert not pvff._strict_passed(_verdict({"I1": "PASS", "I2": "FAIL"}))
    # empty blocks
    assert not pvff._strict_passed(_verdict({}))
    # duplicate ids block
    v = _verdict({"I1": "PASS"})
    v["rubric_results"].append({"id": "I1", "name": "I1", "verdict": "PASS", "evidence": None})
    assert not pvff._strict_passed(v)
    # Parser-level duplicate metadata must also block after results are folded.
    v = _verdict({"I1": "PASS"})
    v["duplicate_ids"] = ["I1"]
    assert not pvff._strict_passed(v)


def test_unknown_rubric_does_not_ship(tmp_path, monkeypatch):
    def fake_gen(config, *, prompt, output, **kw):
        _write_png(Path(output))
        return {"output": str(output)}

    def fake_validate(config, **kw):
        return _verdict({"I1": "PASS", "I2": None}, overall="PASS")

    monkeypatch.setattr(image_gen, "generate_image", fake_gen)
    monkeypatch.setattr(asset_image_validator, "validate_image", fake_validate)

    out = pvff.produce_validated_first_frame(
        None,
        image_prompt="a knight",
        character_description="a knight in armor",
        output_dir=tmp_path / "run",
        max_attempts=2,
    )
    assert out["passed"] is False


def test_passing_final_records_hash_bound_provenance(tmp_path, monkeypatch):
    rubrics = [
        {"id": "I1", "name": "I1", "description": ""},
        {"id": "I2", "name": "I2", "description": ""},
    ]

    def fake_gen(config, *, prompt, output, **kw):
        _write_png(Path(output))
        return {"output": str(output)}

    def fake_validate(config, **kw):
        return _verdict({"I1": "PASS", "I2": "PASS"})

    monkeypatch.setattr(image_gen, "generate_image", fake_gen)
    monkeypatch.setattr(asset_image_validator, "validate_image", fake_validate)
    out = pvff.produce_validated_first_frame(
        None,
        image_prompt="a knight",
        character_description="a knight in armor",
        output_dir=tmp_path / "run",
        rubric_override=rubrics,
        max_attempts=1,
    )
    image = Path(out["final_image"])
    assert out["passed"] is True
    assert pvff.has_validated_provenance(
        image,
        expected_rubrics=rubrics,
        character_description="a knight in armor",
        framing_profile="grounded_sprite",
    )
    assert not pvff.has_validated_provenance(
        image,
        expected_rubrics=[
            {"id": "P1", "name": "P1", "description": ""},
            {"id": "P2", "name": "P2", "description": ""},
        ],
        character_description="a knight in armor",
        framing_profile="centered_asset",
    )
    assert not pvff.has_validated_provenance(
        image,
        expected_rubrics=[
            {"id": "I1", "name": "I1", "description": "changed semantics"},
            {"id": "I2", "name": "I2", "description": ""},
        ],
        character_description="a knight in armor",
        framing_profile="grounded_sprite",
    )
    image.write_bytes(image.read_bytes() + b"tampered")
    assert not pvff.has_validated_provenance(
        image,
        expected_rubrics=rubrics,
        character_description="a knight in armor",
        framing_profile="grounded_sprite",
    )


# -------------------- FF-01: no stale final on failure --------------------


def test_stale_final_not_reported_when_run_fails(tmp_path, monkeypatch):
    out_dir = tmp_path / "run"
    # Pre-seed a stale final/ from a "previous run".
    stale = out_dir / "final"
    _write_png(stale / "image.png")

    def fail_gen(config, *, prompt, output, **kw):
        raise ToolError("IMAGE_GEN_FAILED", "boom")

    monkeypatch.setattr(image_gen, "generate_image", fail_gen)

    out = pvff.produce_validated_first_frame(
        None,
        image_prompt="a knight",
        character_description="a knight in armor",
        output_dir=out_dir,
        max_attempts=2,
    )
    assert out["passed"] is False
    assert out["final_image"] is None
    assert out["final_dir"] is None
    assert not (out_dir / "final" / "image.png").exists()


# -------------------- FF-02: keep prior usable image --------------------


def test_fallback_keeps_prior_image_over_infra_error(tmp_path, monkeypatch):
    out_dir = tmp_path / "run"
    calls = {"n": 0}

    def gen(config, *, prompt, output, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            _write_png(Path(output))  # attempt 1 produces an image
            return {"output": str(output)}
        raise ToolError("NETWORK_ERROR", "transient")  # attempt 2 infra error

    def validate(config, **kw):
        return _verdict({"I1": "FAIL", "I2": "PASS"})  # attempt 1 FAILs

    monkeypatch.setattr(image_gen, "generate_image", gen)
    monkeypatch.setattr(asset_image_validator, "validate_image", validate)

    out = pvff.produce_validated_first_frame(
        None,
        image_prompt="a knight",
        character_description="a knight in armor",
        output_dir=out_dir,
        max_attempts=2,
    )
    assert out["passed"] is False  # never cleanly passed
    # but attempt 1's image is preserved as the best-effort final
    assert out["selected_attempt"] == 1
    assert out["final_image"] is not None
    assert (out_dir / "final" / "image.png").exists()


def test_fallback_prefers_judged_image_over_later_unjudged_image(
    tmp_path, monkeypatch
):
    calls = {"validate": 0}

    def gen(config, *, prompt, output, **kw):
        _write_png(Path(output))
        return {"output": str(output)}

    def validate(config, **kw):
        calls["validate"] += 1
        if calls["validate"] == 1:
            return _verdict({"I1": "FAIL", "I2": "PASS"})
        raise ToolError("VLM_ERROR", "down")

    monkeypatch.setattr(image_gen, "generate_image", gen)
    monkeypatch.setattr(asset_image_validator, "validate_image", validate)
    out = pvff.produce_validated_first_frame(
        None,
        image_prompt="a knight",
        character_description="a knight in armor",
        output_dir=tmp_path / "run",
        max_attempts=2,
    )
    assert out["selected_attempt"] == 1
    assert out["final_verdict"] == "FAIL"


def test_fallback_prefers_complete_fail_over_later_unknown(
    tmp_path, monkeypatch
):
    calls = {"validate": 0}

    def gen(config, *, prompt, output, **kw):
        _write_png(Path(output))
        return {"output": str(output)}

    def validate(config, **kw):
        calls["validate"] += 1
        if calls["validate"] == 1:
            return _verdict({"I1": "FAIL", "I2": "PASS"})
        return _verdict({"I1": "PASS", "I2": None}, overall="FAIL")

    monkeypatch.setattr(image_gen, "generate_image", gen)
    monkeypatch.setattr(asset_image_validator, "validate_image", validate)
    out = pvff.produce_validated_first_frame(
        None,
        image_prompt="a knight",
        character_description="a knight in armor",
        output_dir=tmp_path / "run",
        max_attempts=2,
    )
    assert out["selected_attempt"] == 1


# -------------------- FF (stale attempt): reused dir must not leak old verdict --------------------


def test_reused_attempt_dir_does_not_publish_stale_verdict(tmp_path, monkeypatch):
    import json

    out_dir = tmp_path / "run"
    # Pre-seed attempt_01/ from a "previous run" with an OLD PASS verdict.
    a1 = out_dir / "attempt_01"
    a1.mkdir(parents=True)
    (a1 / "verdict.json").write_text(json.dumps({"passed": True, "run": "old"}))
    _write_png(a1 / "image.png")

    # This run: attempt 1 writes a FRESH image, then the validator errors before
    # a new verdict is written. Fallback selects attempt 1's new image; the OLD
    # verdict must NOT be copied to final/.
    def gen(config, *, prompt, output, **kw):
        _write_png(Path(output))
        return {"output": str(output)}

    def boom(config, **kw):
        raise ToolError("VLM_ERROR", "down")

    monkeypatch.setattr(image_gen, "generate_image", gen)
    monkeypatch.setattr(asset_image_validator, "validate_image", boom)

    out = pvff.produce_validated_first_frame(
        None,
        image_prompt="a knight",
        character_description="a knight in armor",
        output_dir=out_dir,
        max_attempts=1,
    )
    assert out["passed"] is False
    assert out["final_verdict"] is None
    # No stale verdict.json survives in the attempt dir or in final/.
    assert not (out_dir / "attempt_01" / "verdict.json").exists()
    assert not (out_dir / "final" / "verdict.json").exists()
