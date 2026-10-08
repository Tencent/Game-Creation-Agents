from __future__ import annotations

import json
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier, Event, Lock

import pytest
from verify_test_helpers import prepare_epoch

from gca_mcp import playtest_layout as layout
from gca_mcp.errors import ToolError
from gca_mcp.tools import verify_tools, video_tools
from gca_mcp.verify_checkpoint import checkpoint_issues, load_checkpoints
from gca_mcp.verify_epoch import sha256_file


def _artifact_result(root: Path, name: str) -> dict:
    path = root / ".playtest" / "verify" / "dag-test" / f"{name}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{}", encoding="utf-8")
    return {
        "output": str(path.relative_to(root)),
        "returncode": 0,
        "blocked": False,
    }


@pytest.mark.parametrize(
    ("filenames", "requested", "message"),
    [
        (["boss fight.mjs"], None, "lowercase slug"),
        (["boss.mjs"], ["boss", "boss"], "must be unique"),
    ],
)
def test_verify_scenarios_reject_output_collisions(
    tmp_path,
    filenames,
    requested,
    message,
) -> None:
    interactions = tmp_path / ".playtest" / layout.INTERACTIONS_DIR
    interactions.mkdir(parents=True)
    for filename in filenames:
        (interactions / filename).write_text(
            "export default async () => {};\n",
            encoding="utf-8",
        )

    with pytest.raises(ToolError, match=message):
        verify_tools._verify_scenarios(tmp_path, requested)


def test_physics_alignment_materializer_writes_explicit_not_applicable(
    tmp_path,
) -> None:
    ctx, run, _current = prepare_epoch(tmp_path)

    result = verify_tools._materialize_physics_alignment(
        {"verify_run_id": run["verify_run_id"]},
        ctx,
    )
    payload = json.loads(
        (tmp_path / ".playtest" / layout.PHYSICS_ALIGNMENT).read_text(
            encoding="utf-8"
        )
    )

    assert result["applicable"] is False
    assert result["aligned"] is None
    assert payload["status"] == "not_applicable"
    assert payload["producer"] == "gca-mcp.materialize_physics_alignment"


def test_physics_alignment_materializer_projects_interaction_failures(
    tmp_path,
    monkeypatch,
) -> None:
    ctx, run, current = prepare_epoch(tmp_path)
    for relative in (layout.STATE, layout.RESUME):
        path = tmp_path / ".playtest" / relative
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["interaction_profile"] = "arcade-no-gravity"
        path.write_text(json.dumps(payload), encoding="utf-8")
    current["interaction_profile"] = "arcade-no-gravity"
    (tmp_path / ".playtest" / layout.VERIFY_RUN).write_text(
        json.dumps(current),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        verify_tools,
        "_gate_check",
        lambda _args, _ctx: {
            "blocked": True,
            "result_path": ".playtest/verify/gate-results/interaction_contract.json",
            "failures": [
                {
                    "check": "gateInteractionFit",
                    "message": "player body exceeds visible art",
                }
            ],
        },
    )

    result = verify_tools._materialize_physics_alignment(
        {"verify_run_id": run["verify_run_id"]},
        ctx,
    )
    payload = json.loads(
        (tmp_path / ".playtest" / layout.PHYSICS_ALIGNMENT).read_text(
            encoding="utf-8"
        )
    )

    assert result["blocked"] is True
    assert payload["applicable"] is True
    assert payload["aligned"] is False
    assert payload["issues"][0]["root_cause"] == "gateInteractionFit"


