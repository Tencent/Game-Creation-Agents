from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parents[4]
SESSION_HOOKS = REPO_ROOT / ".claude" / "hooks" / "session"


def _load_boundary_module():
    module_path = SESSION_HOOKS / "guard-agent-boundary.py"
    spec = importlib.util.spec_from_file_location("guard_agent_boundary", module_path)
    assert spec is not None and spec.loader is not None
    sys.path.insert(0, str(SESSION_HOOKS))
    try:
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        sys.path.remove(str(SESSION_HOOKS))


BOUNDARY = _load_boundary_module()
PIPELINE_SESSION = sys.modules["pipeline_session"]


def _bound_stop_hook_project(
    tmp_path: Path,
) -> tuple[Path, Path, str]:
    project = tmp_path / "repo"
    game_dir = project / "playground" / "hook-test"
    game_dir.mkdir(parents=True)
    (game_dir / ".playtest").mkdir()
    (game_dir / ".playtest" / "state.json").write_text("{}\n", encoding="utf-8")
    (project / ".claude").mkdir()
    (project / ".claude" / "hooks").symlink_to(
        REPO_ROOT / ".claude" / "hooks",
        target_is_directory=True,
    )
    (project / "gca.config.yaml").symlink_to(REPO_ROOT / "gca.config.yaml")

    session_id = "stop-hook-session"
    binding_file, _marker, _closed = PIPELINE_SESSION.binding_paths(
        project,
        session_id,
    )
    binding_file.parent.mkdir(parents=True)
    binding_file.write_text(
        json.dumps(
            {
                "schema_version": PIPELINE_SESSION.SCHEMA_VERSION,
                "kind": "pipeline-session",
                "session_id": session_id,
                "workspace_mode": "resume",
                "game_dir": str(game_dir),
                "game_slug": game_dir.name,
            }
        ),
        encoding="utf-8",
    )
    return project, binding_file, session_id


def _run_stop_hook(
    project: Path,
    session_id: str,
    background_tasks: object,
) -> subprocess.CompletedProcess[str]:
    payload = {
        "session_id": session_id,
        "cwd": str(project),
        "hook_event_name": "Stop",
        "stop_hook_active": False,
        "last_assistant_message": "Waiting for background work.",
        "background_tasks": background_tasks,
        "session_crons": [],
    }
    return subprocess.run(
        ["bash", str(REPO_ROOT / ".claude" / "hooks" / "enforce-pipeline.sh")],
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        check=False,
        env={**os.environ, "CLAUDE_PROJECT_DIR": str(project)},
    )


@pytest.mark.parametrize(
    "authority",
    ["pipeline-orchestrator", *sorted(BOUNDARY.PIPELINE_AGENTS)],
)
def test_pipeline_authorities_cannot_directly_write_state_json(authority: str) -> None:
    assert not BOUNDARY.write_allowed(authority, Path(".playtest/state.json"))


def test_boundary_still_exposes_each_authority_owned_write() -> None:
    assert BOUNDARY.write_allowed("pipeline-orchestrator", Path("PROMPT.md"))
    assert BOUNDARY.write_allowed("design-agent", Path("GAME_DESIGN.md"))
    assert BOUNDARY.write_allowed("level-design-agent", Path("levels/stage_1.json"))
    assert BOUNDARY.write_allowed("implement-agent", Path("src/main.js"))
    assert BOUNDARY.write_allowed("scaffold-agent", Path("src/main.js"))
    assert BOUNDARY.write_allowed(
        "verify-agent",
        Path(".playtest/tests/smoke.test.mjs"),
    )


def test_scaffold_cannot_write_implement_owned_level_adapters() -> None:
    assert not BOUNDARY.write_allowed(
        "scaffold-agent",
        Path("src/level/LevelLoader.js"),
    )
    assert BOUNDARY.write_allowed(
        "implement-agent",
        Path("src/level/LevelLoader.js"),
    )


