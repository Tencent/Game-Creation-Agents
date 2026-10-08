from __future__ import annotations

import json
import os
from concurrent.futures import ThreadPoolExecutor
from multiprocessing import get_context
from pathlib import Path
from types import SimpleNamespace

import pytest
from verify_test_helpers import (
    make_tool_ctx as _ctx,
)
from verify_test_helpers import (
    prepare_begin_ready as _prepare_begin_ready,
)
from verify_test_helpers import (
    prepare_epoch as _prepare_epoch,
)
from verify_test_helpers import (
    runtime_evidence as _runtime_evidence,
)
from verify_test_helpers import (
    write_pipeline_identity as _write_pipeline_identity,
)

from gca_mcp import playtest_layout as layout
from gca_mcp.config import Config
from gca_mcp.errors import ToolError
from gca_mcp.evidence import compute_build_digest
from gca_mcp.tools import verify_tools


def _begin_in_subprocess(root, ready, start, results):
    ready.put(True)
    if not start.wait(timeout=10):
        results.put(("timeout", None))
        return
    try:
        result = verify_tools._begin_verify_run(
            {"mode": "iterate", "url": "http://localhost:8080/"},
            _ctx(Path(root)),
        )
        results.put(("ok", result["verify_run_id"]))
    except ToolError as exc:
        results.put((exc.code, None))


def test_layout_materializer_does_not_reuse_old_alignment_on_write_failure(
    tmp_path, monkeypatch
):
    ctx, run, _current = _prepare_epoch(tmp_path)
    alignment_path = tmp_path / ".playtest" / layout.LAYOUT_ALIGNMENT
    alignment_path.parent.mkdir(parents=True)
    alignment_path.write_text(
        json.dumps({"status": "passed", "aligned": True, "run_id": "old"}),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        verify_tools,
        "run_node_script",
        lambda *args, **kwargs: {
            "returncode": 0,
            "stdout": json.dumps(
                {
                    "gate": "materialize_layout_alignment",
                    "blocked": False,
                    "wrote": False,
                    "blocking_messages": [],
                }
            ),
            "stderr": "",
        },
    )

    result = verify_tools._materialize_layout_alignment(
        {
            "game_dir": str(tmp_path),
            "verify_run_id": run["verify_run_id"],
        },
        ctx,
    )

    assert result["blocked"] is True
    assert result["alignment_missing"] is False
    assert result["alignment_not_current"] is True
    assert "aligned" not in result
    assert "run_id" not in result


def test_play_game_rejects_outer_timeout_that_preempts_watchdog(tmp_path):
    ctx, run, _current = _prepare_epoch(tmp_path)

    with pytest.raises(ToolError, match="max_wall"):
        verify_tools._play_game(
            {
                "url": run["url"],
                "verify_run_id": run["verify_run_id"],
                "max_wall": 300,
                "timeout_seconds": 314,
            },
            ctx,
        )


