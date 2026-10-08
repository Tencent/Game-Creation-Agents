from __future__ import annotations

import hashlib
import json
from types import SimpleNamespace

import pytest
from PIL import Image

from gca_mcp.adapters import concept_ground, detect_layout
from gca_mcp.adapters.concept_ground import (
    _extract_json_array,
    _norm_box,
    crop_from_detect,
    ground_and_crop,
)
from gca_mcp.config import Config
from gca_mcp.errors import ToolError
from gca_mcp.server import ServerState, ToolCtx
from gca_mcp.tools import concept_tools
from gca_mcp.workspace import open_workspace


def _sha256(path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _open_production_context(tmp_path):
    repo = tmp_path / "repo"
    (repo / "playground").mkdir(parents=True)
    workspace = open_workspace(
        "playground/game",
        repo_root=repo,
        mode="create",
    )
    state = ServerState(
        config=Config.from_env(),
        repo_root=repo,
        workspace=workspace,
    )
    return workspace, ToolCtx(state=state)


def _concept_tool(name: str):
    return next(tool for tool in concept_tools.TOOLS if tool.name == name)


def test_concept_paths_cannot_escape_active_workspace(tmp_path) -> None:
    workspace = tmp_path / "playground/game"
    workspace.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    ctx = SimpleNamespace(
        state=SimpleNamespace(workspace=SimpleNamespace(root=workspace))
    )

    assert concept_tools._resolve_under_workspace(ctx, "design/mockup.png") == (
        workspace / "design/mockup.png"
    )
    with pytest.raises(ToolError, match="escapes the active workspace"):
        concept_tools._resolve_under_workspace(ctx, str(outside / "escape.png"))

    (workspace / "design").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ToolError, match="escapes the active workspace"):
        concept_tools._resolve_under_workspace(ctx, "design/escape.png")


def test_detect_background_key_cannot_suppress_full_mockup_crop(tmp_path) -> None:
    image_path = tmp_path / "gameplay.png"
    detect_path = tmp_path / "gameplay.detect.json"
    out_dir = tmp_path / "crops"
    Image.new("RGB", (100, 80), (10, 20, 30)).save(image_path)
    detect_path.write_text(
        json.dumps(
            {
                "source": str(image_path),
                "source_sha256": _sha256(image_path),
                "elements": [
                    {
                        "key": "background",
                        "role": "background",
                        "kind": "zone",
                        "box": [0.1, 0.1, 0.5, 0.5],
                    },
                    {
                        "key": "player",
                        "role": "player",
                        "kind": "spawn",
                        "box": [0.4, 0.4, 0.2, 0.3],
                    },
                ]
            }
        ),
        encoding="utf-8",
    )

    result = crop_from_detect(
        image_path=image_path,
        detect_path=detect_path,
        out_dir=out_dir,
        padding=0,
    )

    backgrounds = [
        crop for crop in result["crops"] if crop["label"] == "background"
    ]
    assert backgrounds == [
        {
            "label": "background",
            "key": "background",
            "crop": "background.png",
            "path": str(out_dir / "background.png"),
            "sha256": _sha256(out_dir / "background.png"),
            "box_px": [0, 0, 100, 80],
            "role": "background",
            "kind": "zone",
            "source": "full_mockup",
        }
    ]
    for crop in result["crops"]:
        assert crop["sha256"] == _sha256(out_dir / crop["crop"])
    with Image.open(out_dir / "background.png") as background:
        assert background.size == (100, 80)


def test_extract_json_array_rejects_truncated_outer_array() -> None:
    truncated = '[{"label":"health","box":[0.1,0.2,0.3,0.4]}'

    with pytest.raises(ToolError, match="outer JSON array was closed"):
        _extract_json_array(truncated)


def test_norm_box_clamps_normalized_overshoot_and_rejects_other_spaces() -> None:
    assert _norm_box([0.1, 0.2, 1.01, 0.8], 100, 80) == (10, 16, 100, 64)
    assert _norm_box([-0.01, 0.2, 0.4, 1.01], 100, 80) == (0, 16, 40, 80)
    assert _norm_box([0, 0, 2, 1], 100, 80) is None
    assert _norm_box([10, 20, 300, 400], 1024, 1024) is None
    assert _norm_box([20, 30, 280, 700], 1024, 1024) is None


