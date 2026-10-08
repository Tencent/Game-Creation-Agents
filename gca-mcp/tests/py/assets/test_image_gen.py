"""Regression tests for image_gen prompt framing (IMG-01) and sizing (IMG-02)."""

from __future__ import annotations

import copy
import json
import random
from pathlib import Path
from types import SimpleNamespace

import pytest

from gca_mcp.adapters import image_gen as ig
from gca_mcp.config import Config, load_runtime_config
from gca_mcp.errors import ToolError


def _has_foot(p: str) -> bool:
    return "MUST touch the bottom edge of the canvas" in p


def _has_margin(p: str) -> bool:
    return "CENTERED WITH MARGIN" in p


# -------------------- IMG-01: mutually-exclusive framing --------------------


def test_grounded_sprite_uses_grounded_framing_only():
    p = ig.wrap_prompt_with_safe_bg(
        "a knight", safe_bg_color="#FF00FF", framing_profile="grounded_sprite"
    )
    assert _has_foot(p) and not _has_margin(p)


def test_roleless_safebg_is_centered_not_grounded():
    p = ig.wrap_prompt_with_safe_bg("GAME OVER lettering", safe_bg_color="#FF00FF")
    assert _has_margin(p) and not _has_foot(p)
    assert "preserve that requested text exactly" in p


def test_centered_asset_uses_margin_only():
    p = ig.wrap_prompt_with_safe_bg("an icon", safe_bg_color=None, subject_margin=True)
    assert _has_margin(p) and not _has_foot(p)


def test_full_scene_uses_neither():
    p = ig.wrap_prompt_with_safe_bg(
        "a map", safe_bg_color=None, subject_margin=False, flat_2d=True
    )
    assert not _has_foot(p) and not _has_margin(p)


def test_framing_blocks_never_co_occur():
    for sbg in (None, "#FF00FF", "#00F000"):
        for profile in (None, "grounded_sprite", "centered_asset", "full_scene"):
            for sm in (True, False):
                p = ig.wrap_prompt_with_safe_bg(
                    "x",
                    safe_bg_color=sbg,
                    framing_profile=profile,
                    subject_margin=sm,
                )
                assert not (_has_foot(p) and _has_margin(p)), (sbg, profile, sm)


def test_safe_bg_avoidance_only_forbids_selected_key():
    magenta = ig.wrap_prompt_with_safe_bg(
        "green slime", safe_bg_color="#FF00FF", asset_type="prop"
    )
    green = ig.wrap_prompt_with_safe_bg(
        "pink crystal", safe_bg_color="#00F000", asset_type="prop"
    )
    assert "selected matting key #FF00FF" in magenta
    assert "requested green foreground" in magenta
    assert "selected matting key #00F000" in green
    assert "requested pink/magenta foreground" in green


# -------------------- IMG-02: normalize_size legality --------------------


def test_resolve_size_uses_runtime_config_authority():
    config = Config.from_env(config_path=Path(__file__).parents[4] / "gca.config.yaml")
    image = config.assets.image

    assert (
        ig.resolve_size(
            "auto",
            asset_type="ui",
            size_by_asset_type=image.size_by_asset_type,
            size_by_aspect=image.size_by_aspect,
        )
        == "816x816"
    )
    assert (
        ig.resolve_size(
            "auto",
            aspect="landscape",
            size_by_asset_type=image.size_by_asset_type,
            size_by_aspect=image.size_by_aspect,
        )
        == "1536x864"
    )
    assert ig.resolve_size("640x640", asset_type="ui") == "640x640"
    assert ig.resolve_size("auto", asset_type="unknown") == "auto"
    assert (
        ig.resolve_size(
            "auto",
            asset_type="unknown",
            size_by_asset_type=image.size_by_asset_type,
        )
        == "auto"
    )
    assert ig.resolve_size("auto", aspect="cinematic") == "auto"
    assert (
        ig.resolve_size(
            "auto",
            aspect="cinematic",
            size_by_aspect=image.size_by_aspect,
        )
        == "auto"
    )


