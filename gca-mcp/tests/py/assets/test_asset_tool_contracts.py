from __future__ import annotations

import hashlib
import json
import multiprocessing as mp
import os
import threading
import weakref
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

from gca_mcp import workspace as workspace_module
from gca_mcp.adapters import matting_availability
from gca_mcp.config import Config
from gca_mcp.errors import ToolError
from gca_mcp.tools import asset_tools, path_tools, postprocess_tools
from gca_mcp.verify_epoch import build_mutation_flock, verify_flock
from gca_mcp.workspace import WorkspaceMeta


@pytest.fixture(autouse=True)
def _reset_rmbg_availability():
    matting_availability.reset_rmbg_availability()
    yield
    matting_availability.reset_rmbg_availability()


def _slot_gdd(*rows: tuple[str, str, str | None]) -> str:
    body = "\n".join(
        f"| {key} | {action or '—'} | {description} |"
        for key, description, action in rows
    )
    return (
        "### 2A: Asset Plan\n"
        "| Key | Action | Description |\n"
        "|---|---|---|\n"
        f"{body}\n"
    )


class _TestLease:
    closed = False

    @staticmethod
    def assert_current() -> None:
        return None


def _close_test_workspace(root_fd: int, parent_fd: int) -> None:
    for descriptor in (root_fd, parent_fd):
        try:
            os.close(descriptor)
        except OSError:
            pass


def _ctx(tmp_path):
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    parent_fd = os.open(tmp_path.parent, flags)
    root_fd = os.open(tmp_path.name, flags, dir_fd=parent_fd)
    workspace = WorkspaceMeta(
        root=tmp_path,
        name=tmp_path.name,
        mode="test",
        _lease_file=_TestLease(),
        _root_fd=root_fd,
        _parent_fd=parent_fd,
    )
    weakref.finalize(workspace, _close_test_workspace, root_fd, parent_fd)
    return SimpleNamespace(
        config=Config.from_env(),
        state=SimpleNamespace(workspace=workspace),
        repo_root=tmp_path,
    )


def _write_quality_state(tmp_path, profile):
    state = tmp_path / ".playtest" / "state.json"
    state.parent.mkdir(parents=True, exist_ok=True)
    state.write_text(json.dumps({"quality_profile": profile}), encoding="utf-8")


def _stub_static_image_providers(monkeypatch, *, size=(40, 20), captured=None):
    calls = captured if captured is not None else {}

    def write_output(**kwargs):
        calls.update(kwargs)
        kwargs["output"].parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGBA", size, (255, 255, 255, 255)).save(kwargs["output"])
        return {
            "output": str(kwargs["output"]),
            "size": f"{size[0]}x{size[1]}",
            "request_id": "static-request",
        }

    monkeypatch.setattr(
        asset_tools.image_gen,
        "generate_image",
        lambda _config, **kwargs: write_output(**kwargs),
    )
    monkeypatch.setattr(
        asset_tools.image_gen,
        "edit_image",
        lambda _config, **kwargs: write_output(**kwargs),
    )
    monkeypatch.setattr(
        asset_tools.rmbg2,
        "remove_background",
        lambda *_args, **_kwargs: None,
    )
    return calls


def _lease_worker(root, output, start, release, results):
    start.wait(timeout=5)
    try:
        with asset_tools._owned_output_lock(Path(output), Path(root)):
            marker = json.loads(
                (Path(output) / ".gca-asset-run.lock").read_text(encoding="utf-8")
            )
            results.put(("entered", str(output), marker))
            release.wait(timeout=5)
    except Exception as exc:  # noqa: BLE001 - cross-process test transport
        results.put(("error", str(output), type(exc).__name__, str(exc)))


def test_asset_build_mutations_reject_active_verify_epoch(tmp_path):
    image = tmp_path / "assets" / "ui" / "icon.png"
    image.parent.mkdir(parents=True)
    Image.new("RGBA", (32, 32), (255, 255, 255, 255)).save(image)
    run_path = tmp_path / ".playtest" / "verify" / "current-run.json"
    run_path.parent.mkdir(parents=True)
    run_path.write_text(json.dumps({"status": "active"}), encoding="utf-8")
    tool = next(tool for tool in asset_tools.TOOLS if tool.name == "scale_uniform")

    with pytest.raises(ToolError, match="cannot change during an active"):
        tool.handler(
            {"image": "assets/ui/icon.png", "target_height": 16},
            _ctx(tmp_path),
        )

    run_path.write_text(json.dumps({"status": "finalized"}), encoding="utf-8")
    tool.handler(
        {"image": "assets/ui/icon.png", "target_height": 16},
        _ctx(tmp_path),
    )
    with Image.open(image) as scaled:
        assert scaled.size == (16, 16)


def test_registered_sprite_actions_can_run_concurrently(
    tmp_path,
    monkeypatch,
):
    reference = tmp_path / "assets/sprites/hero/reference/final/image.png"
    reference.parent.mkdir(parents=True)
    Image.new("RGBA", (16, 16), (20, 40, 80, 255)).save(reference)
    barrier = threading.Barrier(2)
    active_lock = threading.Lock()
    active = 0
    peak = 0

    def fake_action(_config, **kwargs):
        nonlocal active, peak
        assert kwargs["video_n_candidates"] == 1
        with active_lock:
            active += 1
            peak = max(peak, active)
        barrier.wait(timeout=3)
        with active_lock:
            active -= 1
        return {"passed": True, "output_dir": str(kwargs["output_dir"])}

    monkeypatch.setattr(
        asset_tools.produce_validated_sprite_action_adapter,
        "produce_validated_sprite_action",
        fake_action,
    )
    tool = next(
        tool
        for tool in asset_tools.TOOLS
        if tool.name == "produce_validated_sprite_action"
    )
    ctx = _ctx(tmp_path)

    def run(action):
        return tool.handler(
            {
                "video_user_content": f"hero {action}",
                "character_description": "blue hero",
                "first_frame_override": (
                    "assets/sprites/hero/reference/final/image.png"
                ),
                "output_dir": f"assets/sprites/hero/{action}",
            },
            ctx,
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(run, ("idle", "flap")))

    assert peak == 2
    assert all(result["passed"] is True for result in results)


def test_video_generation_tool_defaults_to_one_attempt():
    tools = {tool.name: tool for tool in asset_tools.TOOLS}

    assert (
        tools["produce_validated_video"]
        .input_schema["properties"]["n_candidates"]["default"]
        == 1
    )
    assert (
        tools["produce_validated_sprite_action"]
        .input_schema["properties"]["video_n_candidates"]["default"]
        == 1
    )


def test_parallel_build_guard_still_excludes_verify_transition(tmp_path):
    started = threading.Event()
    acquired = threading.Event()

    def begin_verify():
        started.set()
        with verify_flock(tmp_path, exclusive=True):
            acquired.set()

    with ThreadPoolExecutor(max_workers=1) as executor:
        with build_mutation_flock(tmp_path, exclusive=False):
            future = executor.submit(begin_verify)
            assert started.wait(timeout=1)
            assert acquired.wait(timeout=0.1) is False
        future.result(timeout=2)

    assert acquired.is_set()


def test_parallel_pack_calls_serialize_the_same_output(
    tmp_path,
    monkeypatch,
):
    frames = tmp_path / "assets/sprites/hero/idle/transparent_frames"
    frames.mkdir(parents=True)
    first_entered = threading.Event()
    second_entered = threading.Event()
    release_first = threading.Event()
    call_lock = threading.Lock()
    calls = 0

    def fake_pack(**kwargs):
        nonlocal calls
        with call_lock:
            calls += 1
            call = calls
        if call == 1:
            first_entered.set()
            assert release_first.wait(timeout=2)
        else:
            second_entered.set()
        return {"sheet": str(kwargs["output"])}

    monkeypatch.setattr(
        asset_tools.spritesheet_packer,
        "pack_spritesheet",
        fake_pack,
    )
    tool = next(
        tool for tool in asset_tools.TOOLS if tool.name == "pack_spritesheet"
    )
    args = {
        "frames_dir": (
            "assets/sprites/hero/idle/transparent_frames"
        ),
        "output": "assets/sprites/hero/idle-sheet.png",
        "frame_width": 16,
        "frame_height": 16,
    }
    ctx = _ctx(tmp_path)

    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(tool.handler, args, ctx)
        assert first_entered.wait(timeout=1)
        second = executor.submit(tool.handler, args, ctx)
        assert second_entered.wait(timeout=0.1) is False
        release_first.set()
        first.result(timeout=2)
        second.result(timeout=2)

    assert calls == 2
    assert second_entered.is_set()


def test_pack_spritesheet_requires_canonical_png_output(tmp_path):
    frames = tmp_path / "assets/sprites/hero/idle/transparent_frames"
    frames.mkdir(parents=True)

    with pytest.raises(ToolError, match="lowercase .png"):
        asset_tools._pack_spritesheet(
            {
                "frames_dir": (
                    "assets/sprites/hero/idle/transparent_frames"
                ),
                "output": "assets/sprites/hero/idle-sheet.webp",
                "frame_width": 16,
                "frame_height": 16,
            },
            _ctx(tmp_path),
        )


def test_first_frame_schema_and_handler_support_projectile_profile(
    tmp_path, monkeypatch
):
    tool = next(
        tool
        for tool in asset_tools.TOOLS
        if tool.name == "produce_validated_first_frame"
    )
    profile = tool.input_schema["properties"]["animation_profile"]
    assert profile["enum"] == ["character", "projectile"]
    assert "foot_anchor" not in tool.input_schema["properties"]

    captured = {}

    def fake_first_frame(config, **kwargs):
        captured.update(kwargs)
        return {"passed": True}

    monkeypatch.setattr(
        asset_tools.produce_validated_first_frame_adapter,
        "produce_validated_first_frame",
        fake_first_frame,
    )
    asset_tools._produce_validated_first_frame(
        {
            "image_prompt": "fireball",
            "character_description": "free-flying fireball",
            "output_dir": "assets/fireball",
            "animation_profile": "projectile",
        },
        _ctx(tmp_path),
    )
    assert captured["framing_profile"] == "centered_asset"
    assert "foot_anchor" not in captured
    assert [rubric["id"] for rubric in captured["rubric_override"]] == [
        "P1",
        "P2",
        "P3",
        "P4",
        "P5",
        "P6",
    ]


def test_sprite_handler_preserves_runtime_only_fx_default(tmp_path, monkeypatch):
    captured = {}

    def fake_sprite(config, **kwargs):
        captured.update(kwargs)
        return {"passed": True}

    monkeypatch.setattr(
        asset_tools.produce_validated_sprite_action_adapter,
        "produce_validated_sprite_action",
        fake_sprite,
    )
    asset_tools._produce_validated_sprite_action(
        {
            "video_user_content": "knight attacks",
            "output_dir": "assets/knight-attack",
            "first_frame_override": "assets/reference.png",
        },
        _ctx(tmp_path),
    )
    assert captured["effects"] is False
    sprite_tool = next(
        tool
        for tool in asset_tools.TOOLS
        if tool.name == "produce_validated_sprite_action"
    )
    assert sprite_tool.input_schema["properties"]["effects"]["default"] is False
    assert "foot_anchor" not in sprite_tool.input_schema["properties"]

    prompt_tool = next(
        tool for tool in asset_tools.TOOLS if tool.name == "build_i2v_prompt"
    )
    assert prompt_tool.input_schema["properties"]["effects"]["default"] is False


@pytest.mark.parametrize(
    ("tool_name", "valid_args", "legacy_arg"),
    [
        (
            "generate_image",
            {"prompt": "hero", "output": "assets/hero.png"},
            ("foot_anchor", True),
        ),
        (
            "edit_image",
            {
                "prompt": "hero",
                "input": "assets/source.png",
                "output": "assets/hero.png",
            },
            ("foot_anchor", True),
        ),
        (
            "produce_static_asset",
            {
                "prompt": "hero",
                "output": "assets/hero.png",
                "asset_type": "character",
            },
            ("foot_anchor", True),
        ),
        (
            "extend_background",
            {"input": "design/source.png", "output": "assets/background.png"},
            ("work_dir", "tmp"),
        ),
        (
            "extend_background",
            {"input": "design/source.png", "output": "assets/background.png"},
            ("keep_intermediates", True),
        ),
        (
            "validate_asset_slot",
            {"image": "assets/hero.png", "slot_key": "hero"},
            ("slot_description", "hero"),
        ),
    ],
)
def test_removed_asset_tool_arguments_are_rejected(tool_name, valid_args, legacy_arg):
    tool = next(tool for tool in asset_tools.TOOLS if tool.name == tool_name)
    legacy_name, legacy_value = legacy_arg
    assert legacy_name not in tool.input_schema["properties"]
    with pytest.raises(ToolError) as exc_info:
        tool.validate_arguments({**valid_args, legacy_name: legacy_value})
    assert exc_info.value.code == "INVALID_ARGUMENT"


