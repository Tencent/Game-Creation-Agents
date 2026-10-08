from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from mcp.types import CallToolResult

from gca_mcp.errors import ToolError
from gca_mcp.server import ServerState, ToolDef, _dispatch, build_registry


def _tool(calls: list[dict]) -> ToolDef:
    def handler(args, _ctx):
        calls.append(args)
        return {"ok": True}

    return ToolDef(
        name="schema_test",
        description="Test dispatcher schema validation.",
        input_schema={
            "type": "object",
            "properties": {
                "count": {"type": "integer", "minimum": 1},
                "enabled": {"type": "boolean"},
            },
            "required": ["count"],
            "additionalProperties": False,
        },
        handler=handler,
    )


@pytest.mark.parametrize(
    "arguments",
    [
        {},
        {"count": "do-not-echo"},
        {"count": 0},
        {"count": 1, "unknown": True},
    ],
)
async def test_dispatch_rejects_invalid_arguments_before_handler(
    tmp_path,
    arguments,
) -> None:
    calls: list[dict] = []
    tool = _tool(calls)
    state = ServerState(config=SimpleNamespace(), repo_root=tmp_path)

    response = await _dispatch(
        tool.name,
        arguments,
        state,
        {tool.name: tool},
    )
    assert isinstance(response, CallToolResult)
    assert response.isError is True
    payload = json.loads(response.content[0].text)

    assert payload["error"]["code"] == "INVALID_ARGUMENT"
    assert "do-not-echo" not in response.content[0].text
    assert calls == []


async def test_dispatch_accepts_schema_valid_arguments(tmp_path) -> None:
    calls: list[dict] = []
    tool = _tool(calls)
    state = ServerState(config=SimpleNamespace(), repo_root=tmp_path)

    response = await _dispatch(
        tool.name,
        {"count": 2, "enabled": False},
        state,
        {tool.name: tool},
    )

    assert isinstance(response, list)
    assert json.loads(response[0].text) == {"ok": True}
    assert calls == [{"count": 2, "enabled": False}]


def test_registry_compiles_every_declared_input_schema() -> None:
    registry = build_registry()

    assert registry
    assert all(tool.input_schema for tool in registry.values())


def test_registry_exposes_mcp_compatible_public_schemas() -> None:
    forbidden_root_keys = {"allOf", "anyOf", "oneOf", "if", "then", "else"}

    for name, tool in build_registry().items():
        assert tool.input_schema.get("type") == "object", name
        assert forbidden_root_keys.isdisjoint(tool.input_schema), name
        assert tool.to_mcp_tool().inputSchema == tool.input_schema


def test_gate_check_keeps_conditional_schema_server_side() -> None:
    tool = build_registry()["gate_check"]

    assert "allOf" not in tool.input_schema
    assert tool.validation_schema is not None
    assert "allOf" in tool.validation_schema
    assert "allOf" not in tool.to_mcp_tool().inputSchema


def test_tool_definition_supports_private_composition_validation() -> None:
    public_schema = {
        "type": "object",
        "properties": {
            "left": {"type": "string"},
            "right": {"type": "string"},
        },
        "additionalProperties": False,
    }
    tool = ToolDef(
        name="private_one_of",
        description="Exercise server-only composition.",
        input_schema=public_schema,
        validation_schema={
            **public_schema,
            "oneOf": [
                {"required": ["left"]},
                {"required": ["right"]},
            ],
        },
        handler=lambda _args, _ctx: None,
    )

    tool.validate_arguments({"left": "ok"})
    with pytest.raises(ToolError, match="oneOf"):
        tool.validate_arguments({"left": "x", "right": "y"})


@pytest.mark.parametrize(
    ("step", "is_valid"),
    [
        (1, False),
        (2, True),
        (2.0, True),
        (7, True),
        (7.0, True),
        (8, False),
    ],
)
def test_gate_check_step_uses_current_pipeline_phase_range(
    step: int | float,
    is_valid: bool,
) -> None:
    tool = build_registry()["gate_check"]
    arguments = {"gate": "step_done", "step": step}
    if step == 7:
        arguments["verify_run_id"] = "verify-test"

    if is_valid:
        tool.validate_arguments(arguments)
    else:
        with pytest.raises(ToolError, match="Invalid arguments"):
            tool.validate_arguments(arguments)


def test_gate_check_requires_step_only_for_step_done() -> None:
    tool = build_registry()["gate_check"]

    with pytest.raises(ToolError, match="missing required field.*step"):
        tool.validate_arguments({"gate": "step_done"})

    tool.validate_arguments({"gate": "scaffold_done"})
    tool.validate_arguments({"gate": "scaffold_done", "step": 1})
    tool.validate_arguments({"gate": "scaffold_done", "step": 8})


def test_gate_check_phase_7_requires_verify_run_id() -> None:
    tool = build_registry()["gate_check"]

    tool.validate_arguments({"gate": "step_done", "step": 6})
    with pytest.raises(ToolError, match="missing required field.*verify_run_id"):
        tool.validate_arguments({"gate": "step_done", "step": 7})
    tool.validate_arguments(
        {
            "gate": "step_done",
            "step": 7,
            "verify_run_id": "verify-test",
        }
    )


async def test_dispatch_rejects_step_done_without_step_before_workspace(
    tmp_path,
) -> None:
    state = ServerState(config=SimpleNamespace(), repo_root=tmp_path)
    response = await _dispatch(
        "gate_check",
        {"gate": "step_done"},
        state,
        build_registry(),
    )

    assert isinstance(response, CallToolResult)
    assert response.isError is True
    payload = json.loads(response.content[0].text)
    assert payload["error"]["code"] == "INVALID_ARGUMENT"


def test_tool_definition_rejects_an_invalid_schema() -> None:
    with pytest.raises(RuntimeError, match="Invalid input schema"):
        ToolDef(
            name="broken",
            description="Broken schema.",
            input_schema={"type": "not-a-json-schema-type"},
            handler=lambda _args, _ctx: None,
        )


def test_tool_definition_rejects_public_root_composition() -> None:
    with pytest.raises(RuntimeError, match="Unsupported top-level MCP input schema"):
        ToolDef(
            name="public_all_of",
            description="Invalid public composition.",
            input_schema={
                "type": "object",
                "properties": {},
                "allOf": [{"type": "object"}],
            },
            handler=lambda _args, _ctx: None,
        )
