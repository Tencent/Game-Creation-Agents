from __future__ import annotations

import json
import subprocess
import sys
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from gca_mcp import providers
from gca_mcp.adapters import matting_availability
from gca_mcp.config import Config
from gca_mcp.errors import ToolError
from gca_mcp.evidence import compute_build_digest
from gca_mcp.server import ServerState, ToolCtx
from gca_mcp.tools import lifecycle, verify_tools
from gca_mcp.tools.lifecycle import (
    _audit_event_counts,
    _close_workspace,
    _init_workspace,
    _validate_state_transition,
)
from gca_mcp.tools.lifecycle import (
    _write_pipeline_state as _write_pipeline_state_raw,
)
from gca_mcp.workspace import open_workspace


@pytest.fixture(autouse=True)
def _reset_rmbg_availability():
    matting_availability.reset_rmbg_availability()
    yield
    matting_availability.reset_rmbg_availability()


def _write_pipeline_state(args, ctx):
    """Supply the non-variable routing fields used by most state tests."""
    return _write_pipeline_state_raw(
        {
            "genre": "custom",
            "interaction_profile": "none",
            **args,
        },
        ctx,
    )


def test_delete_scaffold_owned_paths_is_narrow_and_epoch_safe(
    tmp_path,
    request,
    monkeypatch,
) -> None:
    repo = tmp_path / "repo"
    (repo / "playground").mkdir(parents=True)
    opened = open_workspace(
        "playground/game",
        repo_root=repo,
        mode="create",
    )
    request.addfinalizer(opened.close)
    root = opened.root
    scene = root / "src/scenes/OldScene.js"
    test_file = root / ".playtest/tests/old.test.mjs"
    shared = root / "src/config.js"
    for path in (scene, test_file, shared):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("stale", encoding="utf-8")
    ctx = ToolCtx(
        state=ServerState(
            config=Config.from_env(),
            repo_root=repo,
            workspace=opened,
        )
    )
    current = root / ".playtest/verify/current-run.json"
    current.parent.mkdir(parents=True)
    (root / ".playtest/state.json").write_text(
        json.dumps(
            {
                "pipeline_run_id": "pipeline-test",
                "build_digest": "sha256:placeholder",
            }
        ),
        encoding="utf-8",
    )

    def declare(paths: list[str]) -> None:
        declaration = root / ".playtest/inputs/scaffold-reproject.json"
        declaration.parent.mkdir(parents=True, exist_ok=True)
        declaration.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "producer": "scaffold-agent",
                    "mode": "reproject",
                    "reproject_id": f"reproject-{uuid.uuid4().hex}",
                    "stale_owned_paths": paths,
                }
            ),
            encoding="utf-8",
        )

    declare(["src/scenes/OldScene.js"])
    current.write_text(json.dumps({"status": "active"}), encoding="utf-8")
    with pytest.raises(ToolError, match="active verification epoch"):
        lifecycle._seal_scaffold_reproject({}, ctx)

    current.write_text(json.dumps({"status": "finalized"}), encoding="utf-8")
    declare(["src/config.js"])
    with pytest.raises(ToolError, match="not a deletable"):
        lifecycle._seal_scaffold_reproject({}, ctx)
    declare(["src/scenes/OldScene.js", ".playtest/tests/old.test.mjs"])
    sealed = lifecycle._seal_scaffold_reproject({}, ctx)
    with pytest.raises(ToolError, match="no caller-supplied paths"):
        lifecycle._seal_scaffold_reproject(
            {"paths": ["src/scenes/OldScene.js"]},
            ctx,
        )
    with pytest.raises(ToolError) as replay_error:
        lifecycle._seal_scaffold_reproject({}, ctx)
    assert replay_error.value.code == "REPLAYED_RECEIPT"
    current.write_text(json.dumps({"status": "active"}), encoding="utf-8")
    with pytest.raises(ToolError, match="active verification epoch"):
        lifecycle._delete_scaffold_owned_paths(
            {"receipt_id": sealed["receipt_id"]},
            ctx,
        )
    current.write_text(json.dumps({"status": "finalized"}), encoding="utf-8")
    result = lifecycle._delete_scaffold_owned_paths(
        {"receipt_id": sealed["receipt_id"]},
        ctx,
    )
    assert result["deleted"] == [
        "src/scenes/OldScene.js",
        ".playtest/tests/old.test.mjs",
    ]
    assert not scene.exists()
    assert not test_file.exists()
    with pytest.raises(ToolError, match="stale, consumed, or cross-run"):
        lifecycle._delete_scaffold_owned_paths(
            {"receipt_id": sealed["receipt_id"]},
            ctx,
        )
    assert shared.is_file()

    declaration = root / ".playtest/inputs/scaffold-reproject.json"
    declaration.unlink()
    forged = root / "PROMPT.md"
    forged.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "producer": "scaffold-agent",
                "mode": "reproject",
                "reproject_id": f"reproject-{uuid.uuid4().hex}",
                "stale_owned_paths": ["src/scenes/Shared.js"],
            }
        ),
        encoding="utf-8",
    )
    declaration.symlink_to(forged)
    with pytest.raises(ToolError, match="symlinked"):
        lifecycle._seal_scaffold_reproject({}, ctx)
    declaration.unlink()

    symlink = root / "src/scenes/Shared.js"
    symlink.symlink_to(shared)
    declare(["src/scenes/Shared.js"])
    with pytest.raises(ToolError, match="may not traverse a symlink"):
        lifecycle._seal_scaffold_reproject({}, ctx)
    assert shared.is_file()

    changed = root / "src/scenes/Changed.js"
    changed.write_text("before", encoding="utf-8")
    declare(["src/scenes/Changed.js"])
    changed_receipt = lifecycle._seal_scaffold_reproject({}, ctx)
    changed.write_text("after", encoding="utf-8")
    with pytest.raises(ToolError, match="Build inputs changed"):
        lifecycle._delete_scaffold_owned_paths(
            {"receipt_id": changed_receipt["receipt_id"]},
            ctx,
        )
    assert changed.is_file()

    race = root / "src/scenes/Race.js"
    race.write_text("inside", encoding="utf-8")
    outside = tmp_path / "outside-scenes"
    outside.mkdir()
    outside_race = outside / "Race.js"
    outside_race.write_text("outside", encoding="utf-8")
    declare(["src/scenes/Race.js"])
    race_receipt = lifecycle._seal_scaffold_reproject({}, ctx)
    real_unlink = opened.unlink_from_pinned

    def replace_parent_then_unlink(relative, expected):
        scenes = root / "src/scenes"
        original_scenes = root / "src/scenes-original"
        scenes.rename(original_scenes)
        scenes.symlink_to(outside, target_is_directory=True)
        try:
            return real_unlink(relative, expected)
        finally:
            scenes.unlink()
            original_scenes.rename(scenes)

    monkeypatch.setattr(opened, "unlink_from_pinned", replace_parent_then_unlink)
    with pytest.raises(ToolError, match="symlinked directory"):
        lifecycle._delete_scaffold_owned_paths(
            {"receipt_id": race_receipt["receipt_id"]},
            ctx,
        )
    assert outside_race.read_text(encoding="utf-8") == "outside"
    assert race.is_file()