def test_slot_validation_binds_evidence_to_image_and_gdd(tmp_path, monkeypatch):
    image = tmp_path / "assets" / "hero.png"
    image.parent.mkdir(parents=True)
    image.write_bytes(b"hero-v1")
    gdd = tmp_path / "GAME_DESIGN.md"
    gdd.write_text(_slot_gdd(("hero", "blue armored hero", None)))
    (tmp_path / "assets" / "asset-manifest.json").write_text(
        json.dumps(
            {
                "assets": [
                    {
                        "key": "hero",
                        "sourceStrategy": "generated-image",
                        "description": "blue armored hero",
                        "path": "assets/hero.png",
                    }
                ],
            }
        )
    )

    monkeypatch.setattr(
        asset_tools.asset_slot_validator,
        "validate_asset_slot",
        lambda _config, **kwargs: {
            "image": str(kwargs["image_path"]),
            "passed": True,
        },
    )
    result = asset_tools._validate_asset_slot(
        {
            "image": "assets/hero.png",
            "slot_key": "hero",
        },
        _ctx(tmp_path),
    )
    assert result["schema_version"] == 3
    assert result["image_sha256"] == hashlib.sha256(b"hero-v1").hexdigest()
    assert result["gdd_sha256"] == hashlib.sha256(gdd.read_bytes()).hexdigest()
    assert (
        result["slot_description_sha256"]
        == hashlib.sha256(b"blue armored hero").hexdigest()
    )
    assert result["gdd_slot_context_sha256"]
    assert result["producer"] == "gca-mcp.validate_asset_slot"
    assert result["logical_key"] == "hero"
    assert result["action"] is None
    assert result["path"] == "assets/hero.png"
    assert result["artifact_sha256"] == hashlib.sha256(b"hero-v1").hexdigest()
    assert result["canonical_rubric_version"] == (
        asset_tools.asset_slot_validator.CANONICAL_SLOT_RUBRIC_VERSION
    )
    assert result["canonical_rubric_sha256"] == (
        asset_tools.asset_slot_validator.CANONICAL_SLOT_RUBRIC_SHA256
    )
    manifest_payload = json.loads((tmp_path / "assets/asset-manifest.json").read_text())
    assert result["slot_contract_sha256"] == asset_tools._slot_contract_sha256(
        manifest_payload["assets"][0]
    )
    report = tmp_path / ".playtest/verify_middle/slot/hero.json"
    assert report.is_file()
    assert (
        json.loads(report.read_text())["manifest_sha256"]
        == hashlib.sha256(
            (tmp_path / "assets/asset-manifest.json").read_bytes()
        ).hexdigest()
    )

    gdd.write_text("# Hero contract\n\nred armored hero\n")
    with pytest.raises(ToolError, match="not uniquely bound"):
        asset_tools._validate_asset_slot(
            {
                "image": "assets/hero.png",
                "slot_key": "hero",
            },
            _ctx(tmp_path),
        )

    tool = next(
        tool for tool in asset_tools.TOOLS if tool.name == "validate_asset_slot"
    )
    assert "rubric_override" not in tool.input_schema["properties"]
    tool.validate_arguments(
        {
            "items": [
                {"image": "assets/hero.png", "slot_key": "hero"},
                {"image": "assets/ui.png", "slot_key": "ui"},
            ]
        }
    )
    with pytest.raises(ToolError, match="minItems"):
        tool.validate_arguments({"items": []})
    with pytest.raises(ToolError, match="rubric_override is not accepted"):
        asset_tools._validate_asset_slot(
            {
                "image": "assets/hero.png",
                "slot_key": "hero",
                "rubric_override": [],
            },
            _ctx(tmp_path),
        )


def test_gdd_slot_context_hash_matches_cross_runtime_golden():
    gdd = (
        "### 2A: Character Plan\r"
        "| Key | Action | Description |\u2028"
        "|---|---|---|\r\n"
        "| `hero` | idle | blue  armored\thero |\n"
    )
    assert asset_tools._gdd_slot_context_sha256(
        gdd,
        key="hero",
        action="idle",
        description="blue armored hero",
    ) == "2f150fee9151b743ce6d539322bf1841599087da9896d87ef25e842aaa664b15"

    duplicate = gdd + "| hero | idle | obsolete blue armored hero |\n"
    with pytest.raises(ValueError, match="not unique"):
        asset_tools._gdd_slot_context_sha256(
            duplicate,
            key="hero",
            action="idle",
            description="blue armored hero",
        )

    stale_secondary_table = (
        "### 2A: Character Plan\n"
        "| Key | Action | Description |\n"
        "|---|---|---|\n"
        "| hero | run | red armored hero |\n\n"
        "**Changelog**\n"
        "| Key | Action | Description |\n"
        "|---|---|---|\n"
        "| hero | idle | blue armored hero |\n"
    )
    with pytest.raises(ValueError, match="not unique"):
        asset_tools._gdd_slot_context_sha256(
            stale_secondary_table,
            key="hero",
            action="idle",
            description="blue armored hero",
        )


def test_slot_validation_reuses_hash_valid_passed_receipt(tmp_path, monkeypatch):
    image = tmp_path / "assets" / "hero.png"
    image.parent.mkdir(parents=True)
    image.write_bytes(b"hero-v1")
    (tmp_path / "GAME_DESIGN.md").write_text(
        _slot_gdd(("hero", "blue armored hero", None))
    )
    (tmp_path / "assets" / "asset-manifest.json").write_text(
        json.dumps(
            {
                "assets": [
                    {
                        "key": "hero",
                        "sourceStrategy": "generated-image",
                        "description": "blue armored hero",
                        "path": "assets/hero.png",
                    }
                ]
            }
        )
    )
    calls = 0

    def fake_validate(_config, **_kwargs):
        nonlocal calls
        calls += 1
        return {"passed": True}

    monkeypatch.setattr(
        asset_tools.asset_slot_validator,
        "validate_asset_slot",
        fake_validate,
    )
    args = {"image": "assets/hero.png", "slot_key": "hero"}
    first = asset_tools._validate_asset_slot(args, _ctx(tmp_path))
    second = asset_tools._validate_asset_slot(args, _ctx(tmp_path))

    assert first["reused"] is False
    assert second["reused"] is True
    assert calls == 1
    assert second["validation_policy_sha256"]

    (tmp_path / "GAME_DESIGN.md").write_text(
        _slot_gdd(("hero", "blue armored hero", None))
        + "\n# Unrelated design update\nnotes changed\n"
    )
    manifest_path = tmp_path / "assets" / "asset-manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["assets"].append(
        {
            "key": "ui-frame",
            "sourceStrategy": "generated-image",
            "description": "unrelated frame",
            "path": "assets/ui-frame.png",
        }
    )
    manifest_path.write_text(json.dumps(manifest))
    unchanged_slot = asset_tools._validate_asset_slot(args, _ctx(tmp_path))
    assert unchanged_slot["reused"] is True
    assert calls == 1

    (tmp_path / "GAME_DESIGN.md").write_text(
        _slot_gdd(("hero", "red armored hero", None))
        + "\n# Changelog\nhero | blue armored hero\n"
    )
    with pytest.raises(ToolError, match="not uniquely bound"):
        asset_tools._validate_asset_slot(args, _ctx(tmp_path))
    assert calls == 1

    (tmp_path / "GAME_DESIGN.md").write_text(
        _slot_gdd(("hero", "blue armored hero", None))
        + "\n# Unrelated design update\nnotes changed\n"
    )
    image.write_bytes(b"hero-v2")
    changed_image = asset_tools._validate_asset_slot(args, _ctx(tmp_path))
    assert changed_image["reused"] is False
    assert calls == 2


def test_slot_validation_batch_uses_one_worker_per_item(tmp_path, monkeypatch):
    assets = tmp_path / "assets"
    assets.mkdir()
    item_count = 6
    entries = []
    items = []
    descriptions = []
    for index in range(item_count):
        key = f"ui-{index}"
        description = f"interface asset {index}"
        (assets / f"{key}.png").write_bytes(key.encode())
        entries.append(
            {
                "key": key,
                "sourceStrategy": "generated-image",
                "description": description,
                "path": f"assets/{key}.png",
            }
        )
        items.append({"image": f"assets/{key}.png", "slot_key": key})
        descriptions.append(description)
    (tmp_path / "GAME_DESIGN.md").write_text(
        _slot_gdd(
            *(
                (f"ui-{index}", description, None)
                for index, description in enumerate(descriptions)
            )
        )
    )
    (assets / "asset-manifest.json").write_text(
        json.dumps({"assets": entries})
    )

    barrier = threading.Barrier(item_count)
    counter_lock = threading.Lock()
    active = 0
    peak_active = 0

    def fake_validate(_config, **_kwargs):
        nonlocal active, peak_active
        with counter_lock:
            active += 1
            peak_active = max(peak_active, active)
        barrier.wait(timeout=5)
        with counter_lock:
            active -= 1
        return {"passed": True}

    monkeypatch.setattr(
        asset_tools.asset_slot_validator,
        "validate_asset_slot",
        fake_validate,
    )
    result = asset_tools._validate_asset_slot(
        {"items": items},
        _ctx(tmp_path),
    )

    assert result["passed"] is True
    assert result["max_workers"] == item_count
    assert result["n_passed"] == item_count
    assert peak_active == item_count
    assert [item["key"] for item in result["results"]] == [
        f"ui-{index}" for index in range(item_count)
    ]


def test_slot_validation_and_epoch_sealing_accept_nested_sprite_manifest(
    tmp_path,
    monkeypatch,
):
    from gca_mcp.tools import verify_tools

    image = tmp_path / "assets/sprites/hero/idle-sheet.png"
    image.parent.mkdir(parents=True)
    Image.new("RGBA", (16, 16), (0, 0, 255, 255)).save(image)
    metadata = tmp_path / "assets/sprites/hero/idle-sheet.json"
    metadata.write_text(
        json.dumps({"frameWidth": 16, "frameHeight": 16, "frameCount": 1})
    )
    (tmp_path / "GAME_DESIGN.md").write_text(
        _slot_gdd(("hero", "nested blue hero", "idle"))
    )
    entry = {
        "sourceStrategy": "generated-image",
        "description": "nested blue hero",
        "layout": "per-action",
        "defaultAction": "idle",
        "path": "assets/sprites/hero/idle-sheet.png",
        "metadata": "assets/sprites/hero/idle-sheet.json",
        "actions": {
            "idle": {
                "textureKey": "hero-idle",
                "path": "assets/sprites/hero/idle-sheet.png",
                "metadata": "assets/sprites/hero/idle-sheet.json",
            }
        },
    }
    (tmp_path / "assets/asset-manifest.json").write_text(
        json.dumps({"sprites": {"hero": entry}})
    )
    monkeypatch.setattr(
        asset_tools.asset_slot_validator,
        "validate_asset_slot",
        lambda _config, **kwargs: {
            "image": str(kwargs["image_path"]),
            "passed": True,
        },
    )

    result = asset_tools._validate_asset_slot(
        {
            "image": "assets/sprites/hero/idle-sheet.png",
            "slot_key": "hero",
        },
        _ctx(tmp_path),
    )
    normalized = {"key": "hero", "category": "sprite", **entry}
    assert result["slot_contract_sha256"] == asset_tools._slot_contract_sha256(
        normalized
    )
    (tmp_path / "GAME_DESIGN.md").write_text(
        _slot_gdd(("hero", "nested blue hero", "idle"))
        + "\n# Unrelated prose changed\nnotes changed\n"
    )
    manifest_path = tmp_path / "assets/asset-manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["unrelatedReducerMetadata"] = {"version": 2}
    manifest_path.write_text(json.dumps(manifest))

    identity = {
        "pipeline_run_id": "pipeline-nested",
        "build_digest": "build-nested",
        "verify_run_id": "verify-nested",
    }
    sealed = verify_tools._seal_slot_receipts(tmp_path, identity)
    assert sealed == [".playtest/verify_middle/slot/hero.json"]
    receipt = json.loads(
        (tmp_path / ".playtest/verify_middle/slot/hero.json").read_text()
    )
    assert all(receipt[key] == value for key, value in identity.items())

    (tmp_path / "GAME_DESIGN.md").write_text("# Hero description removed\n")
    with pytest.raises(ToolError, match="current slot contract/GDD"):
        verify_tools._seal_slot_receipts(tmp_path, identity)


def test_slot_validation_binds_runtime_sheet_and_crops_frame_zero(
    tmp_path, monkeypatch
):
    sprite_dir = tmp_path / "assets" / "sprites" / "hero"
    sprite_dir.mkdir(parents=True)
    sheet = sprite_dir / "idle-sheet.png"
    image = Image.new("RGBA", (32, 16), (0, 0, 255, 255))
    image.paste(Image.new("RGBA", (16, 16), (255, 0, 0, 255)), (0, 0))
    image.save(sheet)
    metadata = sprite_dir / "idle-sheet.json"
    metadata.write_text(
        json.dumps({"frameWidth": 16, "frameHeight": 16, "frameCount": 2})
    )
    reference = sprite_dir / "reference.png"
    Image.new("RGBA", (16, 16), (255, 0, 0, 255)).save(reference)
    description = "blue armored hero"
    (tmp_path / "GAME_DESIGN.md").write_text(
        _slot_gdd(("hero", description, "idle"))
    )
    manifest = {
        "assets": [
            {
                "key": "hero",
                "category": "character",
                "sourceStrategy": "generated-image",
                "description": description,
                "layout": "per-action",
                "defaultAction": "idle",
                "path": "assets/sprites/hero/idle-sheet.png",
                "metadata": "assets/sprites/hero/idle-sheet.json",
                "actions": {
                    "idle": {
                        "textureKey": "hero-idle",
                        "path": "assets/sprites/hero/idle-sheet.png",
                        "metadata": "assets/sprites/hero/idle-sheet.json",
                    }
                },
            }
        ]
    }
    (tmp_path / "assets" / "asset-manifest.json").write_text(json.dumps(manifest))

    captured = {}

    def fake_validate(_config, **kwargs):
        captured.update(kwargs)
        with Image.open(kwargs["image_path"]) as frame:
            captured["frame_size"] = frame.size
            captured["first_pixel"] = frame.getpixel((0, 0))
        return {"passed": True}

    monkeypatch.setattr(
        asset_tools.asset_slot_validator,
        "validate_asset_slot",
        fake_validate,
    )

    with pytest.raises(ToolError, match="exactly match"):
        asset_tools._validate_asset_slot(
            {
                "image": "assets/sprites/hero/reference.png",
                "slot_key": "hero",
                "action": "idle",
            },
            _ctx(tmp_path),
        )

    result = asset_tools._validate_asset_slot(
        {
            "image": "assets/sprites/hero/idle-sheet.png",
            "slot_key": "hero",
            "action": "idle",
        },
        _ctx(tmp_path),
    )

    assert captured["frame_size"] == (16, 16)
    assert captured["first_pixel"] == (255, 0, 0, 255)
    assert captured["image_path"] == (
        tmp_path / ".playtest/verify_middle/slot/hero.idle.frame.png"
    )
    assert result["image"] == "assets/sprites/hero/idle-sheet.png"
    assert result["path"] == "assets/sprites/hero/idle-sheet.png"
    assert result["action"] == "idle"
    assert result["runtime_texture_key"] == "hero-idle"
    assert result["artifact_sha256"] == hashlib.sha256(sheet.read_bytes()).hexdigest()
    receipt = json.loads(
        (tmp_path / ".playtest/verify_middle/slot/hero.json").read_text()
    )
    assert (
        receipt["validation_frame_sha256"]
        == hashlib.sha256(captured["image_path"].read_bytes()).hexdigest()
    )


