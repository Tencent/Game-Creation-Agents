"""MCP stdio server — tool registry + dispatcher.

Mirrors `video-creation-mcp`'s approach: each tool is a frozen
``ToolDef`` (schema + handler co-located), the dispatcher is a one-liner
lookup, and lifecycle gating (``requires_workspace``) is declared on the
ToolDef rather than re-checked inside every handler.

Tool modules are imported lazily by :func:`build_registry` so that a
malformed handler in one module doesn't bring the whole server down at
import-time.
"""

from __future__ import annotations

import asyncio
import importlib
import inspect
import threading
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError, ValidationError
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import CallToolResult, TextContent, Tool

from .config import Config
from .errors import ToolError
from .serialize import format_error, format_text_content
from .workspace import WorkspaceMeta


# -------------------- server state --------------------


@dataclass
class ServerState:
    """Per-process state.

    ``workspace`` is ``None`` in lazy mode until the agent calls
    ``init_workspace``. Workspace mutation waits for active workspace-bound
    calls; independent asset calls still run concurrently.
    """

    config: Config
    workspace: WorkspaceMeta | None = None
    repo_root: Path = field(default_factory=lambda: Path.cwd())
    workspace_condition: threading.Condition = field(
        default_factory=lambda: threading.Condition(threading.RLock())
    )
    active_workspace_calls: int = 0

    @property
    def workspace_open(self) -> bool:
        with self.workspace_condition:
            return self.workspace is not None

    def begin_workspace_call(self) -> WorkspaceMeta | None:
        with self.workspace_condition:
            workspace = self.workspace
            if workspace is not None:
                workspace.assert_identity()
                self.active_workspace_calls += 1
            return workspace

    def end_workspace_call(self) -> None:
        with self.workspace_condition:
            self.active_workspace_calls -= 1
            self.workspace_condition.notify_all()

    @contextmanager
    def workspace_mutation(self):
        with self.workspace_condition:
            while self.active_workspace_calls:
                self.workspace_condition.wait()
            yield

    def close_current_workspace(self) -> WorkspaceMeta | None:
        """Detach the workspace after all workspace-bound calls finish."""
        with self.workspace_mutation():
            workspace = self.workspace
            self.workspace = None
            if workspace is not None:
                workspace.close()
            return workspace


@dataclass
class ToolCtx:
    """Per-call context passed to handlers.

    ``ToolDef.requires_workspace=True`` guarantees ``state.workspace`` is
    populated when the handler runs.
    """

    state: ServerState

    @property
    def config(self) -> Config:
        return self.state.config

    @property
    def workspace(self) -> WorkspaceMeta:
        assert self.state.workspace is not None
        return self.state.workspace

    @property
    def repo_root(self) -> Path:
        return self.state.repo_root


Handler = Callable[[dict[str, Any], ToolCtx], Awaitable[Any] | Any]
_UNSUPPORTED_PUBLIC_SCHEMA_ROOT_KEYS = frozenset(
    {"allOf", "anyOf", "oneOf", "if", "then", "else"}
)


def _validation_path(error: ValidationError) -> str:
    path = "$"
    for part in error.absolute_path:
        path += f"[{part}]" if isinstance(part, int) else f".{part}"
    return path


def _validation_summary(error: ValidationError) -> str:
    """Describe a schema failure without echoing caller-supplied values."""
    path = _validation_path(error)
    rule = error.validator
    if rule == "required" and isinstance(error.instance, dict):
        missing = [
            str(key)
            for key in error.validator_value
            if key not in error.instance
        ]
        return f"{path} is missing required field(s): {', '.join(missing)}"
    if rule == "additionalProperties" and isinstance(error.instance, dict):
        declared = set(error.schema.get("properties", {}))
        extras = sorted(str(key) for key in error.instance if key not in declared)
        return f"{path} contains unknown field(s): {', '.join(extras)}"
    if rule == "type":
        return f"{path} must have type {error.validator_value!r}"
    if rule == "enum":
        return f"{path} must be one of {error.validator_value!r}"
    if rule in {"minimum", "maximum", "minLength", "maxLength", "minItems", "maxItems"}:
        return f"{path} violates {rule}={error.validator_value!r}"
    return f"{path} violates schema rule {rule!r}"


