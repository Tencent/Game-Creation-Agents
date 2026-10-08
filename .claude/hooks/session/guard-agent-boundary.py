#!/usr/bin/env python3
"""Enforce active-workspace and single-writer boundaries for pipeline agents."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import sys
from pathlib import Path
from typing import Any

from pipeline_session import (
    PIPELINE_AGENTS,
    SCHEMA_VERSION,
    agent_binding_path,
    load_parent_binding,
    pipeline_background_tasks,
    valid_identifier,
)


WRITE_TOOLS = {"Edit", "MultiEdit", "Notebook", "NotebookEdit", "Write"}
SHELL_TOOLS = {"Bash", "Shell"}
PROJECT = Path(os.environ.get("CLAUDE_PROJECT_DIR", os.getcwd())).resolve()
PLAYGROUND = (PROJECT / "playground").resolve(strict=False)


def deny(message: str) -> None:
    print(f"Agent boundary blocked: {message}", file=sys.stderr)
    raise SystemExit(2)


def subagent_binding_valid(
    session_id: str,
    agent_id: Any,
    agent_type: Any,
    parent: dict[str, Any],
    parent_raw: bytes,
) -> bool:
    if not valid_identifier(agent_id) or agent_type not in PIPELINE_AGENTS:
        return False
    target = agent_binding_path(PROJECT, session_id, agent_id)
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return (
        isinstance(data, dict)
        and data.get("schema_version") == SCHEMA_VERSION
        and data.get("kind") == "pipeline-subagent"
        and data.get("session_id") == session_id
        and data.get("agent_id") == agent_id
        and data.get("agent_type") == agent_type
        and data.get("game_dir") == parent.get("game_dir")
        and data.get("game_slug") == parent.get("game_slug")
        and data.get("workspace_mode") == parent.get("workspace_mode")
        and data.get("parent_binding_sha256")
        == hashlib.sha256(parent_raw).hexdigest()
    )


def resolve_input_path(raw: Any, payload: dict[str, Any]) -> Path | None:
    if not isinstance(raw, str) or not raw:
        return None
    try:
        path = Path(raw).expanduser()
        if not path.is_absolute():
            cwd_raw = payload.get("cwd")
            cwd = Path(cwd_raw) if isinstance(cwd_raw, str) else PROJECT
            if not cwd.is_absolute():
                cwd = PROJECT
            path = cwd / path
        return path.resolve(strict=False)
    except (OSError, RuntimeError, TypeError, ValueError):
        return None


def requested_paths(payload: dict[str, Any]) -> list[Path]:
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        return []
    raw_paths: list[Any] = []
    for key in ("file_path", "notebook_path", "path"):
        if key in tool_input:
            raw_paths.append(tool_input[key])
    edits = tool_input.get("edits")
    if isinstance(edits, list):
        for edit in edits:
            if not isinstance(edit, dict):
                continue
            for key in ("file_path", "notebook_path", "path"):
                if key in edit:
                    raw_paths.append(edit[key])
    paths: list[Path] = []
    for raw in raw_paths:
        path = resolve_input_path(raw, payload)
        if path is not None and path not in paths:
            paths.append(path)
    return paths


def under(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def relative_to_active(path: Path, parent: dict[str, Any]) -> Path | None:
    active = Path(parent["game_dir"]).resolve(strict=False)
    try:
        return path.relative_to(active)
    except ValueError:
        return None


def critic_read_allowed(rel: Path) -> bool:
    text = rel.as_posix()
    return (
        text == "GAME_DESIGN.md"
        or text == ".playtest/inputs/rubric.json"
        or text == ".playtest/verify"
        or text.startswith(".playtest/verify/")
        or text == ".playtest/verify_middle/visual"
        or text.startswith(".playtest/verify_middle/visual/")
    )


def write_allowed(authority: str, rel: Path) -> bool:
    text = rel.as_posix()
    if authority == "pipeline-orchestrator":
        return text == "PROMPT.md"
    if authority == "critic-agent":
        return text == ".playtest/verify/critic/critic-report.json"
    if authority == "design-agent":
        return text == "GAME_DESIGN.md" or text.startswith("design/")
    if authority == "level-design-agent":
        return text.startswith("levels/")
    if authority == "verify-agent":
        return (
            text.startswith(".playtest/tests/")
            or text == ".playtest/verify/alignment/gameplay/alignment.json"
            or text == ".playtest/verify/alignment/physics/alignment.json"
        )
    if authority == "implement-agent":
        return (
            text.startswith("src/")
            or text.startswith(".playtest/tests/")
        )
    if authority == "asset-integration-agent":
        return (
            text.startswith("src/")
            or text.startswith(".playtest/tests/")
            or text == ".playtest/inputs/display-params.json"
        )
    if authority == "scaffold-agent":
        return (
            text == "index.html"
            or text in {"package.json", "jsconfig.json"}
            or (
                text.startswith("src/")
                and rel.parts[:2] != ("src", "level")
            )
            or text == "assets/asset-manifest.json"
            or text.startswith(".playtest/tests/")
            or text.startswith(".playtest/inputs/")
        )
    return False


def build_input_path(rel: Path) -> bool:
    text = rel.as_posix()
    return (
        text in {"index.html", "GAME_DESIGN.md"}
        or rel.parts[:1] in {
            ("src",),
            ("assets",),
            ("levels",),
            ("design",),
        }
        or rel.parts[:2] in {
            (".playtest", "tests"),
            (".playtest", "inputs"),
        }
    )


def verify_epoch_active(parent: dict[str, Any]) -> bool:
    path = (
        Path(parent["game_dir"])
        / ".playtest"
        / "verify"
        / "current-run.json"
    )
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return False
    except (OSError, json.JSONDecodeError):
        deny("current verification epoch state is unreadable; refusing build write")
    return isinstance(value, dict) and value.get("status") == "active"


def shell_tokens(command: Any) -> list[str] | None:
    if not isinstance(command, str) or not command or "\x00" in command:
        return None
    if "\n" in command or "\r" in command or "$" in command or "`" in command:
        return None
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars=";&|<>")
        lexer.whitespace_split = True
        lexer.commenters = ""
        tokens = list(lexer)
    except ValueError:
        return None
    if not tokens or any(token in {";", ";;", "&", "&&", "|", "||", "<", ">", ">>"} for token in tokens):
        return None
    return tokens


def token_path(token: str, payload: dict[str, Any]) -> Path | None:
    return resolve_input_path(token, payload)


def preflight_command_allowed(tokens: list[str], payload: dict[str, Any]) -> bool:
    return (
        len(tokens) == 4
        and tokens[0] in {"python", "python3"}
        and token_path(tokens[1], payload) == PROJECT / "scripts" / "preflight_env.py"
        and tokens[2:] == ["--quick", "--auto-fix"]
    )


def audit_command_allowed(
    tokens: list[str], payload: dict[str, Any], active: Path
) -> bool:
    return (
        len(tokens) >= 4
        and tokens[0] == "bash"
        and token_path(tokens[1], payload)
        == PROJECT / ".claude" / "hooks" / "audit-log.sh"
        and token_path(tokens[2], payload) == active
        and re.fullmatch(r"[a-z][a-z0-9_]{0,63}", tokens[3]) is not None
    )


def serve_command_allowed(
    tokens: list[str], payload: dict[str, Any], active: Path
) -> bool:
    if (
        len(tokens) < 3
        or tokens[0] not in {"python", "python3"}
        or token_path(tokens[1], payload) != PROJECT / "scripts" / "serve_game.py"
        or token_path(tokens[2], payload) != active
    ):
        return False
    rest = tokens[3:]
    if rest == ["--stop"] or not rest:
        return True
    if len(rest) % 2 != 0:
        return False
    allowed = {"--base", "--host", "--port"}
    for index in range(0, len(rest), 2):
        option, value = rest[index : index + 2]
        if option not in allowed:
            return False
        if option == "--host":
            if value != "127.0.0.1":
                return False
        elif not value.isdigit() or not 1 <= int(value) <= 65535:
            return False
    return True


def prompt_archive_command_allowed(
    tokens: list[str],
    payload: dict[str, Any],
    active: Path,
) -> bool:
    """Allow one literal Markdown prompt copy into the active workspace."""
    if len(tokens) != 3 or tokens[0] != "cp":
        return False
    source = token_path(tokens[1], payload)
    destination = token_path(tokens[2], payload)
    canonical_prompt = active / "PROMPT.md"
    return (
        source is not None
        and source.is_file()
        and source.suffix.lower() in {".md", ".txt"}
        and destination == canonical_prompt
        and source != canonical_prompt
        and not canonical_prompt.exists()
    )


def shell_mentions_playground(command: Any) -> bool:
    if not isinstance(command, str):
        return False
    return "playground" in command or str(PLAYGROUND) in command


def enforce_critic_read(
    payload: dict[str, Any],
    parent_status: str,
    parent: dict[str, Any] | None,
    parent_raw: bytes | None,
) -> None:
    session_id = payload.get("session_id")
    agent_id = payload.get("agent_id")
    if parent_status != "valid" or parent is None or parent_raw is None:
        deny("critic discovery requires a successful active parent init_workspace binding")
    if agent_id is not None and not subagent_binding_valid(
        session_id, agent_id, "critic-agent", parent, parent_raw
    ):
        deny("critic discovery requires its immutable parent-derived subagent binding")
    paths = requested_paths(payload)
    if len(paths) != 1:
        deny("critic reads/globs require one explicit evidence path")
    rel = relative_to_active(paths[0], parent)
    if rel is None or not critic_read_allowed(rel):
        deny(
            "critic may only read dispatched GDD/rubric/verification evidence "
            f"from the active game, got {paths[0]}"
        )


def enforce_write(
    payload: dict[str, Any],
    parent_status: str,
    parent: dict[str, Any] | None,
    parent_raw: bytes | None,
) -> None:
    tool = payload.get("tool_name")
    agent = payload.get("agent_type")
    agent_id = payload.get("agent_id")
    session_id = payload.get("session_id")
    paths = requested_paths(payload)
    touches_playground = any(under(path, PLAYGROUND) for path in paths)
    pipeline_context = (
        agent in PIPELINE_AGENTS
        or parent_status != "missing"
        or touches_playground
    )
    if not pipeline_context:
        return
    if parent_status != "valid" or parent is None or parent_raw is None:
        deny(
            f"{tool} requires a successful active parent init_workspace binding; "
            f"session binding is {parent_status}"
        )
    if agent_id is not None:
        if not subagent_binding_valid(
            session_id, agent_id, agent, parent, parent_raw
        ):
            deny(f"{tool} requires an immutable parent-derived subagent binding")
        authority = agent
    else:
        if isinstance(agent, str) and agent and agent not in PIPELINE_AGENTS:
            deny(f"unrecognized bound-session authority {agent!r}")
        authority = agent if agent in PIPELINE_AGENTS else "pipeline-orchestrator"
    if not paths:
        deny(f"{authority} {tool} requires an explicit target path")
    for path in paths:
        rel = relative_to_active(path, parent)
        if rel is None:
            deny(
                f"{authority} may only {str(tool).lower()} inside active "
                f"GAME_DIR {parent['game_dir']}, got {path}"
            )
        if build_input_path(rel) and verify_epoch_active(parent):
            deny(
                f"{authority} may not mutate build input {rel.as_posix()} "
                "during an active verification epoch"
            )
        if not write_allowed(authority, rel):
            deny(f"{authority} may not {str(tool).lower()} {rel.as_posix()}")


def enforce_shell(
    payload: dict[str, Any],
    parent_status: str,
    parent: dict[str, Any] | None,
    parent_raw: bytes | None,
) -> None:
    tool_input = payload.get("tool_input")
    command = tool_input.get("command") if isinstance(tool_input, dict) else None
    agent = payload.get("agent_type")
    agent_id = payload.get("agent_id")
    relevant = (
        agent in PIPELINE_AGENTS
        or parent_status != "missing"
        or shell_mentions_playground(command)
    )
    if not relevant:
        return
    tokens = shell_tokens(command)
    if tokens is None:
        deny("pipeline shell commands must be single, substitution-free argv-like commands")
    if parent_status in {"missing", "pending"} and preflight_command_allowed(
        tokens, payload
    ):
        return
    if parent_status != "valid" or parent is None or parent_raw is None:
        deny(
            "pipeline shell use requires a successful active parent "
            f"init_workspace binding; session binding is {parent_status}"
        )
    if agent_id is not None or agent in PIPELINE_AGENTS:
        if agent_id is not None and not subagent_binding_valid(
            payload.get("session_id"), agent_id, agent, parent, parent_raw
        ):
            deny("subagent shell use lacks an immutable parent-derived binding")
        deny(f"{agent or 'subagent'} has no direct shell authority")
    active = Path(parent["game_dir"]).resolve(strict=False)
    if (
        preflight_command_allowed(tokens, payload)
        or audit_command_allowed(tokens, payload, active)
        or serve_command_allowed(tokens, payload, active)
        or (
            not verify_epoch_active(parent)
            and prompt_archive_command_allowed(tokens, payload, active)
        )
    ):
        return
    deny(
        "bound pipeline shell use is limited to preflight, one-file prompt "
        "archival, active-game audit, and active-game server lifecycle commands"
    )


def hook_main() -> int:
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, OSError):
        deny("hook input is not valid JSON")
    if not isinstance(payload, dict):
        deny("hook input must be a JSON object")
    tool = payload.get("tool_name")
    agent = payload.get("agent_type")
    parent_status, parent, _target, parent_raw = load_parent_binding(
        PROJECT,
        payload.get("session_id"),
    )

    if tool in {"Read", "Glob", "Grep"} and agent == "critic-agent":
        enforce_critic_read(payload, parent_status, parent, parent_raw)
        return 0
    if tool in WRITE_TOOLS:
        enforce_write(payload, parent_status, parent, parent_raw)
        return 0
    if tool in SHELL_TOOLS:
        enforce_shell(payload, parent_status, parent, parent_raw)
        return 0
    return 0


def resolve_session(
    session_id: Any,
    *,
    in_flight_tasks: list[dict[str, str]] | None = None,
) -> int:
    status, parent, target, _raw = load_parent_binding(PROJECT, session_id)
    if status in {"missing", "closed"}:
        return 3
    if status != "valid" or parent is None or target is None:
        print(f"pipeline session binding is {status}", file=sys.stderr)
        return 2
    result: dict[str, Any] = {
        "binding_file": str(target),
        "game_dir": parent["game_dir"],
        "game_slug": parent["game_slug"],
        "workspace_mode": parent["workspace_mode"],
    }
    if in_flight_tasks is not None:
        result["pipeline_background_tasks"] = in_flight_tasks
        result["pipeline_background_task_count"] = len(in_flight_tasks)
    print(
        json.dumps(result)
    )
    return 0


def resolve_hook_session() -> int:
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, OSError):
        print("hook input is not valid JSON", file=sys.stderr)
        return 2
    if not isinstance(payload, dict):
        print("hook input must be a JSON object", file=sys.stderr)
        return 2
    if payload.get("agent_id") is not None:
        return 3
    in_flight_tasks = (
        pipeline_background_tasks(payload)
        if payload.get("hook_event_name") == "Stop"
        else []
    )
    return resolve_session(
        payload.get("session_id"),
        in_flight_tasks=in_flight_tasks,
    )


def main() -> int:
    if len(sys.argv) == 2 and sys.argv[1] == "--resolve-hook-session":
        return resolve_hook_session()
    if len(sys.argv) == 3 and sys.argv[1] == "--resolve-session":
        return resolve_session(sys.argv[2])
    if len(sys.argv) != 1:
        print("unsupported guard-agent-boundary arguments", file=sys.stderr)
        return 2
    return hook_main()


if __name__ == "__main__":
    raise SystemExit(main())