def test_pipeline_output_cannot_escape_workspace(tmp_path):
    with pytest.raises(ToolError, match="escapes"):
        asset_tools._resolve_owned_output_dir(_ctx(tmp_path), "../outside")
    with pytest.raises(ToolError, match="dedicated"):
        asset_tools._resolve_owned_output_dir(_ctx(tmp_path), "assets")


def test_mutating_asset_paths_require_workspace_and_owned_subtrees(tmp_path):
    no_workspace = _ctx(tmp_path)
    no_workspace.state.workspace = None
    with pytest.raises(ToolError, match="active workspace"):
        asset_tools._resolve_asset_output(no_workspace, "assets/hero.png")

    ctx = _ctx(tmp_path)
    assert (
        asset_tools._resolve_asset_output(ctx, "assets/hero.png")
        == tmp_path / "assets/hero.png"
    )
    assert (
        asset_tools._resolve_asset_output(ctx, "design/mockup.png")
        == tmp_path / "design/mockup.png"
    )
    for forbidden in (
        "src/hero.png",
        ".playtest/state.json",
        ".playtest/verify/hero.png",
    ):
        with pytest.raises(ToolError, match="authority-owned"):
            asset_tools._resolve_asset_output(ctx, forbidden)

    outside = tmp_path.parent / f"{tmp_path.name}-asset-output"
    outside.mkdir()
    assets = tmp_path / "assets"
    assets.mkdir()
    (assets / "linked").symlink_to(outside, target_is_directory=True)
    try:
        with pytest.raises(ToolError, match="escapes"):
            asset_tools._resolve_asset_output(
                ctx,
                "assets/linked/hero.png",
            )
        with pytest.raises(ToolError, match="escapes"):
            asset_tools._resolve_asset_output(ctx, str(outside / "hero.png"))
    finally:
        (assets / "linked").unlink()
        outside.rmdir()


class _StopAfterGenerate(Exception):
    """Halt the producer once the generator arguments have been captured."""


def _capture_generate_kwargs(monkeypatch):
    seen: dict[str, object] = {}

    def _stub(*_args, **kwargs):
        seen.update(kwargs)
        raise _StopAfterGenerate

    monkeypatch.setattr(asset_tools.image_gen, "generate_image", _stub)
    return seen


def test_parallax_defaults_to_a_mattable_plate(tmp_path, monkeypatch):
    # A parallax band composites above the sky, so by default it must be matted.
    # A chroma plate reaching the generator is what makes that possible; when
    # the type forced skip_matting there was no plate, the magenta stayed baked
    # in, and switching asset_type to dodge it broke the finalizer's assetType
    # parity check instead.
    seen = _capture_generate_kwargs(monkeypatch)

    with pytest.raises(_StopAfterGenerate):
        asset_tools._produce_static_asset(
            {
                "prompt": "distant skyline band",
                "output": "assets/maps/map-skyline.png",
                "asset_type": "parallax",
            },
            _ctx(tmp_path),
        )

    assert seen["safe_bg_color"] == "#FF00FF"


def test_parallax_bottom_layer_can_declare_itself_opaque(tmp_path, monkeypatch):
    # The sky at the bottom of the stack really is edge-to-edge; it opts out per
    # call rather than by dragging the whole category back to opaque.
    seen = _capture_generate_kwargs(monkeypatch)

    with pytest.raises(_StopAfterGenerate):
        asset_tools._produce_static_asset(
            {
                "prompt": "flat sky gradient",
                "output": "assets/maps/map-sky.png",
                "asset_type": "parallax",
                "skip_matting": True,
            },
            _ctx(tmp_path),
        )

    assert seen["safe_bg_color"] is None


def test_background_stays_opaque_without_a_flag(tmp_path, monkeypatch):
    seen = _capture_generate_kwargs(monkeypatch)

    with pytest.raises(_StopAfterGenerate):
        asset_tools._produce_static_asset(
            {
                "prompt": "full backdrop",
                "output": "assets/maps/map-base.png",
                "asset_type": "background",
            },
            _ctx(tmp_path),
        )

    assert seen["safe_bg_color"] is None


def test_skip_matting_stays_rejected_for_a_matted_type(tmp_path, monkeypatch):
    monkeypatch.setattr(
        asset_tools.image_gen,
        "generate_image",
        lambda *_a, **_k: pytest.fail("skip_matting on a prop must fail first"),
    )

    with pytest.raises(ToolError, match="only valid for background/map/parallax"):
        asset_tools._produce_static_asset(
            {
                "prompt": "a crate",
                "output": "assets/props/crate.png",
                "asset_type": "prop",
                "skip_matting": True,
            },
            _ctx(tmp_path),
        )


@pytest.mark.parametrize("output", ["assets", "design", "assets/props/crate.jpg"])
def test_static_output_requires_strict_png_file_before_provider(
    tmp_path,
    monkeypatch,
    output,
):
    monkeypatch.setattr(
        asset_tools.image_gen,
        "generate_image",
        lambda *_args, **_kwargs: pytest.fail(
            "invalid static output must fail before provider invocation"
        ),
    )

    with pytest.raises(ToolError, match="strict descendants|end with .png"):
        asset_tools._produce_static_asset(
            {
                "prompt": "paint a crate",
                "output": output,
                "asset_type": "prop",
            },
            _ctx(tmp_path),
        )


@pytest.mark.parametrize("output", ["assets", "design", "assets/maps/next.jpg"])
def test_outpaint_output_requires_strict_png_file_before_provider(
    tmp_path,
    monkeypatch,
    output,
):
    source = tmp_path / "assets" / "maps" / "source.png"
    source.parent.mkdir(parents=True)
    Image.new("RGBA", (8, 4), (1, 2, 3, 255)).save(source, format="PNG")
    monkeypatch.setattr(
        asset_tools.bg_extend,
        "extend_right",
        lambda *_args, **_kwargs: pytest.fail(
            "invalid outpaint output must fail before provider invocation"
        ),
    )

    with pytest.raises(ToolError, match="strict descendants|end with .png"):
        asset_tools._extend_background(
            {
                "input": "assets/maps/source.png",
                "output": output,
            },
            _ctx(tmp_path),
        )


def test_generated_static_asset_rejects_non_png_provider_content(
    tmp_path,
    monkeypatch,
):
    def write_jpeg(_config, **kwargs):
        kwargs["output"].parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (16, 12), (1, 2, 3)).save(
            kwargs["output"],
            format="JPEG",
        )
        return {"output": str(kwargs["output"]), "size": "16x12"}

    monkeypatch.setattr(asset_tools.image_gen, "generate_image", write_jpeg)
    with pytest.raises(ToolError, match="decode as PNG"):
        asset_tools._produce_static_asset(
            {
                "prompt": "paint a background",
                "output": "assets/maps/background.png",
                "asset_type": "background",
                "skip_matting": True,
            },
            _ctx(tmp_path),
        )

    assert not (tmp_path / "assets/maps/background.png").exists()
    assert not (tmp_path / "assets/maps/background.asset.json").exists()


@pytest.mark.parametrize(
    ("safe_bg_color", "key_rgb"),
    [
        ("#FF00FF", (255, 0, 255, 255)),
        ("#00F000", (0, 240, 0, 255)),
    ],
)
def test_static_color_key_backend_uses_actual_safe_background(
    tmp_path,
    monkeypatch,
    safe_bg_color,
    key_rgb,
):
    ctx = _ctx(tmp_path)
    ctx.config = replace(
        ctx.config,
        assets=replace(
            ctx.config.assets,
            matting=replace(ctx.config.assets.matting, backend="color_key"),
        ),
    )

    def write_keyed_image(_config, **kwargs):
        output = kwargs["output"]
        output.parent.mkdir(parents=True, exist_ok=True)
        image = Image.new("RGBA", (16, 16), key_rgb)
        for x in range(5, 11):
            for y in range(5, 11):
                image.putpixel((x, y), (240, 240, 240, 255))
        image.save(output, format="PNG")
        return {"output": str(output), "size": "16x16"}

    monkeypatch.setattr(asset_tools.image_gen, "generate_image", write_keyed_image)
    result = asset_tools._produce_static_asset(
        {
            "prompt": "paint a crate",
            "output": "assets/props/crate.png",
            "asset_type": "prop",
            "safe_bg_color": safe_bg_color,
        },
        ctx,
    )

    output = tmp_path / "assets/props/crate.png"
    with Image.open(output) as image:
        assert image.convert("RGBA").getchannel("A").getextrema() == (0, 255)
    sidecar = json.loads(output.with_suffix(".asset.json").read_text(encoding="utf-8"))
    assert result["matted"] is True
    assert result["source_contract"]["matting"] == "color_key"
    assert result["source_contract"]["mattingKeyColor"] == safe_bg_color
    assert result["provenance"]["mattingKeyColor"] == safe_bg_color
    assert sidecar["source_contract"]["matting"] == "color_key"
    assert sidecar["source_contract"]["mattingKeyColor"] == safe_bg_color
    assert sidecar["provenance"]["mattingKeyColor"] == safe_bg_color


@pytest.mark.parametrize("status", [404, 502])
def test_static_auto_backend_falls_back_and_seals_provenance(
    tmp_path,
    monkeypatch,
    status,
):
    ctx = _ctx(tmp_path)
    assert ctx.config.assets.matting.backend == "auto"

    def write_keyed_image(_config, **kwargs):
        output = kwargs["output"]
        output.parent.mkdir(parents=True, exist_ok=True)
        image = Image.new("RGBA", (16, 16), (255, 0, 255, 255))
        for x in range(5, 11):
            for y in range(5, 11):
                image.putpixel((x, y), (240, 240, 240, 255))
        image.save(output, format="PNG")
        return {"output": str(output), "size": "16x16"}

    rmbg_calls = 0

    def unavailable_rmbg(*_args, **_kwargs):
        nonlocal rmbg_calls
        rmbg_calls += 1
        raise ToolError(
            "RMBG2_FAILED",
            f"RMBG2 service returned HTTP {status}.",
        )

    monkeypatch.setattr(asset_tools.image_gen, "generate_image", write_keyed_image)
    monkeypatch.setattr(
        asset_tools.rmbg2,
        "remove_background",
        unavailable_rmbg,
    )

    result = asset_tools._produce_static_asset(
        {
            "prompt": "paint a crate",
            "output": "assets/props/crate.png",
            "asset_type": "prop",
        },
        ctx,
    )
    cached = asset_tools._produce_static_asset(
        {
            "prompt": "paint another crate",
            "output": "assets/props/crate-cached.png",
            "asset_type": "prop",
        },
        ctx,
    )

    output = tmp_path / "assets/props/crate.png"
    sidecar = json.loads(
        output.with_suffix(".asset.json").read_text(encoding="utf-8")
    )
    assert result["matting"] == "color_key"
    assert result["matting_requested"] == "auto"
    assert result["matting_fallback"] is True
    assert result["matting_fallback_code"] == "RMBG2_FAILED"
    assert result["rmbg_probe_cached"] is False
    assert cached["rmbg_probe_cached"] is True
    assert cached["matting"] == "color_key"
    assert rmbg_calls == 1
    assert result["source_contract"]["matting"] == "color_key"
    assert result["source_contract"]["mattingRequested"] == "auto"
    assert result["source_contract"]["mattingFallback"] is True
    assert result["provenance"]["mattingFallbackCode"] == "RMBG2_FAILED"
    assert sidecar["source_contract"]["matting"] == "color_key"
    assert sidecar["source_contract"]["mattingRequested"] == "auto"
    assert sidecar["provenance"]["mattingKeyColor"] == "#FF00FF"


@pytest.mark.parametrize(
    ("code", "message"),
    [
        ("AUTH_FAILED", "RMBG2 service rejected the request (HTTP 401)."),
        ("RMBG2_FAILED", "RMBG2 service returned HTTP 400."),
    ],
)
def test_static_auto_backend_does_not_hide_non_transient_rmbg_errors(
    tmp_path,
    monkeypatch,
    code,
    message,
):
    _stub_static_image_providers(monkeypatch)
    monkeypatch.setattr(
        asset_tools.rmbg2,
        "remove_background",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            ToolError(code, message)
        ),
    )

    with pytest.raises(ToolError) as exc_info:
        asset_tools._produce_static_asset(
            {
                "prompt": "paint a crate",
                "output": "assets/props/crate.png",
                "asset_type": "prop",
            },
            _ctx(tmp_path),
        )

    assert exc_info.value.code == code
    assert not (tmp_path / "assets/props/crate.png").exists()