def test_verify_dag_runs_branches_concurrently_and_reuses_checkpoints(
    tmp_path,
    monkeypatch,
) -> None:
    ctx, run, current = prepare_epoch(tmp_path)
    barrier = Barrier(5, timeout=3)
    calls: Counter[str] = Counter()
    producer_args: dict[str, dict] = {}

    def parallel(name: str):
        def handler(handler_args, _ctx):
            calls[name] += 1
            producer_args[name] = handler_args
            barrier.wait()
            return _artifact_result(tmp_path, name)

        return handler

    for name in (
        "_run_tests",
        "_play_game",
        "_visual_diff",
        "_capture_layout",
        "_capture_runtime_assets",
    ):
        monkeypatch.setattr(verify_tools, name, parallel(name))

    def materializer(name: str):
        def handler(_args, _ctx):
            calls[name] += 1
            return _artifact_result(tmp_path, name)

        return handler

    monkeypatch.setattr(
        verify_tools,
        "_materialize_gameplay_alignment",
        materializer("gameplay_alignment"),
    )
    monkeypatch.setattr(
        verify_tools,
        "_materialize_layout_alignment",
        materializer("layout_alignment"),
    )
    monkeypatch.setattr(
        verify_tools,
        "_materialize_physics_alignment",
        materializer("physics_alignment"),
    )

    finalized = []

    def finalize(args, _ctx):
        checkpoints = load_checkpoints(tmp_path, current)
        assert len(checkpoints) == 8
        assert all(checkpoint["status"] == "completed" for checkpoint in checkpoints)
        finalized.append(args)
        return {"status": "done", "run_status": "finalized"}

    gates = []
    monkeypatch.setattr(verify_tools, "_write_verify_report", finalize)
    monkeypatch.setattr(
        verify_tools,
        "_gate_check",
        lambda args, _ctx: gates.append(args) or {"blocked": False},
    )

    args = {
        "mode": "iterate",
        "url": run["url"],
        "verify_run_id": run["verify_run_id"],
    }
    first = verify_tools._run_verify_dag(args, ctx)
    second = verify_tools._run_verify_dag(args, ctx)

    assert first["status"] == "done"
    assert second["status"] == "done"
    assert barrier.broken is False
    assert calls == Counter(
        {
            "_run_tests": 1,
            "_play_game": 1,
            "_visual_diff": 1,
            "_capture_layout": 1,
            "_capture_runtime_assets": 1,
            "gameplay_alignment": 1,
            "physics_alignment": 1,
            "layout_alignment": 1,
        }
    )
    assert len(finalized) == 2
    assert len(gates) == 2
    assert (
        producer_args["_play_game"]["max_wall"]
        == ctx.config.verify.play_max_wall_seconds
    )
    assert all(gate["gate"] == "verify_report" for gate in gates)
    assert all(
        result["result"]["checkpoint_reused"] is True
        for result in second["producer_results"].values()
    )


def test_verify_dag_seals_partial_report_and_runs_gate_after_producer_failure(
    tmp_path,
    monkeypatch,
) -> None:
    ctx, run, current = prepare_epoch(tmp_path)

    monkeypatch.setattr(
        verify_tools,
        "_play_game",
        lambda _args, _ctx: (_ for _ in ()).throw(
            ToolError("NETWORK_ERROR", "provider unavailable")
        ),
    )
    for name in (
        "_run_tests",
        "_visual_diff",
        "_capture_layout",
        "_capture_runtime_assets",
        "_materialize_gameplay_alignment",
        "_materialize_physics_alignment",
        "_materialize_layout_alignment",
    ):
        monkeypatch.setattr(
            verify_tools,
            name,
            lambda _args, _ctx, name=name: _artifact_result(tmp_path, name),
        )

    observed = {}

    def finalize(args, _ctx):
        observed["report_args"] = args
        observed["checkpoints"] = load_checkpoints(tmp_path, current)
        report_path = tmp_path / ".playtest" / layout.VERIFY_REPORT
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text("{}", encoding="utf-8")
        return {"status": "partial", "run_status": "finalized"}

    def gate(args, _ctx):
        observed["gate_args"] = args
        return {"blocked": True, "failures": []}

    monkeypatch.setattr(verify_tools, "_write_verify_report", finalize)
    monkeypatch.setattr(verify_tools, "_gate_check", gate)

    result = verify_tools._run_verify_dag(
        {
            "mode": "iterate",
            "url": run["url"],
            "verify_run_id": run["verify_run_id"],
        },
        ctx,
    )

    assert result["status"] == "partial"
    assert observed["report_args"]["status"] == "partial"
    assert observed["gate_args"]["gate"] == "verify_report"
    play_checkpoint = next(
        checkpoint
        for checkpoint in observed["checkpoints"]
        if checkpoint["producer_name"] == "play_game"
    )
    assert play_checkpoint["status"] == "failed"
    assert play_checkpoint["error"]["code"] == "NETWORK_ERROR"
    assert result["producer_results"]["play"]["status"] == "failed"
    assert (tmp_path / ".playtest" / layout.VERIFY_REPORT).exists()