@dataclass(frozen=True)
class ToolDef:
    """Public MCP definition plus optional richer server-side validation."""

    name: str
    description: str
    input_schema: dict[str, Any]
    handler: Handler
    requires_workspace: bool = False
    validation_schema: dict[str, Any] | None = None
    _validator: Draft202012Validator = field(
        init=False,
        repr=False,
        compare=False,
    )

    def __post_init__(self) -> None:
        try:
            Draft202012Validator.check_schema(self.input_schema)
        except SchemaError as exc:
            raise RuntimeError(
                f"Invalid input schema for tool {self.name!r}: {exc.message}"
            ) from exc

        unsupported = sorted(
            _UNSUPPORTED_PUBLIC_SCHEMA_ROOT_KEYS.intersection(self.input_schema)
        )
        if unsupported:
            raise RuntimeError(
                f"Unsupported top-level MCP input schema keyword(s) for tool "
                f"{self.name!r}: {', '.join(unsupported)}. Move composition and "
                "conditional rules to validation_schema."
            )
        if self.input_schema.get("type") != "object":
            raise RuntimeError(
                f"Invalid input schema for tool {self.name!r}: the public MCP "
                "schema must have top-level type 'object'."
            )

        effective_schema = (
            self.validation_schema
            if self.validation_schema is not None
            else self.input_schema
        )
        if self.validation_schema is not None:
            try:
                Draft202012Validator.check_schema(self.validation_schema)
            except SchemaError as exc:
                raise RuntimeError(
                    f"Invalid validation schema for tool {self.name!r}: {exc.message}"
                ) from exc
        object.__setattr__(
            self,
            "_validator",
            Draft202012Validator(effective_schema),
        )

    def to_mcp_tool(self) -> Tool:
        return Tool(
            name=self.name,
            description=self.description,
            inputSchema=self.input_schema,
        )

    def validate_arguments(self, args: Any) -> None:
        errors = sorted(
            self._validator.iter_errors(args),
            key=lambda error: tuple(str(part) for part in error.absolute_path),
        )
        if not errors:
            return
        shown = [_validation_summary(error) for error in errors[:3]]
        if len(errors) > len(shown):
            shown.append(f"{len(errors) - len(shown)} more error(s)")
        raise ToolError(
            "INVALID_ARGUMENT",
            f"Invalid arguments for {self.name!r}: {'; '.join(shown)}.",
        )


# -------------------- registry --------------------


def build_registry() -> dict[str, ToolDef]:
    """Aggregate ``TOOLS`` from every module under :mod:`gca_mcp.tools`."""
    modules = [
        "gca_mcp.tools.lifecycle",
        "gca_mcp.tools.asset_tools",
        "gca_mcp.tools.concept_tools",
        "gca_mcp.tools.video_tools",
        "gca_mcp.tools.postprocess_tools",
        "gca_mcp.tools.verify_tools",
        "gca_mcp.tools.path_tools",
    ]
    registry: dict[str, ToolDef] = {}
    for modname in modules:
        mod = importlib.import_module(modname)
        for tool in getattr(mod, "TOOLS", []):
            if tool.name in registry:
                raise RuntimeError(f"Duplicate tool name registered: {tool.name}")
            registry[tool.name] = tool
    return registry


# -------------------- dispatcher --------------------


async def _dispatch(
    name: str,
    args: dict[str, Any],
    state: ServerState,
    registry: dict[str, ToolDef],
) -> list[TextContent] | CallToolResult:
    tool = registry.get(name)
    if tool is None:
        return format_error(ToolError("UNKNOWN_TOOL", f"Unknown tool: {name!r}"))

    try:
        tool.validate_arguments(args)
    except ToolError as exc:
        return format_error(exc)

    workspace_call = False
    if name not in {"init_workspace", "close_workspace"}:
        try:
            workspace = state.begin_workspace_call()
        except ToolError as exc:
            return format_error(exc)
        if workspace is None and tool.requires_workspace:
            return format_error(
                ToolError(
                    "NO_WORKSPACE_OPEN",
                    f"{name!r} requires an open workspace.",
                    hint="Call init_workspace(path=…, mode=…) first.",
                )
            )
        workspace_call = workspace is not None

    ctx = ToolCtx(state=state)
    try:
        if inspect.iscoroutinefunction(tool.handler):
            result = await tool.handler(args or {}, ctx)
        else:
            # Tool handlers are synchronous and routinely block on network
            # I/O (e.g. ``requests.post`` in the image/video adapters).
            # The SDK already dispatches each incoming request via
            # ``task_group.start_soon`` (see mcp.server.lowlevel.server),
            # so the only thing forcing tool calls to run one-at-a-time is
            # running that blocking work on the event-loop thread. Offload
            # sync handlers to a worker thread so independent calls — e.g.
            # several ``generate_image`` invocations issued in one turn —
            # overlap instead of serialising behind each other's HTTP wait.
            result = await asyncio.to_thread(tool.handler, args or {}, ctx)
        if inspect.isawaitable(result):
            result = await result
    except ToolError as e:
        return format_error(e)
    except Exception as e:  # noqa: BLE001
        return format_error(
            ToolError("INTERNAL_ERROR", f"{type(e).__name__}: {e}")
        )
    finally:
        if workspace_call:
            state.end_workspace_call()

    return format_text_content(result)


# -------------------- entry point --------------------


async def run_stdio_server(
    *,
    config: Config,
    workspace: WorkspaceMeta | None,
    repo_root: Path,
) -> None:
    """Boot the stdio server and block until the client disconnects."""
    state = ServerState(config=config, workspace=workspace, repo_root=repo_root)
    try:
        registry = build_registry()
        app = Server("gca-mcp")

        @app.list_tools()
        async def _list_tools() -> list[Tool]:
            return [t.to_mcp_tool() for t in registry.values()]

        @app.call_tool()
        async def _call_tool(
            name: str,
            arguments: dict[str, Any] | None,
        ) -> list[TextContent] | CallToolResult:
            return await _dispatch(name, arguments or {}, state, registry)

        async with stdio_server() as (read_stream, write_stream):
            await app.run(
                read_stream,
                write_stream,
                app.create_initialization_options(),
            )
    finally:
        state.close_current_workspace()


__all__ = [
    "ServerState",
    "ToolCtx",
    "ToolDef",
    "build_registry",
    "run_stdio_server",
]