def test_static_local_rmbg_tool_error_does_not_poison_probe_cache(
    tmp_path,
    monkeypatch,
):
    _stub_static_image_providers(monkeypatch)
    calls = 0

    def local_then_success(_config, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise ToolError("INPUT_FILE_NOT_FOUND", "local staged file vanished")
        output = kwargs["output"]
        image = Image.new("RGBA", (16, 16), (0, 0, 0, 0))
        for x in range(4, 12):
            for y in range(4, 12):
                image.putpixel((x, y), (240, 240, 240, 255))
        image.save(output, format="PNG")
        return {"output": str(output)}

    monkeypatch.setattr(
        asset_tools.rmbg2,
        "remove_background",
        local_then_success,
    )
    ctx = _ctx(tmp_path)

    with pytest.raises(ToolError) as exc_info:
        asset_tools._produce_static_asset(
            {
                "prompt": "paint a crate",
                "output": "assets/props/crate.png",
                "asset_type": "prop",
            },
            ctx,
        )
    assert exc_info.value.code == "INPUT_FILE_NOT_FOUND"

    result = asset_tools._produce_static_asset(
        {
            "prompt": "paint another crate",
            "output": "assets/props/crate-retry.png",
            "asset_type": "prop",
        },
        ctx,
    )
    assert calls == 2
    assert result["matting"] == "rmbg2"
    assert result["rmbg_probe_cached"] is False


def test_static_explicit_rmbg2_backend_does_not_fallback_on_502(
    tmp_path,
    monkeypatch,
):
    ctx = _ctx(tmp_path)
    ctx.config = replace(
        ctx.config,
        assets=replace(
            ctx.config.assets,
            matting=replace(ctx.config.assets.matting, backend="rmbg2"),
        ),
    )
    _stub_static_image_providers(monkeypatch)
    monkeypatch.setattr(
        asset_tools.rmbg2,
        "remove_background",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            ToolError("RMBG2_FAILED", "RMBG2 service returned HTTP 502.")
        ),
    )

    with pytest.raises(ToolError) as exc_info:
        asset_tools._produce_static_asset(
            {
                "prompt": "paint a crate",
                "output": "assets/props/crate.png",
                "asset_type": "prop",
            },
            ctx,
        )

    assert exc_info.value.code == "RMBG2_FAILED"
    assert not (tmp_path / "assets/props/crate.png").exists()


@pytest.mark.parametrize(
    ("plate_color", "failure_hint"),
    [
        ((255, 255, 255, 255), "no pixels matched key color"),
        ((255, 0, 255, 255), "removed the entire image foreground"),
    ],
)
def test_static_color_key_backend_rejects_false_success(
    tmp_path,
    monkeypatch,
    plate_color,
    failure_hint,
):
    ctx = _ctx(tmp_path)
    ctx.config = replace(
        ctx.config,
        assets=replace(
            ctx.config.assets,
            matting=replace(ctx.config.assets.matting, backend="color_key"),
        ),
    )

    def write_plate(_config, **kwargs):
        output = kwargs["output"]
        output.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGBA", (16, 16), plate_color).save(
            output,
            format="PNG",
        )
        return {"output": str(output), "size": "16x16"}

    monkeypatch.setattr(asset_tools.image_gen, "generate_image", write_plate)
    with pytest.raises(ToolError) as exc_info:
        asset_tools._produce_static_asset(
            {
                "prompt": "paint a crate",
                "output": "assets/props/crate.png",
                "asset_type": "prop",
            },
            ctx,
        )

    assert exc_info.value.code == "COLOR_KEY_MATTING_FAILED"
    assert failure_hint in str(exc_info.value.hint)
    assert not (tmp_path / "assets/props/crate.png").exists()
    assert not (tmp_path / "assets/props/crate.asset.json").exists()


def test_repo_reference_allowlist_is_read_only(tmp_path):
    workspace = tmp_path / "playground" / "game"
    workspace.mkdir(parents=True)
    reference = (
        tmp_path / ".claude" / "skills" / "game-pipeline" / "references" / "sample.png"
    )
    reference.parent.mkdir(parents=True)
    reference.write_bytes(b"reference")
    ctx = SimpleNamespace(
        config=Config.from_env(),
        state=SimpleNamespace(workspace=SimpleNamespace(root=workspace)),
        repo_root=tmp_path,
    )

    assert (
        asset_tools._resolve_asset_input(
            ctx,
            str(reference),
            allow_repo_reference=True,
        )
        == reference
    )
    with pytest.raises(ToolError, match="read-only"):
        asset_tools._resolve_asset_input(ctx, str(reference))
    with pytest.raises(ToolError, match="escapes"):
        asset_tools._resolve_asset_output(ctx, str(reference))


def test_pipeline_refuses_non_owned_existing_directory(tmp_path):
    output = tmp_path / "assets" / "hero"
    output.mkdir(parents=True)
    (output / "real-asset.png").write_bytes(b"do not delete")
    with pytest.raises(ToolError, match="non-owned"):
        with asset_tools._owned_output_lock(output):
            pass


def test_pipeline_adopts_legacy_rmbg_output_and_blocks_overlap(tmp_path):
    legacy = tmp_path / "assets" / "hero" / "rmbg2"
    (legacy / "transparent_frames").mkdir(parents=True)
    (legacy / "transparent.gif").write_bytes(b"gif")
    with asset_tools._owned_output_lock(legacy):
        assert (legacy / ".gca-asset-output").is_file()

    parent = tmp_path / "assets" / "other-hero"
    child = parent / "video"
    with asset_tools._owned_output_lock(parent):
        with pytest.raises(ToolError, match="overlapping"):
            with asset_tools._owned_output_lock(child):
                pass


def test_parent_child_output_lock_registration_is_atomic(tmp_path):
    parent = tmp_path / "assets" / "atomic-hero"
    child = parent / "video"
    entered = threading.Event()
    release = threading.Event()

    def hold_parent():
        with asset_tools._owned_output_lock(parent):
            entered.set()
            release.wait(timeout=5)

    thread = threading.Thread(target=hold_parent)
    thread.start()
    assert entered.wait(timeout=5)
    try:
        with pytest.raises(ToolError, match="overlapping"):
            with asset_tools._owned_output_lock(child):
                pass
    finally:
        release.set()
        thread.join(timeout=5)


def test_parent_child_output_lease_conflict_is_atomic_across_processes(
    tmp_path,
):
    parent = tmp_path / "assets" / "process-hero"
    child = parent / "video"
    process_ctx = mp.get_context("spawn")
    start = process_ctx.Event()
    release = process_ctx.Event()
    results = process_ctx.Queue()
    processes = [
        process_ctx.Process(
            target=_lease_worker,
            args=(tmp_path, output, start, release, results),
        )
        for output in (parent, child)
    ]
    for process in processes:
        process.start()
    start.set()
    try:
        outcomes = [results.get(timeout=15), results.get(timeout=15)]
        entered = [outcome for outcome in outcomes if outcome[0] == "entered"]
        errors = [outcome for outcome in outcomes if outcome[0] == "error"]
        assert len(entered) == 1
        assert entered[0][2]["producer"] == "gca-mcp.asset-output-lease"
        assert len(errors) == 1
        assert errors[0][2] == "ToolError"
        assert "overlapping output path" in errors[0][3]
    finally:
        release.set()
        for process in processes:
            process.join(timeout=10)
            if process.is_alive():
                process.terminate()
                process.join(timeout=5)
    assert all(process.exitcode == 0 for process in processes)


def test_opaque_static_producer_metadata_is_consumed_by_reducer(tmp_path, monkeypatch):
    assets = tmp_path / "assets"
    assets.mkdir()
    manifest = assets / "asset-manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "qualityProfile": "responsive",
                "assets": [
                    {
                        "key": "world-background",
                        "category": "background",
                        "sourceStrategy": "generated-image",
                        "description": "opaque world background",
                        "path": "assets/world-background.png",
                    }
                ],
            }
        )
    )

    def fake_generate(_config, **kwargs):
        kwargs["output"].parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (96, 54), (20, 40, 60)).save(kwargs["output"])
        return {
            "output": str(kwargs["output"]),
            "size": "96x54",
            "request_id": "request-background",
        }

    monkeypatch.setattr(
        asset_tools.image_gen,
        "generate_image",
        fake_generate,
    )
    monkeypatch.setattr(
        asset_tools.rmbg2,
        "remove_background",
        lambda *_args, **_kwargs: pytest.fail("opaque asset must not be matted"),
    )

    produced = asset_tools._produce_static_asset(
        {
            "prompt": "paint an opaque world background",
            "output": "assets/world-background.png",
            "asset_type": "background",
            "skip_matting": True,
        },
        _ctx(tmp_path),
    )
    artifact = assets / "world-background.png"
    sidecar = assets / "world-background.asset.json"
    metadata = json.loads(sidecar.read_text())
    assert produced["metadata_sidecar"] == str(sidecar)
    assert (
        produced["artifact_sha256"] == hashlib.sha256(artifact.read_bytes()).hexdigest()
    )
    assert metadata["producer"] == "produce_static_asset"
    assert metadata["artifact"] == "assets/world-background.png"
    assert metadata["width"] == 96
    assert metadata["height"] == 54
    assert metadata["provenance"]["request_id"] == "request-background"
    assert metadata["source_contract"] == {
        "version": 1,
        "sourceStrategy": "generated-image",
        "assetType": "background",
        "generationMode": "text-to-image",
        "matting": "skipped-opaque",
        "path": "assets/world-background.png",
    }
    assert list(assets.glob("*.tmp")) == []

    reduced = asset_tools._finalize_asset_manifest({}, _ctx(tmp_path))
    assert reduced["updated"] == ["world-background"]
    finalized = json.loads(manifest.read_text())
    assert finalized["assets"][0]["width"] == 96
    assert finalized["assets"][0]["height"] == 54


def test_art_directed_ui_requires_authored_size_before_generation(
    tmp_path, monkeypatch
):
    state = tmp_path / ".playtest" / "state.json"
    state.parent.mkdir(parents=True)
    state.write_text(json.dumps({"quality_profile": "art_directed"}))
    called = False

    def fake_generate(*_args, **_kwargs):
        nonlocal called
        called = True
        pytest.fail("invalid UI sizing must fail before generation")

    monkeypatch.setattr(asset_tools.image_gen, "generate_image", fake_generate)

    with pytest.raises(ToolError, match="authored §2E native dimension"):
        asset_tools._produce_static_asset(
            {
                "prompt": "paint a health icon",
                "output": "assets/ui/health.png",
                "asset_type": "ui",
            },
            _ctx(tmp_path),
        )
    assert called is False


@pytest.mark.parametrize(
    ("state_text", "message"),
    [
        (None, "state.json"),
        ("{not-json", "Could not read routed quality_profile"),
        (json.dumps({"quality_profile": "unknown"}), "valid routed quality_profile"),
    ],
)
def test_static_ui_requires_valid_routing_state_before_generation(
    tmp_path, monkeypatch, state_text, message
):
    state = tmp_path / ".playtest" / "state.json"
    state.parent.mkdir(parents=True)
    if state_text is not None:
        state.write_text(state_text)
    called = False

    def fake_generate(*_args, **_kwargs):
        nonlocal called
        called = True
        pytest.fail("invalid routing state must fail before generation")

    monkeypatch.setattr(asset_tools.image_gen, "generate_image", fake_generate)

    with pytest.raises(ToolError, match=message):
        asset_tools._produce_static_asset(
            {
                "prompt": "paint a health icon",
                "output": "assets/ui/health.png",
                "asset_type": "ui",
                "target_height": 32,
            },
            _ctx(tmp_path),
        )
    assert called is False


def test_static_ui_uniform_scale_is_verified_before_seal(tmp_path, monkeypatch):
    state = tmp_path / ".playtest" / "state.json"
    state.parent.mkdir(parents=True)
    state.write_text(json.dumps({"quality_profile": "art_directed"}))

    def fake_generate(_config, **kwargs):
        kwargs["output"].parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGBA", (128, 64), (255, 255, 255, 255)).save(kwargs["output"])
        return {"output": str(kwargs["output"]), "size": "128x64"}

    monkeypatch.setattr(asset_tools.image_gen, "generate_image", fake_generate)
    monkeypatch.setattr(
        asset_tools.rmbg2,
        "remove_background",
        lambda *_args, **_kwargs: None,
    )

    produced = asset_tools._produce_static_asset(
        {
            "prompt": "paint a health bar",
            "output": "assets/ui/health.png",
            "asset_type": "ui",
            "target_height": 32,
            "padding": 0,
            "clean_threshold": None,
        },
        _ctx(tmp_path),
    )

    assert produced["scale"]["after"] == {"w": 64, "h": 32}
    assert produced["width"] == 64
    assert produced["height"] == 32
    with Image.open(tmp_path / "assets" / "ui" / "health.png") as image:
        assert image.size == (64, 32)


