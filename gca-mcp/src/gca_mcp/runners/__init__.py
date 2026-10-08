"""Bundled runner scripts shipped with gca-mcp.

Layout:
  assets/  — Pillow-based asset post-processing (Python).
  verify/  — Playwright / Doubao video analysis (Node.js .mjs).

Subprocess invocations go through :mod:`gca_mcp.adapters.script_runner`,
which exposes a ``runner_path(group, name)`` helper.
"""
