from __future__ import annotations

import json
from functools import partial
from pathlib import Path

import pytest

from gca_mcp import playtest_layout as layout
from gca_mcp.errors import ToolError
from gca_mcp.tools import video_tools
from verify_test_helpers import prepare_epoch, runtime_evidence as _runtime_evidence

_prepare_epoch = partial(
    prepare_epoch,
    mode="full",
    pipeline_run_id="pipeline-video",
    state_transaction_id="transaction-video",
)


def _fake_record(run):
    def invoke(_script, cli, **_kwargs):
        output = Path(cli[cli.index("--output") + 1])
        output.mkdir(parents=True)
        runtime = _runtime_evidence(run)
        (output / "recording.webm").write_bytes(b"current-video")
        (output / "state-timeline.json").write_text(
            json.dumps([{"t": 0, "scenes": {}}]),
            encoding="utf-8",
        )
        (output / "performance-summary.json").write_text(
            json.dumps(
                {
                    "generated_at": "2026-07-13T00:00:00Z",
                    "runtime_identity": runtime,
                    "fps": {"p50": 60},
                }
            ),
            encoding="utf-8",
        )
        return {
            "returncode": 0,
            "stdout": json.dumps({"runtime_identity": runtime}),
            "stderr": "",
        }

    return invoke


def test_record_receipt_binds_video_timeline_url_and_runtime(
    tmp_path,
    monkeypatch,
):
    ctx, begun, current = _prepare_epoch(tmp_path)
    monkeypatch.setattr(video_tools, "run_node_script", _fake_record(current))

    result = video_tools._record_with_state(
        {
            "url": begun["url"],
            "verify_run_id": begun["verify_run_id"],
            "output": ".playtest/verify/recording/core-loop",
            "duration": 1,
        },
        ctx,
    )

    receipt = json.loads(
        (tmp_path / result["video_receipt"]).read_text(encoding="utf-8")
    )
    assert receipt["producer"] == "gca-mcp.record_with_state"
    assert receipt["url"] == begun["url"]
    assert receipt["video_sha256"] == result["video_sha256"]
    assert receipt["timeline_sha256"] == result["timeline_sha256"]
    assert receipt["runtime_identity"]["verified"] is True
    assert receipt["verify_run_id"] == begun["verify_run_id"]


def test_analyze_video_accepts_only_current_receipt(tmp_path, monkeypatch):
    ctx, begun, current = _prepare_epoch(tmp_path)
    monkeypatch.setattr(video_tools, "run_node_script", _fake_record(current))
    recorded = video_tools._record_with_state(
        {
            "url": begun["url"],
            "verify_run_id": begun["verify_run_id"],
            "output": ".playtest/verify/recording/boss",
            "duration": 1,
        },
        ctx,
    )
    video = tmp_path / recorded["output"] / "recording.webm"

    def fake_analyze(_script, cli, **_kwargs):
        output = Path(cli[cli.index("--output") + 1])
        evidence = video.parent / "evidence"
        evidence.mkdir()
        output.write_text(
            json.dumps(
                {
                    "analysis_status": "completed",
                    "analysis_errors": [],
                    "analysis": "ok",
                    "segment_analyses": [{"segment": 1, "analysis": "ok"}],
                    "evidence_dir": str(evidence),
                }
            ),
            encoding="utf-8",
        )
        return {"returncode": 0, "stdout": "", "stderr": ""}

    monkeypatch.setattr(video_tools, "run_node_script", fake_analyze)
    analyzed = video_tools._analyze_video(
        {
            "video": recorded["output"] + "/recording.webm",
            "verify_run_id": begun["verify_run_id"],
        },
        ctx,
    )
    payload = json.loads((tmp_path / analyzed["output"]).read_text(encoding="utf-8"))
    assert payload["video_sha256"] == recorded["video_sha256"]
    assert payload["video_receipt"] == recorded["video_receipt"]
    assert payload["video"].startswith(".playtest/verify/recording/")

    video.write_bytes(b"tampered-old-video")
    with pytest.raises(ToolError, match="stale, altered, or cross-run"):
        video_tools._analyze_video(
            {
                "video": recorded["output"] + "/recording.webm",
                "verify_run_id": begun["verify_run_id"],
            },
            ctx,
        )