@pytest.mark.parametrize(
    (
        "profile",
        "producer_sizing",
        "manifest_sizing",
        "expected_sizing",
        "expected_size",
        "decorative",
    ),
    [
        (
            "art_directed",
            {"target_width": 80},
            {"targetWidth": 80},
            {
                "mode": "target-width",
                "targetWidth": 80,
                "finalDimensions": {"width": 80, "height": 40},
                "aspectPreserved": True,
                "resampler": "lanczos",
            },
            (80, 40),
            False,
        ),
        (
            "art_directed",
            {"target_height": 30},
            {"targetHeight": 30},
            {
                "mode": "target-height",
                "targetHeight": 30,
                "finalDimensions": {"width": 60, "height": 30},
                "aspectPreserved": True,
                "resampler": "lanczos",
            },
            (60, 30),
            False,
        ),
        (
            "responsive",
            {"max_box": [60, 60]},
            {"maxBox": [60, 60]},
            {
                "mode": "max-box",
                "maxBox": [60, 60],
                "finalDimensions": {"width": 60, "height": 30},
                "aspectPreserved": True,
                "resampler": "lanczos",
            },
            (60, 30),
            False,
        ),
        (
            "minimal",
            {"max_box": [50, 50]},
            {"logicalBox": [50, 50]},
            {
                "mode": "max-box",
                "maxBox": [50, 50],
                "finalDimensions": {"width": 50, "height": 25},
                "aspectPreserved": True,
                "resampler": "lanczos",
            },
            (50, 25),
            True,
        ),
        (
            "pixel_art",
            {"base_size": [32, 32]},
            {"baseSize": [32, 32], "integerScales": [1, 2]},
            {
                "mode": "base-size",
                "baseSize": [32, 32],
                "finalDimensions": {"width": 32, "height": 32},
                "aspectPreserved": True,
                "resampler": "nearest-neighbor",
            },
            (32, 32),
            False,
        ),
    ],
)
def test_ui_sizing_contract_round_trips_without_rewriting_authored_mode(
    tmp_path,
    monkeypatch,
    profile,
    producer_sizing,
    manifest_sizing,
    expected_sizing,
    expected_size,
    decorative,
):
    _write_quality_state(tmp_path, profile)
    _stub_static_image_providers(monkeypatch)
    manifest_path = tmp_path / "assets" / "asset-manifest.json"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    entry = {
        "key": "status-panel",
        "category": "overlay",
        "sourceStrategy": "generated-image",
        "description": "status panel overlay",
        "path": "assets/ui/status-panel.png",
        **manifest_sizing,
    }
    if decorative:
        entry["decorative"] = True
    manifest_path.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "qualityProfile": profile,
                "assets": [entry],
            }
        ),
        encoding="utf-8",
    )

    asset_tools._produce_static_asset(
        {
            "prompt": "paint a status panel",
            "output": "assets/ui/status-panel.png",
            "asset_type": "ui",
            "padding": 0,
            "clean_threshold": None,
            **producer_sizing,
        },
        _ctx(tmp_path),
    )

    artifact = tmp_path / "assets" / "ui" / "status-panel.png"
    metadata = json.loads(artifact.with_suffix(".asset.json").read_text())
    source_contract = metadata["source_contract"]
    assert source_contract["qualityProfile"] == profile
    assert source_contract["sizingContract"] == expected_sizing
    assert not {"baseSize", "sizing", "resampler"} & source_contract.keys()
    assert (metadata["width"], metadata["height"]) == expected_size

    assert asset_tools._finalize_asset_manifest({}, _ctx(tmp_path))["updated"] == [
        "status-panel"
    ]
    finalized_entry = json.loads(manifest_path.read_text())["assets"][0]
    assert (finalized_entry["width"], finalized_entry["height"]) == expected_size
    for field, value in manifest_sizing.items():
        assert finalized_entry[field] == value
    sizing_fields = {
        "targetWidth",
        "targetHeight",
        "maxBox",
        "logicalBox",
        "baseSize",
    }
    assert sizing_fields & finalized_entry.keys() == sizing_fields & entry.keys()

    if profile == "minimal":
        invalid = json.loads(manifest_path.read_text())
        invalid_entry = invalid["assets"][0]
        invalid_entry.pop("logicalBox")
        invalid_entry["targetWidth"] = expected_size[0]
        manifest_path.write_text(json.dumps(invalid), encoding="utf-8")
        with pytest.raises(ToolError, match="stale/hash-mismatched"):
            asset_tools._finalize_asset_manifest({}, _ctx(tmp_path))


@pytest.mark.parametrize(
    ("profile", "valid_sizing", "invalid_sizings"),
    [
        (
            "art_directed",
            {"targetWidth": 40},
            [
                {"targetWidth": 39},
                {"targetWidth": 40, "targetHeight": 20},
            ],
        ),
        (
            "responsive",
            {"maxBox": [40, 20]},
            [
                {"maxBox": [39, 20]},
                {"maxBox": [40, 20], "logicalBox": [40, 20]},
            ],
        ),
        (
            "minimal",
            {"logicalBox": [40, 20]},
            [{"logicalBox": [40, 19]}],
        ),
        (
            "pixel_art",
            {"baseSize": [40, 20], "integerScales": [1, 2]},
            [
                {"baseSize": [39, 20], "integerScales": [1]},
                {"baseSize": [40, 20], "integerScales": []},
            ],
        ),
    ],
)
def test_supplied_ui_finalization_validates_actual_profile_dimensions(
    tmp_path,
    profile,
    valid_sizing,
    invalid_sizings,
):
    artifact = tmp_path / "assets" / "ui" / "supplied-panel.png"
    artifact.parent.mkdir(parents=True)
    Image.new("RGBA", (40, 20), (1, 2, 3, 255)).save(artifact, format="PNG")
    provenance = {"source": "user-upload"}
    asset_tools._register_existing_static_asset(
        {
            "image": "assets/ui/supplied-panel.png",
            "provenance": provenance,
        },
        _ctx(tmp_path),
    )

    manifest = tmp_path / "assets" / "asset-manifest.json"
    base_entry = {
        "key": "supplied-panel",
        "category": "panel",
        "sourceStrategy": "supplied-image",
        "provenance": provenance,
        "path": "assets/ui/supplied-panel.png",
    }

    def write_manifest(sizing):
        manifest.write_text(
            json.dumps(
                {
                    "schema_version": 2,
                    "qualityProfile": profile,
                    "assets": [{**base_entry, **sizing}],
                }
            ),
            encoding="utf-8",
        )

    write_manifest(valid_sizing)
    assert asset_tools._finalize_asset_manifest({}, _ctx(tmp_path))["updated"] == [
        "supplied-panel"
    ]

    for invalid_sizing in invalid_sizings:
        write_manifest(invalid_sizing)
        with pytest.raises(ToolError, match="stale/hash-mismatched"):
            asset_tools._finalize_asset_manifest({}, _ctx(tmp_path))


def test_finalizer_rejects_ui_profile_mismatch_extra_mode_and_target_drift(
    tmp_path, monkeypatch
):
    _write_quality_state(tmp_path, "art_directed")
    _stub_static_image_providers(monkeypatch)
    manifest_path = tmp_path / "assets" / "asset-manifest.json"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    base_manifest = {
        "schema_version": 2,
        "qualityProfile": "art_directed",
        "assets": [
            {
                "key": "health",
                "category": "icon",
                "sourceStrategy": "generated-image",
                "description": "health icon",
                "path": "assets/ui/health.png",
                "targetHeight": 20,
            }
        ],
    }
    manifest_path.write_text(json.dumps(base_manifest), encoding="utf-8")
    asset_tools._produce_static_asset(
        {
            "prompt": "paint a health icon",
            "output": "assets/ui/health.png",
            "asset_type": "ui",
            "target_height": 20,
            "padding": 0,
            "clean_threshold": None,
        },
        _ctx(tmp_path),
    )
    asset_tools._finalize_asset_manifest({}, _ctx(tmp_path))

    invalid_manifests = []
    target_drift = json.loads(json.dumps(base_manifest))
    target_drift["assets"][0]["targetHeight"] = 19
    invalid_manifests.append(target_drift)
    extra_mode = json.loads(json.dumps(base_manifest))
    extra_mode["assets"][0]["maxBox"] = [40, 20]
    invalid_manifests.append(extra_mode)
    profile_drift = json.loads(json.dumps(base_manifest))
    profile_drift["qualityProfile"] = "responsive"
    invalid_manifests.append(profile_drift)

    for invalid in invalid_manifests:
        manifest_path.write_text(json.dumps(invalid), encoding="utf-8")
        with pytest.raises(ToolError, match="stale/hash-mismatched"):
            asset_tools._finalize_asset_manifest({}, _ctx(tmp_path))


@pytest.mark.parametrize(
    "args",
    [
        {
            "prompt": "paint a widget",
            "output": "assets/ui/widget.png",
        },
        {
            "prompt": "paint lettering",
            "output": "assets/ui/title.png",
            "asset_type": "lettering",
        },
        {
            "prompt": "paint a dashboard",
            "output": "assets/ui/dashboard.png",
            "asset_type": "dashboard",
        },
        {
            "prompt": "paint a widget",
            "output": "assets/ui/widget.png",
            "asset_type": "ui",
            "target_height": 32,
            "skip_matting": True,
        },
    ],
)
def test_static_asset_contract_errors_fail_before_generation(
    tmp_path, monkeypatch, args
):
    state = tmp_path / ".playtest" / "state.json"
    state.parent.mkdir(parents=True)
    state.write_text(json.dumps({"quality_profile": "art_directed"}))

    monkeypatch.setattr(
        asset_tools.image_gen,
        "generate_image",
        lambda *_args, **_kwargs: pytest.fail("contract errors must not generate"),
    )

    with pytest.raises(ToolError):
        asset_tools._produce_static_asset(args, _ctx(tmp_path))


def test_art_directed_ui_rejects_max_box_before_generation(tmp_path, monkeypatch):
    state = tmp_path / ".playtest" / "state.json"
    state.parent.mkdir(parents=True)
    state.write_text(json.dumps({"quality_profile": "art_directed"}))
    monkeypatch.setattr(
        asset_tools.image_gen,
        "generate_image",
        lambda *_args, **_kwargs: pytest.fail("max_box must fail before generation"),
    )

    with pytest.raises(ToolError, match="max_box is reserved"):
        asset_tools._produce_static_asset(
            {
                "prompt": "paint a panel",
                "output": "assets/ui/panel.png",
                "asset_type": "ui",
                "max_box": [320, 180],
            },
            _ctx(tmp_path),
        )


@pytest.mark.parametrize(
    "target",
    [
        {"target_height": 0},
        {"target_width": -1},
        {"max_box": [0, 100]},
    ],
)
def test_invalid_static_target_preserves_existing_artifact_without_generation(
    tmp_path, monkeypatch, target
):
    final = tmp_path / "assets" / "props" / "crate.png"
    final.parent.mkdir(parents=True)
    Image.new("RGBA", (12, 10), (1, 2, 3, 255)).save(final)
    original = final.read_bytes()
    monkeypatch.setattr(
        asset_tools.image_gen,
        "generate_image",
        lambda *_args, **_kwargs: pytest.fail("invalid target must not generate"),
    )

    with pytest.raises(ToolError, match="positive integer"):
        asset_tools._produce_static_asset(
            {
                "prompt": "paint a crate",
                "output": "assets/props/crate.png",
                "asset_type": "prop",
                **target,
            },
            _ctx(tmp_path),
        )

    assert final.read_bytes() == original


def test_static_staging_failure_restores_existing_artifact_and_metadata(
    tmp_path, monkeypatch
):
    final = tmp_path / "assets" / "props" / "crate.png"
    metadata = final.with_suffix(".asset.json")
    bbox = final.with_suffix(".bbox.json")
    final.parent.mkdir(parents=True)
    Image.new("RGBA", (12, 10), (1, 2, 3, 255)).save(final)
    metadata.write_text('{"old":true}')
    bbox.write_text('{"image":"old","contentBbox":{"x":1,"y":1,"w":8,"h":8}}')
    original = final.read_bytes()
    original_metadata = metadata.read_text()
    original_bbox = bbox.read_text()

    def fake_generate(_config, **kwargs):
        kwargs["output"].parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGBA", (64, 64), (255, 255, 255, 255)).save(kwargs["output"])
        return {"output": str(kwargs["output"]), "size": "64x64"}

    monkeypatch.setattr(asset_tools.image_gen, "generate_image", fake_generate)
    monkeypatch.setattr(
        asset_tools.rmbg2,
        "remove_background",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            ToolError("RMBG2_FAILED", "provider failed")
        ),
    )

    with pytest.raises(ToolError, match="provider failed"):
        asset_tools._produce_static_asset(
            {
                "prompt": "paint a crate",
                "output": "assets/props/crate.png",
                "asset_type": "prop",
            },
            _ctx(tmp_path),
        )

    assert final.read_bytes() == original
    assert metadata.read_text() == original_metadata
    assert bbox.read_text() == original_bbox
    assert list(final.parent.glob("*.tmp.png")) == []


def test_static_publish_rolls_back_when_metadata_commit_fails(tmp_path, monkeypatch):
    final = tmp_path / "assets" / "props" / "crate.png"
    metadata = final.with_suffix(".asset.json")
    bbox = final.with_suffix(".bbox.json")
    final.parent.mkdir(parents=True)
    Image.new("RGBA", (12, 10), (1, 2, 3, 255)).save(final)
    metadata.write_text('{"old":true}')
    bbox.write_text('{"image":"old","contentBbox":{"x":1,"y":1,"w":8,"h":8}}')
    original = final.read_bytes()
    original_metadata = metadata.read_text()
    original_bbox = bbox.read_text()

    def fake_generate(_config, **kwargs):
        kwargs["output"].parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGBA", (64, 64), (255, 255, 255, 255)).save(kwargs["output"])
        return {"output": str(kwargs["output"]), "size": "64x64"}

    monkeypatch.setattr(asset_tools.image_gen, "generate_image", fake_generate)
    monkeypatch.setattr(
        asset_tools.rmbg2,
        "remove_background",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        asset_tools,
        "_seal_static_asset_metadata",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            ToolError("INVALID_ARTIFACT", "metadata commit failed")
        ),
    )

    with pytest.raises(ToolError, match="metadata commit failed"):
        asset_tools._produce_static_asset(
            {
                "prompt": "paint a crate",
                "output": "assets/props/crate.png",
                "asset_type": "prop",
                "padding": 0,
                "clean_threshold": None,
            },
            _ctx(tmp_path),
        )

    assert final.read_bytes() == original
    assert metadata.read_text() == original_metadata
    assert bbox.read_text() == original_bbox