def test_prompt_archive_allows_one_literal_copy_into_active_game(
    tmp_path: Path,
) -> None:
    source = tmp_path / "game_prompts" / "Prompt.md"
    source.parent.mkdir()
    source.write_text("Build a game.\n", encoding="utf-8")
    active = tmp_path / "repo" / "playground" / "game"
    active.mkdir(parents=True)
    destination = active / "PROMPT.md"
    payload = {"cwd": str(tmp_path)}
    tokens = BOUNDARY.shell_tokens(f'cp "{source}" "{destination}"')

    assert tokens is not None
    assert BOUNDARY.prompt_archive_command_allowed(
        tokens,
        payload,
        active,
    )
    BOUNDARY.enforce_shell(
        {
            **payload,
            "tool_input": {"command": f'cp "{source}" "{destination}"'},
            "agent_type": None,
            "agent_id": None,
        },
        "valid",
        {"game_dir": str(active)},
        b"binding",
    )


def test_prompt_archive_rejects_compound_overwrite_and_non_prompt_sources(
    tmp_path: Path,
) -> None:
    source = tmp_path / "Prompt.md"
    source.write_text("Build a game.\n", encoding="utf-8")
    secret = tmp_path / ".env"
    secret.write_text("SECRET=value\n", encoding="utf-8")
    active = tmp_path / "repo" / "playground" / "game"
    active.mkdir(parents=True)
    destination = active / "PROMPT.md"
    payload = {"cwd": str(tmp_path)}

    assert (
        BOUNDARY.shell_tokens(
            f'cp "{source}" "{destination}" && echo archived'
        )
        is None
    )
    assert not BOUNDARY.prompt_archive_command_allowed(
        ["cp", str(secret), str(destination)],
        payload,
        active,
    )
    assert not BOUNDARY.prompt_archive_command_allowed(
        ["cp", str(source), str(active / "OTHER.md")],
        payload,
        active,
    )

    destination.write_text("canonical\n", encoding="utf-8")
    assert not BOUNDARY.prompt_archive_command_allowed(
        ["cp", str(source), str(destination)],
        payload,
        active,
    )


def test_pipeline_background_tasks_include_only_owned_workers() -> None:
    tasks = PIPELINE_SESSION.pipeline_background_tasks(
        {
            "background_tasks": [
                {
                    "id": "asset-worker",
                    "type": "subagent",
                    "status": "running",
                    "agent_type": "assets-agent",
                },
                {
                    "id": "implementation",
                    "type": "workflow",
                    "status": "running",
                    "name": "Implement scenes",
                },
                {
                    "id": "server",
                    "type": "shell",
                    "status": "running",
                },
                {
                    "id": "explorer",
                    "type": "subagent",
                    "status": "running",
                    "agent_type": "Explore",
                },
                {
                    "id": "finished-asset-worker",
                    "type": "subagent",
                    "status": "completed",
                    "agent_type": "assets-agent",
                },
                {
                    "id": "asset-worker",
                    "type": "subagent",
                    "status": "running",
                    "agent_type": "assets-agent",
                },
            ]
        }
    )

    assert tasks == [
        {
            "id": "asset-worker",
            "type": "subagent",
            "status": "running",
            "agent_type": "assets-agent",
        },
        {
            "id": "implementation",
            "type": "workflow",
            "status": "running",
            "name": "Implement scenes",
        },
    ]


@pytest.mark.parametrize("background_tasks", [None, {}, "running"])
def test_pipeline_background_tasks_fail_closed_on_invalid_registry(
    background_tasks: object,
) -> None:
    assert (
        PIPELINE_SESSION.pipeline_background_tasks(
            {"background_tasks": background_tasks}
        )
        == []
    )