def _write_runtime_config(tmp_path, mutate):
    source = load_runtime_config(Path(__file__).parents[4] / "gca.config.yaml")
    payload = copy.deepcopy(source)
    mutate(payload)
    path = tmp_path / "gca.config.json"
    path.write_text(json.dumps(payload))
    return path


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (
            lambda cfg: cfg["gates"]["ui_scale"].__setitem__("layout_check", "false"),
            "layout_check must be a boolean",
        ),
        (
            lambda cfg: cfg["gates"]["ui_scale"]["max_canvas_fraction"].__setitem__(
                "hud", 55
            ),
            "max_canvas_fraction.hud",
        ),
        (
            lambda cfg: cfg["gates"]["ui_scale"].pop("max_texture_px"),
            "max_texture_px is required",
        ),
    ],
)
def test_runtime_config_rejects_invalid_ui_scale(tmp_path, mutate, message):
    path = _write_runtime_config(tmp_path, mutate)

    with pytest.raises(ValueError, match=message):
        Config.from_env(config_path=path)


def test_runtime_config_accepts_zero_aspect_and_overlap_tolerance(tmp_path):
    def mutate(cfg):
        cfg["gates"]["ui_scale"]["aspect_tolerance"] = 0
        cfg["gates"]["ui_scale"]["overlap_min_px"] = 0

    config = Config.from_env(config_path=_write_runtime_config(tmp_path, mutate))

    assert config.gates.ui_scale.aspect_tolerance == 0
    assert config.gates.ui_scale.overlap_min_px == 0


def test_configured_image_size_must_be_provider_legal(tmp_path):
    path = _write_runtime_config(
        tmp_path,
        lambda cfg: cfg["assets"]["image"]["size_by_asset_type"].__setitem__(
            "ui", "typo"
        ),
    )
    with pytest.raises(ValueError, match="must be a WxH size"):
        Config.from_env(config_path=path)


def test_normalize_size_review_examples_are_legal():
    for ex in (
        "1x1548",
        "13269x9939",
        "10820x15472",
        "10362x6701",
        "960x540",
        "375x667",
    ):
        out = ig.normalize_size(ex)
        assert ig.is_valid_gpt_image_2_size(out), (ex, out)


def test_normalize_size_unparseable_falls_back_to_legal():
    for ex in ("", "x", "abc", "0x0", "-5x10", "autox"):
        out = ig.normalize_size(ex)
        assert ig.is_valid_gpt_image_2_size(out)


def test_normalize_size_property_random_inputs_all_legal():
    random.seed(0)
    for _ in range(2000):
        w = random.randint(1, 20000)
        h = random.randint(1, 20000)
        out = ig.normalize_size(f"{w}x{h}")
        assert ig.is_valid_gpt_image_2_size(out), (w, h, out)


# -------------------- IMG-01 follow-up: role-based framing profile --------------------
# The framing block must be chosen by asset ROLE (asset_type / explicit
# framing_profile), NOT by whether safe_bg_color is set. produce_static_asset
# auto-fills a safe bg for every matted prop/UI/icon; under the old rule that
# handed them the grounded-CHARACTER block ("single full-body character only",
# "no UI"). These lock the correct mapping.


def test_ui_and_prop_on_safe_bg_get_centered_not_grounded():
    # A matted UI/prop with an auto-filled safe bg must be centered_asset,
    # never the grounded-character foot-anchor block.
    for at in ("ui", "button", "icon", "hud", "prop", "item", "tileset"):
        p = ig.wrap_prompt_with_safe_bg("x", safe_bg_color="#FF00FF", asset_type=at)
        assert _has_margin(p) and not _has_foot(p), at


def test_character_types_on_safe_bg_get_grounded():
    for at in ("character", "sprite", "player", "enemy", "boss", "creature"):
        p = ig.wrap_prompt_with_safe_bg("x", safe_bg_color="#FF00FF", asset_type=at)
        assert _has_foot(p) and not _has_margin(p), at


def test_opaque_scene_types_get_no_framing_block():
    for at in ("background", "map", "parallax"):
        p = ig.wrap_prompt_with_safe_bg("x", asset_type=at)
        assert not _has_foot(p) and not _has_margin(p), at


def test_every_quality_parameter_states_its_default_and_its_cost():
    # Render effort dominates the wall-clock of an asset run: one 1024x1024 image
    # takes 25s at low and 115s at high, across dozens of assets with retries.
    # produce_static_asset shipped a bare enum with neither a default nor a
    # described cost, and agents duly reached for "high" on flat vector art.
    from gca_mcp.tools import asset_tools

    bare = []
    for tool in asset_tools.TOOLS:
        properties = (tool.input_schema or {}).get("properties", {})
        for name, spec in properties.items():
            if "quality" not in name or not isinstance(spec, dict):
                continue
            if spec.get("enum") != ["low", "medium", "high"]:
                continue
            if spec.get("default") != "low" or not spec.get("description"):
                bare.append(f"{tool.name}.{name}")

    assert not bare, f"quality parameters advertising no default or cost: {bare}"