def test_static_publish_rejects_parent_symlink_swap_without_external_writes(
    tmp_path,
    monkeypatch,
):
    ctx = _ctx(tmp_path)
    workspace = ctx.state.workspace
    parent = tmp_path / "assets" / "props"
    parent.mkdir(parents=True)
    final = parent / "crate.png"
    metadata = final.with_suffix(".asset.json")
    bbox = final.with_suffix(".bbox.json")
    Image.new("RGBA", (12, 10), (1, 2, 3, 255)).save(final, format="PNG")
    metadata.write_text('{"old":"metadata"}', encoding="utf-8")
    bbox.write_text('{"old":"bbox"}', encoding="utf-8")
    originals = {path: path.read_bytes() for path in (final, metadata, bbox)}

    staging = tmp_path / "staging"
    staging.mkdir()
    staged = staging / "crate.png"
    Image.new("RGBA", (20, 16), (9, 8, 7, 255)).save(staged, format="PNG")

    outside = tmp_path.parent / f"{tmp_path.name}-outside-static"
    outside.mkdir()
    (outside / "sentinel").write_bytes(b"outside")
    displaced = tmp_path / "assets" / "props-original"
    real_commit = workspace.commit_files_to_pinned

    def swap_parent_then_commit(*args, **kwargs):
        parent.rename(displaced)
        parent.symlink_to(outside, target_is_directory=True)
        try:
            return real_commit(*args, **kwargs)
        finally:
            parent.unlink()
            displaced.rename(parent)

    monkeypatch.setattr(
        workspace,
        "commit_files_to_pinned",
        swap_parent_then_commit,
    )
    try:
        with pytest.raises(ToolError, match="symlinked directory"):
            asset_tools._publish_staged_static_asset(
                ctx,
                staged_artifact=staged,
                final_artifact=final,
                staged_bbox=None,
                producer="produce_static_asset",
                provenance={"source": "generated"},
                source_contract={
                    "sourceStrategy": "generated-image",
                    "assetType": "prop",
                    "generationMode": "text-to-image",
                },
                result={},
            )

        for path, original in originals.items():
            assert path.read_bytes() == original
        assert {path.name for path in outside.iterdir()} == {"sentinel"}
    finally:
        for path in outside.iterdir():
            path.unlink()
        outside.rmdir()


def test_static_multi_file_transaction_restores_all_outputs_and_stale_bbox(
    tmp_path,
    monkeypatch,
):
    ctx = _ctx(tmp_path)
    parent = tmp_path / "assets" / "props"
    parent.mkdir(parents=True)
    final = parent / "crate.png"
    metadata = final.with_suffix(".asset.json")
    bbox = final.with_suffix(".bbox.json")
    auxiliary = parent / "crate-composite.png"
    Image.new("RGBA", (12, 10), (1, 2, 3, 255)).save(final, format="PNG")
    metadata.write_text('{"old":"metadata"}', encoding="utf-8")
    bbox.write_text('{"old":"bbox"}', encoding="utf-8")
    Image.new("RGBA", (12, 10), (4, 5, 6, 255)).save(
        auxiliary,
        format="PNG",
    )
    originals = {path: path.read_bytes() for path in (final, metadata, bbox, auxiliary)}

    staging = tmp_path / "staging"
    staging.mkdir()
    staged = staging / "crate.png"
    staged_auxiliary = staging / "crate-composite.png"
    Image.new("RGBA", (20, 16), (9, 8, 7, 255)).save(staged, format="PNG")
    Image.new("RGBA", (20, 16), (6, 7, 8, 255)).save(
        staged_auxiliary,
        format="PNG",
    )

    real_rename = workspace_module.os.rename
    injected = False

    def fail_metadata_install(source, destination, *args, **kwargs):
        nonlocal injected
        if (
            not injected
            and str(source).startswith(".crate.asset.json.")
            and str(source).endswith(".tmp")
            and destination == "crate.asset.json"
        ):
            injected = True
            raise OSError("injected metadata install failure")
        return real_rename(source, destination, *args, **kwargs)

    monkeypatch.setattr(workspace_module.os, "rename", fail_metadata_install)
    with pytest.raises(OSError, match="metadata install failure"):
        asset_tools._publish_staged_static_asset(
            ctx,
            staged_artifact=staged,
            final_artifact=final,
            staged_bbox=None,
            staged_auxiliary=[(staged_auxiliary, auxiliary)],
            producer="produce_static_asset",
            provenance={"source": "generated"},
            source_contract={
                "sourceStrategy": "generated-image",
                "assetType": "prop",
                "generationMode": "text-to-image",
            },
            result={},
        )

    assert injected is True
    for path, original in originals.items():
        assert path.read_bytes() == original
    assert not [path for path in parent.iterdir() if path.name.startswith(".crate")]


def test_static_asset_tooldef_matches_handler_contract():
    tool = next(
        tool for tool in asset_tools.TOOLS if tool.name == "produce_static_asset"
    )
    schema = tool.input_schema
    properties = schema["properties"]

    assert schema["required"] == ["prompt", "output", "asset_type"]
    assert "input" not in properties
    assert {
        "ref_image",
        "retry_delay",
        "enforce_safe_bg_avoidance",
        "aug_hard",
        "aug_soft",
        "rmbg_fg_cutoff",
        "max_size",
        "max_workers",
        "timeout",
    } <= properties.keys()
    assert properties["target_height"]["minimum"] == 1
    assert properties["target_width"]["minimum"] == 1
    assert properties["max_box"]["items"]["minimum"] == 1
    assert properties["base_size"]["items"]["minimum"] == 1
    assert properties["base_size"]["minItems"] == 2
    assert properties["base_size"]["maxItems"] == 2
    assert set(properties["asset_type"]["enum"]) == set(
        asset_tools._CANONICAL_STATIC_ASSET_TYPES
    )


def test_register_existing_static_asset_seals_metadata_and_bbox(tmp_path):
    asset = tmp_path / "assets" / "props" / "supplied-crate.png"
    asset.parent.mkdir(parents=True)
    image = Image.new("RGBA", (64, 48), (0, 0, 0, 0))
    for x in range(8, 48):
        for y in range(6, 42):
            image.putpixel((x, y), (255, 255, 255, 255))
    image.save(asset)

    result = asset_tools._register_existing_static_asset(
        {
            "image": "assets/props/supplied-crate.png",
            "provenance": {"source": "user-upload"},
            "collidable": True,
        },
        _ctx(tmp_path),
    )

    assert result["width"] == 64
    assert result["height"] == 48
    assert result["artifact_sha256"] == hashlib.sha256(asset.read_bytes()).hexdigest()
    metadata = json.loads(asset.with_suffix(".asset.json").read_text())
    assert metadata["producer"] == "register_existing_static_asset"
    assert metadata["provenance"] == {"source": "user-upload"}
    assert metadata["source_contract"] == {
        "version": 1,
        "sourceStrategy": "supplied-image",
        "registration": "preserve-pixels",
        "path": "assets/props/supplied-crate.png",
    }
    bbox = json.loads(asset.with_suffix(".bbox.json").read_text())
    assert bbox["contentBbox"] == {"x": 8, "y": 6, "w": 40, "h": 36}

    manifest = tmp_path / "assets" / "asset-manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "qualityProfile": "responsive",
                "assets": [
                    {
                        "key": "supplied-crate",
                        "category": "prop",
                        "sourceStrategy": "supplied-image",
                        "provenance": {"source": "user-upload"},
                        "path": "assets/props/supplied-crate.png",
                    }
                ],
            }
        )
    )
    finalized = asset_tools._finalize_asset_manifest({}, _ctx(tmp_path))
    assert finalized["updated"] == ["supplied-crate"]

    changed = json.loads(manifest.read_text())
    changed["assets"][0]["provenance"] = {"source": "other"}
    manifest.write_text(json.dumps(changed))
    with pytest.raises(ToolError, match="stale/hash-mismatched"):
        asset_tools._finalize_asset_manifest({}, _ctx(tmp_path))


def test_register_existing_static_asset_cannot_escape_workspace(tmp_path):
    outside = tmp_path.parent / f"{tmp_path.name}-outside.png"
    Image.new("RGBA", (32, 32), (255, 255, 255, 255)).save(outside)
    try:
        with pytest.raises(ToolError, match="escapes"):
            asset_tools._register_existing_static_asset(
                {
                    "image": str(outside),
                    "provenance": {"source": "user-upload"},
                },
                _ctx(tmp_path),
            )
    finally:
        outside.unlink(missing_ok=True)


@pytest.mark.parametrize(
    ("name", "image_format"),
    [
        ("supplied.jpg", "JPEG"),
        ("supplied.png", "JPEG"),
    ],
)
def test_register_existing_static_asset_requires_png_path_and_decoded_format(
    tmp_path,
    name,
    image_format,
):
    asset = tmp_path / "assets" / "props" / name
    asset.parent.mkdir(parents=True)
    Image.new("RGB", (16, 12), (1, 2, 3)).save(asset, format=image_format)

    with pytest.raises(ToolError, match="end with .png|decode as PNG"):
        asset_tools._register_existing_static_asset(
            {
                "image": f"assets/props/{name}",
                "provenance": {"source": "user-upload"},
            },
            _ctx(tmp_path),
        )

    assert not asset.with_suffix(".asset.json").exists()


def test_finalizer_rejects_non_png_static_content_and_extension(tmp_path):
    assets = tmp_path / "assets"
    assets.mkdir()
    manifest = assets / "asset-manifest.json"
    artifact = assets / "supplied.png"
    Image.new("RGB", (16, 12), (1, 2, 3)).save(artifact, format="JPEG")
    digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
    artifact.with_suffix(".asset.json").write_text(
        json.dumps(
            {
                "schema_version": 2,
                "producer": "register_existing_static_asset",
                "artifact": "assets/supplied.png",
                "artifact_sha256": digest,
                "width": 16,
                "height": 12,
                "provenance": {"source": "user-upload"},
                "source_contract": {
                    "version": 1,
                    "sourceStrategy": "supplied-image",
                    "registration": "preserve-pixels",
                    "path": "assets/supplied.png",
                },
            }
        ),
        encoding="utf-8",
    )
    base_entry = {
        "key": "supplied",
        "category": "item",
        "sourceStrategy": "supplied-image",
        "provenance": {"source": "user-upload"},
        "path": "assets/supplied.png",
    }
    manifest.write_text(
        json.dumps({"schema_version": 2, "assets": [base_entry]}),
        encoding="utf-8",
    )

    with pytest.raises(ToolError, match="stale/hash-mismatched"):
        asset_tools._finalize_asset_manifest({}, _ctx(tmp_path))

    non_png_path = assets / "supplied.jpg"
    artifact.rename(non_png_path)
    base_entry["path"] = "assets/supplied.jpg"
    manifest.write_text(
        json.dumps({"schema_version": 2, "assets": [base_entry]}),
        encoding="utf-8",
    )
    with pytest.raises(ToolError, match="end with .png"):
        asset_tools._finalize_asset_manifest({}, _ctx(tmp_path))


def test_register_existing_noncollidable_removes_stale_bbox_without_pixel_change(
    tmp_path,
):
    asset = tmp_path / "assets" / "props" / "supplied-crate.png"
    asset.parent.mkdir(parents=True)
    Image.new("RGBA", (12, 10), (1, 2, 3, 255)).save(asset)
    original = asset.read_bytes()
    bbox = asset.with_suffix(".bbox.json")
    bbox.write_text(
        json.dumps(
            {
                "image": str(asset),
                "contentBbox": {"x": 1, "y": 1, "w": 8, "h": 8},
            }
        ),
        encoding="utf-8",
    )

    asset_tools._register_existing_static_asset(
        {
            "image": "assets/props/supplied-crate.png",
            "provenance": {"source": "user-upload"},
            "collidable": False,
        },
        _ctx(tmp_path),
    )

    assert asset.read_bytes() == original
    assert not bbox.exists()
    assert asset.with_suffix(".asset.json").is_file()


def test_finalize_asset_manifest_is_single_writer_and_fail_closed(tmp_path):
    asset = tmp_path / "assets" / "props" / "crate.png"
    asset.parent.mkdir(parents=True)
    Image.new("RGBA", (64, 48), (255, 255, 255, 255)).save(asset)
    manifest = tmp_path / "assets" / "asset-manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "assets": [
                    {
                        "key": "crate",
                        "category": "prop",
                        "sourceStrategy": "generated-image",
                        "description": "wooden crate",
                        "path": "assets/props/crate.png",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ToolError, match="missing/invalid asset metadata"):
        asset_tools._finalize_asset_manifest({}, _ctx(tmp_path))

    metadata = asset.with_suffix(".asset.json")
    metadata.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "producer": "produce_static_asset",
                "artifact": "assets/props/crate.png",
                "artifact_sha256": hashlib.sha256(asset.read_bytes()).hexdigest(),
                "width": 64,
                "height": 48,
                "provenance": {
                    "source": "generated",
                    "producer": "gca-mcp.produce_static_asset",
                    "generation_method": "text-to-image",
                },
                "source_contract": {
                    "version": 1,
                    "sourceStrategy": "generated-image",
                    "assetType": "prop",
                    "generationMode": "text-to-image",
                    "path": "assets/props/crate.png",
                },
            }
        ),
        encoding="utf-8",
    )
    sidecar = asset.with_suffix(".bbox.json")
    sidecar.write_text(
        json.dumps({"contentBbox": {"x": 1, "y": 2, "w": 30, "h": 40}}),
        encoding="utf-8",
    )
    result = asset_tools._finalize_asset_manifest({}, _ctx(tmp_path))
    assert result["atomic"] is True
    assert result["updated"] == ["crate"]
    saved = json.loads(manifest.read_text(encoding="utf-8"))
    assert saved["assets"][0]["bbox"] == {"x": 1, "y": 2, "w": 30, "h": 40}
    assert saved["assets"][0]["width"] == 64
    assert saved["assets"][0]["height"] == 48

    old = asset.stat().st_mtime_ns - 10_000_000
    os.utime(sidecar, ns=(old, old))
    with pytest.raises(ToolError, match="stale/hash-mismatched"):
        asset_tools._finalize_asset_manifest({}, _ctx(tmp_path))