def test_verify_dag_emergency_seals_report_and_still_runs_gate(
    tmp_path,
    monkeypatch,
) -> None:
    ctx, run, _current = prepare_epoch(tmp_path)
    for name in (
        "_run_tests",
        "_play_game",
        "_visual_diff",
        "_capture_layout",
        "_capture_runtime_assets",
        "_materialize_gameplay_alignment",
        "_materialize_physics_alignment",
        "_materialize_layout_alignment",
    ):
        monkeypatch.setattr(
            verify_tools,
            name,
            lambda _args, _ctx, name=name: _artifact_result(tmp_path, name),
        )
    monkeypatch.setattr(
        verify_tools,
        "_write_verify_report",
        lambda _args, _ctx: (_ for _ in ()).throw(
            ToolError("FINALIZER_FAILED", "aggregate writer crashed")
        ),
    )
    gate_calls = []

    def gate(args, _ctx):
        gate_calls.append(args)
        current = json.loads(
            (tmp_path / ".playtest" / layout.VERIFY_RUN).read_text(
                encoding="utf-8"
            )
        )
        assert current["status"] == "finalized"
        return {"blocked": True}

    monkeypatch.setattr(verify_tools, "_gate_check", gate)

    result = verify_tools._run_verify_dag(
        {
            "mode": "iterate",
            "url": run["url"],
            "verify_run_id": run["verify_run_id"],
        },
        ctx,
    )
    report = json.loads(
        (tmp_path / ".playtest" / layout.VERIFY_REPORT).read_text(encoding="utf-8")
    )

    assert result["status"] == "partial"
    assert result["run_status"] == "finalized"
    assert result["verify_report"]["emergency_seal"] is True
    assert report["emergency_seal"] is True
    assert report["status"] == "partial"
    assert gate_calls


def test_verify_dag_preserves_soft_blocking_messages_in_report_args(
    tmp_path,
    monkeypatch,
) -> None:
    ctx, run, current = prepare_epoch(tmp_path)
    for name in (
        "_run_tests",
        "_play_game",
        "_visual_diff",
        "_capture_layout",
        "_capture_runtime_assets",
        "_materialize_gameplay_alignment",
        "_materialize_physics_alignment",
    ):
        monkeypatch.setattr(
            verify_tools,
            name,
            lambda _args, _ctx, name=name: _artifact_result(tmp_path, name),
        )
    layout_result = _artifact_result(tmp_path, "layout_alignment")
    layout_result.update(
        {
            "blocked": True,
            "wrote": False,
            "blocking_messages": ["GameScene capture is missing"],
        }
    )
    monkeypatch.setattr(
        verify_tools,
        "_materialize_layout_alignment",
        lambda _args, _ctx: layout_result,
    )
    finalized_args = []

    def finalize(args, _ctx):
        finalized_args.append(args)
        return {"status": "blocked", "run_status": "finalized"}

    monkeypatch.setattr(verify_tools, "_write_verify_report", finalize)
    monkeypatch.setattr(
        verify_tools,
        "_gate_check",
        lambda _args, _ctx: {"blocked": True},
    )

    result = verify_tools._run_verify_dag(
        {
            "mode": "iterate",
            "url": run["url"],
            "verify_run_id": run["verify_run_id"],
        },
        ctx,
    )

    assert result["status"] == "blocked"
    assert finalized_args[0]["status"] == "blocked"
    issues = checkpoint_issues(load_checkpoints(tmp_path, current))
    assert any(
        issue["root_cause"] == "GameScene capture is missing"
        for issue in issues
    )


def test_verify_dag_returns_finalized_epoch_idempotently(
    tmp_path,
    monkeypatch,
) -> None:
    ctx, run, current = prepare_epoch(tmp_path)
    identity = {
        key: current[key]
        for key in ("pipeline_run_id", "build_digest", "verify_run_id")
    }
    report_path = tmp_path / ".playtest" / layout.VERIFY_REPORT
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(
        json.dumps(
            {
                **identity,
                "status": "partial",
                "summary": {"producer_checkpoints": {"total": 0}},
                "issues": [{"severity": "high", "description": "fixture"}],
            }
        ),
        encoding="utf-8",
    )
    current.update(
        {
            "status": "finalized",
            "result_status": "partial",
            "report": f".playtest/{layout.VERIFY_REPORT}",
            "report_sha256": sha256_file(report_path),
        }
    )
    (tmp_path / ".playtest" / layout.VERIFY_RUN).write_text(
        json.dumps(current),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        verify_tools,
        "_gate_check",
        lambda args, _ctx: {"gate": args["gate"], "blocked": True},
    )
    monkeypatch.setattr(
        verify_tools,
        "_run_tests",
        lambda _args, _ctx: (_ for _ in ()).throw(
            AssertionError("finalized replay must not run producers")
        ),
    )

    result = verify_tools._run_verify_dag(
        {
            "mode": "iterate",
            "url": run["url"],
            "verify_run_id": run["verify_run_id"],
        },
        ctx,
    )

    assert result["status"] == "partial"
    assert result["run_status"] == "finalized"
    assert result["resumed"] is True
    assert result["replayed_finalized"] is True
    assert result["producer_results"] == {}
    assert result["verify_report"]["report_sha256"] == current["report_sha256"]
    assert result["gate"]["gate"] == "verify_report"