def test_begin_verify_run_clears_current_outputs_and_seals_identity(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src/main.js").write_text("export const build = 1;\n")
    playtest = tmp_path / ".playtest"
    old_report = playtest / layout.VERIFY_REPORT
    old_report.parent.mkdir(parents=True)
    old_report.write_text(json.dumps({"status": "old"}))
    old_screenshot = playtest / layout.TEST_SCREENSHOTS_DIR / "old.png"
    old_screenshot.parent.mkdir(parents=True)
    old_screenshot.write_bytes(b"old")
    (tmp_path / "assets").mkdir()
    (tmp_path / "assets/asset-manifest.json").write_text(json.dumps({"assets": []}))

    digest = compute_build_digest(tmp_path, tmp_path / "gca.config.yaml")
    _write_pipeline_identity(tmp_path, digest)
    ctx = SimpleNamespace(
        config=Config.from_env(),
        repo_root=tmp_path,
        state=SimpleNamespace(workspace=SimpleNamespace(root=tmp_path)),
    )
    verify_tools._write_asset_integration_report(
        {"game_dir": str(tmp_path)},
        ctx,
    )
    integration = playtest / layout.ASSET_INTEGRATION_REPORT
    result = verify_tools._begin_verify_run(
        {"game_dir": str(tmp_path), "mode": "iterate", "url": "http://localhost"},
        ctx,
    )

    assert not old_report.exists()
    assert not old_screenshot.exists()
    assert result["pipeline_run_id"] == "pipeline-test"
    assert result["verify_run_id"].startswith("verify-")
    sealed = json.loads(integration.read_text())
    assert sealed["verify_run_id"] == result["verify_run_id"]
    current = json.loads(
        (playtest / layout.VERIFY_DIR / "current-run.json").read_text()
    )
    assert current["build_digest"] == digest
    assert current["quality_profile"] == "art_directed"
    assert current["level_model"] == "none"
    assert current["status"] == "active"
    assert current["url"] == "http://localhost/"
    assert (playtest / layout.RUNTIME_TOKEN_RECEIPT).is_file()
    assert compute_build_digest(tmp_path, tmp_path / "gca.config.yaml") == digest
    with pytest.raises(ToolError, match="mode must match begin_verify_run"):
        verify_tools._write_verify_report(
            {
                "game_dir": str(tmp_path),
                "mode": "full",
                "url": result["url"],
                "verify_run_id": result["verify_run_id"],
            },
            ctx,
        )


def test_begin_verify_run_rejects_stale_pipeline_digest(tmp_path):
    (tmp_path / "src").mkdir()
    source = tmp_path / "src/main.js"
    source.write_text("export const build = 1;\n")
    playtest = tmp_path / ".playtest"
    playtest.mkdir()
    digest = compute_build_digest(tmp_path, tmp_path / "gca.config.yaml")
    _write_pipeline_identity(tmp_path, digest)
    source.write_text("export const build = 2;\n")
    ctx = SimpleNamespace(
        config=Config.from_env(),
        repo_root=tmp_path,
        state=SimpleNamespace(workspace=SimpleNamespace(root=tmp_path)),
    )
    with pytest.raises(ToolError, match="Build inputs changed"):
        verify_tools._begin_verify_run(
            {
                "game_dir": str(tmp_path),
                "mode": "iterate",
                "url": "http://localhost:8080/",
            },
            ctx,
        )


def test_begin_verify_run_rejects_stale_asset_integration(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src/main.js").write_text("export const build = 1;\n")
    playtest = tmp_path / ".playtest"
    (tmp_path / "assets").mkdir()
    (tmp_path / "assets/asset-manifest.json").write_text(json.dumps({"assets": []}))
    digest = compute_build_digest(tmp_path, tmp_path / "gca.config.yaml")
    _write_pipeline_identity(tmp_path, digest)
    ctx = SimpleNamespace(
        config=Config.from_env(),
        repo_root=tmp_path,
        state=SimpleNamespace(workspace=SimpleNamespace(root=tmp_path)),
    )
    verify_tools._write_asset_integration_report(
        {"game_dir": str(tmp_path)},
        ctx,
    )
    integration = playtest / layout.ASSET_INTEGRATION_REPORT
    os.utime(integration, (1, 1))

    with pytest.raises(ToolError, match="asset-integration-report"):
        verify_tools._begin_verify_run(
            {
                "game_dir": str(tmp_path),
                "mode": "iterate",
                "url": "http://localhost:8080/",
            },
            ctx,
        )
    assert not (playtest / layout.VERIFY_RUN).exists()


def test_begin_failure_never_publishes_an_orphan_active_epoch(
    tmp_path,
    monkeypatch,
):
    ctx = _prepare_begin_ready(tmp_path)
    monkeypatch.setattr(
        verify_tools,
        "_seal_slot_receipts",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            ToolError("INVALID_SLOT_VALIDATION", "invalid slot")
        ),
    )

    with pytest.raises(ToolError, match="invalid slot"):
        verify_tools._begin_verify_run(
            {"mode": "iterate", "url": "http://localhost:8080/"},
            ctx,
        )

    assert not (tmp_path / ".playtest" / layout.VERIFY_RUN).exists()


def test_begin_fails_closed_on_malformed_current_run(tmp_path):
    ctx = _prepare_begin_ready(tmp_path)
    current = tmp_path / ".playtest" / layout.VERIFY_RUN
    current.parent.mkdir(parents=True, exist_ok=True)
    current.write_text("{malformed", encoding="utf-8")
    marker = tmp_path / ".playtest" / layout.TEST_RUN_DIR / "keep.txt"
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text("keep", encoding="utf-8")

    with pytest.raises(ToolError, match="invalid JSON"):
        verify_tools._begin_verify_run(
            {"mode": "iterate", "url": "http://localhost:8080/"},
            ctx,
        )

    assert marker.read_text(encoding="utf-8") == "keep"


def test_asset_integration_receipt_derives_missing_and_unwired_failures(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src/main.js").write_text("export const build = 1;\n")
    (tmp_path / "assets").mkdir()
    (tmp_path / "assets/asset-manifest.json").write_text(
        json.dumps(
            {
                "assets": [
                    {
                        "key": "ui-logo",
                        "category": "ui",
                        "sourceStrategy": "generated-image",
                        "path": "assets/ui/logo.png",
                    }
                ],
            }
        )
    )
    digest = compute_build_digest(tmp_path, tmp_path / "gca.config.yaml")
    _write_pipeline_identity(tmp_path, digest)
    ctx = SimpleNamespace(
        config=Config.from_env(),
        repo_root=tmp_path,
        state=SimpleNamespace(workspace=SimpleNamespace(root=tmp_path)),
    )
    report = verify_tools._write_asset_integration_report({}, ctx)
    assert report["producer"] == "gca-mcp.write_asset_integration_report"
    assert report["summary"]["manifest_entries"] == 1
    assert report["summary"]["blocking_issue_count"] == 2
    with pytest.raises(ToolError, match="MCP-owned receipt"):
        verify_tools._begin_verify_run(
            {"mode": "iterate", "url": "http://localhost:8080/"},
            ctx,
        )


def test_nested_manifest_assets_are_integrated_and_build_bound(tmp_path):
    (tmp_path / "src").mkdir()
    runtime_path = "assets/backgrounds/sky.png"
    (tmp_path / "src/main.js").write_text(
        f"scene.load.image('sky', '{runtime_path}');\n"
    )
    image = tmp_path / runtime_path
    image.parent.mkdir(parents=True)
    image.write_bytes(b"sky-v1")
    (tmp_path / "assets/asset-manifest.json").write_text(
        json.dumps(
            {
                "backgrounds": {
                    "sky": {
                        "sourceStrategy": "generated-image",
                        "description": "nested sky",
                        "path": runtime_path,
                    }
                }
            }
        )
    )
    first_digest = compute_build_digest(tmp_path, tmp_path / "gca.config.yaml")
    image.write_bytes(b"sky-v2")
    second_digest = compute_build_digest(tmp_path, tmp_path / "gca.config.yaml")
    assert second_digest != first_digest
    _write_pipeline_identity(tmp_path, second_digest)

    report = verify_tools._write_asset_integration_report({}, _ctx(tmp_path))
    assert report["summary"] == {
        "manifest_entries": 1,
        "files_checked": 1,
        "assets_wired": 1,
        "blocking_issue_count": 0,
    }


@pytest.mark.parametrize(
    "path_value",
    [
        "assets/%2e%2e/runtime.png",
        "assets/ui/runtime.png?version=1",
        "assets/ui/runtime.png#fragment",
        "assets\\ui\\runtime.png",
        "/assets/ui/runtime.png",
        "assets/ui/runtime image.png",
        "assets/ui/runtime\x01.png",
    ],
)
def test_asset_integration_receipt_rejects_noncanonical_runtime_paths(
    tmp_path,
    path_value,
):
    (tmp_path / "src").mkdir()
    (tmp_path / "src/main.js").write_text("export const build = 1;\n")
    (tmp_path / "assets").mkdir()
    (tmp_path / "assets/asset-manifest.json").write_text(
        json.dumps(
            {
                "assets": [
                    {
                        "key": "ui-runtime",
                        "path": path_value,
                    }
                ]
            }
        )
    )
    digest = compute_build_digest(tmp_path, tmp_path / "gca.config.yaml")
    _write_pipeline_identity(tmp_path, digest)
    report = verify_tools._write_asset_integration_report({}, _ctx(tmp_path))
    path_issues = [
        issue
        for issue in report["blocking_issues"]
        if issue["category"] == "missing_manifest_path"
    ]
    assert len(path_issues) == 1
    assert "unsafe/non-canonical" in path_issues[0]["description"]


def test_release_gate_requires_verify_epoch_identity(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src/main.js").write_text("export const build = 1;\n")
    playtest = tmp_path / ".playtest"
    playtest.mkdir()
    digest = compute_build_digest(tmp_path, tmp_path / "gca.config.yaml")
    _write_pipeline_identity(tmp_path, digest)
    ctx = SimpleNamespace(
        config=Config.from_env(),
        repo_root=tmp_path,
        state=SimpleNamespace(workspace=SimpleNamespace(root=tmp_path)),
    )
    with pytest.raises(ToolError, match="begin_verify_run"):
        verify_tools._gate_check(
            {"game_dir": str(tmp_path), "gate": "ready_to_ship"},
            ctx,
        )


def test_step_done_phase_7_requires_and_seals_verify_epoch(
    tmp_path,
    monkeypatch,
):
    ctx, run, _current = _prepare_epoch(tmp_path, mode="full")
    with pytest.raises(ToolError, match="not allowed for this operation"):
        verify_tools._gate_check(
            {
                "gate": "step_done",
                "step": 7,
                "verify_run_id": run["verify_run_id"],
            },
            ctx,
        )

    verify_tools._write_verify_report(
        {
            "mode": "full",
            "url": run["url"],
            "verify_run_id": run["verify_run_id"],
        },
        ctx,
    )
    monkeypatch.setattr(
        verify_tools,
        "run_node_script",
        lambda *_args, **_kwargs: {
            "returncode": 0,
            "stdout": json.dumps(
                {"gate": "step_done", "blocked": False, "failures": []}
            ),
            "stderr": "",
        },
    )

    with pytest.raises(ToolError):
        verify_tools._gate_check(
            {"gate": "step_done", "step": 7},
            ctx,
        )
    with pytest.raises(ToolError):
        verify_tools._gate_check(
            {
                "gate": "step_done",
                "step": 7,
                "verify_run_id": "verify-wrong",
            },
            ctx,
        )

    gate = verify_tools._gate_check(
        {
            "gate": "step_done",
            "step": 7,
            "verify_run_id": run["verify_run_id"],
        },
        ctx,
    )
    assert gate["verify_run_id"] == run["verify_run_id"]
    assert gate["step"] == 7
    receipt = json.loads(
        (tmp_path / gate["result_path"]).read_text(encoding="utf-8")
    )
    assert receipt["verify_run_id"] == run["verify_run_id"]
    assert receipt["step"] == 7
    assert gate["result_path"].endswith("gate-results/step_done-7.json")


def test_step_done_normalizes_integral_step_for_phase_specific_receipt(
    tmp_path,
    monkeypatch,
):
    ctx = _prepare_begin_ready(tmp_path)
    monkeypatch.setattr(
        verify_tools,
        "run_node_script",
        lambda *_args, **_kwargs: {
            "returncode": 0,
            "stdout": json.dumps(
                {"gate": "step_done", "blocked": False, "failures": []}
            ),
            "stderr": "",
        },
    )

    gate = verify_tools._gate_check({"gate": "step_done", "step": 2.0}, ctx)
    assert gate["step"] == 2
    assert isinstance(gate["step"], int)
    assert gate["result_path"].endswith("gate-results/step_done-2.json")
    receipt = json.loads(
        (tmp_path / gate["result_path"]).read_text(encoding="utf-8")
    )
    assert receipt["step"] == 2
    assert receipt["gate"] == "step_done"


def test_slot_validation_gate_requires_and_records_verify_epoch(
    tmp_path,
    monkeypatch,
):
    ctx, run, _current = _prepare_epoch(tmp_path)
    monkeypatch.setattr(
        verify_tools,
        "run_node_script",
        lambda *_args, **_kwargs: {
            "returncode": 0,
            "stdout": json.dumps(
                {"gate": "slot_validation", "blocked": False, "failures": []}
            ),
            "stderr": "",
        },
    )

    with pytest.raises(ToolError):
        verify_tools._gate_check({"gate": "slot_validation"}, ctx)

    gate = verify_tools._gate_check(
        {
            "gate": "slot_validation",
            "verify_run_id": run["verify_run_id"],
        },
        ctx,
    )
    assert gate["verify_run_id"] == run["verify_run_id"]
    receipt = json.loads(
        (tmp_path / gate["result_path"]).read_text(encoding="utf-8")
    )
    assert receipt["verify_run_id"] == run["verify_run_id"]


def test_pipeline_identity_rejects_split_state_resume_snapshot(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src/main.js").write_text("export const build = 1;\n")
    digest = compute_build_digest(tmp_path, tmp_path / "gca.config.yaml")
    _write_pipeline_identity(tmp_path, digest)
    resume_path = tmp_path / ".playtest/resume.json"
    resume = json.loads(resume_path.read_text())
    resume["quality_profile"] = "minimal"
    resume_path.write_text(json.dumps(resume))
    ctx = SimpleNamespace(repo_root=tmp_path)
    with pytest.raises(ToolError, match="one identity/transaction snapshot"):
        verify_tools._require_pipeline_identity(tmp_path, ctx)

    resume["quality_profile"] = "art_directed"
    resume_path.write_text(json.dumps(resume))
    (tmp_path / ".playtest/state-transaction.pending.json").write_text("{}")
    with pytest.raises(ToolError, match="transaction is incomplete"):
        verify_tools._require_pipeline_identity(tmp_path, ctx)


def test_runtime_verification_uses_frozen_counter_migration_snapshot(tmp_path):
    digest = compute_build_digest(tmp_path, tmp_path / "gca.config.yaml")
    _write_pipeline_identity(tmp_path, digest)
    state_path = tmp_path / ".playtest/state.json"
    state = json.loads(state_path.read_text())
    state["iteration_count"] = 1
    state["audit_offsets"]["iteration_count"] = 1
    state["counter_migration"] = {
        "version": 1,
        "source": "legacy_unscoped_audit",
        "pipeline_run_id": state["pipeline_run_id"],
        "migrated_at": "2026-07-13T00:00:00Z",
        "snapshot": {
            "counters": {
                "verify_runs": 0,
                "iteration_count": 1,
            },
            "unscoped_audit_counts": {
                "verify_run": 0,
                "iteration_complete": 0,
            },
            "audit_tolerance": 1,
            "tolerated_missing": {
                "verify_runs": 0,
                "iteration_count": 1,
            },
        },
    }
    state_path.write_text(json.dumps(state), encoding="utf-8")
    ctx = SimpleNamespace(repo_root=tmp_path)

    assert (
        verify_tools._require_pipeline_identity(
            tmp_path,
            ctx,
        )["pipeline_run_id"]
        == "pipeline-test"
    )

    state["iteration_count"] = 2
    state_path.write_text(json.dumps(state), encoding="utf-8")
    with pytest.raises(ToolError, match="run-scoped `iteration_complete`"):
        verify_tools._require_pipeline_identity(tmp_path, ctx)


def test_concurrent_begin_verify_run_has_one_cas_winner(tmp_path):
    ctx = _prepare_begin_ready(tmp_path)
    args = {"mode": "iterate", "url": "http://localhost:8080/"}

    def begin():
        try:
            return ("ok", verify_tools._begin_verify_run(args, ctx))
        except ToolError as exc:
            return (exc.code, None)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _index: begin(), range(2)))

    assert [kind for kind, _ in results].count("ok") == 1
    assert [kind for kind, _ in results].count("VERIFY_RUN_ACTIVE") == 1
    current = json.loads(
        (tmp_path / ".playtest" / layout.VERIFY_RUN).read_text(encoding="utf-8")
    )
    winner = next(payload for kind, payload in results if kind == "ok")
    assert current["verify_run_id"] == winner["verify_run_id"]
    assert current["status"] == "active"


def test_begin_verify_run_explicitly_resumes_matching_active_epoch(tmp_path):
    ctx = _prepare_begin_ready(tmp_path)
    args = {"mode": "iterate", "url": "http://localhost:8080/"}
    started = verify_tools._begin_verify_run(args, ctx)
    marker = tmp_path / ".playtest" / layout.TEST_RUN_DIR / "partial.json"
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text("{}", encoding="utf-8")

    resumed = verify_tools._begin_verify_run(
        {**args, "resume_active": True},
        ctx,
    )

    assert started["resumed"] is False
    assert resumed["resumed"] is True
    assert resumed["verify_run_id"] == started["verify_run_id"]
    assert resumed["runtime_receipt_sha256"] == started["runtime_receipt_sha256"]
    assert marker.is_file()

    with pytest.raises(ToolError, match="mode must match"):
        verify_tools._begin_verify_run(
            {
                "mode": "full",
                "url": args["url"],
                "resume_active": True,
            },
            ctx,
        )
    with pytest.raises(ToolError, match="URL must match"):
        verify_tools._begin_verify_run(
            {
                "mode": args["mode"],
                "url": "http://localhost:8081/",
                "resume_active": True,
            },
            ctx,
        )


def test_begin_verify_run_blocks_new_iterate_epoch_after_budget(
    tmp_path,
) -> None:
    ctx = _prepare_begin_ready(tmp_path)
    playtest = tmp_path / ".playtest"
    state_path = playtest / "state.json"
    resume_path = playtest / "resume.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    budget = {
        "max_iterations": 2,
        "used_iterations": 2,
        "remaining_iterations": 0,
        "exhausted": True,
    }
    state["iteration_count"] = 2
    state["iteration_budget"] = budget
    state_path.write_text(json.dumps(state), encoding="utf-8")
    resume = json.loads(resume_path.read_text(encoding="utf-8"))
    resume["iteration_budget"] = budget
    resume_path.write_text(json.dumps(resume), encoding="utf-8")
    (playtest / "audit.jsonl").write_text(
        "\n".join(
            json.dumps(
                {
                    "event": "iteration_complete",
                    "pipeline_run_id": state["pipeline_run_id"],
                }
            )
            for _index in range(2)
        )
        + "\n",
        encoding="utf-8",
    )

    with pytest.raises(ToolError, match="pipeline.max_iterations=2") as error:
        verify_tools._begin_verify_run(
            {"mode": "iterate", "url": "http://localhost:8080/"},
            ctx,
        )
    assert error.value.code == "ITERATION_BUDGET_EXHAUSTED"
    assert not (playtest / layout.VERIFY_RUN).exists()

    full = verify_tools._begin_verify_run(
        {"mode": "full", "url": "http://localhost:8080/"},
        ctx,
    )
    assert full["mode"] == "full"
    assert full["iteration_budget"] == budget


def test_cross_process_begin_verify_run_has_one_flock_cas_winner(tmp_path):
    _prepare_begin_ready(tmp_path)
    mp = get_context("spawn")
    ready = mp.Queue()
    start = mp.Event()
    results = mp.Queue()
    processes = [
        mp.Process(
            target=_begin_in_subprocess,
            args=(str(tmp_path), ready, start, results),
        )
        for _ in range(2)
    ]
    for process in processes:
        process.start()
    for _ in processes:
        assert ready.get(timeout=15) is True
    start.set()
    outcomes = [results.get(timeout=30) for _ in processes]
    for process in processes:
        process.join(timeout=30)
        assert process.exitcode == 0

    assert [kind for kind, _ in outcomes].count("ok") == 1
    assert [kind for kind, _ in outcomes].count("VERIFY_RUN_ACTIVE") == 1


def test_producer_rejects_stale_id_and_epoch_url(tmp_path, monkeypatch):
    ctx, run, _current = _prepare_epoch(tmp_path)
    called = False

    def should_not_run(*_args, **_kwargs):
        nonlocal called
        called = True
        raise AssertionError("runner process must not run")

    monkeypatch.setattr(verify_tools, "run_node_script", should_not_run)
    with pytest.raises(ToolError, match="stale"):
        verify_tools._capture_runtime_assets(
            {
                "url": run["url"],
                "verify_run_id": "verify-stale",
            },
            ctx,
        )
    with pytest.raises(ToolError, match="must equal"):
        verify_tools._capture_runtime_assets(
            {
                "url": "http://localhost:9999/",
                "verify_run_id": run["verify_run_id"],
            },
            ctx,
        )
    assert called is False


def test_runtime_registry_receipt_is_token_and_manifest_bound(
    tmp_path,
    monkeypatch,
):
    ctx, run, current = _prepare_epoch(tmp_path)
    before_digest = compute_build_digest(tmp_path, tmp_path / "gca.config.yaml")
    hero_binding = {
        "scene": "GameScene",
        "texture_key": "hero",
        "object_type": "Sprite",
        "name": "hero",
        "layout_key": "hero",
        "bind": None,
        "instance": 0,
        "visible": True,
    }

    monkeypatch.setattr(
        verify_tools,
        "run_node_script",
        lambda *_args, **_kwargs: {
            "returncode": 0,
            "stdout": json.dumps(
                {
                    "runtime_identity": _runtime_evidence(current),
                    "loaded_texture_keys": ["hero", "__DEFAULT"],
                    "animation_frame_texture_keys": ["hero"],
                    "display_list_texture_keys": ["hero"],
                    "display_object_texture_bindings": [hero_binding],
                    "game_config": {"pixelArt": False},
                    "active_scene_keys": ["GameScene"],
                    "registered_scene_keys": ["BootScene", "GameScene"],
                    "reachable_scene_keys": ["BootScene", "GameScene"],
                    "scenes": [
                        {
                            "scene": "BootScene",
                            "reachable": True,
                            "texture_keys": [],
                            "texture_bindings": [],
                        },
                        {
                            "scene": "GameScene",
                            "reachable": True,
                            "texture_keys": ["hero"],
                            "texture_bindings": [hero_binding],
                        },
                    ],
                }
            ),
            "stderr": "",
        },
    )

    result = verify_tools._capture_runtime_assets(
        {
            "url": run["url"],
            "verify_run_id": run["verify_run_id"],
        },
        ctx,
    )

    receipt_path = tmp_path / result["receipt"]
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert receipt["producer"] == "gca-mcp.capture_runtime_assets"
    assert receipt["runtime_identity"]["verified"] is True
    assert receipt["runtime_token_sha256"] == run["runtime_token_sha256"]
    assert receipt["runtime_receipt_sha256"] == run["runtime_receipt_sha256"]
    assert receipt["display_list_texture_keys"] == ["hero"]
    assert receipt["display_object_texture_bindings"] == [hero_binding]
    assert receipt["game_config"] == {"pixelArt": False}
    assert receipt["summary"]["display_object_texture_binding_count"] == 1
    assert receipt["summary"]["reachable_scene_count"] == 2
    assert compute_build_digest(tmp_path, tmp_path / "gca.config.yaml") == before_digest


@pytest.mark.parametrize(
    ("loaded_keys", "expected_loaded", "expected_used"),
    [
        (["hero-idle", "hero-pose"], True, True),
        (["hero-idle"], False, False),
    ],
)
def test_runtime_registry_aggregates_per_action_contracts(
    tmp_path,
    monkeypatch,
    loaded_keys,
    expected_loaded,
    expected_used,
):
    (tmp_path / "src").mkdir()
    (tmp_path / "src/main.js").write_text(
        "\n".join(
            [
                "const heroIdle = 'hero-idle';",
                "const heroPose = 'hero-pose';",
                "const idlePath = 'assets/sprites/hero/idle-sheet.png';",
                "const posePath = 'assets/sprites/hero/pose-sheet.png';",
            ]
        ),
        encoding="utf-8",
    )
    sprite_dir = tmp_path / "assets/sprites/hero"
    sprite_dir.mkdir(parents=True)
    for action, frame_count in (("idle", 4), ("pose", 1)):
        (sprite_dir / f"{action}-sheet.png").write_bytes(b"sheet")
        (sprite_dir / f"{action}-sheet.json").write_text(
            json.dumps(
                {
                    "frameWidth": 32,
                    "frameHeight": 32,
                    "frameCount": frame_count,
                }
            ),
            encoding="utf-8",
        )
    (tmp_path / "assets/asset-manifest.json").write_text(
        json.dumps(
            {
                "assets": [
                    {
                        "key": "hero",
                        "category": "sprite",
                        "layout": "per-action",
                        "defaultAction": "idle",
                        "path": "assets/sprites/hero/idle-sheet.png",
                        "metadata": "assets/sprites/hero/idle-sheet.json",
                        "actions": {
                            action: {
                                "textureKey": f"hero-{action}",
                                "path": f"assets/sprites/hero/{action}-sheet.png",
                                "metadata": (
                                    f"assets/sprites/hero/{action}-sheet.json"
                                ),
                            }
                            for action in ("idle", "pose")
                        },
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    digest = compute_build_digest(tmp_path, tmp_path / "gca.config.yaml")
    _write_pipeline_identity(tmp_path, digest)
    ctx = _ctx(tmp_path)
    verify_tools._write_asset_integration_report({}, ctx)
    begun = verify_tools._begin_verify_run(
        {"mode": "iterate", "url": "http://localhost:8080/"},
        ctx,
    )
    current = json.loads(
        (tmp_path / ".playtest" / layout.VERIFY_RUN).read_text(encoding="utf-8")
    )
    monkeypatch.setattr(
        verify_tools,
        "run_node_script",
        lambda *_args, **_kwargs: {
            "returncode": 0,
            "stdout": json.dumps(
                {
                    "runtime_identity": _runtime_evidence(current),
                    "loaded_texture_keys": loaded_keys,
                    "animation_frame_texture_keys": ["hero-idle"],
                    "display_list_texture_keys": [],
                    "display_object_texture_bindings": [],
                    "game_config": {"pixelArt": False},
                    "active_scene_keys": [],
                    "registered_scene_keys": [],
                    "reachable_scene_keys": [],
                    "scenes": [],
                }
            ),
            "stderr": "",
        },
    )

    result = verify_tools._capture_runtime_assets(
        {
            "url": begun["url"],
            "verify_run_id": begun["verify_run_id"],
        },
        ctx,
    )
    receipt = json.loads((tmp_path / result["receipt"]).read_text(encoding="utf-8"))
    row = receipt["manifest_assets"][0]
    assert row["loaded"] is expected_loaded
    assert row["used"] is expected_used
    assert row["animation_frame_texture_keys"] == ["hero-idle"]


@pytest.mark.parametrize("variant", ["missing-game-config", "forged-binding-summary"])
def test_runtime_registry_rejects_invalid_runtime_texture_evidence(
    tmp_path,
    monkeypatch,
    variant,
):
    ctx, run, current = _prepare_epoch(tmp_path)
    binding = {
        "scene": "GameScene",
        "texture_key": "hero",
        "object_type": "Sprite",
        "name": "hero",
        "layout_key": "hero",
        "bind": None,
        "instance": 0,
        "visible": True,
    }
    captured = {
        "runtime_identity": _runtime_evidence(current),
        "loaded_texture_keys": ["hero"],
        "animation_frame_texture_keys": [],
        "display_list_texture_keys": ["hero"],
        "display_object_texture_bindings": [binding],
        "game_config": {"pixelArt": False},
        "active_scene_keys": ["GameScene"],
        "registered_scene_keys": ["GameScene"],
        "reachable_scene_keys": ["GameScene"],
        "scenes": [
            {
                "scene": "GameScene",
                "reachable": True,
                "texture_keys": ["hero"],
                "texture_bindings": [binding],
            }
        ],
    }
    if variant == "missing-game-config":
        captured.pop("game_config")
    else:
        captured["display_object_texture_bindings"] = []
    monkeypatch.setattr(
        verify_tools,
        "run_node_script",
        lambda *_args, **_kwargs: {
            "returncode": 0,
            "stdout": json.dumps(captured),
            "stderr": "",
        },
    )

    with pytest.raises(ToolError, match="pixelArt|bindings disagree"):
        verify_tools._capture_runtime_assets(
            {
                "url": run["url"],
                "verify_run_id": run["verify_run_id"],
            },
            ctx,
        )
    assert not (tmp_path / ".playtest" / layout.RUNTIME_ASSETS_RECEIPT).exists()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        pytest.param(
            "runtime_token_sha256",
            "foreign-token",
            id="wrong-served-token",
        ),
        pytest.param(
            "page_url",
            "http://localhost:9999/",
            id="redirected-runtime-page",
        ),
    ],
)
def test_runtime_registry_rejects_cross_epoch_runtime_identity(
    tmp_path,
    monkeypatch,
    field,
    value,
):
    ctx, run, current = _prepare_epoch(tmp_path)
    evidence = _runtime_evidence(current)
    evidence[field] = value
    monkeypatch.setattr(
        verify_tools,
        "run_node_script",
        lambda *_args, **_kwargs: {
            "returncode": 0,
            "stdout": json.dumps(
                {
                    "runtime_identity": evidence,
                    "loaded_texture_keys": [],
                    "animation_frame_texture_keys": [],
                    "display_list_texture_keys": [],
                    "active_scene_keys": [],
                    "registered_scene_keys": [],
                    "reachable_scene_keys": [],
                    "scenes": [],
                }
            ),
            "stderr": "",
        },
    )
    with pytest.raises(ToolError, match="another URL, workspace, or run"):
        verify_tools._capture_runtime_assets(
            {
                "url": run["url"],
                "verify_run_id": run["verify_run_id"],
            },
            ctx,
        )
    assert not (tmp_path / ".playtest" / layout.RUNTIME_ASSETS_RECEIPT).exists()


def test_write_report_finalizes_epoch_and_rejects_late_producer(
    tmp_path,
    monkeypatch,
):
    ctx, run, _current = _prepare_epoch(tmp_path)
    report = verify_tools._write_verify_report(
        {
            "mode": "iterate",
            "url": run["url"],
            "verify_run_id": run["verify_run_id"],
        },
        ctx,
    )
    assert report["run_status"] == "finalized"
    report_payload = json.loads(
        (tmp_path / ".playtest" / layout.VERIFY_REPORT).read_text(encoding="utf-8")
    )
    assert report_payload["schema_version"] == 4
    assert "legacy_recording_count" not in report_payload["summary"]
    assert "legacy_recordings" not in report_payload["artifacts"]
    current = json.loads(
        (tmp_path / ".playtest" / layout.VERIFY_RUN).read_text(encoding="utf-8")
    )
    assert current["status"] == "finalized"
    assert current["result_status"] == "blocked"
    assert current["report_sha256"] == report["report_sha256"]

    monkeypatch.setattr(
        verify_tools,
        "run_node_script",
        lambda *_args, **_kwargs: pytest.fail("finalized producer launched"),
    )
    with pytest.raises(ToolError, match="finalized"):
        verify_tools._capture_runtime_assets(
            {
                "url": run["url"],
                "verify_run_id": run["verify_run_id"],
            },
            ctx,
        )
    monkeypatch.setattr(
        verify_tools,
        "run_node_script",
        lambda *_args, **_kwargs: {
            "returncode": 0,
            "stdout": "{}",
            "stderr": "",
        },
    )
    gate = verify_tools._gate_check(
        {
            "gate": "ready_to_ship",
            "verify_run_id": run["verify_run_id"],
        },
        ctx,
    )
    assert gate["result_path"] == (".playtest/verify/gate-results/ready_to_ship.json")
    gate_receipt = json.loads(
        (tmp_path / gate["result_path"]).read_text(encoding="utf-8")
    )
    assert gate_receipt["verify_run_id"] == run["verify_run_id"]
    assert gate_receipt["blocked"] is False
    assert gate["verify_run_id"] == run["verify_run_id"]


def test_verify_tooldefs_require_explicit_epoch_identity_and_url():
    tools = {tool.name: tool for tool in verify_tools.TOOLS}
    assert {"mode", "url"} <= set(tools["begin_verify_run"].input_schema["required"])
    for name in (
        "run_tests",
        "play_game",
        "visual_diff",
        "capture_layout",
        "capture_runtime_assets",
        "write_verify_report",
    ):
        required = set(tools[name].input_schema["required"])
        assert {"url", "verify_run_id"} <= required
        assert tools[name].requires_workspace is True
    assert "verify_run_id" in set(
        tools["materialize_layout_alignment"].input_schema["required"]
    )


def test_verify_tooldefs_explain_scoped_tests_and_layout_issue_forwarding():
    tools = {tool.name: tool for tool in verify_tools.TOOLS}
    run_tests = tools["run_tests"]
    assert "selectors or a `tests` path" in run_tests.description
    properties = run_tests.input_schema["properties"]
    assert "exact canonical `.playtest/tests/` path" in properties["tests"]["description"]
    for selector in ("filter", "id", "changed"):
        description = properties[selector]["description"]
        assert "diagnostic" in description.lower()
        assert "gate evidence" in description

    materialize = tools["materialize_layout_alignment"].description
    assert "blocking_messages/issues" in materialize
    assert "run_verify_dag owns" in materialize
    assert "aggregate before the composite gate" in materialize


def test_gate_check_schema_requires_identity_for_every_epoch_gate():
    gate_check = next(tool for tool in verify_tools.TOOLS if tool.name == "gate_check")

    for gate in verify_tools._VERIFY_EPOCH_GATES:
        with pytest.raises(ToolError, match="missing required field.*verify_run_id"):
            gate_check.validate_arguments({"gate": gate})
        gate_check.validate_arguments(
            {"gate": gate, "verify_run_id": "verify-test"}
        )


def test_gate_diagnostic_delegates_to_gate_check_with_fixed_gate(monkeypatch):
    captured: dict = {}

    def _fake_gate_check(args, ctx):
        captured.update(args)
        return {"gate": args["gate"], "blocked": False, "failures": []}

    monkeypatch.setattr(verify_tools, "_gate_check", _fake_gate_check)
    handler = verify_tools._make_gate_diagnostic("ui_contract")
    out = handler({"game_dir": "playground/x"}, object())

    assert captured["gate"] == "ui_contract"
    assert captured["game_dir"] == "playground/x"
    assert out["gate"] == "ui_contract"


def test_gate_diagnostic_tooldefs_are_advisory_non_epoch():
    tools = {tool.name: tool for tool in verify_tools.TOOLS}
    for name in (
        "check_ui_contract",
        "check_td_contract",
        "check_interaction_contract",
    ):
        assert name in tools, name
        schema = tools[name].input_schema
        # Advisory diagnostics: no verify_run_id, no gate arg, workspace-scoped.
        assert "verify_run_id" not in schema["properties"]
        assert "gate" not in schema["properties"]
        assert schema.get("required", []) == []
        assert tools[name].requires_workspace is True