def test_parallax_is_full_scene_but_not_opaque():
    # A parallax layer fills the canvas like a background, yet composites above
    # the sky, so it needs alpha over its content line. Conflating the two left
    # a transparent parallax band impossible to produce: the type forced
    # skip_matting, and any other asset_type broke the manifest assetType parity
    # check the finalizer enforces.
    assert "parallax" in ig.FULL_SCENE_ASSET_TYPES
    assert "parallax" not in ig.OPAQUE_SCENE_ASSET_TYPES
    assert ig.default_framing_profile("parallax") == "full_scene"
    # A chroma plate is what makes the band mattable at all.
    assert ig.default_safe_bg_color("parallax", "#FF00FF") == "#FF00FF"


def test_background_and_map_stay_opaque():
    for at in ("background", "map"):
        assert at in ig.OPAQUE_SCENE_ASSET_TYPES, at
        assert ig.default_framing_profile(at) == "full_scene", at
        assert ig.default_safe_bg_color(at, "#FF00FF") is None, at


def test_explicit_framing_profile_overrides_asset_type():
    # A caller correcting a mis-classified asset: force centered on a character.
    p = ig.wrap_prompt_with_safe_bg(
        "x",
        safe_bg_color="#FF00FF",
        asset_type="character",
        framing_profile="centered_asset",
    )
    assert _has_margin(p) and not _has_foot(p)


def test_invalid_framing_profile_raises():
    with pytest.raises(ToolError):
        ig.wrap_prompt_with_safe_bg("x", framing_profile="bogus")


def test_default_framing_profile_mapping():
    assert ig.default_framing_profile("character") == "grounded_sprite"
    assert ig.default_framing_profile("ui") == "centered_asset"
    assert ig.default_framing_profile("prop") == "centered_asset"
    assert ig.default_framing_profile("background") == "full_scene"
    assert ig.default_framing_profile(None) == "centered_asset"
    assert ig.default_framing_profile("") == "centered_asset"


# -------------------- IMG-02 follow-up: bounded area (no ceiling drift) --------------------
# The old ratio-first lexicographic sort drifted ordinary mobile canvases to
# the ~6-7 MP ceiling. normalize_size must now pick the smallest sufficient
# legal size within the aspect tolerance.


def test_mobile_canvases_not_inflated_to_ceiling():
    # These used to blow up to 6-7 MP; they should land near the ~655k floor.
    for ex in ("375x667", "960x540", "414x896", "360x640"):
        out = ig.normalize_size(ex)
        w, h = (int(v) for v in out.split("x"))
        assert ig.is_valid_gpt_image_2_size(out), (ex, out)
        assert w * h < 2_000_000, (ex, out, w * h)


def test_normalize_size_preserves_aspect_within_tolerance():
    import math

    for ex in ("960x540", "375x667", "1920x1080", "768x1024"):
        w0, h0 = (int(v) for v in ex.split("x"))
        out = ig.normalize_size(ex)
        w, h = (int(v) for v in out.split("x"))
        # aspect error stays within the tolerance band (or the 3:1 clamp)
        err = abs(math.log((w / h) / (w0 / h0)))
        assert err <= ig.NORMALIZE_ASPECT_LOG_TOL + 1e-6, (ex, out, err)


def test_normalize_size_area_inflation_bounded():
    # No mobile canvas should be inflated more than the documented factor over
    # the pixel floor.
    for ex in ("375x667", "960x540"):
        w, h = (int(v) for v in ig.normalize_size(ex).split("x"))
        assert w * h <= ig.GPT_IMAGE_2_MIN_PIXELS * ig.NORMALIZE_MAX_AREA_INFLATION


# -------------------- PR #123 review #4: fidelity / symmetry / continuity --------------------
# The review flagged three concrete regressions in the previous revision:
#   1. near-legal high-res requests collapsed to ~1.3 MP (3000x2000 -> 1392x928)
#      while a one-pixel-different 3008x2000 passed through at ~6 MP;
#   2. >3:1 portrait/landscape inputs were not transpose-symmetric (1x1548 vs
#      1548x1 gave unrelated shapes);
#   3. the portrait result was ~7× the minimum legal area.


