"""Small path and subprocess helpers shared by MCP tool modules."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from ..errors import ToolError


def workspace_relative(game_dir: Path, path: Path) -> str:
    try:
        return path.resolve().relative_to(game_dir.resolve()).as_posix()
    except ValueError as exc:
        raise ToolError(
            "PATH_ESCAPE",
            f"Path escapes the active workspace: {path}",
        ) from exc


def resolve_workspace_path(
    game_dir: Path,
    value: Any,
    *,
    field: str,
    allowed_roots: tuple[str, ...],
    default: str | None = None,
    must_exist: bool = False,
) -> Path:
    raw = value if value is not None else default
    if not isinstance(raw, str) or not raw:
        raise ToolError("INVALID_PATH", f"`{field}` is required.")
    root = game_dir.resolve()
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = root / path
    path = path.resolve()
    if not path.is_relative_to(root):
        raise ToolError("PATH_ESCAPE", f"`{field}` escapes the active workspace.")
    allowed = tuple((root / relative).resolve() for relative in allowed_roots)
    if not any(path == candidate or path.is_relative_to(candidate) for candidate in allowed):
        raise ToolError(
            "PATH_NOT_ALLOWED",
            f"`{field}` must stay under: {', '.join(allowed_roots)}.",
        )
    if must_exist and not path.exists():
        raise ToolError(
            "INPUT_NOT_FOUND",
            f"`{field}` does not exist: {workspace_relative(root, path)}",
        )
    return path


def parse_json_output(text: str) -> Any:
    value = (text or "").strip()
    if not value:
        return None
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        start = value.find("{")
        end = value.rfind("}")
        if start >= 0 and end > start:
            try:
                return json.loads(value[start : end + 1])
            except json.JSONDecodeError:
                pass
    return None


def slot_contract_sha256(entry: dict[str, Any]) -> str:
    contract = {
        key: entry.get(key)
        for key in (
            "key",
            "category",
            "sourceStrategy",
            "description",
            "path",
            "metadata",
            "layout",
            "defaultAction",
        )
        if key in entry
    }
    actions = entry.get("actions")
    if isinstance(actions, dict):
        contract["actions"] = {
            name: {
                key: spec.get(key)
                for key in ("textureKey", "path", "metadata")
                if key in spec
            }
            for name, spec in sorted(actions.items())
            if isinstance(spec, dict)
        }
    encoded = json.dumps(
        contract,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def gdd_slot_context_sha256(
    gdd_text: str,
    *,
    key: str,
    description: str,
    action: str | None = None,
) -> str:
    """Hash one exact key/action/description record in canonical GDD §2 tables."""

    def normalize(value: str) -> str:
        cleaned = (
            value.strip(" \t\f\v")
            .replace("\t", " ")
            .replace("\f", " ")
            .replace("\v", " ")
        )
        return " ".join(part for part in cleaned.split(" ") if part)

    def split_row(line: str) -> list[str]:
        def clean_cell(value: str) -> str:
            cleaned = normalize(value)
            if len(cleaned) >= 2 and cleaned[0] == cleaned[-1] == "`":
                cleaned = normalize(cleaned[1:-1])
            return cleaned

        cells: list[str] = []
        cell: list[str] = []
        index = 0
        while index < len(line):
            char = line[index]
            if char == "\\" and index + 1 < len(line) and line[index + 1] == "|":
                cell.append("|")
                index += 2
                continue
            if char == "|":
                cells.append(clean_cell("".join(cell)))
                cell = []
            else:
                cell.append(char)
            index += 1
        cells.append(clean_cell("".join(cell)))
        if cells and not cells[0]:
            cells.pop(0)
        if cells and not cells[-1]:
            cells.pop()
        return cells

    normalized_text = (
        gdd_text.replace("\r\n", "\n")
        .replace("\r", "\n")
        .replace("\u0085", "\n")
        .replace("\u2028", "\n")
        .replace("\u2029", "\n")
    )
    lines = normalized_text.split("\n")
    normalized_description = normalize(description)
    normalized_key = normalize(key).lower()
    normalized_action = normalize(action).lower() if action is not None else None
    if not normalized_description or not normalized_key:
        raise ValueError("slot key and description must be non-empty")

    valid_sections = {"2a", "2b", "2c", "2d", "2e"}
    section: str | None = None
    columns: dict[str, int] | None = None
    seen_tables: set[str] = set()
    duplicate_table = False
    duplicate_match = False
    in_fence = False
    authoritative: list[dict[str, str | None]] = []
    for line in lines:
        stripped = line.strip(" \t\f\v")
        if stripped.startswith(("```", "~~~")):
            in_fence = not in_fence
            columns = None
            continue
        if in_fence or stripped.startswith(">"):
            continue
        if stripped.startswith("#"):
            title = normalize(stripped.lstrip("#"))
            first = title.split(" ", 1)[0].rstrip(".:").lstrip("§").lower()
            section = first if first in valid_sections else None
            columns = None
            continue
        if section is None:
            continue
        if "|" not in stripped:
            if stripped:
                columns = None
            continue
        cells = split_row(stripped)
        lowered = [cell.lower() for cell in cells]
        if "key" in lowered and "description" in lowered:
            duplicate_table = section in seen_tables
            seen_tables.add(section)
            columns = {
                "key": lowered.index("key"),
                "description": lowered.index("description"),
            }
            if "action" in lowered:
                columns["action"] = lowered.index("action")
            continue
        if columns is None or all(
            not cell or set(cell) <= {"-", ":"} for cell in cells
        ):
            continue
        if max(columns.values()) >= len(cells):
            continue
        row_key = cells[columns["key"]].lower()
        if row_key != normalized_key:
            continue
        row_action = (
            cells[columns["action"]].lower()
            if "action" in columns
            else None
        )
        if normalized_action is not None and row_action != normalized_action:
            continue
        if duplicate_table:
            duplicate_match = True
            continue
        authoritative.append(
            {
                "action": row_action,
                "description": cells[columns["description"]],
                "section": section,
            }
        )

    if duplicate_match or len(authoritative) != 1:
        raise ValueError(
            "slot key/action is not unique in canonical GAME_DESIGN.md §2 tables"
        )
    selected = authoritative[0]
    if selected["description"] != normalized_description:
        raise ValueError(
            "manifest description does not match its canonical GAME_DESIGN.md slot"
        )

    payload = {
        "action": normalized_action,
        "description": normalized_description,
        "key": normalized_key,
        "schema_version": 1,
        "section": selected["section"],
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
