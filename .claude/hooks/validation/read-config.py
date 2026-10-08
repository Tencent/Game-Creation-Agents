#!/usr/bin/env python3
"""Read a dotted key from repo-level gca.config.yaml.

Usage: read-config.py <repo-root> <dotted.key> <default>
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any


def strip_yaml_comment(line: str) -> str:
    quote = None
    escaped = False
    for idx, ch in enumerate(line):
        if escaped:
            escaped = False
            continue
        if ch == "\\":
            escaped = True
            continue
        if ch in {"'", '"'}:
            quote = None if quote == ch else ch if quote is None else quote
            continue
        if ch == "#" and quote is None:
            return line[:idx]
    return line


def parse_scalar(value: str) -> Any:
    value = value.strip()
    if (value.startswith('"') and value.endswith('"')) or (
        value.startswith("'") and value.endswith("'")
    ):
        return value[1:-1]
    lowered = value.lower()
    if lowered == "true":
        return True
    if lowered == "false":
        return False
    if lowered in {"null", "none"}:
        return None
    try:
        return int(value)
    except ValueError:
        pass
    try:
        return float(value)
    except ValueError:
        return value


def parse_simple_yaml(text: str) -> dict[str, Any]:
    root: dict[str, Any] = {}
    stack: list[tuple[int, dict[str, Any]]] = [(-1, root)]
    for raw_line in text.splitlines():
        line = strip_yaml_comment(raw_line).rstrip()
        if not line.strip():
            continue
        if ":" not in line:
            raise ValueError(f"invalid YAML line: {raw_line!r}")
        indent = len(line) - len(line.lstrip(" "))
        key, _, raw_value = line.strip().partition(":")
        while stack and indent <= stack[-1][0]:
            stack.pop()
        if not stack:
            raise ValueError(f"invalid YAML indentation: {raw_line!r}")
        parent = stack[-1][1]
        if raw_value.strip() == "":
            child: dict[str, Any] = {}
            parent[key.strip()] = child
            stack.append((indent, child))
        else:
            parent[key.strip()] = parse_scalar(raw_value)
    return root


def load_config(repo_root: Path) -> dict[str, Any]:
    config_path = repo_root / "gca.config.yaml"
    if not config_path.is_file():
        raise FileNotFoundError(f"required config authority is missing: {config_path}")
    return parse_simple_yaml(config_path.read_text(encoding="utf-8"))


def get(data: dict[str, Any], dotted_key: str, default: str) -> Any:
    current: Any = data
    for part in dotted_key.split("."):
        if not isinstance(current, dict) or part not in current:
            return default
        current = current[part]
    return current


def main() -> int:
    if len(sys.argv) != 4:
        print("usage: read-config.py <repo-root> <dotted.key> <default>", file=sys.stderr)
        return 2

    repo_root = Path(sys.argv[1])
    dotted_key = sys.argv[2]
    default = sys.argv[3]
    try:
        data = load_config(repo_root)
    except (OSError, ValueError) as exc:
        print(f"invalid config authority: {exc}", file=sys.stderr)
        return 2

    value = get(data, dotted_key, default)
    if isinstance(value, bool):
        print("true" if value else "false")
    else:
        print(value)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
