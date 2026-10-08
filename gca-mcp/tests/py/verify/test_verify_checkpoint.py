from __future__ import annotations

import json

import pytest

from gca_mcp.errors import ToolError
from gca_mcp.verify_checkpoint import (
    checkpoint_issues,
    checkpoint_path,
    read_checkpoint,
    run_checkpointed,
)


def _run() -> dict[str, str]:
    return {
        "pipeline_run_id": "pipeline-test",
        "build_digest": "a" * 64,
        "verify_run_id": "verify-test",
        "mode": "iterate",
        "url": "http://localhost:8080/",
    }


def test_completed_checkpoint_is_reused_only_while_artifact_exists(tmp_path) -> None:
    run = _run()
    artifact = tmp_path / ".playtest" / "verify" / "test-run" / "test-results.json"
    artifact.parent.mkdir(parents=True)
    calls = 0
    cleanups = 0

    def producer():
        nonlocal calls
        calls += 1
        artifact.write_text(json.dumps({"ok": True}), encoding="utf-8")
        return {"result_path": str(artifact.relative_to(tmp_path)), "returncode": 0}

    def prepare_retry():
        nonlocal cleanups
        cleanups += 1

    first = run_checkpointed(
        game_dir=tmp_path,
        run=run,
        producer_name="run_tests",
        arguments={"verify_run_id": run["verify_run_id"]},
        call=producer,
        prepare_retry=prepare_retry,
        reuse_completed=True,
    )
    second = run_checkpointed(
        game_dir=tmp_path,
        run=run,
        producer_name="run_tests",
        arguments={"verify_run_id": run["verify_run_id"]},
        call=producer,
        prepare_retry=prepare_retry,
        reuse_completed=True,
    )

    assert calls == 1
    assert first["checkpoint_reused"] is False
    assert second["checkpoint_reused"] is True
    assert cleanups == 1
    artifact.unlink()

    third = run_checkpointed(
        game_dir=tmp_path,
        run=run,
        producer_name="run_tests",
        arguments={"verify_run_id": run["verify_run_id"]},
        call=producer,
        prepare_retry=prepare_retry,
        reuse_completed=True,
    )
    assert calls == 2
    assert cleanups == 2
    assert third["checkpoint_reused"] is False


def test_failed_checkpoint_preserves_error_for_partial_report(tmp_path) -> None:
    run = _run()

    with pytest.raises(ToolError, match="provider unavailable"):
        run_checkpointed(
            game_dir=tmp_path,
            run=run,
            producer_name="visual_diff",
            arguments={"verify_run_id": run["verify_run_id"]},
            call=lambda: (_ for _ in ()).throw(
                ToolError("NETWORK_ERROR", "provider unavailable")
            ),
        )

    checkpoint = read_checkpoint(checkpoint_path(tmp_path, "visual_diff"))
    assert checkpoint["status"] == "failed"
    assert checkpoint["error"]["code"] == "NETWORK_ERROR"
    assert checkpoint_issues([checkpoint]) == [
        {
            "severity": "high",
            "description": "visual_diff failed: NETWORK_ERROR",
            "root_cause": "provider unavailable",
        }
    ]


def test_blocked_checkpoint_is_rerun_instead_of_reused(tmp_path) -> None:
    run = _run()
    artifact = tmp_path / ".playtest" / "verify" / "alignment.json"
    artifact.parent.mkdir(parents=True)
    calls = 0

    def producer():
        nonlocal calls
        calls += 1
        artifact.write_text(json.dumps({"attempt": calls}), encoding="utf-8")
        return {
            "output": str(artifact.relative_to(tmp_path)),
            "blocked": calls == 1,
        }

    first = run_checkpointed(
        game_dir=tmp_path,
        run=run,
        producer_name="materialize_layout_alignment",
        arguments={"verify_run_id": run["verify_run_id"]},
        call=producer,
        reuse_completed=True,
    )
    second = run_checkpointed(
        game_dir=tmp_path,
        run=run,
        producer_name="materialize_layout_alignment",
        arguments={"verify_run_id": run["verify_run_id"]},
        call=producer,
        reuse_completed=True,
    )

    assert first["blocked"] is True
    assert second["blocked"] is False
    assert second["checkpoint_reused"] is False
    assert calls == 2