def test_finalizer_reopens_static_files_and_rejects_dimension_or_decode_drift(
    tmp_path, monkeypatch
):
    _stub_static_image_providers(monkeypatch)
    manifest = tmp_path / "assets" / "asset-manifest.json"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "assets": [
                    {
                        "key": "potion",
                        "category": "item",
                        "sourceStrategy": "generated-image",
                        "description": "healing potion",
                        "path": "assets/items/potion.png",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    asset_tools._produce_static_asset(
        {
            "prompt": "paint a potion",
            "output": "assets/items/potion.png",
            "asset_type": "item",
            "padding": 0,
            "clean_threshold": None,
        },
        _ctx(tmp_path),
    )
    asset_tools._finalize_asset_manifest({}, _ctx(tmp_path))

    artifact = tmp_path / "assets" / "items" / "potion.png"
    sidecar = artifact.with_suffix(".asset.json")
    metadata = json.loads(sidecar.read_text())
    metadata["width"] += 1
    sidecar.write_text(json.dumps(metadata), encoding="utf-8")
    with pytest.raises(ToolError, match="stale/hash-mismatched"):
        asset_tools._finalize_asset_manifest({}, _ctx(tmp_path))

    metadata["width"] -= 1
    artifact.write_bytes(b"not-a-readable-image")
    metadata["artifact_sha256"] = hashlib.sha256(artifact.read_bytes()).hexdigest()
    sidecar.write_text(json.dumps(metadata), encoding="utf-8")
    with pytest.raises(ToolError, match="stale/hash-mismatched"):
        asset_tools._finalize_asset_manifest({}, _ctx(tmp_path))


def test_crop_seed_reference_is_sealed_and_revalidated_at_finalization(
    tmp_path, monkeypatch
):
    reference = tmp_path / "design" / "crops" / "potion.png"
    reference.parent.mkdir(parents=True)
    Image.new("RGBA", (24, 24), (10, 20, 30, 255)).save(reference)
    reference_bytes = reference.read_bytes()
    reference_sha256 = hashlib.sha256(reference_bytes).hexdigest()
    captured = {}
    _stub_static_image_providers(monkeypatch, captured=captured)

    manifest = tmp_path / "assets" / "asset-manifest.json"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "assets": [
                    {
                        "key": "potion",
                        "category": "item",
                        "sourceStrategy": "generated-image",
                        "description": "crop-seeded potion",
                        "path": "assets/items/potion.png",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    asset_tools._produce_static_asset(
        {
            "prompt": "preserve the potion design",
            "output": "assets/items/potion.png",
            "asset_type": "item",
            "ref_image": "design/crops/potion.png",
            "padding": 0,
            "clean_threshold": None,
        },
        _ctx(tmp_path),
    )
    assert captured["input_path"] == str(reference)

    sidecar = tmp_path / "assets" / "items" / "potion.asset.json"
    metadata = json.loads(sidecar.read_text())
    expected_reference = {
        "referenceArtifact": "design/crops/potion.png",
        "referenceArtifactSha256": reference_sha256,
    }
    assert {
        field: metadata["provenance"][field] for field in expected_reference
    } == expected_reference
    assert {
        field: metadata["source_contract"][field] for field in expected_reference
    } == expected_reference
    assert asset_tools._finalize_asset_manifest({}, _ctx(tmp_path))["updated"] == [
        "potion"
    ]

    Image.new("RGBA", (24, 24), (99, 20, 30, 255)).save(reference)
    with pytest.raises(ToolError, match="stale/hash-mismatched"):
        asset_tools._finalize_asset_manifest({}, _ctx(tmp_path))

    reference.write_bytes(reference_bytes)
    reference.unlink()
    with pytest.raises(ToolError, match="stale/hash-mismatched"):
        asset_tools._finalize_asset_manifest({}, _ctx(tmp_path))


def test_crop_seed_reference_rejects_noncanonical_inputs_before_provider_call(
    tmp_path, monkeypatch
):
    called = False

    def fail_edit(*_args, **_kwargs):
        nonlocal called
        called = True
        pytest.fail("invalid reference must fail before provider invocation")

    monkeypatch.setattr(asset_tools.image_gen, "edit_image", fail_edit)
    with pytest.raises(ToolError, match="URLs are not accepted"):
        asset_tools._produce_static_asset(
            {
                "prompt": "paint a potion",
                "output": "assets/items/potion.png",
                "asset_type": "item",
                "ref_image": "https://example.invalid/potion.png",
            },
            _ctx(tmp_path),
        )

    with pytest.raises(ToolError, match="Reference image not found"):
        asset_tools._produce_static_asset(
            {
                "prompt": "paint a potion",
                "output": "assets/items/potion.png",
                "asset_type": "item",
                "ref_image": "design/missing.png",
            },
            _ctx(tmp_path),
        )

    repository_reference = (
        tmp_path / ".claude" / "skills" / "game-pipeline" / "references" / "potion.png"
    )
    repository_reference.parent.mkdir(parents=True)
    Image.new("RGBA", (8, 8), (1, 2, 3, 255)).save(repository_reference)
    with pytest.raises(ToolError, match="must resolve under workspace"):
        asset_tools._produce_static_asset(
            {
                "prompt": "paint a potion",
                "output": "assets/items/potion.png",
                "asset_type": "item",
                "ref_image": str(repository_reference),
            },
            _ctx(tmp_path),
        )
    assert called is False


def test_finalize_asset_manifest_tool_requires_workspace():
    tools = {tool.name: tool for tool in asset_tools.TOOLS}
    mutating = {
        "generate_image",
        "edit_image",
        "generate_seedance_video",
        "extend_background",
        "rmbg2_video_matting",
        "produce_validated_video",
        "validate_asset_slot",
        "produce_validated_first_frame",
        "produce_validated_sprite_action",
        "analyze_sprite_metadata",
        "pack_spritesheet",
        "register_existing_static_asset",
        "finalize_asset_manifest",
        "content_bbox",
        "trim_transparent",
        "scale_uniform",
        "produce_static_asset",
    }
    assert all(tools[name].requires_workspace is True for name in mutating)
    assert (
        tools["finalize_asset_manifest"].input_schema["additionalProperties"] is False
    )
    postprocess_mutating = {
        tool.name: tool.requires_workspace for tool in postprocess_tools.TOOLS
    }
    assert postprocess_mutating == {
        "postprocess": True,
        "color_key_matting": True,
        "rmbg2_matting": True,
    }
    # The legacy grid path is retired: check_raw_grid is gone and postprocess no
    # longer exposes split-sheet / extract-props.
    postprocess_tool = next(
        tool for tool in postprocess_tools.TOOLS if tool.name == "postprocess"
    )
    command_enum = postprocess_tool.input_schema["properties"]["command"]["enum"]
    assert "split-sheet" not in command_enum
    assert "extract-props" not in command_enum
    assert set(command_enum) == {"compose-preview", "make-seamless"}
    color_key_tool = next(
        tool for tool in postprocess_tools.TOOLS if tool.name == "color_key_matting"
    )
    color_key_properties = color_key_tool.input_schema["properties"]
    assert color_key_properties["key_color"]["enum"] == ["#FF00FF", "#00F000"]
    assert color_key_properties["threshold"]["minimum"] == 0
    assert color_key_properties["edge_threshold"]["minimum"] == 1
    for name in ("threshold", "edge_threshold"):
        assert color_key_properties[name]["maximum"] == 255
    assert {tool.name: tool.requires_workspace for tool in path_tools.TOOLS} == {
        "render_layout_mask": True,
    }


def test_color_key_tool_uses_runtime_config_defaults(tmp_path, monkeypatch) -> None:
    source = tmp_path / "assets" / "raw.png"
    source.parent.mkdir(parents=True)
    Image.new("RGBA", (8, 8), (0, 240, 0, 255)).save(source, format="PNG")
    ctx = _ctx(tmp_path)
    ctx.config = replace(
        ctx.config,
        assets=replace(
            ctx.config.assets,
            image=replace(ctx.config.assets.image, safe_bg_color="#00F000"),
            matting=replace(
                ctx.config.assets.matting,
                color_key_threshold=77,
                color_key_edge_threshold=88,
            ),
        ),
    )
    captured = {}

    def fake_run(script, cli, check):
        captured.update({"script": script, "cli": cli, "check": check})
        return {
            "returncode": 0,
            "stdout": str(tmp_path / "assets" / "out.png"),
            "stderr": "",
        }

    monkeypatch.setattr(postprocess_tools, "run_python_script", fake_run)
    postprocess_tools._color_key_matting(
        {"input": "assets/raw.png", "output": "assets/out.png"},
        ctx,
    )

    assert captured["check"] is False
    assert captured["cli"] == [
        "--input",
        str(source),
        "--output",
        str(tmp_path / "assets" / "out.png"),
        "--key-color",
        "#00F000",
        "--threshold",
        "77",
        "--edge-threshold",
        "88",
    ]


def test_color_key_tool_rejects_invalid_threshold_pair_before_runner(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        postprocess_tools,
        "run_python_script",
        lambda *_args, **_kwargs: pytest.fail("invalid config reached runner"),
    )

    with pytest.raises(ToolError) as exc_info:
        postprocess_tools._color_key_matting(
            {
                "input": "assets/raw.png",
                "output": "assets/out.png",
                "threshold": 151,
                "edge_threshold": 150,
            },
            _ctx(tmp_path),
        )

    assert exc_info.value.code == "INVALID_CONFIG"


def test_color_key_tool_maps_runner_failure_to_stable_error(
    tmp_path,
    monkeypatch,
) -> None:
    source = tmp_path / "assets" / "raw.png"
    source.parent.mkdir(parents=True)
    Image.new("RGBA", (8, 8), (255, 255, 255, 255)).save(source, format="PNG")
    monkeypatch.setattr(
        postprocess_tools,
        "run_python_script",
        lambda *_args, **_kwargs: {
            "returncode": 1,
            "stdout": "",
            "stderr": "no pixels matched key color",
        },
    )

    with pytest.raises(ToolError) as exc_info:
        postprocess_tools._color_key_matting(
            {"input": "assets/raw.png", "output": "assets/out.png"},
            _ctx(tmp_path),
        )

    assert exc_info.value.code == "COLOR_KEY_MATTING_FAILED"


def test_postprocess_and_layout_mask_respect_workspace_epoch(tmp_path) -> None:
    current = tmp_path / ".playtest/verify/current-run.json"
    current.parent.mkdir(parents=True)
    current.write_text(json.dumps({"status": "active"}), encoding="utf-8")
    ctx = _ctx(tmp_path)
    postprocess = next(
        tool.handler
        for tool in postprocess_tools.TOOLS
        if tool.name == "postprocess"
    )
    with pytest.raises(ToolError, match="active verification epoch"):
        postprocess({}, ctx)

    outside = tmp_path.parent / "outside-layout.json"
    outside.write_text("{}", encoding="utf-8")
    with pytest.raises(ToolError, match="escapes the active workspace"):
        path_tools._resolve_under_workspace(ctx, str(outside))


@pytest.mark.parametrize(
    "source_name",
    [
        "near-02.png",
        "near-02.asset.json",
        "near-02.bbox.json",
        "near-02-composite.png",
        "near-02-outpaint-full.png",
    ],
)
def test_extend_background_rejects_all_publish_path_collisions_before_provider(
    tmp_path, monkeypatch, source_name
):
    maps = tmp_path / "assets" / "maps"
    maps.mkdir(parents=True)
    source = maps / source_name
    Image.new("RGBA", (8, 4), (20, 30, 40, 255)).save(source, format="PNG")
    called = False

    def fail_provider(*_args, **_kwargs):
        nonlocal called
        called = True
        pytest.fail("colliding source must fail before provider invocation")

    monkeypatch.setattr(asset_tools.bg_extend, "extend_right", fail_provider)
    with pytest.raises(ToolError, match="input collides"):
        asset_tools._extend_background(
            {
                "input": f"assets/maps/{source_name}",
                "output": "assets/maps/near-02.png",
            },
            _ctx(tmp_path),
        )
    assert called is False


def test_extend_background_seals_persistent_evidence_and_finalizes(
    tmp_path, monkeypatch
):
    maps = tmp_path / "assets" / "maps"
    maps.mkdir(parents=True)
    source = maps / "near-01.png"
    source_image = Image.new("RGBA", (8, 4), (0, 0, 0, 255))
    for x in range(8):
        for y in range(4):
            source_image.putpixel((x, y), (x * 20, y * 30, 100, 255))
    source_image.save(source)
    source_sha256 = hashlib.sha256(source.read_bytes()).hexdigest()
    output = maps / "near-02.png"
    manifest = tmp_path / "assets" / "asset-manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "qualityProfile": "responsive",
                "assets": [
                    {
                        "key": "near-02",
                        "category": "background",
                        "sourceStrategy": "generated-image",
                        "description": "second near-background segment",
                        "path": "assets/maps/near-02.png",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    captured = {}

    def fake_edit(_config, **kwargs):
        captured.update(kwargs)
        with Image.open(kwargs["input_path"]) as composite:
            captured["composite_size"] = composite.size
        # Deliberately drift from the requested 12x4 canvas and its aspect.
        Image.new("RGBA", (20, 10), (12, 200, 80, 255)).save(kwargs["output"])
        return {
            "output": str(kwargs["output"]),
            "size": "20x10",
            "request_id": "outpaint-request",
        }

    monkeypatch.setattr(asset_tools.bg_extend.image_gen, "edit_image", fake_edit)
    produced = asset_tools._extend_background(
        {
            "input": "assets/maps/near-01.png",
            "output": "assets/maps/near-02.png",
        },
        _ctx(tmp_path),
    )

    composite_path = maps / "near-02-composite.png"
    full_path = maps / "near-02-outpaint-full.png"
    assert captured["composite_size"] == (12, 4)
    assert captured["size"] == "12x4"
    assert captured["asset_type"] == "background"
    assert captured["framing_profile"] == "full_scene"
    assert captured["subject_margin"] is False
    assert captured["enforce_safe_bg_avoidance"] is False
    assert produced["output"] == str(output)
    assert produced["composite"] == str(composite_path)
    assert produced["outpaint_full"] == str(full_path)
    assert produced["size"] == "8x4"
    assert output.is_file() and composite_path.is_file() and full_path.is_file()

    with (
        Image.open(source) as original,
        Image.open(composite_path) as composite,
        Image.open(full_path) as full,
        Image.open(output) as runtime,
    ):
        expected_overlap = original.crop((4, 0, 8, 4)).tobytes()
        assert composite.size == (12, 4)
        assert full.size == (12, 4)
        assert runtime.size == (8, 4)
        assert composite.crop((0, 0, 4, 4)).tobytes() == expected_overlap
        assert full.crop((0, 0, 4, 4)).tobytes() == expected_overlap

    metadata_path = maps / "near-02.asset.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    expected_artifacts = {
        "composite": "assets/maps/near-02-composite.png",
        "outpaint_full": "assets/maps/near-02-outpaint-full.png",
    }
    expected_review_hashes = {
        "composite": hashlib.sha256(composite_path.read_bytes()).hexdigest(),
        "outpaint_full": hashlib.sha256(full_path.read_bytes()).hexdigest(),
    }
    assert metadata["producer"] == "extend_background"
    assert (
        metadata["artifact_sha256"] == hashlib.sha256(output.read_bytes()).hexdigest()
    )
    assert (metadata["width"], metadata["height"]) == (8, 4)
    assert metadata["source_sha256"] == source_sha256
    assert metadata["lineage"][-1] == {
        "artifact": "assets/maps/near-01.png",
        "sha256": source_sha256,
    }
    assert metadata["artifact_paths"] == expected_artifacts
    assert metadata["review_artifact_sha256"] == expected_review_hashes
    assert metadata["source_contract"]["assetType"] == "background"
    assert metadata["source_contract"]["sourceSha256"] == source_sha256
    assert metadata["source_contract"]["reviewArtifacts"] == expected_artifacts
    assert metadata["source_contract"]["reviewArtifactSha256"] == expected_review_hashes

    reduced = asset_tools._finalize_asset_manifest({}, _ctx(tmp_path))
    assert reduced["updated"] == ["near-02"]
    finalized = json.loads(manifest.read_text(encoding="utf-8"))
    assert finalized["assets"][0]["width"] == 8
    assert finalized["assets"][0]["height"] == 4

    composite_bytes = composite_path.read_bytes()
    composite_path.write_bytes(composite_bytes + b"tampered")
    with pytest.raises(ToolError, match="stale/hash-mismatched"):
        asset_tools._finalize_asset_manifest({}, _ctx(tmp_path))
    composite_path.write_bytes(composite_bytes)

    source.write_bytes(source.read_bytes() + b"tampered")
    with pytest.raises(ToolError, match="stale/hash-mismatched"):
        asset_tools._finalize_asset_manifest({}, _ctx(tmp_path))


def test_extend_background_rolls_back_runtime_and_review_artifacts(
    tmp_path, monkeypatch
):
    maps = tmp_path / "assets" / "maps"
    maps.mkdir(parents=True)
    source = maps / "near-01.png"
    Image.new("RGBA", (8, 4), (20, 30, 40, 255)).save(source)

    output = maps / "near-02.png"
    composite = maps / "near-02-composite.png"
    outpaint_full = maps / "near-02-outpaint-full.png"
    metadata = maps / "near-02.asset.json"
    bbox = maps / "near-02.bbox.json"
    Image.new("RGBA", (8, 4), (1, 2, 3, 255)).save(output)
    Image.new("RGBA", (12, 4), (4, 5, 6, 255)).save(composite)
    Image.new("RGBA", (12, 4), (7, 8, 9, 255)).save(outpaint_full)
    metadata.write_text('{"old":true}', encoding="utf-8")
    bbox.write_text('{"old":true}', encoding="utf-8")
    originals = {
        path: path.read_bytes()
        for path in (output, composite, outpaint_full, metadata, bbox)
    }

    def fake_edit(_config, **kwargs):
        Image.new("RGBA", (12, 4), (100, 110, 120, 255)).save(kwargs["output"])
        return {"output": str(kwargs["output"]), "request_id": "new-request"}

    monkeypatch.setattr(asset_tools.bg_extend.image_gen, "edit_image", fake_edit)
    monkeypatch.setattr(
        asset_tools,
        "_seal_static_asset_metadata",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("seal failed")),
    )

    with pytest.raises(RuntimeError, match="seal failed"):
        asset_tools._extend_background(
            {
                "input": "assets/maps/near-01.png",
                "output": "assets/maps/near-02.png",
            },
            _ctx(tmp_path),
        )

    for path, original in originals.items():
        assert path.read_bytes() == original
    assert not [path for path in maps.iterdir() if ".tmp" in path.name]

    monkeypatch.setattr(
        asset_tools.bg_extend.image_gen,
        "edit_image",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("provider failed")
        ),
    )
    with pytest.raises(RuntimeError, match="provider failed"):
        asset_tools._extend_background(
            {
                "input": "assets/maps/near-01.png",
                "output": "assets/maps/near-02.png",
            },
            _ctx(tmp_path),
        )
    for path, original in originals.items():
        assert path.read_bytes() == original
    assert not [path for path in maps.iterdir() if ".tmp" in path.name]


def test_pixel_art_ui_uses_exact_nearest_base_size_and_finalizes(tmp_path, monkeypatch):
    state = tmp_path / ".playtest" / "state.json"
    state.parent.mkdir(parents=True)
    state.write_text(json.dumps({"quality_profile": "pixel_art"}))
    manifest = tmp_path / "assets" / "asset-manifest.json"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "qualityProfile": "pixel_art",
                "assets": [
                    {
                        "key": "status-panel",
                        "category": "panel",
                        "sourceStrategy": "generated-image",
                        "description": "pixel status panel",
                        "path": "assets/ui/status-panel.png",
                        "baseSize": [16, 16],
                        "integerScales": [1, 2],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    def fake_generate(_config, **kwargs):
        kwargs["output"].parent.mkdir(parents=True, exist_ok=True)
        image = Image.new("RGBA", (8, 4), (255, 0, 0, 255))
        for x in range(4, 8):
            for y in range(4):
                image.putpixel((x, y), (0, 0, 255, 255))
        image.save(kwargs["output"])
        return {"output": str(kwargs["output"]), "size": "8x4"}

    monkeypatch.setattr(asset_tools.image_gen, "generate_image", fake_generate)
    monkeypatch.setattr(
        asset_tools.rmbg2,
        "remove_background",
        lambda *_args, **_kwargs: None,
    )
    produced = asset_tools._produce_static_asset(
        {
            "prompt": "paint a two-color pixel status panel",
            "output": "assets/ui/status-panel.png",
            "asset_type": "ui",
            "base_size": [16, 16],
            "padding": 0,
            "clean_threshold": None,
        },
        _ctx(tmp_path),
    )

    artifact = tmp_path / "assets" / "ui" / "status-panel.png"
    assert produced["width"] == 16
    assert produced["height"] == 16
    assert produced["sizing"]["resampler"] == "nearest-neighbor"
    with Image.open(artifact) as image:
        assert image.size == (16, 16)
        assert image.getpixel((0, 0))[3] == 0
        assert image.getpixel((0, 4)) == (255, 0, 0, 255)
        assert image.getpixel((15, 11)) == (0, 0, 255, 255)
        assert image.getpixel((15, 15))[3] == 0

    metadata = json.loads(
        artifact.with_suffix(".asset.json").read_text(encoding="utf-8")
    )
    contract = metadata["source_contract"]
    assert contract["assetType"] == "ui"
    assert contract["qualityProfile"] == "pixel_art"
    assert contract["sizingContract"] == {
        "mode": "base-size",
        "baseSize": [16, 16],
        "finalDimensions": {"width": 16, "height": 16},
        "aspectPreserved": True,
        "resampler": "nearest-neighbor",
    }
    assert not {"baseSize", "sizing", "resampler"} & contract.keys()
    assert asset_tools._finalize_asset_manifest({}, _ctx(tmp_path))["updated"] == [
        "status-panel"
    ]

    invalid = json.loads(manifest.read_text())
    invalid["assets"][0]["baseSize"] = [15, 16]
    manifest.write_text(json.dumps(invalid), encoding="utf-8")
    with pytest.raises(ToolError, match="stale/hash-mismatched"):
        asset_tools._finalize_asset_manifest({}, _ctx(tmp_path))

    invalid["assets"][0]["baseSize"] = [16, 16]
    invalid["assets"][0]["targetWidth"] = 16
    manifest.write_text(json.dumps(invalid), encoding="utf-8")
    with pytest.raises(ToolError, match="stale/hash-mismatched"):
        asset_tools._finalize_asset_manifest({}, _ctx(tmp_path))


@pytest.mark.parametrize(
    ("profile", "sizing", "message"),
    [
        ("pixel_art", {}, "base_size"),
        (
            "pixel_art",
            {"base_size": [16, 16], "target_height": 16},
            "at most ONE",
        ),
        ("art_directed", {"base_size": [16, 16]}, "art_directed"),
        ("responsive", {"target_height": 16}, "max_box"),
        ("minimal", {}, "max_box"),
    ],
)
def test_ui_profile_sizing_contracts_fail_before_generation(
    tmp_path, monkeypatch, profile, sizing, message
):
    state = tmp_path / ".playtest" / "state.json"
    state.parent.mkdir(parents=True)
    state.write_text(json.dumps({"quality_profile": profile}))
    monkeypatch.setattr(
        asset_tools.image_gen,
        "generate_image",
        lambda *_args, **_kwargs: pytest.fail(
            "invalid sizing contract must not generate"
        ),
    )

    with pytest.raises(ToolError, match=message):
        asset_tools._produce_static_asset(
            {
                "prompt": "paint a UI panel",
                "output": "assets/ui/panel.png",
                "asset_type": "ui",
                **sizing,
            },
            _ctx(tmp_path),
        )


@pytest.mark.parametrize("base_size", [[16], [16, 1.5], [True, 16]])
def test_pixel_art_base_size_requires_exact_positive_integers(
    tmp_path, monkeypatch, base_size
):
    state = tmp_path / ".playtest" / "state.json"
    state.parent.mkdir(parents=True)
    state.write_text(json.dumps({"quality_profile": "pixel_art"}))
    monkeypatch.setattr(
        asset_tools.image_gen,
        "generate_image",
        lambda *_args, **_kwargs: pytest.fail("invalid base_size must not generate"),
    )
    with pytest.raises(ToolError):
        asset_tools._produce_static_asset(
            {
                "prompt": "paint a UI panel",
                "output": "assets/ui/panel.png",
                "asset_type": "ui",
                "base_size": base_size,
            },
            _ctx(tmp_path),
        )


def test_static_asset_rejects_unknown_type_before_generation(tmp_path, monkeypatch):
    monkeypatch.setattr(
        asset_tools.image_gen,
        "generate_image",
        lambda *_args, **_kwargs: pytest.fail("unknown type must not generate"),
    )
    with pytest.raises(ToolError, match="Unknown static"):
        asset_tools._produce_static_asset(
            {
                "prompt": "paint something",
                "output": "assets/props/unknown.png",
                "asset_type": "arbitrary-string",
            },
            _ctx(tmp_path),
        )


def test_finalizer_rejects_generated_asset_type_category_mismatch(
    tmp_path, monkeypatch
):
    manifest = tmp_path / "assets" / "asset-manifest.json"
    manifest.parent.mkdir()
    manifest.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "assets": [
                    {
                        "key": "world-background",
                        "category": "background",
                        "sourceStrategy": "generated-image",
                        "description": "world background",
                        "path": "assets/maps/world.png",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    def fake_generate(_config, **kwargs):
        kwargs["output"].parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGBA", (32, 32), (255, 255, 255, 255)).save(kwargs["output"])
        return {"output": str(kwargs["output"]), "size": "32x32"}

    monkeypatch.setattr(asset_tools.image_gen, "generate_image", fake_generate)
    monkeypatch.setattr(
        asset_tools.rmbg2,
        "remove_background",
        lambda *_args, **_kwargs: None,
    )
    asset_tools._produce_static_asset(
        {
            "prompt": "paint a prop under a background path",
            "output": "assets/maps/world.png",
            "asset_type": "prop",
            "padding": 0,
            "clean_threshold": None,
        },
        _ctx(tmp_path),
    )

    with pytest.raises(ToolError, match="stale/hash-mismatched"):
        asset_tools._finalize_asset_manifest({}, _ctx(tmp_path))


def test_staged_bbox_points_to_final_and_noncollidable_overwrite_removes_it(
    tmp_path, monkeypatch
):
    final = tmp_path / "assets" / "props" / "crate.png"
    generation = 0

    def fake_generate(_config, **kwargs):
        nonlocal generation
        generation += 1
        kwargs["output"].parent.mkdir(parents=True, exist_ok=True)
        image = Image.new("RGBA", (16, 16), (0, 0, 0, 0))
        color = (generation * 20, 40, 60, 255)
        for x in range(3, 13):
            for y in range(2, 14):
                image.putpixel((x, y), color)
        image.save(kwargs["output"])
        return {"output": str(kwargs["output"]), "size": "16x16"}

    monkeypatch.setattr(asset_tools.image_gen, "generate_image", fake_generate)
    monkeypatch.setattr(
        asset_tools.rmbg2,
        "remove_background",
        lambda *_args, **_kwargs: None,
    )
    common = {
        "prompt": "paint a crate",
        "output": "assets/props/crate.png",
        "asset_type": "prop",
        "padding": 0,
        "clean_threshold": None,
    }
    asset_tools._produce_static_asset(
        {**common, "collidable": True},
        _ctx(tmp_path),
    )
    bbox_path = final.with_suffix(".bbox.json")
    bbox = json.loads(bbox_path.read_text(encoding="utf-8"))
    assert bbox["image"] == str(final)

    canonical_sources = []
    real_replace = os.replace

    def observing_replace(source, destination):
        source_path = Path(source)
        if source_path in {
            final,
            final.with_suffix(".asset.json"),
            bbox_path,
        }:
            canonical_sources.append(source_path)
        return real_replace(source, destination)

    monkeypatch.setattr(asset_tools.os, "replace", observing_replace)
    asset_tools._produce_static_asset(
        {**common, "collidable": False},
        _ctx(tmp_path),
    )
    assert canonical_sources == []
    assert final.is_file()
    assert final.with_suffix(".asset.json").is_file()
    assert not bbox_path.exists()