def test_detect_layout_uses_dedicated_token_budget(tmp_path, monkeypatch) -> None:
    workspace = tmp_path / "playground" / "game"
    design = workspace / "design"
    design.mkdir(parents=True)
    (design / "gameplay.png").write_bytes(b"placeholder")
    captured = {}

    def fake_detect(_config, **kwargs):
        captured.update(kwargs)
        return {"n_detected": 0}

    monkeypatch.setattr(concept_tools.detect_layout, "detect_layout", fake_detect)
    ctx = SimpleNamespace(
        state=SimpleNamespace(workspace=SimpleNamespace(root=workspace)),
        config=SimpleNamespace(
            assets=SimpleNamespace(
                vlm=SimpleNamespace(
                    detect_max_tokens=2500,
                    image_timeout_seconds=90,
                )
            )
        ),
    )

    concept_tools._detect_layout({"input": "design/gameplay.png"}, ctx)

    assert captured["vlm_max_tokens"] == 2500


def test_legacy_ground_crop_matches_schema_and_honors_background(
    tmp_path, monkeypatch
) -> None:
    image_path = tmp_path / "mockup.png"
    out_dir = tmp_path / "crops"
    Image.new("RGB", (100, 80), (10, 20, 30)).save(image_path)
    monkeypatch.setattr(
        concept_ground,
        "call_vlm_on_image",
        lambda **_kwargs: (
            '[{"label":"player","role":"entity","kind":"spawn",'
            '"box":[0.1,0.2,0.4,0.8]}]'
        ),
    )

    result = ground_and_crop(
        SimpleNamespace(),
        image_path=image_path,
        out_dir=out_dir,
        padding=0,
        min_area_frac=0,
        add_background=True,
    )

    assert result["background"] is True
    assert [crop["key"] for crop in result["crops"]] == ["player", "background"]
    assert result["crops"][0]["source"] == "vlm"
    assert result["crops"][0]["role"] == "entity"
    assert result["crops"][1]["source"] == "full_mockup"
    assert result["missed"] == []
    assert result["n_grounded"] == 1
    assert result["n_rejected"] == 0
    manifest = json.loads((out_dir / "crops.json").read_text())
    assert manifest["bound_to"] is None
    assert manifest["detection_sha256"] is None
    assert manifest["source_sha256"] == _sha256(image_path)
    assert manifest["crops"] == result["crops"]
    assert manifest["waived"] == []
    for crop in result["crops"]:
        assert crop["sha256"] == _sha256(out_dir / crop["crop"])

    player_path = out_dir / "player.png"
    sealed_player_sha256 = manifest["crops"][0]["sha256"]
    player_path.write_bytes(b"tampered")
    assert _sha256(player_path) != sealed_player_sha256
    assert json.loads((out_dir / "crops.json").read_text())["crops"][0][
        "sha256"
    ] == sealed_player_sha256


def test_detect_layout_writes_box_canvas_px_not_crop_box_px(
    tmp_path, monkeypatch
) -> None:
    image_path = tmp_path / "mockup.png"
    out_path = tmp_path / "mockup.detect.json"
    Image.new("RGB", (100, 80), (10, 20, 30)).save(image_path)
    monkeypatch.setattr(
        detect_layout,
        "call_vlm_on_image",
        lambda **_kwargs: (
            '[{"label":"results_panel","role":"panel",'
            '"box":[0.1,0.2,0.6,0.8]}]'
        ),
    )

    result = detect_layout.detect_layout(
        SimpleNamespace(),
        image_path=image_path,
        out_path=out_path,
        canvas=(1000, 500),
        min_area_frac=0,
    )

    element = result["elements"][0]
    assert element["box_canvas_px"] == [100, 100, 500, 300]
    assert "box_px" not in element
    assert result["n_rejected"] == 0


def test_detect_layout_fails_when_all_returned_boxes_are_invalid(
    tmp_path, monkeypatch
) -> None:
    image_path = tmp_path / "mockup.png"
    out_path = tmp_path / "mockup.detect.json"
    Image.new("RGB", (100, 80), (10, 20, 30)).save(image_path)
    monkeypatch.setattr(
        detect_layout,
        "call_vlm_on_image",
        lambda **_kwargs: '[{"label":"player","box":[10,20,70,75]}]',
    )

    with pytest.raises(ToolError, match="every normalized box was invalid"):
        detect_layout.detect_layout(
            SimpleNamespace(),
            image_path=image_path,
            out_path=out_path,
        )

    assert not out_path.exists()


