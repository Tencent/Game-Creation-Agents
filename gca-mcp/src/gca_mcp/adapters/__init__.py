"""Adapters wrap external APIs / SDKs behind a uniform interface.

Each adapter:
- Pulls credentials/endpoints from :mod:`gca_mcp.config`.
- Translates HTTP / SDK errors into :class:`gca_mcp.errors.ToolError`
  with a stable code so the MCP layer can render a consistent payload.
- Has zero coupling to the MCP server module — testable in isolation.
"""
