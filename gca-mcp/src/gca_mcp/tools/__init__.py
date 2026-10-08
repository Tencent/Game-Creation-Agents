"""All MCP tool definitions live here, grouped by concern.

Each module exports a list of :class:`gca_mcp.server.ToolDef`, and
:func:`gca_mcp.server.build_registry` aggregates them. Adding a new
tool means appending one ``ToolDef`` — no edits to the dispatcher.
"""

from . import (  # noqa: F401
    asset_tools,
    concept_tools,
    lifecycle,
    path_tools,
    postprocess_tools,
    verify_tools,
    video_tools,
)