def test_explicit_runtime_config_is_required(tmp_path) -> None:
    with pytest.raises(FileNotFoundError, match="Required runtime config"):
        Config.from_env(config_path=tmp_path / "gca.config.yaml")
    malformed = tmp_path / "gca.config.yaml"
    malformed.write_text("quality:\n  invalid line\n")
    with pytest.raises(ValueError, match="Invalid runtime config"):
        Config.from_env(config_path=malformed)


def _matting_probe_ctx(backend: str):
    config = Config.from_env()
    config = replace(
        config,
        assets=replace(
            config.assets,
            matting=replace(config.assets.matting, backend=backend),
        ),
    )
    return SimpleNamespace(config=config)


@pytest.mark.parametrize("status", [404, 502])
def test_auto_matting_preflight_uses_color_key_for_fallback_eligible_rmbg_status(
    tmp_path,
    monkeypatch,
    status,
) -> None:
    provider = SimpleNamespace(
        rmbg_matte=lambda *_args, **_kwargs: (_ for _ in ()).throw(
            ToolError(
                "RMBG2_FAILED",
                f"RMBG2 service returned HTTP {status}.",
            )
        )
    )
    monkeypatch.setattr(providers, "get_provider", lambda _config: provider)
    tiny = lifecycle._tiny_png(tmp_path / "tiny.png")

    checks = lifecycle._matting_preflight_checks(
        _matting_probe_ctx("auto"),
        tiny,
        timeout=1,
    )
    report = lifecycle._summarize(checks)

    assert report["ok"] is True
    assert checks[0]["name"] == "rmbg2"
    assert checks[0]["severity"] == "warning"
    assert checks[0]["fallback_available"] is True
    assert checks[1]["name"] == "color_key_matting"
    assert checks[1]["ok"] is True


def _image_input_probe_ctx(monkeypatch, resolved_url: str):
    """Stub the provider transport so only frame encoding varies."""
    monkeypatch.setattr(
        providers,
        "get_provider",
        lambda _config: SimpleNamespace(
            ensure_image_url=lambda _config, path: resolved_url or str(path),
        ),
    )
    return SimpleNamespace(config=Config.from_env())


def test_image_input_probe_passes_when_frames_are_inlined(
    tmp_path,
    monkeypatch,
) -> None:
    ctx = _image_input_probe_ctx(
        monkeypatch,
        "data:image/png;base64,iVBORw0KGgo=",
    )
    tiny = lifecycle._tiny_png(tmp_path / "tiny.png")

    result = lifecycle._probe_image_input(ctx, tiny)

    assert result["ok"] is True
    assert result["backend"] == "inline"


def test_image_input_probe_rejects_non_inline_result(
    tmp_path,
    monkeypatch,
) -> None:
    ctx = _image_input_probe_ctx(monkeypatch, "https://example.invalid/tiny.png")
    tiny = lifecycle._tiny_png(tmp_path / "tiny.png")

    result = lifecycle._probe_image_input(ctx, tiny)

    assert result["ok"] is False
    assert result["severity"] == "blocker"


def test_image_input_probe_reports_encoding_failure(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        providers,
        "get_provider",
        lambda _config: SimpleNamespace(
            ensure_image_url=lambda *_a, **_k: (_ for _ in ()).throw(
                ToolError("INVALID_IMAGE", "Image could not be encoded.")
            ),
        ),
    )
    ctx = SimpleNamespace(config=Config.from_env())
    tiny = lifecycle._tiny_png(tmp_path / "tiny.png")

    result = lifecycle._probe_image_input(ctx, tiny)

    assert result["ok"] is False
    assert result["severity"] == "blocker"
    assert result["code"] == "INVALID_IMAGE"