def test_analyze_video_rejects_provider_failure_payload(tmp_path, monkeypatch):
    ctx, begun, current = _prepare_epoch(tmp_path)
    monkeypatch.setattr(video_tools, "run_node_script", _fake_record(current))
    recorded = video_tools._record_with_state(
        {
            "url": begun["url"],
            "verify_run_id": begun["verify_run_id"],
            "output": ".playtest/verify/recording/failure",
            "duration": 1,
        },
        ctx,
    )

    def fake_failed_analysis(_script, cli, **_kwargs):
        output = Path(cli[cli.index("--output") + 1])
        output.write_text(
            json.dumps(
                {
                    "analysis_status": "failed",
                    "analysis_errors": ["provider unavailable"],
                    "analysis": "Analysis failed: provider unavailable",
                    "segment_analyses": [
                        {"segment": 1, "error": "provider unavailable"}
                    ],
                }
            ),
            encoding="utf-8",
        )
        return {"returncode": 0, "stdout": "", "stderr": ""}

    monkeypatch.setattr(video_tools, "run_node_script", fake_failed_analysis)
    with pytest.raises(ToolError, match="complete successful analysis"):
        video_tools._analyze_video(
            {
                "video": recorded["output"] + "/recording.webm",
                "verify_run_id": begun["verify_run_id"],
            },
            ctx,
        )
    assert not (
        tmp_path
        / ".playtest/verify/recording/failure/video-analysis.json"
    ).exists()


def test_video_paths_and_epoch_url_fail_closed(tmp_path, monkeypatch):
    ctx, begun, _current = _prepare_epoch(tmp_path)
    monkeypatch.setattr(
        video_tools,
        "run_node_script",
        lambda *_args, **_kwargs: pytest.fail("unsafe request launched runner"),
    )
    with pytest.raises(ToolError, match="must equal"):
        video_tools._record_with_state(
            {
                "url": "http://localhost:9999/",
                "verify_run_id": begun["verify_run_id"],
                "output": ".playtest/verify/recording/core",
            },
            ctx,
        )
    with pytest.raises(ToolError, match="escapes"):
        video_tools._record_with_state(
            {
                "url": begun["url"],
                "verify_run_id": begun["verify_run_id"],
                "output": "../escape",
            },
            ctx,
        )

    outside_dir = tmp_path.parent / f"{tmp_path.name}-recording-escape"
    outside_dir.mkdir()
    recording_root = tmp_path / ".playtest" / layout.RECORDING_DIR
    recording_root.mkdir(parents=True, exist_ok=True)
    linked_output = recording_root / "linked"
    linked_output.symlink_to(outside_dir, target_is_directory=True)
    try:
        with pytest.raises(ToolError, match="escapes"):
            video_tools._record_with_state(
                {
                    "url": begun["url"],
                    "verify_run_id": begun["verify_run_id"],
                    "output": ".playtest/verify/recording/linked",
                },
                ctx,
            )
    finally:
        linked_output.unlink(missing_ok=True)
        outside_dir.rmdir()

    outside = tmp_path.parent / f"{tmp_path.name}-foreign.webm"
    outside.write_bytes(b"foreign")
    try:
        with pytest.raises(ToolError, match="escapes"):
            video_tools._analyze_video(
                {
                    "video": str(outside),
                    "verify_run_id": begun["verify_run_id"],
                },
                ctx,
            )
    finally:
        outside.unlink(missing_ok=True)


def test_video_tooldefs_require_explicit_epoch_identity():
    tools = {tool.name: tool for tool in video_tools.TOOLS}
    assert set(tools["record_with_state"].input_schema["required"]) == {
        "url",
        "verify_run_id",
        "output",
    }
    assert set(tools["analyze_video"].input_schema["required"]) == {
        "video",
        "verify_run_id",
    }
    assert tools["record_with_state"].requires_workspace is True
    assert tools["analyze_video"].requires_workspace is True
