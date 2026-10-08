"""Regression test for produce_validated_sprite_action override pass (SA-01)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from PIL import Image

from gca_mcp.adapters import asset_image_validator
from gca_mcp.adapters import produce_validated_first_frame as pvff
from gca_mcp.adapters import produce_validated_sprite_action as pvsa
from gca_mcp.adapters import produce_validated_video as video_orch


def _make_image(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (64, 64), (255, 0, 255)).save(path)


def _pass_override_validation(monkeypatch) -> None:
    def fake_validate(config, *, rubric_override=None, **kw):
        rubrics = (
            rubric_override
            or asset_image_validator.rubric_dicts(
                asset_image_validator.DEFAULT_IMAGE_RUBRICS
            )
        )
        results = [
            {
                "id": rubric["id"],
                "name": rubric["name"],
                "verdict": "PASS",
                "evidence": None,
            }
            for rubric in rubrics
        ]
        return {
            "verdict": "PASS",
            "passed": True,
            "rubric_results": results,
            "failures": [],
            "n_passed": len(results),
            "n_failed": 0,
            "n_unknown": 0,
            "duplicate_ids": [],
            "unexpected_ids": [],
            "raw_response": "",
        }

    monkeypatch.setattr(asset_image_validator, "validate_image", fake_validate)


def test_override_plus_video_pass_yields_overall_passed(tmp_path, monkeypatch):
    override = tmp_path / "shared-ref.png"
    _make_image(override)

    def fake_video(config, **kw):
        return {
            "passed": True,
            "usable": True,
            "winner_candidate": 1,
            "final_video": str(tmp_path / "v.mp4"),
            "final_transparent_gif": str(tmp_path / "t.gif"),
            "final_dir": str(tmp_path / "final"),
        }

    monkeypatch.setattr(video_orch, "produce_validated_video", fake_video)
    _pass_override_validation(monkeypatch)

    out = pvsa.produce_validated_sprite_action(
        None,
        image_prompt="",
        character_description="",
        video_user_content="knight walks",
        output_dir=tmp_path / "run",
        first_frame_override=str(override),
    )
    assert out["image_stage"] == "override_validated"
    assert out["image_passed"] is True
    assert out["video_passed"] is True
    assert out["passed"] is True
    assert out["image_content_validated"] is True


def test_hash_bound_provenance_skips_duplicate_validation(tmp_path, monkeypatch):
    override = tmp_path / "final" / "image.png"
    _make_image(override)
    rubric_ids = [
        rubric.id for rubric in asset_image_validator.DEFAULT_IMAGE_RUBRICS
    ]
    rubric_contract = asset_image_validator.rubric_dicts(
        asset_image_validator.DEFAULT_IMAGE_RUBRICS
    )
    verdict = {
        "passed": True,
        "rubric_results": [
            {"id": rubric_id, "name": rubric_id, "verdict": "PASS"}
            for rubric_id in rubric_ids
        ],
        "n_unknown": 0,
        "duplicate_ids": [],
        "unexpected_ids": [],
        "image_sha256": pvff._sha256(override),
        "validation_contract": {
            "rubric_ids": rubric_ids,
            "rubric_contract_sha256": pvff._rubric_contract_sha256(
                rubric_contract
            ),
            "character_description_sha256": pvff._text_sha256(
                "knight walks"
            ),
            "framing_profile": "grounded_sprite",
        },
    }
    (override.parent / "verdict.json").write_text(json.dumps(verdict))
    monkeypatch.setattr(
        asset_image_validator,
        "validate_image",
        lambda *args, **kwargs: pytest.fail("provenance should avoid revalidation"),
    )
    monkeypatch.setattr(
        video_orch,
        "produce_validated_video",
        _capture_video({}),
    )
    out = pvsa.produce_validated_sprite_action(
        None,
        image_prompt="",
        character_description="",
        video_user_content="knight walks",
        output_dir=tmp_path / "run",
        first_frame_override=str(override),
    )
    assert out["image_stage"] == "override_validated"
    assert out["image_result"]["provenance_valid"] is True


def test_override_plus_video_fail_is_not_passed(tmp_path, monkeypatch):
    override = tmp_path / "shared-ref.png"
    _make_image(override)

    def fake_video(config, **kw):
        return {"passed": False, "usable": True, "winner_candidate": 1}

    monkeypatch.setattr(video_orch, "produce_validated_video", fake_video)
    _pass_override_validation(monkeypatch)

    out = pvsa.produce_validated_sprite_action(
        None,
        image_prompt="",
        character_description="",
        video_user_content="knight walks",
        output_dir=tmp_path / "run",
        first_frame_override=str(override),
    )
    assert out["image_stage"] == "override_validated"
    assert out["passed"] is False
    assert out["best_effort"] is True


def test_failed_image_stage_with_usable_video_is_best_effort(
    tmp_path, monkeypatch
):
    image = tmp_path / "failed-image.png"
    _make_image(image)
    monkeypatch.setattr(
        pvsa.image_orch,
        "produce_validated_first_frame",
        lambda *args, **kwargs: {
            "passed": False,
            "final_image": str(image),
            "final_failures": [{"id": "I1"}],
            "n_attempts": 1,
        },
    )
    monkeypatch.setattr(
        video_orch,
        "produce_validated_video",
        _capture_video({}),
    )
    out = pvsa.produce_validated_sprite_action(
        None,
        image_prompt="knight",
        character_description="knight",
        video_user_content="knight walks",
        output_dir=tmp_path / "run",
    )
    assert out["passed"] is False
    assert out["usable"] is True
    assert out["best_effort"] is True


# -------------------- review #6: override key-color binding --------------------


def _make_green_image(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (64, 64), (0, 0xF0, 0)).save(path)


def test_override_binds_detected_green_bg(tmp_path, monkeypatch):
    # A reference created on #00F000 must drive matting/prompt at #00F000, not
    # the magenta default — otherwise the wrong color is keyed out.
    override = tmp_path / "green-ref.png"
    _make_green_image(override)
    captured: dict = {}
    monkeypatch.setattr(video_orch, "produce_validated_video", _capture_video(captured))
    _pass_override_validation(monkeypatch)

    out = pvsa.produce_validated_sprite_action(
        None,
        image_prompt="",
        character_description="",
        video_user_content="slime hops",
        output_dir=tmp_path / "run",
        first_frame_override=str(override),
    )
    assert out["image_result"]["detected_bg_hex"] == "#00F000"
    assert out["bg_hex"] == "#00F000"
    assert captured["bg_hex"] == "#00F000"


def test_override_conflicting_explicit_bg_rejected(tmp_path):
    from gca_mcp.errors import ToolError

    # Override is green, but caller forces magenta → refuse (don't key wrong bg).
    override = tmp_path / "green-ref.png"
    _make_green_image(override)
    with pytest.raises(ToolError):
        pvsa.produce_validated_sprite_action(
            None,
            image_prompt="",
            character_description="",
            video_user_content="slime hops",
            output_dir=tmp_path / "run",
            first_frame_override=str(override),
            bg_hex="#FF00FF",
        )


def test_generated_frame_conflicting_matting_bg_is_rejected(tmp_path):
    from gca_mcp.errors import ToolError

    with pytest.raises(ToolError, match="must match"):
        pvsa.produce_validated_sprite_action(
            None,
            image_prompt="knight",
            character_description="knight",
            video_user_content="knight walks",
            output_dir=tmp_path / "run",
            safe_bg_color="#FF00FF",
            bg_hex="#00F000",
        )


def test_generated_frame_actual_border_must_match_config(tmp_path, monkeypatch):
    generated = tmp_path / "generated-green.png"
    _make_green_image(generated)
    monkeypatch.setattr(
        pvsa.image_orch,
        "produce_validated_first_frame",
        lambda *args, **kwargs: {
            "passed": True,
            "final_image": str(generated),
        },
    )
    monkeypatch.setattr(
        video_orch,
        "produce_validated_video",
        lambda *args, **kwargs: pytest.fail(
            "video must not run after generated background mismatch"
        ),
    )

    out = pvsa.produce_validated_sprite_action(
        None,
        image_prompt="knight",
        character_description="knight",
        video_user_content="knight walks",
        output_dir=tmp_path / "run",
        safe_bg_color="#FF00FF",
    )
    assert out["passed"] is False
    assert out["image_stage"] == "failed"
    assert out["image_result"]["detected_bg_hex"] == "#00F000"
    assert "expected #FF00FF" in out["image_result"]["error"]


# -------------------- projectile animation profile --------------------


def _capture_video(store):
    def fake_video(config, **kw):
        store.update(kw)
        return {
            "passed": True,
            "usable": True,
            "winner_candidate": 1,
            "final_video": "v",
            "final_transparent_gif": "g",
            "final_dir": "d",
        }

    return fake_video


def test_projectile_profile_uses_projectile_contract(tmp_path, monkeypatch):
    from gca_mcp.adapters import asset_video_validator as avv

    override = tmp_path / "ref.png"
    _make_image(override)
    captured: dict = {}
    monkeypatch.setattr(video_orch, "produce_validated_video", _capture_video(captured))
    _pass_override_validation(monkeypatch)

    out = pvsa.produce_validated_sprite_action(
        None,
        image_prompt="",
        character_description="",
        video_user_content="fireball flies right",
        output_dir=tmp_path / "run",
        first_frame_override=str(override),
        animation_profile="projectile",
    )
    # projectile rubric groups forwarded to the video stage
    assert captured["framing_rubrics"] == avv.PROJECTILE_FRAMING_RUBRICS
    assert captured["content_rubrics"] == avv.PROJECTILE_CONTENT_RUBRICS
    # free-flying body: no feet-glue alignment
    assert captured["align_feet_to_bottom"] is False
    assert out["animation_profile"] == "projectile"


def test_character_profile_uses_character_defaults(tmp_path, monkeypatch):
    override = tmp_path / "ref.png"
    _make_image(override)
    captured: dict = {}
    monkeypatch.setattr(video_orch, "produce_validated_video", _capture_video(captured))
    _pass_override_validation(monkeypatch)

    pvsa.produce_validated_sprite_action(
        None,
        image_prompt="",
        character_description="",
        video_user_content="knight walks",
        output_dir=tmp_path / "run",
        first_frame_override=str(override),
    )
    # character default: no projectile rubric override, feet aligned
    assert "framing_rubrics" not in captured
    assert "content_rubrics" not in captured
    assert captured["align_feet_to_bottom"] is True
    assert "CRITICAL: NO EFFECTS" in captured["base_prompt"]


def test_explicit_align_overrides_projectile_default(tmp_path, monkeypatch):
    override = tmp_path / "ref.png"
    _make_image(override)
    captured: dict = {}
    monkeypatch.setattr(video_orch, "produce_validated_video", _capture_video(captured))
    _pass_override_validation(monkeypatch)

    pvsa.produce_validated_sprite_action(
        None,
        image_prompt="",
        character_description="",
        video_user_content="orb hovers",
        output_dir=tmp_path / "run",
        first_frame_override=str(override),
        animation_profile="projectile",
        align_feet_to_bottom=True,  # explicit override wins over profile default
    )
    assert captured["align_feet_to_bottom"] is True


def test_opaque_non_chroma_override_is_rejected(tmp_path):
    from gca_mcp.errors import ToolError

    override = tmp_path / "white.png"
    Image.new("RGB", (64, 64), "white").save(override)
    with pytest.raises(ToolError, match="opaque"):
        pvsa.produce_validated_sprite_action(
            None,
            image_prompt="",
            character_description="",
            video_user_content="orb hovers",
            output_dir=tmp_path / "run",
            first_frame_override=str(override),
        )


def test_transparent_override_is_staged_on_real_safe_bg(tmp_path, monkeypatch):
    override = tmp_path / "transparent.png"
    image = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    for x in range(20, 44):
        for y in range(20, 44):
            image.putpixel((x, y), (200, 20, 20, 255))
    image.save(override)
    captured: dict = {}
    monkeypatch.setattr(video_orch, "produce_validated_video", _capture_video(captured))
    _pass_override_validation(monkeypatch)

    out = pvsa.produce_validated_sprite_action(
        None,
        image_prompt="",
        character_description="red orb",
        video_user_content="red orb hovers",
        output_dir=tmp_path / "run",
        first_frame_override=str(override),
    )
    staged = Path(out["final_image"])
    assert staged.name == "override_staged.png"
    with Image.open(staged).convert("RGB") as result:
        assert result.getpixel((0, 0)) in ((255, 0, 255), (0, 240, 0))
    assert captured["first_frame"] == str(staged)


def test_invalid_animation_profile_raises(tmp_path):
    from gca_mcp.errors import ToolError

    override = tmp_path / "ref.png"
    _make_image(override)
    with pytest.raises(ToolError):
        pvsa.produce_validated_sprite_action(
            None,
            image_prompt="",
            character_description="",
            video_user_content="x",
            output_dir=tmp_path / "run",
            first_frame_override=str(override),
            animation_profile="bogus",
        )