def test_stop_hook_allows_pipeline_background_pause_and_resets_counter(
    tmp_path: Path,
) -> None:
    project, binding_file, session_id = _bound_stop_hook_project(tmp_path)
    counter = Path(f"{binding_file}.block_count")
    counter.write_text("4\n", encoding="utf-8")

    result = _run_stop_hook(
        project,
        session_id,
        [
            {
                "id": "asset-worker",
                "type": "subagent",
                "status": "running",
                "agent_type": "assets-agent",
            }
        ],
    )

    assert result.returncode == 0, result.stderr
    assert not counter.exists()
    assert "CANNOT STOP" not in result.stderr


def test_stop_hook_does_not_treat_background_shell_as_pipeline_worker(
    tmp_path: Path,
) -> None:
    project, binding_file, session_id = _bound_stop_hook_project(tmp_path)

    result = _run_stop_hook(
        project,
        session_id,
        [
            {
                "id": "game-server",
                "type": "shell",
                "status": "running",
                "command": "serve_game.py",
            }
        ],
    )

    assert result.returncode == 2
    assert "CANNOT STOP (1/5)" in result.stderr
    assert Path(f"{binding_file}.block_count").read_text(encoding="utf-8").strip() == "1"


def test_stop_hook_routes_startup_without_running_release_checklist(
    tmp_path: Path,
) -> None:
    project, _binding_file, session_id = _bound_stop_hook_project(tmp_path)
    game_dir = project / "playground" / "hook-test"
    (game_dir / ".playtest" / "state.json").write_text(
        json.dumps(
            {
                "phase": "startup",
                "currentStep": "design",
                "phase_status": "completed",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    (game_dir / ".playtest" / "resume.json").write_text(
        json.dumps({"next_action": "Dispatch design-agent"}) + "\n",
        encoding="utf-8",
    )

    result = _run_stop_hook(project, session_id, [])

    assert result.returncode == 2
    assert "phase=startup, step=design, status=completed" in result.stderr
    assert "ACTION REQUIRED: Dispatch design-agent" in result.stderr
    assert "index.html missing phaser@4" not in result.stderr
    assert "ready_to_ship" not in result.stderr


def test_stop_hook_blocks_bound_workspace_before_bootstrap_state(
    tmp_path: Path,
) -> None:
    project, _binding_file, session_id = _bound_stop_hook_project(tmp_path)
    game_dir = project / "playground" / "hook-test"
    (game_dir / ".playtest" / "state.json").unlink()

    result = _run_stop_hook(project, session_id, [])

    assert result.returncode == 2
    assert ".playtest/state.json has not been created" in result.stderr
    assert "mcp__gca__write_pipeline_state" in result.stderr


def _run_close_workspace_guard(
    project: Path,
    session_id: str,
    *,
    agent_id: str | None = None,
) -> subprocess.CompletedProcess[str]:
    payload: dict[str, object] = {
        "session_id": session_id,
        "cwd": str(project),
        "hook_event_name": "PreToolUse",
        "tool_name": "mcp__gca__close_workspace",
        "tool_input": {},
    }
    if agent_id is not None:
        payload["agent_id"] = agent_id
    env = {**os.environ, "CLAUDE_PROJECT_DIR": str(project)}
    env.pop("GCA_ALLOW_MCP_RESTART", None)
    return subprocess.run(
        ["bash", str(SESSION_HOOKS / "guard-mcp-restart.sh")],
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        check=False,
        env=env,
    )


def test_close_workspace_guard_blocks_while_a_pipeline_holds_state(
    tmp_path: Path,
) -> None:
    project, _binding_file, session_id = _bound_stop_hook_project(tmp_path)

    result = _run_close_workspace_guard(project, session_id)

    assert result.returncode == 2
    assert "do not close/restart the gca MCP workspace mid-pipeline" in result.stderr


def test_close_workspace_guard_allows_recovery_from_a_stray_binding(
    tmp_path: Path,
) -> None:
    # A workspace bound without persisted Startup state has no pipeline to
    # strand. Blocking here strands the session instead: the MCP server leases
    # one workspace per process, so the real game directory stays unreachable
    # behind WORKSPACE_ALREADY_OPEN and only an external restart clears it.
    project, _binding_file, session_id = _bound_stop_hook_project(tmp_path)
    (project / "playground" / "hook-test" / ".playtest" / "state.json").unlink()

    result = _run_close_workspace_guard(project, session_id)

    assert result.returncode == 0
    assert result.stderr == ""


def test_close_workspace_guard_still_blocks_subagents(tmp_path: Path) -> None:
    project, _binding_file, session_id = _bound_stop_hook_project(tmp_path)
    (project / "playground" / "hook-test" / ".playtest" / "state.json").unlink()

    result = _run_close_workspace_guard(project, session_id, agent_id="explore-1")

    assert result.returncode == 2


def test_stop_hook_excludes_preflight_smoke_from_gameplay_screenshots(
    tmp_path: Path,
) -> None:
    project, _binding_file, session_id = _bound_stop_hook_project(tmp_path)
    game_dir = project / "playground" / "hook-test"
    (game_dir / ".playtest" / "state.json").write_text(
        json.dumps(
            {
                "phase": "verify",
                "currentStep": "verify_iterate_1",
                "phase_status": "in_progress",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    preflight = game_dir / ".playtest" / "preflight"
    preflight.mkdir()
    (preflight / "rmbg-smoke.png").write_bytes(b"not gameplay evidence")

    result = _run_stop_hook(project, session_id, [])

    assert result.returncode == 2
    assert "Only 0 screenshots" in result.stderr
    assert "Only 1 screenshots" not in result.stderr
    assert "run_verify_dag" in result.stderr
    assert "mcp__gca__run_tests" not in result.stderr
    assert "mcp__gca__write_verify_report" not in result.stderr


def test_stop_hook_seals_unresolved_exit_at_iteration_budget(
    tmp_path: Path,
) -> None:
    project, binding_file, session_id = _bound_stop_hook_project(tmp_path)
    game_dir = project / "playground" / "hook-test"
    (game_dir / ".playtest" / "state.json").write_text(
        json.dumps(
            {
                "phase": "verify",
                "currentStep": "iteration",
                "phase_status": "in_progress",
                "iteration_count": 2,
            }
        )
        + "\n",
        encoding="utf-8",
    )

    result = _run_stop_hook(project, session_id, [])

    assert result.returncode == 0, result.stderr
    assert "exhausted its automatic iteration budget (2/2)" in result.stdout
    unresolved = json.loads(
        (game_dir / ".playtest" / "stop-hook-unresolved.json").read_text()
    )
    assert unresolved["status"] == "unresolved"
    assert unresolved["reason"] == "iteration_budget_exhausted"
    assert unresolved["iteration_count"] == unresolved["max_iterations"] == 2
    assert not Path(f"{binding_file}.block_count").exists()


def test_audit_log_rejects_iteration_after_budget_exhaustion(
    tmp_path: Path,
) -> None:
    project, _binding_file, _session_id = _bound_stop_hook_project(tmp_path)
    game_dir = project / "playground" / "hook-test"
    (game_dir / ".playtest" / "state.json").write_text(
        json.dumps({"iteration_count": 2}) + "\n",
        encoding="utf-8",
    )

    result = subprocess.run(
        [
            "bash",
            str(project / ".claude" / "hooks" / "audit-log.sh"),
            str(game_dir),
            "iteration_complete",
            "third cycle",
        ],
        text=True,
        capture_output=True,
        check=False,
        env={**os.environ, "CLAUDE_PROJECT_DIR": str(project)},
    )

    assert result.returncode == 2
    assert "ITERATION_BUDGET_EXHAUSTED" in result.stderr
    audit_file = game_dir / ".playtest" / "audit.jsonl"
    assert not audit_file.exists() or not audit_file.read_text().strip()
