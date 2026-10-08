"""Shared immutable pipeline-session identity helpers for Claude hooks."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any


SCHEMA_VERSION = 2
WORKSPACE_MODES = {"create", "resume"}
PIPELINE_AGENTS = {
    "asset-integration-agent",
    "assets-agent",
    "critic-agent",
    "design-agent",
    "implement-agent",
    "level-design-agent",
    "scaffold-agent",
    "verify-agent",
}
TERMINAL_BACKGROUND_TASK_STATUSES = {
    "cancelled",
    "canceled",
    "complete",
    "completed",
    "done",
    "error",
    "failed",
    "finished",
    "stopped",
    "succeeded",
    "success",
}
STRICT_SLUG = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}")


def valid_identifier(value: Any) -> bool:
    return (
        isinstance(value, str)
        and 0 < len(value) <= 512
        and not any(ord(char) < 32 or ord(char) == 127 for char in value)
    )


def pipeline_background_tasks(payload: Any) -> list[dict[str, str]]:
    """Return sanitized in-flight pipeline workers from a Stop hook payload."""
    if not isinstance(payload, dict):
        return []
    raw_tasks = payload.get("background_tasks")
    if not isinstance(raw_tasks, list):
        return []

    tasks: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for raw in raw_tasks:
        if not isinstance(raw, dict):
            continue
        task_id = raw.get("id")
        task_type = raw.get("type")
        status = raw.get("status")
        if (
            not valid_identifier(task_id)
            or not valid_identifier(task_type)
            or not valid_identifier(status)
        ):
            continue
        if status.casefold() in TERMINAL_BACKGROUND_TASK_STATUSES:
            continue

        summary = {
            "id": task_id,
            "type": task_type,
            "status": status,
        }
        if task_type == "subagent":
            agent_type = raw.get("agent_type")
            if agent_type not in PIPELINE_AGENTS:
                continue
            summary["agent_type"] = agent_type
        elif task_type == "workflow":
            name = raw.get("name")
            if not valid_identifier(name):
                continue
            summary["name"] = name
        else:
            # Shell servers, monitors, MCP tasks, teammates, and scheduled work
            # must never suppress the release guard.
            continue

        identity = (task_type, task_id)
        if identity not in seen:
            seen.add(identity)
            tasks.append(summary)
    return tasks


def digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def binding_paths(project: Path, session_id: str) -> tuple[Path, Path, Path]:
    root = project / ".playtest" / "sessions"
    key = digest(session_id)
    return (
        root / f"{key}.json",
        root / f"{key}.pipeline",
        root / f"{key}.closed",
    )


def agent_binding_path(project: Path, session_id: str, agent_id: str) -> Path:
    return (
        project
        / ".playtest"
        / "sessions"
        / "agents"
        / digest(session_id)
        / f"{digest(agent_id)}.json"
    )


def strict_game_root(raw: Any, project: Path) -> tuple[Path, str] | None:
    if not isinstance(raw, str) or not raw:
        return None
    try:
        path = Path(raw).expanduser()
        if not path.is_absolute():
            path = project / path
        path = path.resolve(strict=False)
        relative = path.relative_to((project / "playground").resolve(strict=False))
    except (OSError, RuntimeError, TypeError, ValueError):
        return None
    if len(relative.parts) != 1:
        return None
    slug = relative.parts[0]
    if STRICT_SLUG.fullmatch(slug) is None:
        return None
    return path, slug


def load_parent_binding(
    project: Path,
    session_id: Any,
) -> tuple[str, dict[str, Any] | None, Path | None, bytes | None]:
    if not valid_identifier(session_id):
        return "invalid", None, None, None
    target, marker, closed = binding_paths(project, session_id)
    if closed.exists():
        return "closed", None, target, None
    if not target.exists():
        return ("pending" if marker.exists() else "missing"), None, target, None
    if not target.is_file():
        return "invalid", None, target, None
    try:
        raw = target.read_bytes()
        data = json.loads(raw)
    except (OSError, json.JSONDecodeError):
        return "invalid", None, target, None
    game = (
        strict_game_root(data.get("game_dir"), project)
        if isinstance(data, dict)
        else None
    )
    if (
        not isinstance(data, dict)
        or data.get("schema_version") != SCHEMA_VERSION
        or data.get("kind") != "pipeline-session"
        or data.get("session_id") != session_id
        or data.get("workspace_mode") not in WORKSPACE_MODES
        or game is None
        or data.get("game_slug") != game[1]
        or not game[0].is_dir()
    ):
        return "invalid", None, target, raw
    return "valid", data, target, raw