def test_verify_dag_single_flight_replays_result_for_concurrent_caller(
    tmp_path,
    monkeypatch,
) -> None:
    ctx, run, current = prepare_epoch(tmp_path)
    producer_started = Event()
    release_producer = Event()
    calls: Counter[str] = Counter()

    def slow_tests(_args, _ctx):
        calls["tests"] += 1
        producer_started.set()
        assert release_producer.wait(timeout=3)
        return _artifact_result(tmp_path, "tests")

    monkeypatch.setattr(verify_tools, "_run_tests", slow_tests)
    for name in (
        "_play_game",
        "_visual_diff",
        "_capture_layout",
        "_capture_runtime_assets",
        "_materialize_gameplay_alignment",
        "_materialize_physics_alignment",
        "_materialize_layout_alignment",
    ):
        monkeypatch.setattr(
            verify_tools,
            name,
            lambda _args, _ctx, name=name: _artifact_result(tmp_path, name),
        )

    def finalize(_args, _ctx):
        identity = {
            key: current[key]
            for key in ("pipeline_run_id", "build_digest", "verify_run_id")
        }
        report_path = tmp_path / ".playtest" / layout.VERIFY_REPORT
        report_path.write_text(
            json.dumps(
                {
                    **identity,
                    "status": "done",
                    "summary": {"producer_checkpoints": {"total": 8}},
                    "issues": [],
                }
            ),
            encoding="utf-8",
        )
        finalized = {
            **current,
            "status": "finalized",
            "result_status": "done",
            "report": f".playtest/{layout.VERIFY_REPORT}",
            "report_sha256": sha256_file(report_path),
        }
        (tmp_path / ".playtest" / layout.VERIFY_RUN).write_text(
            json.dumps(finalized),
            encoding="utf-8",
        )
        return {
            **identity,
            "status": "done",
            "run_status": "finalized",
            "report_sha256": finalized["report_sha256"],
        }

    monkeypatch.setattr(verify_tools, "_write_verify_report", finalize)
    monkeypatch.setattr(
        verify_tools,
        "_gate_check",
        lambda _args, _ctx: {"blocked": False},
    )
    args = {
        "mode": "iterate",
        "url": run["url"],
        "verify_run_id": run["verify_run_id"],
    }

    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(verify_tools._run_verify_dag, args, ctx)
        assert producer_started.wait(timeout=3)
        second = executor.submit(verify_tools._run_verify_dag, args, ctx)
        assert second.done() is False
        release_producer.set()
        first_result = first.result(timeout=3)
        second_result = second.result(timeout=3)

    assert calls == Counter({"tests": 1})
    assert first_result["replayed_finalized"] is False
    assert second_result["replayed_finalized"] is True
    assert (
        second_result["verify_report"]["report_sha256"]
        == first_result["verify_report"]["report_sha256"]
    )


