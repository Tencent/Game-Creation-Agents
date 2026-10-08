"""JSON / TextContent helpers for the MCP stdio surface."""

from __future__ import annotations

import json
from typing import Any

from mcp.types import CallToolResult, TextContent

from .errors import ToolError


def format_text_content(value: Any) -> list[TextContent]:
    """Serialise a tool return value to MCP ``TextContent`` items.

    Always emits a single JSON-encoded text block. Agents parse that
    rather than expecting a structured payload — same contract used by
    `video-creation-mcp`.
    """
    text = json.dumps(value, ensure_ascii=False, indent=2, default=_default)
    return [TextContent(type="text", text=text)]


def format_error(err: ToolError) -> CallToolResult:
    payload = {
        "error": {
            "code": err.code,
            "message": err.message,
        }
    }
    if err.hint:
        payload["error"]["hint"] = err.hint
    return CallToolResult(
        content=format_text_content(payload),
        isError=True,
    )


def _default(o: Any) -> Any:
    # Pathlib + dataclasses friendliness
    if hasattr(o, "to_dict"):
        return o.to_dict()
    if hasattr(o, "__fspath__"):
        return str(o)
    return str(o)


__all__ = ["format_error", "format_text_content"]