def test_auto_matting_preflight_reuses_the_first_probe(
    tmp_path,
    monkeypatch,
) -> None:
    calls = 0

    def fail_probe(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        raise ToolError(
            "RMBG2_FAILED",
            "RMBG2 service returned HTTP 502.",
        )

    monkeypatch.setattr(
        providers,
        "get_provider",
        lambda _config: SimpleNamespace(rmbg_matte=fail_probe),
    )
    tiny = lifecycle._tiny_png(tmp_path / "tiny.png")
    ctx = _matting_probe_ctx("auto")

    first = lifecycle._matting_preflight_checks(ctx, tiny, timeout=1)
    second = lifecycle._matting_preflight_checks(ctx, tiny, timeout=1)

    assert calls == 1
    assert first[0].get("probe_cached") is not True
    assert second[0]["probe_cached"] is True
    assert second[0]["fallback_available"] is True


def test_auto_matting_preflight_caches_malformed_provider_output(
    tmp_path,
    monkeypatch,
) -> None:
    calls = 0

    def malformed_probe(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        return b"not-a-png", {"backend": "remote"}

    monkeypatch.setattr(
        providers,
        "get_provider",
        lambda _config: SimpleNamespace(
                        rmbg_matte=malformed_probe,
        ),
    )
    tiny = lifecycle._tiny_png(tmp_path / "tiny.png")
    ctx = _matting_probe_ctx("auto")

    first = lifecycle._matting_preflight_checks(ctx, tiny, timeout=1)
    second = lifecycle._matting_preflight_checks(ctx, tiny, timeout=1)

    assert calls == 1
    assert first[0]["ok"] is False
    assert second[0]["probe_cached"] is True
    assert second[0]["fallback_available"] is True


def test_explicit_rmbg_preflight_keeps_502_blocking(
    tmp_path,
    monkeypatch,
) -> None:
    provider = SimpleNamespace(
        rmbg_matte=lambda *_args, **_kwargs: (_ for _ in ()).throw(
            ToolError(
                "RMBG2_FAILED",
                "RMBG2 service returned HTTP 502.",
            )
        )
    )
    monkeypatch.setattr(providers, "get_provider", lambda _config: provider)
    tiny = lifecycle._tiny_png(tmp_path / "tiny.png")

    checks = lifecycle._matting_preflight_checks(
        _matting_probe_ctx("rmbg2"),
        tiny,
        timeout=1,
    )

    assert lifecycle._summarize(checks)["ok"] is False
    assert checks[0]["severity"] == "blocker"


@pytest.mark.parametrize("backend", ["auto", "rmbg2"])
def test_matting_preflight_keeps_non_fallback_errors_blocking(
    tmp_path,
    monkeypatch,
    backend,
) -> None:
    provider = SimpleNamespace(
        rmbg_matte=lambda *_args, **_kwargs: (_ for _ in ()).throw(
            ToolError(
                "AUTH_FAILED",
                "RMBG2 service rejected the request (HTTP 401).",
            )
        )
    )
    monkeypatch.setattr(providers, "get_provider", lambda _config: provider)
    tiny = lifecycle._tiny_png(tmp_path / "tiny.png")

    checks = lifecycle._matting_preflight_checks(
        _matting_probe_ctx(backend),
        tiny,
        timeout=1,
    )

    assert lifecycle._summarize(checks)["ok"] is False
    assert checks[0]["severity"] == "blocker"


def test_color_key_preflight_does_not_call_rmbg(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        providers,
        "get_provider",
        lambda _config: pytest.fail("explicit color_key must not call RMBG"),
    )
    tiny = lifecycle._tiny_png(tmp_path / "tiny.png")

    checks = lifecycle._matting_preflight_checks(
        _matting_probe_ctx("color_key"),
        tiny,
        timeout=1,
    )

    assert lifecycle._summarize(checks)["ok"] is True
    assert [check["name"] for check in checks] == ["color_key_matting"]


def test_state_counters_require_single_step_and_prior_audit() -> None:
    with pytest.raises(ToolError, match="unchanged or increment"):
        _validate_state_transition(
            {"verify_runs": 1},
            {"verify_runs": 3},
            {"verify_run": 3},
        )

    with pytest.raises(ToolError, match="requires at least 2"):
        _validate_state_transition(
            {"verify_runs": 1},
            {"verify_runs": 2},
            {"verify_run": 1},
        )

    _validate_state_transition(
        {"verify_runs": 1, "iteration_count": 0},
        {"verify_runs": 2, "iteration_count": 1},
        {"verify_run": 2, "iteration_complete": 1},
    )


def test_state_transition_enforces_iteration_budget_on_advance() -> None:
    _validate_state_transition(
        {"iteration_count": 1},
        {"iteration_count": 2},
        {"iteration_complete": 2},
        max_iterations=2,
    )

    with pytest.raises(ToolError, match="pipeline.max_iterations=2") as error:
        _validate_state_transition(
            {"iteration_count": 2},
            {"iteration_count": 3},
            {"iteration_complete": 3},
            max_iterations=2,
        )
    assert error.value.code == "ITERATION_BUDGET_EXHAUSTED"

    # A run created under a previously larger budget may still persist
    # non-counter state while reporting its already-exhausted status.
    _validate_state_transition(
        {"iteration_count": 3},
        {},
        {"iteration_complete": 3},
        max_iterations=2,
    )


def test_assets_generated_requires_canonical_audit_event() -> None:
    with pytest.raises(ToolError, match="assets_generated"):
        _validate_state_transition({}, {"assets_generated": True}, {})

    _validate_state_transition(
        {},
        {"assets_generated": True},
        {"assets_generated": 1},
    )


def test_legacy_counter_migration_freezes_one_run_scoped_baseline(tmp_path) -> None:
    playtest = tmp_path / ".playtest"
    playtest.mkdir()
    (playtest / "state.json").write_text(
        json.dumps({
            "phase": "verify",
            "iterationCount": 2,
            "pipeline": {"verify": {"run_count": 3}},
        }),
        encoding="utf-8",
    )
    (playtest / "audit.jsonl").write_text(
        "\n".join([
            json.dumps({"event": "iteration_complete"}),
            json.dumps({"event": "verify_run"}),
            json.dumps({"event": "verify_run"}),
        ])
        + "\n",
        encoding="utf-8",
    )
    config = Config.from_env()
    ctx = SimpleNamespace(
        config=replace(
            config,
            pipeline=replace(config.pipeline, max_iterations=3),
        ),
        repo_root=tmp_path,
        state=SimpleNamespace(workspace=SimpleNamespace(root=tmp_path)),
    )

    result = _write_pipeline_state(
        {
            "phase": "verify",
            "quality_profile": "responsive",
            "level_model": "none",
        },
        ctx,
    )
    migrated = json.loads((playtest / "state.json").read_text())
    assert migrated["verify_runs"] == 3
    assert migrated["iteration_count"] == 2
    assert migrated["audit_offsets"] == {
        "verify_runs": 3,
        "iteration_count": 2,
    }
    marker = migrated["counter_migration"]
    assert marker["pipeline_run_id"] == result["pipeline_run_id"]
    assert marker["snapshot"]["tolerated_missing"] == {
        "verify_runs": 1,
        "iteration_count": 1,
    }

    _write_pipeline_state({"phase": "verify"}, ctx)
    unchanged = json.loads((playtest / "state.json").read_text())
    assert unchanged["audit_offsets"] == migrated["audit_offsets"]
    assert unchanged["counter_migration"] == marker

    with pytest.raises(ToolError, match="run-scoped `iteration_complete`"):
        _write_pipeline_state(
            {"phase": "verify", "iteration_count": 3},
            ctx,
        )

    with (playtest / "audit.jsonl").open("a", encoding="utf-8") as stream:
        stream.write(json.dumps({"event": "iteration_complete"}) + "\n")
    with pytest.raises(ToolError, match="run-scoped `iteration_complete`"):
        _write_pipeline_state(
            {"phase": "verify", "iteration_count": 3},
            ctx,
        )

    with (playtest / "audit.jsonl").open("a", encoding="utf-8") as stream:
        stream.write(json.dumps({
            "event": "iteration_complete",
            "pipeline_run_id": result["pipeline_run_id"],
        }) + "\n")
    _write_pipeline_state(
        {"phase": "verify", "iteration_count": 3},
        ctx,
    )
    advanced = json.loads((playtest / "state.json").read_text())
    assert advanced["iteration_count"] == 3
    assert advanced["audit_offsets"] == migrated["audit_offsets"]
    assert advanced["counter_migration"] == marker


def test_initialized_audit_offsets_cannot_expand_or_reinitialize(tmp_path) -> None:
    playtest = tmp_path / ".playtest"
    playtest.mkdir()
    (playtest / "state.json").write_text(
        json.dumps({"iterationCount": 1}),
        encoding="utf-8",
    )
    ctx = SimpleNamespace(
        config=Config.from_env(),
        repo_root=tmp_path,
        state=SimpleNamespace(workspace=SimpleNamespace(root=tmp_path)),
    )
    _write_pipeline_state(
        {
            "phase": "startup",
            "quality_profile": "minimal",
            "level_model": "none",
        },
        ctx,
    )
    state_path = playtest / "state.json"
    migrated = json.loads(state_path.read_text())

    expanded = {
        **migrated,
        "audit_offsets": {
            **migrated["audit_offsets"],
            "iteration_count": 2,
        },
    }
    state_path.write_text(json.dumps(expanded), encoding="utf-8")
    with pytest.raises(ToolError, match="snapshot for iteration_count"):
        _write_pipeline_state({"phase": "design"}, ctx)

    without_marker = dict(migrated)
    without_marker.pop("counter_migration")
    state_path.write_text(json.dumps(without_marker), encoding="utf-8")
    with pytest.raises(ToolError, match="require a counter_migration"):
        _write_pipeline_state({"phase": "design"}, ctx)


def test_legacy_counter_migration_rejects_gap_above_tolerance(tmp_path) -> None:
    playtest = tmp_path / ".playtest"
    playtest.mkdir()
    state_path = playtest / "state.json"
    state_path.write_text(
        json.dumps({"iterationCount": 2}),
        encoding="utf-8",
    )
    ctx = SimpleNamespace(
        config=Config.from_env(),
        repo_root=tmp_path,
        state=SimpleNamespace(workspace=SimpleNamespace(root=tmp_path)),
    )

    with pytest.raises(ToolError, match="above migration tolerance"):
        _write_pipeline_state(
            {
                "phase": "startup",
                "quality_profile": "minimal",
                "level_model": "none",
            },
            ctx,
        )
    assert json.loads(state_path.read_text()) == {"iterationCount": 2}


def test_state_hook_uses_frozen_migration_offset_not_live_tolerance(
    tmp_path,
) -> None:
    repo_root = tmp_path / "repo"
    game_dir = repo_root / "playground" / "migration-game"
    playtest = game_dir / ".playtest"
    playtest.mkdir(parents=True)
    (repo_root / "gca.config.yaml").write_text(
        "\n".join([
            "pipeline:",
            "  min_iteration_gap_seconds: 0",
            "  min_screenshots: 0",
            "  min_unique_screenshots: 0",
            "  staleness_tolerance_seconds: 0",
            "verify:",
            "  skip_video: true",
        ])
        + "\n",
        encoding="utf-8",
    )
    (playtest / "state.json").write_text(
        json.dumps({"iterationCount": 1}),
        encoding="utf-8",
    )
    ctx = SimpleNamespace(
        config=Config.from_env(),
        repo_root=repo_root,
        state=SimpleNamespace(workspace=SimpleNamespace(root=game_dir)),
    )
    _write_pipeline_state(
        {
            "phase": "startup",
            "genre": "custom",
            "interaction_profile": "none",
            "quality_profile": "responsive",
            "level_model": "none",
        },
        ctx,
    )

    hook = (
        Path(__file__).parents[4]
        / ".claude"
        / "hooks"
        / "validation"
        / "validate-state-json.py"
    )
    passed = subprocess.run(
        [sys.executable, str(hook), str(game_dir)],
        text=True,
        capture_output=True,
        check=False,
    )
    assert passed.returncode == 0, passed.stdout + passed.stderr

    state_path = playtest / "state.json"
    state = json.loads(state_path.read_text())
    state["iteration_count"] = 2
    state_path.write_text(json.dumps(state), encoding="utf-8")
    rejected = subprocess.run(
        [sys.executable, str(hook), str(game_dir)],
        text=True,
        capture_output=True,
        check=False,
    )
    assert rejected.returncode == 1
    assert "this pipeline_run_id only has 0 iteration_complete" in rejected.stdout

    state["iteration_count"] = 3
    state["iteration_budget"] = {
        "max_iterations": 2,
        "used_iterations": 3,
        "remaining_iterations": 0,
        "exhausted": True,
    }
    state_path.write_text(json.dumps(state), encoding="utf-8")
    over_budget = subprocess.run(
        [sys.executable, str(hook), str(game_dir)],
        text=True,
        capture_output=True,
        check=False,
    )
    assert over_budget.returncode == 1
    assert "exceeds pipeline.max_iterations=2" in over_budget.stdout


def test_audit_event_counts_ignores_malformed_lines(tmp_path) -> None:
    playtest = tmp_path / ".playtest"
    playtest.mkdir()
    (playtest / "audit.jsonl").write_text(
        "\n".join(
            [
                json.dumps({"event": "verify_run"}),
                "not-json",
                json.dumps({"event": "verify_run"}),
                json.dumps({"event": "iteration_complete"}),
            ]
        ),
        encoding="utf-8",
    )

    assert _audit_event_counts(tmp_path) == {
        "verify_run": 2,
        "iteration_complete": 1,
    }


def test_state_and_resume_share_atomic_run_identity(tmp_path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src/main.js").write_text("export const version = 1;\n")
    ctx = SimpleNamespace(
        config=Config.from_env(),
        repo_root=tmp_path,
        state=SimpleNamespace(workspace=SimpleNamespace(root=tmp_path)),
    )

    first = _write_pipeline_state(
        {
            "phase": "startup",
            "status": "completed",
            "next_action": "design",
            "quality_profile": "responsive",
            "level_model": "none",
        },
        ctx,
    )
    state = json.loads((tmp_path / ".playtest/state.json").read_text())
    resume = json.loads((tmp_path / ".playtest/resume.json").read_text())
    assert state["pipeline_run_id"] == resume["pipeline_run_id"]
    assert state["build_digest"] == resume["build_digest"]
    assert state["state_transaction_id"] == resume["state_transaction_id"]
    assert state["quality_profile"] == resume["quality_profile"] == "responsive"
    assert state["level_model"] == resume["level_model"] == "none"
    assert state["level_design_required"] is False
    assert "has_levels" not in state
    assert "level_files" not in state
    assert "level_files" not in resume
    expected_budget = {
        "max_iterations": 2,
        "used_iterations": 0,
        "remaining_iterations": 2,
        "exhausted": False,
    }
    assert state["iteration_budget"] == expected_budget
    assert resume["iteration_budget"] == expected_budget
    assert first["iteration_budget"] == expected_budget
    assert first["pipeline_run_id"] == state["pipeline_run_id"]

    old_digest = state["build_digest"]
    (tmp_path / "src/main.js").write_text("export const version = 2;\n")
    second = _write_pipeline_state(
        {"phase": "build", "status": "in_progress"},
        ctx,
    )
    assert second["pipeline_run_id"] == first["pipeline_run_id"]
    assert second["build_digest"] != old_digest
    entries = [
        json.loads(line)
        for line in (tmp_path / ".playtest/audit.jsonl").read_text().splitlines()
    ]
    assert all(entry["pipeline_run_id"] == first["pipeline_run_id"] for entry in entries)
    assert all(entry["build_digest"].startswith("sha256:") for entry in entries)


def test_authored_level_files_are_dynamic_atomic_and_preserved(tmp_path) -> None:
    levels = tmp_path / "levels"
    levels.mkdir()
    stage_paths = [
        levels / "stage_1.json",
        levels / "stage_7.json",
    ]
    for path in stage_paths:
        path.write_text("{}\n", encoding="utf-8")
    requested = [str(path.resolve()) for path in stage_paths]
    ctx = SimpleNamespace(
        config=Config.from_env(),
        repo_root=tmp_path,
        state=SimpleNamespace(workspace=SimpleNamespace(root=tmp_path)),
    )

    _write_pipeline_state(
        {
            "phase": "startup",
            "quality_profile": "responsive",
            "level_model": "authored",
        },
        ctx,
    )
    _write_pipeline_state(
        {
            "phase": "level_design",
            "has_levels": True,
            "level_files": requested,
        },
        ctx,
    )
    _write_pipeline_state(
        {
            "phase": "scaffold",
            "details": {"workflow": "scaffold-test"},
        },
        ctx,
    )

    state = json.loads((tmp_path / ".playtest/state.json").read_text())
    resume = json.loads((tmp_path / ".playtest/resume.json").read_text())
    assert state["level_files"] == requested
    assert resume["level_files"] == requested
    assert resume["details"] == {"workflow": "scaffold-test"}
    with pytest.raises(ToolError, match="cannot change from true to false"):
        _write_pipeline_state(
            {
                "phase": "scaffold",
                "has_levels": False,
            },
            ctx,
        )


def test_authored_level_revision_replaces_the_complete_inventory(tmp_path) -> None:
    levels = tmp_path / "levels"
    levels.mkdir()
    first = levels / "stage_1.json"
    first.write_text("{}\n", encoding="utf-8")
    ctx = SimpleNamespace(
        config=Config.from_env(),
        repo_root=tmp_path,
        state=SimpleNamespace(workspace=SimpleNamespace(root=tmp_path)),
    )
    _write_pipeline_state(
        {
            "phase": "startup",
            "quality_profile": "minimal",
            "level_model": "authored",
        },
        ctx,
    )
    _write_pipeline_state(
        {
            "phase": "level_design",
            "has_levels": True,
            "level_files": [str(first.resolve())],
        },
        ctx,
    )

    revised = levels / "stage_9.json"
    revised.write_text("{}\n", encoding="utf-8")
    first.unlink()
    _write_pipeline_state(
        {
            "phase": "iteration",
            "level_files": [str(revised.resolve())],
        },
        ctx,
    )

    state = json.loads((tmp_path / ".playtest/state.json").read_text())
    resume = json.loads((tmp_path / ".playtest/resume.json").read_text())
    assert state["level_files"] == [str(revised.resolve())]
    assert resume["level_files"] == state["level_files"]


def test_authored_level_files_reject_incomplete_duplicate_and_outside_paths(
    tmp_path,
) -> None:
    levels = tmp_path / "levels"
    levels.mkdir()
    stage_one = levels / "stage_1.json"
    stage_two = levels / "stage_2.json"
    stage_one.write_text("{}\n", encoding="utf-8")
    stage_two.write_text("{}\n", encoding="utf-8")
    outside = tmp_path / "other" / "stage_3.json"
    outside.parent.mkdir()
    outside.write_text("{}\n", encoding="utf-8")
    ctx = SimpleNamespace(
        config=Config.from_env(),
        repo_root=tmp_path,
        state=SimpleNamespace(workspace=SimpleNamespace(root=tmp_path)),
    )
    _write_pipeline_state(
        {
            "phase": "startup",
            "quality_profile": "minimal",
            "level_model": "authored",
        },
        ctx,
    )

    with pytest.raises(ToolError, match="complete current authored stage inventory"):
        _write_pipeline_state(
            {
                "phase": "level_design",
                "has_levels": True,
                "level_files": [str(stage_one.resolve())],
            },
            ctx,
        )
    with pytest.raises(ToolError, match="duplicate path"):
        _write_pipeline_state(
            {
                "phase": "level_design",
                "has_levels": True,
                "level_files": [
                    str(stage_one.resolve()),
                    str(stage_one.resolve()),
                ],
            },
            ctx,
        )
    with pytest.raises(ToolError, match="progression"):
        _write_pipeline_state(
            {
                "phase": "level_design",
                "has_levels": True,
                "level_files": [
                    str(stage_two.resolve()),
                    str(stage_one.resolve()),
                ],
            },
            ctx,
        )
    with pytest.raises(ToolError, match="directly under"):
        _write_pipeline_state(
            {
                "phase": "level_design",
                "has_levels": True,
                "level_files": [str(outside.resolve())],
            },
            ctx,
        )


def test_legacy_authored_state_requires_level_inventory_migration(tmp_path) -> None:
    levels = tmp_path / "levels"
    levels.mkdir()
    (levels / "stage_1.json").write_text("{}\n", encoding="utf-8")
    playtest = tmp_path / ".playtest"
    playtest.mkdir()
    (playtest / "state.json").write_text(
        json.dumps(
            {
                "genre": "custom",
                "interaction_profile": "none",
                "quality_profile": "minimal",
                "level_model": "authored",
                "has_levels": True,
            }
        ),
        encoding="utf-8",
    )
    ctx = SimpleNamespace(
        config=Config.from_env(),
        repo_root=tmp_path,
        state=SimpleNamespace(workspace=SimpleNamespace(root=tmp_path)),
    )

    with pytest.raises(ToolError, match="no level_files inventory"):
        _write_pipeline_state({"phase": "resume"}, ctx)


def test_authored_level_directory_symlink_is_rejected(tmp_path) -> None:
    outside = tmp_path / "outside-levels"
    outside.mkdir()
    stage = outside / "stage_1.json"
    stage.write_text("{}\n", encoding="utf-8")
    (tmp_path / "levels").symlink_to(outside, target_is_directory=True)
    ctx = SimpleNamespace(
        config=Config.from_env(),
        repo_root=tmp_path,
        state=SimpleNamespace(workspace=SimpleNamespace(root=tmp_path)),
    )
    _write_pipeline_state(
        {
            "phase": "startup",
            "quality_profile": "minimal",
            "level_model": "authored",
        },
        ctx,
    )

    with pytest.raises(ToolError, match="must not be a symlink"):
        _write_pipeline_state(
            {
                "phase": "level_design",
                "has_levels": True,
                "level_files": [str(tmp_path / "levels/stage_1.json")],
            },
            ctx,
        )


def test_non_authored_state_rejects_level_files(tmp_path) -> None:
    ctx = SimpleNamespace(
        config=Config.from_env(),
        repo_root=tmp_path,
        state=SimpleNamespace(workspace=SimpleNamespace(root=tmp_path)),
    )
    with pytest.raises(ToolError, match="only for level_model='authored'"):
        _write_pipeline_state(
            {
                "phase": "startup",
                "quality_profile": "minimal",
                "level_model": "none",
                "level_files": [str(tmp_path / "levels/stage_1.json")],
            },
            ctx,
        )


def test_pipeline_state_schema_keeps_level_inventory_dynamic() -> None:
    tool = next(
        item for item in lifecycle.TOOLS
        if item.name == "write_pipeline_state"
    )
    level_files = tool.input_schema["properties"]["level_files"]
    assert level_files["type"] == "array"
    assert level_files["minItems"] == 1
    assert level_files["uniqueItems"] is True
    assert "maxItems" not in level_files


@pytest.mark.parametrize(
    ("level_model", "required"),
    [
        ("authored", True),
        ("procedural", False),
        ("board", False),
        ("none", False),
    ],
)
def test_level_model_is_the_routing_authority(
    tmp_path,
    level_model: str,
    required: bool,
) -> None:
    ctx = SimpleNamespace(
        config=Config.from_env(),
        repo_root=tmp_path,
        state=SimpleNamespace(workspace=SimpleNamespace(root=tmp_path)),
    )
    _write_pipeline_state(
        {
            "phase": "startup",
            "quality_profile": "minimal",
            "level_model": level_model,
        },
        ctx,
    )
    state = json.loads((tmp_path / ".playtest/state.json").read_text())
    assert state["level_design_required"] is required
    assert "has_levels" not in state


def test_new_and_legacy_state_require_explicit_routing_metadata(tmp_path) -> None:
    ctx = SimpleNamespace(
        config=Config.from_env(),
        repo_root=tmp_path,
        state=SimpleNamespace(workspace=SimpleNamespace(root=tmp_path)),
    )
    with pytest.raises(ToolError, match="first pipeline state write"):
        _write_pipeline_state(
            {"phase": "startup", "quality_profile": "minimal"},
            ctx,
        )
    with pytest.raises(ToolError, match="genre, interaction_profile"):
        _write_pipeline_state_raw(
            {
                "phase": "startup",
                "quality_profile": "minimal",
                "level_model": "none",
            },
            ctx,
        )

    playtest = tmp_path / ".playtest"
    playtest.mkdir(exist_ok=True)
    (playtest / "state.json").write_text(json.dumps({
        "phase": "design",
        "level_design_required": True,
        "has_levels": True,
    }))
    with pytest.raises(ToolError, match="routing migration requires explicit"):
        _write_pipeline_state({"phase": "build"}, ctx)
    with pytest.raises(ToolError, match="missing level_model"):
        _write_pipeline_state(
            {
                "phase": "build",
                "quality_profile": "art_directed",
            },
            ctx,
        )


def test_explicit_routing_migration_overrides_legacy_booleans(tmp_path) -> None:
    playtest = tmp_path / ".playtest"
    playtest.mkdir()
    (playtest / "state.json").write_text(json.dumps({
        "phase": "design",
        "level_design_required": True,
        "has_levels": True,
    }))
    ctx = SimpleNamespace(
        config=Config.from_env(),
        repo_root=tmp_path,
        state=SimpleNamespace(workspace=SimpleNamespace(root=tmp_path)),
    )
    _write_pipeline_state(
        {
            "phase": "build",
            "quality_profile": "minimal",
            "level_model": "none",
        },
        ctx,
    )
    state = json.loads((playtest / "state.json").read_text())
    assert state["genre"] == "custom"
    assert state["interaction_profile"] == "none"
    assert state["quality_profile"] == "minimal"
    assert state["level_model"] == "none"
    assert state["level_design_required"] is False
    assert state["has_levels"] is False


def test_routing_metadata_is_immutable_and_consistent(tmp_path) -> None:
    ctx = SimpleNamespace(
        config=Config.from_env(),
        repo_root=tmp_path,
        state=SimpleNamespace(workspace=SimpleNamespace(root=tmp_path)),
    )
    _write_pipeline_state(
        {
            "phase": "startup",
            "quality_profile": "pixel_art",
            "level_model": "board",
        },
        ctx,
    )
    with pytest.raises(ToolError, match="cannot change"):
        _write_pipeline_state(
            {"phase": "design", "quality_profile": "minimal"},
            ctx,
        )
    with pytest.raises(ToolError, match="cannot change"):
        _write_pipeline_state(
            {"phase": "design", "genre": "puzzle"},
            ctx,
        )
    with pytest.raises(ToolError, match="design_axes.physics"):
        _write_pipeline_state(
            {
                "phase": "design",
                "design_axes": {"physics": "arcade-gravity"},
            },
            ctx,
        )
    with pytest.raises(ToolError, match="level_design_required"):
        _write_pipeline_state(
            {"phase": "design", "level_design_required": True},
            ctx,
        )
    with pytest.raises(ToolError, match="has_levels=true"):
        _write_pipeline_state(
            {"phase": "design", "has_levels": True},
            ctx,
        )


def test_release_waiver_is_bound_to_current_build(tmp_path) -> None:
    ctx = SimpleNamespace(
        config=Config.from_env(),
        repo_root=tmp_path,
        state=SimpleNamespace(workspace=SimpleNamespace(root=tmp_path)),
    )
    approved_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    waiver_input = {
        "id": "video-performance",
        "reason": "Operator accepts missing video evidence",
        "approved_by": "operator",
        "approved_at": approved_at,
    }
    _write_pipeline_state(
        {
            "phase": "startup",
            "quality_profile": "art_directed",
            "level_model": "none",
            "release_waivers": [waiver_input],
        },
        ctx,
    )
    state = json.loads((tmp_path / ".playtest/state.json").read_text())
    waiver = state["release_waivers"][0]
    assert waiver["pipeline_run_id"] == state["pipeline_run_id"]
    assert waiver["build_digest"] == state["build_digest"]
    assert waiver["approval_event_id"].startswith("waiver-")

    (tmp_path / "src").mkdir()
    (tmp_path / "src/main.js").write_text("export const changed = true;\n")
    with pytest.raises(ToolError, match="old waiver objects cannot be rebound"):
        _write_pipeline_state(
            {
                "phase": "build",
                "release_waivers": [waiver_input],
            },
            ctx,
        )
    renewed = {
        **waiver_input,
        "approved_at": (
            datetime.now(timezone.utc) + timedelta(seconds=2)
        ).isoformat().replace("+00:00", "Z"),
    }
    _write_pipeline_state(
        {"phase": "build", "release_waivers": [renewed]},
        ctx,
    )
    new_state = json.loads((tmp_path / ".playtest/state.json").read_text())
    assert new_state["release_waivers"][0]["build_digest"] == new_state["build_digest"]
    assert (
        new_state["release_waivers"][0]["approval_event_id"]
        != waiver["approval_event_id"]
    )


def test_interrupted_state_transaction_rolls_forward(
    tmp_path,
    monkeypatch,
) -> None:
    ctx = SimpleNamespace(
        config=Config.from_env(),
        repo_root=tmp_path,
        state=SimpleNamespace(workspace=SimpleNamespace(root=tmp_path)),
    )
    real_write = lifecycle._write_json
    failed = False

    def flaky_write(path, data):
        nonlocal failed
        if path.name == "resume.json" and not failed:
            failed = True
            raise OSError("injected projection failure")
        real_write(path, data)

    monkeypatch.setattr(lifecycle, "_write_json", flaky_write)
    with pytest.raises(OSError, match="injected"):
        _write_pipeline_state(
            {
                "phase": "startup",
                "quality_profile": "art_directed",
                "level_model": "none",
            },
            ctx,
        )
    assert (tmp_path / ".playtest/state-transaction.pending.json").is_file()

    monkeypatch.setattr(lifecycle, "_write_json", real_write)
    _write_pipeline_state({"phase": "design"}, ctx)
    assert not (tmp_path / ".playtest/state-transaction.pending.json").exists()
    state = json.loads((tmp_path / ".playtest/state.json").read_text())
    resume = json.loads((tmp_path / ".playtest/resume.json").read_text())
    assert state["state_transaction_id"] == resume["state_transaction_id"]


def test_first_state_transaction_rolls_forward_during_resume(
    tmp_path,
    monkeypatch,
) -> None:
    repo = tmp_path / "repo"
    (repo / "playground").mkdir(parents=True)
    server_state = ServerState(config=Config.from_env(), repo_root=repo)
    ctx = ToolCtx(state=server_state)
    opened = _init_workspace(
        {"path": "playground/game", "mode": "create"},
        ctx,
    )
    game_dir = Path(opened["workspace"]["root"])
    real_write = lifecycle._write_json
    failed = False

    def fail_first_state_projection(path, data):
        nonlocal failed
        if path.name == "state.json" and not failed:
            failed = True
            raise OSError("injected first-state projection failure")
        real_write(path, data)

    monkeypatch.setattr(lifecycle, "_write_json", fail_first_state_projection)
    with pytest.raises(OSError, match="first-state"):
        _write_pipeline_state(
            {
                "phase": "startup",
                "status": "in_progress",
                "quality_profile": "responsive",
                "level_model": "none",
            },
            ctx,
        )
    pending_path = game_dir / ".playtest/state-transaction.pending.json"
    assert pending_path.is_file()
    assert not (game_dir / ".playtest/state.json").exists()
    _close_workspace({}, ctx)

    monkeypatch.setattr(lifecycle, "_write_json", real_write)
    resumed_state = ServerState(config=Config.from_env(), repo_root=repo)
    resumed_ctx = ToolCtx(state=resumed_state)
    resumed = _init_workspace(
        {"path": "playground/game", "mode": "resume"},
        resumed_ctx,
    )
    assert resumed["mode"] == "resume"
    assert not pending_path.exists()
    state = json.loads((game_dir / ".playtest/state.json").read_text())
    resume = json.loads((game_dir / ".playtest/resume.json").read_text())
    assert state["state_transaction_id"] == resume["state_transaction_id"]
    assert state["phase_status"] == resume["status"] == "in_progress"
    _close_workspace({}, resumed_ctx)


def test_resume_rejects_invalid_pending_transaction_before_existing_state(
    tmp_path,
) -> None:
    repo = tmp_path / "repo"
    (repo / "playground").mkdir(parents=True)
    state = ServerState(config=Config.from_env(), repo_root=repo)
    ctx = ToolCtx(state=state)
    opened = _init_workspace(
        {"path": "playground/game", "mode": "create"},
        ctx,
    )
    game_dir = Path(opened["workspace"]["root"])
    _write_pipeline_state(
        {
            "phase": "startup",
            "status": "in_progress",
            "quality_profile": "minimal",
            "level_model": "none",
        },
        ctx,
    )
    state_before = (game_dir / ".playtest/state.json").read_text()
    _close_workspace({}, ctx)

    pending_path = game_dir / ".playtest/state-transaction.pending.json"
    pending_path.write_text(
        json.dumps({
            "schema_version": 1,
            "state_transaction_id": "state-invalid",
            "state": {},
            "resume": {},
        }),
        encoding="utf-8",
    )
    resumed_state = ServerState(config=Config.from_env(), repo_root=repo)
    resumed_ctx = ToolCtx(state=resumed_state)
    with pytest.raises(ToolError) as exc_info:
        _init_workspace(
            {"path": "playground/game", "mode": "resume"},
            resumed_ctx,
        )
    assert exc_info.value.code == "INVALID_STATE_TRANSACTION"
    assert (game_dir / ".playtest/state.json").read_text() == state_before
    assert resumed_state.workspace is None

    # A failed resume must release its lease; removing only the invalid journal
    # allows the untouched committed state to reopen.
    pending_path.unlink()
    reopened = _init_workspace(
        {"path": "playground/game", "mode": "resume"},
        resumed_ctx,
    )
    assert reopened["mode"] == "resume"
    _close_workspace({}, resumed_ctx)


def test_resume_without_pending_journal_does_not_invent_state(tmp_path) -> None:
    repo = tmp_path / "repo"
    playtest = repo / "playground/game/.playtest"
    playtest.mkdir(parents=True)
    resume_path = playtest / "resume.json"
    resume_path.write_text('{"orphaned": true}\n', encoding="utf-8")

    state = ServerState(config=Config.from_env(), repo_root=repo)
    with pytest.raises(ToolError) as exc_info:
        _init_workspace(
            {"path": "playground/game", "mode": "resume"},
            ToolCtx(state=state),
        )
    assert exc_info.value.code == "WORKSPACE_NOT_RESUMABLE"
    assert json.loads(resume_path.read_text()) == {"orphaned": True}
    assert not (playtest / "state.json").exists()


def test_clean_scaffold_refreshes_digest_before_gate_and_completion(
    tmp_path,
    monkeypatch,
) -> None:
    repo = tmp_path / "repo"
    (repo / "playground").mkdir(parents=True)
    server_state = ServerState(config=Config.from_env(), repo_root=repo)
    ctx = ToolCtx(state=server_state)
    opened = _init_workspace(
        {"path": "playground/game", "mode": "create"},
        ctx,
    )
    game_dir = Path(opened["workspace"]["root"])
    _write_pipeline_state(
        {
            "phase": "startup",
            "status": "in_progress",
            "quality_profile": "responsive",
            "level_model": "none",
        },
        ctx,
    )

    # Scaffold writes canonical build inputs after Startup sealed its digest.
    (game_dir / "index.html").write_text(
        '<script src="https://cdn.jsdelivr.net/npm/phaser@4"></script>\n',
        encoding="utf-8",
    )
    (game_dir / "src").mkdir()
    (game_dir / "src/main.js").write_text(
        "window.__phaserGame = {};\n",
        encoding="utf-8",
    )
    with pytest.raises(ToolError) as stale:
        verify_tools._gate_check({"gate": "scaffold_done"}, ctx)
    assert stale.value.code == "STALE_PIPELINE_STATE"

    refreshed = _write_pipeline_state(
        {
            "phase": "scaffold",
            "current_step": "scaffold_gate",
            "status": "in_progress",
        },
        ctx,
    )
    assert refreshed["build_digest"] == compute_build_digest(
        game_dir,
        repo / "gca.config.yaml",
    )
    observed_statuses: list[str] = []

    def passing_gate(_script, _cli, *, cwd, check):
        assert cwd == game_dir
        assert check is False
        persisted = json.loads((game_dir / ".playtest/state.json").read_text())
        observed_statuses.append(persisted["phase_status"])
        assert persisted["build_digest"] == refreshed["build_digest"]
        return {"returncode": 0, "stdout": "scaffold ok", "stderr": ""}

    monkeypatch.setattr(verify_tools, "run_node_script", passing_gate)
    gate = verify_tools._gate_check({"gate": "scaffold_done"}, ctx)
    assert gate["blocked"] is False
    assert observed_statuses == ["in_progress"]

    _write_pipeline_state(
        {
            "phase": "scaffold",
            "current_step": "implement",
            "status": "completed",
        },
        ctx,
    )
    completed = json.loads((game_dir / ".playtest/state.json").read_text())
    assert completed["phase_status"] == "completed"
    assert "scaffold" in completed["completed_phases"]
    assert completed["build_digest"] == refreshed["build_digest"]
    _close_workspace({}, ctx)


def test_concurrent_state_writes_serialize_one_transaction(
    tmp_path,
    monkeypatch,
) -> None:
    ctx = SimpleNamespace(
        config=Config.from_env(),
        repo_root=tmp_path,
        state=SimpleNamespace(workspace=SimpleNamespace(root=tmp_path)),
    )
    _write_pipeline_state(
        {
            "phase": "startup",
            "quality_profile": "responsive",
            "level_model": "none",
        },
        ctx,
    )
    real_write = lifecycle._write_json

    def slow_write(path, data):
        if path.name == "state.json":
            time.sleep(0.02)
        real_write(path, data)

    monkeypatch.setattr(lifecycle, "_write_json", slow_write)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(
            pool.map(
                lambda phase: _write_pipeline_state(
                    {"phase": phase, "status": "in_progress"},
                    ctx,
                ),
                ("design", "build"),
            )
        )

    state = json.loads((tmp_path / ".playtest/state.json").read_text())
    resume = json.loads((tmp_path / ".playtest/resume.json").read_text())
    assert state["state_transaction_id"] == resume["state_transaction_id"]
    assert state["pipeline_run_id"] == resume["pipeline_run_id"]
    assert state["phase"] == resume["phase"]
    assert {result["phase"] for result in results} == {"design", "build"}
    assert not (tmp_path / ".playtest/state-transaction.pending.json").exists()


def test_audit_counts_can_be_scoped_to_pipeline_run(tmp_path) -> None:
    playtest = tmp_path / ".playtest"
    playtest.mkdir()
    (playtest / "audit.jsonl").write_text(
        "\n".join([
            json.dumps({"event": "verify_run", "pipeline_run_id": "run-a"}),
            json.dumps({"event": "verify_run", "pipeline_run_id": "run-b"}),
            json.dumps({"event": "iteration_complete", "pipeline_run_id": "run-a"}),
        ]),
        encoding="utf-8",
    )
    assert _audit_event_counts(tmp_path, "run-a") == {
        "verify_run": 1,
        "iteration_complete": 1,
    }
