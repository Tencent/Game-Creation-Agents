#!/usr/bin/env python3
"""PreToolUse hook: block tool calls that would burn budget when required
env vars (per `.env.example`) are still placeholders or missing.

Wired in settings.json against the `Skill|mcp__gca__.*` matcher. The hook
reads the tool-call payload from stdin and only enforces when:
  - tool_name starts with `mcp__gca__` (any GCA MCP call), OR
  - tool_name == `Skill` AND tool_input.skill == `game-pipeline`.
All other tool calls (other skills, normal Bash/Read/Edit/etc.) are passed
through silently — only the prompt was previously over-broad.

A required var is any line in `.env.example` whose value is a `<...>`
placeholder. Lines starting with `#` are ignored, so commented-out blocks
(for example optional model overrides) don't count.

Resolution order for a var's value: real shell env > `.env` file. A var is
considered SET only if it has a non-empty value that is not a `<...>`
placeholder.

Exit codes:
  0 — gate passed (or not applicable); tool call proceeds.
  2 — required var missing/placeholder; Claude Code blocks the tool call
      and surfaces the stderr message back to the agent.
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

PLACEHOLDER_RE = re.compile(r"^<.*>$")


def repo_root() -> Path:
    env = os.environ.get("CLAUDE_PROJECT_DIR")
    if env:
        return Path(env)
    return Path(__file__).resolve().parents[3]


def parse_env_file(path: Path) -> dict[str, str]:
    """Parse a dotenv-style file. Returns {} if the file is missing."""
    out: dict[str, str] = {}
    if not path.is_file():
        return out
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if (value.startswith('"') and value.endswith('"')) or (
            value.startswith("'") and value.endswith("'")
        ):
            value = value[1:-1]
        out[key] = value
    return out


def required_keys_from_example(example: Path) -> list[str]:
    """Pull every `KEY=<placeholder>` line from .env.example.

    Commented-out lines are skipped, so optional blocks (for example model
    overrides) don't fire false positives.
    """
    keys: list[str] = []
    if not example.is_file():
        return keys
    for raw in example.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        if PLACEHOLDER_RE.match(value.strip()):
            keys.append(key.strip())
    seen: set[str] = set()
    deduped: list[str] = []
    for k in keys:
        if k not in seen:
            seen.add(k)
            deduped.append(k)
    return deduped


def is_satisfied(key: str, env_file: dict[str, str]) -> bool:
    shell_val = os.environ.get(key)
    if shell_val and not PLACEHOLDER_RE.match(shell_val.strip()):
        return True
    file_val = env_file.get(key, "")
    if file_val and not PLACEHOLDER_RE.match(file_val.strip()):
        return True
    return False


def should_enforce(payload: dict) -> bool:
    """Only enforce for tool calls that actually spend GCA budget.

    - Any `mcp__gca__*` tool call may use a billed provider API → enforce.
    - The `game-pipeline` skill orchestrates the full pipeline → enforce.
    - Other skills (/code-review, /run, /verify, ...) and normal tools
      (Bash, Read, Edit, ...) → pass through.
    """
    tool_name = payload.get("tool_name", "")
    if tool_name.startswith("mcp__gca__"):
        return True
    if tool_name == "Skill":
        skill = (payload.get("tool_input") or {}).get("skill", "")
        return skill == "game-pipeline"
    return False


def main() -> int:
    raw = ""
    try:
        raw = sys.stdin.read()
    except Exception:
        pass

    payload: dict = {}
    if raw:
        try:
            payload = json.loads(raw)
        except (ValueError, TypeError):
            payload = {}

    if not should_enforce(payload):
        return 0

    if os.environ.get("GCA_SKIP_ENV_CHECK") == "1":
        return 0

    root = repo_root()
    example = root / ".env.example"
    env_path = root / ".env"

    required = required_keys_from_example(example)
    if not required:
        # No template means nothing to enforce — fail open so a missing
        # .env.example never bricks the harness.
        return 0

    env_file = parse_env_file(env_path)
    missing = [k for k in required if not is_satisfied(k, env_file)]

    if not missing:
        return 0

    tool_name = payload.get("tool_name", "<tool>")
    bar = "=" * 64
    lines = [
        bar,
        f"GCA env guard: blocked `{tool_name}` — credentials not configured.",
        bar,
        f"  .env file: {env_path}",
        "",
        "Missing or still placeholder:",
    ]
    for k in missing:
        lines.append(f"  - {k}")
    lines += [
        "",
        f"Fix: edit {env_path} and replace each `<...>` placeholder with",
        "the real value, then re-run the same request.",
        "",
        "(To bypass for one session: GCA_SKIP_ENV_CHECK=1)",
        bar,
    ]
    print("\n".join(lines), file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