def test_normalize_size_near_legal_highres_preserves_resolution():
    # A near-legal ~6 MP request must NOT collapse to ~1.3 MP — it should land
    # within ~1.5× of its own (legal-clamped) requested area, like its already-
    # legal neighbour does.
    for ex in ("3000x2000", "2801x2800"):
        w0, h0 = (int(v) for v in ex.split("x"))
        req_area = min(ig.GPT_IMAGE_2_MAX_PIXELS, w0 * h0)
        w, h = (int(v) for v in ig.normalize_size(ex).split("x"))
        assert ig.is_valid_gpt_image_2_size(f"{w}x{h}"), ex
        assert w * h >= req_area * 0.6, (ex, w * h, req_area)


def test_normalize_size_near_legal_continuity():
    # A one-grid-step difference must not cause a large area discontinuity: the
    # normalized near-legal input and its already-legal neighbour should have
    # areas within ~1.5× of each other.
    pairs = [("3000x2000", "3008x2000"), ("2801x2800", "2800x2800")]
    for a, b in pairs:
        wa, ha = (int(v) for v in ig.normalize_size(a).split("x"))
        wb, hb = (int(v) for v in ig.normalize_size(b).split("x"))
        ratio = (wa * ha) / (wb * hb)
        assert 0.67 <= ratio <= 1.5, (a, b, wa * ha, wb * hb)


def test_normalize_size_transpose_symmetry():
    # normalize_size("WxH") transposed must equal normalize_size("HxW"): the
    # algorithm treats width and height symmetrically (incl. the >3:1 clamp).
    for w, h in [(1, 1548), (1920, 1080), (375, 667), (3000, 2000), (500, 2500)]:
        a = ig.normalize_size(f"{w}x{h}")
        b = ig.normalize_size(f"{h}x{w}")
        aw, ah = (int(v) for v in a.split("x"))
        bw, bh = (int(v) for v in b.split("x"))
        assert (aw, ah) == (bh, bw), (w, h, a, b)


def test_normalize_size_extreme_portrait_not_over_inflated():
    # An impossible >3:1 portrait (1x1548) must clamp to a legal 1:3 shape near
    # the floor — NOT ~7× the minimum legal area as the old width-only sweep did.
    for ex in ("1x1548", "1548x1"):
        w, h = (int(v) for v in ig.normalize_size(ex).split("x"))
        assert ig.is_valid_gpt_image_2_size(f"{w}x{h}"), ex
        assert w * h <= ig.GPT_IMAGE_2_MIN_PIXELS * ig.NORMALIZE_MAX_AREA_INFLATION, (
            ex,
            w * h,
        )


def test_normalize_size_area_monotonic_in_request():
    # Larger requested area (same aspect) never yields a smaller normalized area.
    prev = 0
    for scale in (1.0, 1.5, 2.0, 3.0, 4.0):
        w, h = int(400 * scale), int(300 * scale)
        nw, nh = (int(v) for v in ig.normalize_size(f"{w}x{h}").split("x"))
        area = nw * nh
        assert area >= prev - 1, (w, h, area, prev)
        prev = area


def test_normalize_size_preserves_legal_three_to_one_at_edge_ceiling():
    assert ig.normalize_size("6000x2000") == "3840x1280"
    assert ig.normalize_size("2000x6000") == "1280x3840"


def test_edit_image_normalizes_illegal_explicit_size(tmp_path, monkeypatch):
    source = tmp_path / "source.png"
    source.write_bytes(b"png")
    captured = {}

    class _FakeProvider:
        def image_output_size(self, config, size):
            return size

        def edit_image(self, config, *, prompt, size, quality, input_path):
            captured["size"] = size
            return {"bytes": b"PNGDATA", "request_id": "req-1"}

    # Transport is owned by the channel now; patch it so the adapter's size
    # normalization + delegation is asserted independent of any wire format.
    monkeypatch.setattr(ig, "get_provider", lambda config=None: _FakeProvider())
    config = SimpleNamespace(
        assets=SimpleNamespace(image=SimpleNamespace(request_timeout_seconds=30)),
    )
    out = Path(tmp_path / "out.png")
    result = ig.edit_image(
        config,
        prompt="edit",
        input_path=str(source),
        output=out,
        size="960x540",
        enforce_safe_bg_avoidance=False,
        subject_margin=False,
    )
    assert ig.is_valid_gpt_image_2_size(captured["size"])
    assert result["size"] == captured["size"]
    assert out.read_bytes() == b"PNGDATA"
