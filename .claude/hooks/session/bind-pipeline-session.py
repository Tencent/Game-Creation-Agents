#!/usr/bin/env python3
"""Persist immutable parent and subagent bindings for one pipeline session."""

from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path
from typing import Any

from pipeline_session import (
    PIPELINE_AGENTS,
    SCHEMA_VERSION,
    WORKSPACE_MODES,
    agent_binding_path,
    binding_paths,
    load_parent_binding,
    strict_game_root,
    valid_identifier,
)

# Claude Code supplies session_id on every hook and adds agent_id/agent_type
# inside a subagent. It does not supply a separate trusted parent-binding field,
# so SubagentStart snapshots the already-bound session identity for that agent.


def project_root() -> Path:
    return Path(os.environ.get("CLAUDE_PROJECT_DIR", os.getcwd())).resolve()


def immutable_write(target: Path, value: dict[str, Any]) -> bool:
    """Create one binding once; an existing different value is never replaced."""

    encoded = (json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8")
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(target, flags, 0o600)
    except FileExistsError:
        try:
            return target.read_bytes() == encoded
        except OSError:
            return False
    except OSError:
        return False
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
    except OSError:
        try:
            target.unlink()
        except OSError:
            pass
        return False
    return True


def response_candidates(value: Any, field: str) -> tuple[list[Any], bool]:
    """Collect one structured MCP result while detecting errors anywhere."""

    candidates: list[Any] = []
    failed = False

    def walk(current: Any) -> None:
        nonlocal failed
        if isinstance(current, str):
            try:
                decoded = json.loads(current)
            except (json.JSONDecodeError, TypeError):
                return
            walk(decoded)
            return
        if isinstance(current, list):
            for item in current:
                walk(item)
            return
        if not isinstance(current, dict):
            return
        status = current.get("status")
        if (
            current.get("isError") is True
            or current.get("ok") is False
            or current.get("success") is False
            or (
                "error" in current
                and current.get("error") not in (None, False, "", {})
            )
            or (
                isinstance(status, str)
                and status.lower() in {"error", "failed", "failure"}
            )
        ):
            failed = True
        if field in current:
            candidates.append(current[field])
        for key in ("content", "result", "text", "tool_response"):
            if key in current:
                walk(current[key])

    walk(value)
    unique: list[Any] = []
    seen: set[str] = set()
    for candidate in candidates:
        try:
            key = json.dumps(candidate, sort_keys=True)
        except (TypeError, ValueError):
            failed = True
            continue
        if key not in seen:
            seen.add(key)
            unique.append(candidate)
    return unique, failed


def mark_pipeline_intent(payload: dict[str, Any], project: Path) -> None:
    if payload.get("agent_id") is not None:
        return
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        return
    skill = tool_input.get("skill") or tool_input.get("name")
    if skill not in {"game-pipeline", "/game-pipeline"}:
        return
    session_id = payload.get("session_id")
    if not valid_identifier(session_id):
        return
    _binding, marker, _closed = binding_paths(project, session_id)
    immutable_write(
        marker,
        {
            "kind": "pipeline-session-intent",
            "schema_version": SCHEMA_VERSION,
            "session_id": session_id,
        },
    )


def bind_successful_init(payload: dict[str, Any], project: Path) -> None:
    """Bind only a successful parent init whose returned identity is explicit."""

    if payload.get("agent_id") is not None:
        return
    session_id = payload.get("session_id")
    tool_input = payload.get("tool_input")
    if not valid_identifier(session_id) or not isinstance(tool_input, dict):
        return
    requested_mode = tool_input.get("mode")
    requested = strict_game_root(tool_input.get("path"), project)
    if requested_mode not in WORKSPACE_MODES or requested is None:
        return
    requested_root, requested_slug = requested
    requested_name = tool_input.get("name")
    if requested_name is not None and requested_name != requested_slug:
        return

    # Lifecycle interface requirement: workspace.mode is the actual open mode
    # (including on reopened responses), never a blind echo of the request.
    candidates, failed = response_candidates(payload.get("tool_response"), "workspace")
    if failed or len(candidates) != 1 or not isinstance(candidates[0], dict):
        return
    workspace = candidates[0]
    returned = strict_game_root(workspace.get("root"), project)
    if (
        returned is None
        or returned[0] != requested_root
        or returned[1] != requested_slug
        or (
            workspace.get("slug") is not None
            and workspace.get("slug") != requested_slug
        )
        or workspace.get("mode") not in WORKSPACE_MODES
        or workspace.get("mode") != requested_mode
        or (
            workspace.get("name") is not None
            and workspace.get("name") != requested_slug
        )
        or not returned[0].is_dir()
    ):
        return

    target, _marker, closed = binding_paths(project, session_id)
    if closed.exists():
        return
    value = {
        "game_dir": str(returned[0]),
        "game_slug": returned[1],
        "kind": "pipeline-session",
        "schema_version": SCHEMA_VERSION,
        "session_id": session_id,
        "workspace_mode": workspace["mode"],
    }
    if not immutable_write(target, value):
        print(
            "Pipeline session binding refused: the session is already bound "
            "to a different workspace identity.",
            file=sys.stderr,
        )


def bind_subagent(payload: dict[str, Any], project: Path) -> None:
    session_id = payload.get("session_id")
    agent_id = payload.get("agent_id")
    agent_type = payload.get("agent_type")
    if (
        not valid_identifier(session_id)
        or not valid_identifier(agent_id)
        or agent_type not in PIPELINE_AGENTS
    ):
        return
    parent_status, parent_data, _parent_path, parent_raw = load_parent_binding(
        project,
        session_id,
    )
    if parent_status != "valid" or parent_data is None or parent_raw is None:
        print(
            f"Pipeline subagent binding unavailable for {agent_type}; "
            "writes will fail closed.",
            file=sys.stderr,
        )
        return
    value = {
        "agent_id": agent_id,
        "agent_type": agent_type,
        "game_dir": parent_data["game_dir"],
        "game_slug": parent_data["game_slug"],
        "kind": "pipeline-subagent",
        "parent_binding_sha256": hashlib.sha256(parent_raw).hexdigest(),
        "schema_version": SCHEMA_VERSION,
        "session_id": session_id,
        "workspace_mode": parent_data["workspace_mode"],
    }
    target = agent_binding_path(project, session_id, agent_id)
    if not immutable_write(target, value):
        print(
            f"Pipeline subagent binding refused for {agent_type}; "
            "the agent identity is already bound differently.",
            file=sys.stderr,
        )


def mark_successful_close(payload: dict[str, Any], project: Path) -> None:
    """Tombstone a parent binding after a successful close; never permit rebinding."""

    if payload.get("agent_id") is not None:
        return
    session_id = payload.get("session_id")
    if not valid_identifier(session_id):
        return
    parent_status, parent_data, _parent_path, _parent_raw = load_parent_binding(
        project,
        session_id,
    )
    if parent_status != "valid" or parent_data is None:
        return
    candidates, failed = response_candidates(payload.get("tool_response"), "closed")
    if failed or len(candidates) != 1:
        return
    closed_workspace = candidates[0]
    if closed_workspace is not None:
        if not isinstance(closed_workspace, dict):
            return
        closed_root = strict_game_root(closed_workspace.get("root"), project)
        if closed_root is None or str(closed_root[0]) != parent_data["game_dir"]:
            return
    _binding, _marker, closed = binding_paths(project, session_id)
    immutable_write(
        closed,
        {
            "game_dir": parent_data["game_dir"],
            "kind": "pipeline-session-closed",
            "schema_version": SCHEMA_VERSION,
            "session_id": session_id,
        },
    )


def main() -> int:
    try:
        payload: dict[str, Any] = json.load(sys.stdin)
    except (json.JSONDecodeError, OSError):
        return 0
    if not isinstance(payload, dict):
        return 0
    project = project_root()
    event = payload.get("hook_event_name")
    tool = payload.get("tool_name")
    if event == "PreToolUse" and tool == "Skill":
        mark_pipeline_intent(payload, project)
    elif event == "SubagentStart":
        bind_subagent(payload, project)
    elif tool == "mcp__gca__init_workspace" and event in (None, "PostToolUse"):
        bind_successful_init(payload, project)
    elif tool == "mcp__gca__close_workspace" and event in (None, "PostToolUse"):
        mark_successful_close(payload, project)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