def test_full_verify_dag_records_first_and_bounds_provider_concurrency(
    tmp_path,
    monkeypatch,
) -> None:
    interactions = tmp_path / ".playtest" / layout.INTERACTIONS_DIR
    interactions.mkdir(parents=True)
    for scenario in ("boss", "intro"):
        (interactions / f"{scenario}.mjs").write_text(
            "export default async () => {};\n",
            encoding="utf-8",
        )
    ctx, run, _current = prepare_epoch(tmp_path, mode="full")
    recorded: list[str] = []
    provider_barrier = Barrier(2, timeout=3)
    provider_lock = Lock()
    provider_active = 0
    provider_peak = 0

    def record(args, _ctx):
        scenario = Path(args["output"]).name
        assert provider_active == 0
        recorded.append(scenario)
        result = _artifact_result(tmp_path, f"record-{scenario}")
        result["video_receipt"] = result["output"]
        return result

    def provider(name: str):
        def handler(_args, _ctx):
            nonlocal provider_active, provider_peak
            assert set(recorded) == {"boss", "intro"}
            with provider_lock:
                provider_active += 1
                provider_peak = max(provider_peak, provider_active)
            try:
                provider_barrier.wait()
                return _artifact_result(tmp_path, name)
            finally:
                with provider_lock:
                    provider_active -= 1

        return handler

    monkeypatch.setattr(video_tools, "_record_with_state", record)
    monkeypatch.setattr(video_tools, "_analyze_video", provider("analyze"))
    monkeypatch.setattr(verify_tools, "_play_game", provider("play"))
    monkeypatch.setattr(verify_tools, "_visual_diff", provider("visual"))
    for name in (
        "_run_tests",
        "_capture_layout",
        "_capture_runtime_assets",
        "_materialize_gameplay_alignment",
        "_materialize_physics_alignment",
        "_materialize_layout_alignment",
    ):
        monkeypatch.setattr(
            verify_tools,
            name,
            lambda _args, _ctx, name=name: _artifact_result(tmp_path, name),
        )
    monkeypatch.setattr(
        verify_tools,
        "_write_verify_report",
        lambda _args, _ctx: {"status": "done", "run_status": "finalized"},
    )
    monkeypatch.setattr(
        verify_tools,
        "_gate_check",
        lambda _args, _ctx: {"blocked": False},
    )

    result = verify_tools._run_verify_dag(
        {
            "mode": "full",
            "url": run["url"],
            "verify_run_id": run["verify_run_id"],
        },
        ctx,
    )

    assert recorded == ["boss", "intro"]
    assert provider_peak == ctx.config.verify.max_parallel_provider_producers
    assert result["status"] == "done"
    assert set(result["producer_results"]) == {
        "record:boss",
        "record:intro",
        "tests",
        "play",
        "visual",
        "layout",
        "runtime_assets",
        "analyze:boss",
        "analyze:intro",
        "gameplay_alignment",
        "physics_alignment",
        "layout_alignment",
    }


def test_full_verify_dag_skips_analysis_when_record_dependency_failed(
    tmp_path,
    monkeypatch,
) -> None:
    interactions = tmp_path / ".playtest" / layout.INTERACTIONS_DIR
    interactions.mkdir(parents=True)
    for scenario in ("boss", "intro"):
        (interactions / f"{scenario}.mjs").write_text(
            "export default async () => {};\n",
            encoding="utf-8",
        )
    ctx, run, _current = prepare_epoch(tmp_path, mode="full")
    analyzed = []

    def record(args, _ctx):
        scenario = Path(args["output"]).name
        if scenario == "boss":
            raise ToolError("RECORD_FAILED", "boss recording failed")
        result = _artifact_result(tmp_path, f"record-{scenario}")
        result["video_receipt"] = result["output"]
        return result

    def analyze(args, _ctx):
        scenario = Path(args["video"]).parent.name
        analyzed.append(scenario)
        return _artifact_result(tmp_path, f"analyze-{scenario}")

    monkeypatch.setattr(video_tools, "_record_with_state", record)
    monkeypatch.setattr(video_tools, "_analyze_video", analyze)
    for name in (
        "_run_tests",
        "_play_game",
        "_visual_diff",
        "_capture_layout",
        "_capture_runtime_assets",
        "_materialize_gameplay_alignment",
        "_materialize_physics_alignment",
        "_materialize_layout_alignment",
    ):
        monkeypatch.setattr(
            verify_tools,
            name,
            lambda _args, _ctx, name=name: _artifact_result(tmp_path, name),
        )
    finalized_args = []
    monkeypatch.setattr(
        verify_tools,
        "_write_verify_report",
        lambda args, _ctx: (
            finalized_args.append(args)
            or {"status": "partial", "run_status": "finalized"}
        ),
    )
    monkeypatch.setattr(
        verify_tools,
        "_gate_check",
        lambda _args, _ctx: {"blocked": True},
    )

    result = verify_tools._run_verify_dag(
        {
            "mode": "full",
            "url": run["url"],
            "verify_run_id": run["verify_run_id"],
        },
        ctx,
    )

    assert analyzed == ["intro"]
    assert "analyze:boss" not in result["producer_results"]
    assert result["producer_results"]["record:boss"]["status"] == "failed"
    assert finalized_args[0]["status"] == "partial"
