"""Subprocess wrapper for bundled Node.js / Python runner scripts.

The complex Node-based tools (Playwright test runner, video recorder,
Doubao video analyzer, gate-check) plus the heavy Pillow script
(`postprocess.py`)
ship inside this package under :mod:`gca_mcp.runners`. They keep their
original CLI surface; this module just spawns them with the requested
argv and surfaces a structured result.

This keeps the agent surface uniform (everything is an MCP tool) while
avoiding a multi-thousand-line port of battle-tested code.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Any

from ..errors import ToolError

# ---- Runner layout -------------------------------------------------------

_RUNNERS_ROOT = Path(__file__).resolve().parent.parent / "runners"
ASSETS_RUNNERS = _RUNNERS_ROOT / "assets"
VERIFY_RUNNERS = _RUNNERS_ROOT / "verify"


def runner_path(group: str, name: str) -> Path:
    """Resolve a bundled runner script by group (``assets`` / ``verify``).

    Centralised so handlers can't accidentally reach for a stale path
    if we re-organise runners/.
    """
    if group == "assets":
        path = ASSETS_RUNNERS / name
    elif group == "verify":
        path = VERIFY_RUNNERS / name
    else:
        raise ToolError(
            "INVALID_CONFIG",
            f"Unknown runner group: {group!r}",
            hint="Use one of: assets, verify.",
        )
    if not path.is_file():
        raise ToolError(
            "INPUT_FILE_NOT_FOUND",
            f"Runner script missing: {path}",
            hint="Reinstall gca-mcp; the runners/ tree may be incomplete.",
        )
    return path


# ---- Subprocess runners --------------------------------------------------


def run_node_script(
    script: Path,
    args: list[str],
    *,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    timeout: float | None = None,
    check: bool = False,
) -> dict[str, Any]:
    """Spawn ``node <script> <args>``.

    ``check=True`` raises ``ToolError(GATE_BLOCKED)`` on non-zero exit;
    otherwise the caller can inspect ``returncode`` (gate-check encodes
    BLOCKED via exit code).
    """
    cmd = ["node", str(script), *args]
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(cwd) if cwd else None,
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except FileNotFoundError as e:
        raise ToolError(
            "INVALID_CONFIG",
            f"`node` executable not found on PATH: {e}",
            hint="Install Node.js 20+ to run game-verify scripts.",
        ) from e
    except subprocess.TimeoutExpired as e:
        raise ToolError("VLM_TIMEOUT", f"node script timed out after {timeout}s") from e

    result = {
        "command": cmd,
        "returncode": proc.returncode,
        "stdout": proc.stdout,
        "stderr": proc.stderr,
    }
    if check and proc.returncode != 0:
        raise ToolError(
            "GATE_BLOCKED",
            f"node script exited with {proc.returncode}",
            hint=proc.stderr.strip()[-500:] if proc.stderr else None,
        )
    return result


def run_python_script(
    script: Path,
    args: list[str],
    *,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    timeout: float | None = None,
    check: bool = True,
) -> dict[str, Any]:
    """Spawn the current python interpreter against a bundled runner script."""
    cmd = [sys.executable, str(script), *args]
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(cwd) if cwd else None,
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as e:
        raise ToolError("VLM_TIMEOUT", f"python script timed out after {timeout}s") from e

    result = {
        "command": cmd,
        "returncode": proc.returncode,
        "stdout": proc.stdout,
        "stderr": proc.stderr,
    }
    if check and proc.returncode != 0:
        raise ToolError(
            "POSTPROCESS_FAILED",
            f"python script exited with {proc.returncode}",
            hint=proc.stderr.strip()[-500:] if proc.stderr else None,
        )
    return result


__all__ = [
    "ASSETS_RUNNERS",
    "VERIFY_RUNNERS",
    "run_node_script",
    "run_python_script",
    "runner_path",
]