def test_detect_layout_rejects_background_only_when_foreground_is_invalid(
    tmp_path,
    monkeypatch,
) -> None:
    image_path = tmp_path / "mockup.png"
    out_path = tmp_path / "mockup.detect.json"
    Image.new("RGB", (100, 80), (10, 20, 30)).save(image_path)
    monkeypatch.setattr(
        detect_layout,
        "call_vlm_on_image",
        lambda **_kwargs: (
            '[{"label":"background","role":"background","box":[0,0,1,1]},'
            '{"label":"player","role":"entity","box":[0,0,2,1]}]'
        ),
    )

    with pytest.raises(ToolError, match="no usable non-background"):
        detect_layout.detect_layout(
            SimpleNamespace(),
            image_path=image_path,
            out_path=out_path,
        )

    assert not out_path.exists()


def test_detect_layout_rejects_empty_success_response(tmp_path, monkeypatch) -> None:
    image_path = tmp_path / "mockup.png"
    out_path = tmp_path / "mockup.detect.json"
    Image.new("RGB", (100, 80), (10, 20, 30)).save(image_path)
    monkeypatch.setattr(
        detect_layout,
        "call_vlm_on_image",
        lambda **_kwargs: "[]",
    )

    with pytest.raises(ToolError, match="empty element array"):
        detect_layout.detect_layout(
            SimpleNamespace(),
            image_path=image_path,
            out_path=out_path,
        )

    assert not out_path.exists()


def test_legacy_ground_crop_fails_before_synthetic_background_when_all_invalid(
    tmp_path, monkeypatch
) -> None:
    image_path = tmp_path / "mockup.png"
    out_dir = tmp_path / "crops"
    Image.new("RGB", (100, 80), (10, 20, 30)).save(image_path)
    monkeypatch.setattr(
        concept_ground,
        "call_vlm_on_image",
        lambda **_kwargs: '[{"label":"player","box":[10,20,70,75]}]',
    )

    with pytest.raises(ToolError, match="every normalized box was invalid"):
        ground_and_crop(
            SimpleNamespace(),
            image_path=image_path,
            out_dir=out_dir,
            add_background=True,
        )

    assert not (out_dir / "background.png").exists()
    assert not (out_dir / "crops.json").exists()


def test_legacy_ground_crop_rejects_empty_response_before_background(
    tmp_path,
    monkeypatch,
) -> None:
    image_path = tmp_path / "mockup.png"
    out_dir = tmp_path / "crops"
    Image.new("RGB", (100, 80), (10, 20, 30)).save(image_path)
    monkeypatch.setattr(
        concept_ground,
        "call_vlm_on_image",
        lambda **_kwargs: "[]",
    )

    with pytest.raises(ToolError, match="empty element array"):
        ground_and_crop(
            SimpleNamespace(),
            image_path=image_path,
            out_dir=out_dir,
            add_background=True,
        )

    assert not out_dir.exists()


def test_detect_driven_crop_rejects_background_malformed_and_tiny_only(
    tmp_path,
) -> None:
    image_path = tmp_path / "gameplay.png"
    detect_path = tmp_path / "gameplay.detect.json"
    out_dir = tmp_path / "crops"
    Image.new("RGB", (100, 100), (10, 20, 30)).save(image_path)
    detect_path.write_text(
        json.dumps(
            {
                "source": str(image_path),
                "source_sha256": _sha256(image_path),
                "elements": [
                    {
                        "key": "background",
                        "box": [0, 0, 1, 1],
                    },
                    {
                        "key": "pixel_like",
                        "box": [0, 0, 2, 1],
                    },
                    {
                        "key": "tiny",
                        "box": [0.2, 0.2, 0.001, 0.001],
                    },
                ],
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ToolError, match="no usable non-background"):
        crop_from_detect(
            image_path=image_path,
            detect_path=detect_path,
            out_dir=out_dir,
            padding=0.2,
            min_area_frac=0.01,
            add_background=True,
        )

    assert not (out_dir / "background.png").exists()
    assert not (out_dir / "crops.json").exists()


def test_production_tools_persist_relative_hash_bound_evidence(
    tmp_path,
    monkeypatch,
) -> None:
    workspace, ctx = _open_production_context(tmp_path)
    try:
        image_path = workspace.root / "design/gameplay.png"
        image_path.parent.mkdir(parents=True)
        Image.new("RGB", (100, 80), (10, 20, 30)).save(image_path)
        monkeypatch.setattr(
            detect_layout,
            "call_vlm_on_image",
            lambda **_kwargs: (
                '[{"label":"player","role":"entity",'
                '"box":[0.1,0.2,0.4,0.8]}]'
            ),
        )

        detect_result = _concept_tool("detect_layout").handler(
            {"input": "design/gameplay.png", "canvas_width": 100, "canvas_height": 80},
            ctx,
        )
        detect_path = workspace.root / "design/gameplay.detect.json"
        detect_record = json.loads(detect_path.read_text(encoding="utf-8"))
        assert detect_record["source"] == "design/gameplay.png"
        assert detect_record["source_sha256"] == _sha256(image_path)
        assert detect_result["image"] == detect_record["source"]
        assert detect_result["out_path"] == "design/gameplay.detect.json"
        assert detect_result["source_sha256"] == detect_record["source_sha256"]
        first_detection_sha256 = _sha256(detect_path)

        crop_result = _concept_tool("ground_crop").handler(
            {
                "input": "design/gameplay.png",
                "detect": "design/gameplay.detect.json",
                "output_dir": "design/crops/gameplay",
                "padding": 0,
            },
            ctx,
        )
        manifest_path = workspace.root / "design/crops/gameplay/crops.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        assert manifest["source"] == "design/gameplay.png"
        assert manifest["source_sha256"] == _sha256(image_path)
        assert manifest["bound_to"] == "design/gameplay.detect.json"
        assert manifest["detection_sha256"] == first_detection_sha256
        assert manifest["crops"][0]["path"] == "design/crops/gameplay/player.png"
        for crop in manifest["crops"]:
            crop_path = workspace.root / crop["path"]
            assert crop_path.is_file()
            assert crop["sha256"] == _sha256(crop_path)
        assert crop_result["image"] == manifest["source"]
        assert crop_result["detect"] == manifest["bound_to"]
        assert crop_result["manifest"] == "design/crops/gameplay/crops.json"
        assert crop_result["crops"] == manifest["crops"]
        assert "gca-concept-" not in json.dumps(manifest)
        assert crop_result["n_grounded"] == 1
        assert crop_result["n_rejected"] == 0
        assert crop_result["missed"] == []

        detect_record["elements"][0]["box"] = [0.5, 0.2, 0.2, 0.3]
        detect_path.write_text(
            json.dumps(detect_record, indent=2),
            encoding="utf-8",
        )
        changed_detection_sha256 = _sha256(detect_path)
        assert changed_detection_sha256 != first_detection_sha256

        changed_result = _concept_tool("ground_crop").handler(
            {
                "input": "design/gameplay.png",
                "detect": "design/gameplay.detect.json",
                "output_dir": "design/crops/gameplay",
                "padding": 0,
            },
            ctx,
        )
        changed_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        assert changed_manifest["detection_sha256"] == changed_detection_sha256
        assert changed_result["detection_sha256"] == changed_detection_sha256
        assert changed_manifest["crops"][0]["box_px"] != manifest["crops"][0]["box_px"]
    finally:
        workspace.close()


def test_production_crop_rerun_removes_stale_crop_files(tmp_path) -> None:
    workspace, ctx = _open_production_context(tmp_path)
    try:
        image_path = workspace.root / "design/gameplay.png"
        image_path.parent.mkdir(parents=True)
        Image.new("RGB", (100, 80), (10, 20, 30)).save(image_path)
        detect_path = workspace.root / "design/gameplay.detect.json"

        def write_detection(elements: list[dict]) -> None:
            detect_path.write_text(
                json.dumps(
                    {
                        "source": "design/gameplay.png",
                        "source_sha256": _sha256(image_path),
                        "elements": elements,
                    },
                    indent=2,
                ),
                encoding="utf-8",
            )

        player = {
            "key": "player",
            "role": "entity",
            "kind": "spawn",
            "box": [0.1, 0.2, 0.3, 0.5],
        }
        enemy = {
            "key": "enemy",
            "role": "entity",
            "kind": "spawn",
            "box": [0.6, 0.2, 0.2, 0.4],
        }
        write_detection([player, enemy])
        tool = _concept_tool("ground_crop")
        args = {
            "input": "design/gameplay.png",
            "detect": "design/gameplay.detect.json",
            "output_dir": "design/crops/gameplay",
            "padding": 0,
        }
        first = tool.handler(args, ctx)
        crop_dir = workspace.root / "design/crops/gameplay"
        assert (crop_dir / "enemy.png").is_file()
        assert {crop["label"] for crop in first["crops"]} == {
            "player",
            "enemy",
            "background",
        }

        write_detection([player])
        second = tool.handler(args, ctx)
        manifest = json.loads((crop_dir / "crops.json").read_text())

        assert not (crop_dir / "enemy.png").exists()
        assert {path.name for path in crop_dir.iterdir()} == {
            "player.png",
            "background.png",
            "crops.json",
        }
        assert not [
            path
            for path in crop_dir.parent.iterdir()
            if path.name.startswith(".gameplay.")
        ]
        assert {crop["label"] for crop in second["crops"]} == {
            "player",
            "background",
        }
        assert second["crops"] == manifest["crops"]
        for crop in second["crops"]:
            assert crop["path"] == f"design/crops/gameplay/{crop['crop']}"
            assert crop["sha256"] == _sha256(workspace.root / crop["path"])
    finally:
        workspace.close()


def test_production_crop_rejects_detection_after_mockup_changes(
    tmp_path,
    monkeypatch,
) -> None:
    workspace, ctx = _open_production_context(tmp_path)
    try:
        image_path = workspace.root / "design/gameplay.png"
        image_path.parent.mkdir(parents=True)
        Image.new("RGB", (100, 80), (10, 20, 30)).save(image_path)
        monkeypatch.setattr(
            detect_layout,
            "call_vlm_on_image",
            lambda **_kwargs: (
                '[{"label":"player","role":"entity",'
                '"box":[0.1,0.2,0.4,0.8]}]'
            ),
        )
        _concept_tool("detect_layout").handler(
            {"input": "design/gameplay.png"},
            ctx,
        )
        Image.new("RGB", (100, 80), (99, 88, 77)).save(image_path)

        with pytest.raises(ToolError, match="mockup content changed"):
            _concept_tool("ground_crop").handler(
                {
                    "input": "design/gameplay.png",
                    "detect": "design/gameplay.detect.json",
                    "output_dir": "design/crops/gameplay",
                },
                ctx,
            )

        assert not (workspace.root / "design/crops/gameplay/crops.json").exists()
    finally:
        workspace.close()


@pytest.mark.parametrize(
    "canvas_args",
    [
        {"canvas_width": 100},
        {"canvas_height": 80},
        {"canvas_width": 0, "canvas_height": 80},
        {"canvas_width": 100, "canvas_height": -1},
    ],
)
def test_detect_layout_rejects_partial_or_nonpositive_canvas(
    tmp_path,
    monkeypatch,
    canvas_args,
) -> None:
    workspace = tmp_path / "playground/game"
    image_path = workspace / "design/gameplay.png"
    image_path.parent.mkdir(parents=True)
    image_path.write_bytes(b"placeholder")
    monkeypatch.setattr(
        concept_tools.detect_layout,
        "detect_layout",
        lambda *_args, **_kwargs: pytest.fail("adapter must not be called"),
    )
    ctx = SimpleNamespace(
        state=SimpleNamespace(workspace=SimpleNamespace(root=workspace)),
        config=SimpleNamespace(
            assets=SimpleNamespace(
                vlm=SimpleNamespace(
                    detect_max_tokens=2500,
                    image_timeout_seconds=90,
                )
            )
        ),
    )

    with pytest.raises(ToolError, match="canvas_|positive integer"):
        concept_tools._detect_layout(
            {"input": "design/gameplay.png", **canvas_args},
            ctx,
        )


@pytest.mark.parametrize(
    ("handler", "args", "default_budget"),
    [
        (
            concept_tools._detect_layout,
            {"input": "design/gameplay.png", "vlm_max_tokens": -1},
            "detect_max_tokens",
        ),
        (
            concept_tools._ground_crop,
            {
                "input": "design/gameplay.png",
                "output_dir": "design/crops/gameplay",
                "vlm_max_tokens": -1,
            },
            "image_max_tokens",
        ),
    ],
)
def test_concept_tools_reject_negative_token_budget(
    tmp_path,
    handler,
    args,
    default_budget,
) -> None:
    workspace = tmp_path / "playground/game"
    image_path = workspace / "design/gameplay.png"
    image_path.parent.mkdir(parents=True)
    image_path.write_bytes(b"placeholder")
    vlm = SimpleNamespace(
        detect_max_tokens=2500,
        image_max_tokens=1200,
        image_timeout_seconds=90,
    )
    assert getattr(vlm, default_budget) > 0
    ctx = SimpleNamespace(
        state=SimpleNamespace(workspace=SimpleNamespace(root=workspace)),
        config=SimpleNamespace(assets=SimpleNamespace(vlm=vlm)),
    )

    with pytest.raises(ToolError, match="vlm_max_tokens.*positive integer"):
        handler(args, ctx)


@pytest.mark.parametrize(
    "args",
    [
        {
            "input": "design/gameplay.png",
            "output_dir": "design",
        },
        {
            "input": "design/gameplay.png",
            "output_dir": "design/crops",
        },
        {
            "input": "design/gameplay.png",
            "output_dir": "design/crops/gameplay/nested",
        },
        {
            "input": "design/gameplay.png",
            "output_dir": "assets/crops/gameplay",
        },
        {
            "input": "design/crops/gameplay/source.png",
            "output_dir": "design/crops/gameplay",
        },
        {
            "input": "design/gameplay.png",
            "detect": "design/crops/gameplay/gameplay.detect.json",
            "output_dir": "design/crops/gameplay",
        },
    ],
)
def test_ground_crop_rejects_unowned_or_overlapping_outputs_before_provider(
    tmp_path,
    monkeypatch,
    args,
) -> None:
    workspace, ctx = _open_production_context(tmp_path)
    try:
        for relative in (
            "design/gameplay.png",
            "design/crops/gameplay/source.png",
        ):
            image_path = workspace.root / relative
            image_path.parent.mkdir(parents=True, exist_ok=True)
            Image.new("RGB", (10, 10), (1, 2, 3)).save(image_path, format="PNG")
        detect_path = workspace.root / "design/crops/gameplay/gameplay.detect.json"
        detect_path.write_text('{"elements":[]}', encoding="utf-8")

        monkeypatch.setattr(
            concept_tools.concept_ground,
            "ground_and_crop",
            lambda *_args, **_kwargs: pytest.fail(
                "invalid crop paths must fail before provider invocation"
            ),
        )
        monkeypatch.setattr(
            concept_tools.concept_ground,
            "crop_from_detect",
            lambda *_args, **_kwargs: pytest.fail(
                "invalid crop paths must fail before provider invocation"
            ),
        )

        with pytest.raises(ToolError, match="exactly|overlaps"):
            _concept_tool("ground_crop").handler(args, ctx)
    finally:
        workspace.close()


@pytest.mark.parametrize(
    "args",
    [
        {
            "input": "design/gameplay.png",
            "output": "design/gameplay.json",
        },
        {
            "input": "design/gameplay.png",
            "output": "assets/gameplay.detect.json",
        },
        {
            "input": "design/gameplay.png",
            "output": "design",
        },
        {
            "input": "design/gameplay.png",
            "output": "design/nested/gameplay.detect.json",
        },
        {
            "input": "assets/gameplay.png",
        },
        {
            "input": "design/same.detect.json",
            "output": "design/same.detect.json",
        },
    ],
)
def test_detect_layout_rejects_noncanonical_or_overlapping_output_before_provider(
    tmp_path,
    monkeypatch,
    args,
) -> None:
    workspace, ctx = _open_production_context(tmp_path)
    try:
        for relative in (
            "design/gameplay.png",
            "assets/gameplay.png",
            "design/same.detect.json",
        ):
            image_path = workspace.root / relative
            image_path.parent.mkdir(parents=True, exist_ok=True)
            Image.new("RGB", (10, 10), (1, 2, 3)).save(image_path, format="PNG")

        monkeypatch.setattr(
            concept_tools.detect_layout,
            "detect_layout",
            lambda *_args, **_kwargs: pytest.fail(
                "invalid detect output must fail before provider invocation"
            ),
        )

        with pytest.raises(ToolError, match="strict file descendant|overlap"):
            _concept_tool("detect_layout").handler(args, ctx)
    finally:
        workspace.close()
