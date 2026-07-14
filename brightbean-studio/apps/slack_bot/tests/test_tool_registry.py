"""Tests for the tool registry and specification."""

from __future__ import annotations

import pytest
from ninja import Schema
from pydantic import ConfigDict

from apps.slack_bot.contracts import ToolResult, ToolResultStatus
from apps.slack_bot.llm.base import LLMToolDefinition
from apps.slack_bot.tool_registry import RegisteredTool, ToolRegistry


# ---------------------------------------------------------------------------
# Fake schema and executor for testing
# ---------------------------------------------------------------------------

class _FakeInput(Schema):
    model_config = ConfigDict(extra="forbid")
    platform: str


def _fake_executor(*, arguments, context):
    return ToolResult(
        status=ToolResultStatus.SUCCESS,
        tool_name="fake_tool",
    )


def _make_tool(name="get_account_stats", description="Get account stats"):
    return RegisteredTool(
        name=name,
        description=description,
        input_schema_type=_FakeInput,
        executor=_fake_executor,
    )


# ===========================================================================
# Registration
# ===========================================================================

def test_register_valid_tool():
    registry = ToolRegistry()
    tool = _make_tool()
    registry.register(tool)
    assert registry.contains("get_account_stats")


def test_retrieve_registered_tool():
    registry = ToolRegistry()
    tool = _make_tool()
    registry.register(tool)
    retrieved = registry.get("get_account_stats")
    assert retrieved is tool


def test_duplicate_name_rejected():
    registry = ToolRegistry()
    registry.register(_make_tool())
    with pytest.raises(ValueError, match="already registered"):
        registry.register(_make_tool())


def test_invalid_name_rejected():
    with pytest.raises(ValueError, match="snake_case"):
        RegisteredTool(
            name="Get-Account-Stats",
            description="d",
            input_schema_type=_FakeInput,
            executor=_fake_executor,
        )


def test_name_starting_with_digit_rejected():
    with pytest.raises(ValueError, match="snake_case"):
        RegisteredTool(
            name="1tool",
            description="d",
            input_schema_type=_FakeInput,
            executor=_fake_executor,
        )


def test_name_with_dot_rejected():
    with pytest.raises(ValueError, match="snake_case"):
        RegisteredTool(
            name="module.tool",
            description="d",
            input_schema_type=_FakeInput,
            executor=_fake_executor,
        )


def test_empty_name_rejected():
    with pytest.raises(ValueError, match="must not be empty"):
        RegisteredTool(
            name="",
            description="d",
            input_schema_type=_FakeInput,
            executor=_fake_executor,
        )


def test_empty_description_rejected():
    with pytest.raises(ValueError, match="description must not be empty"):
        RegisteredTool(
            name="valid_name",
            description="",
            input_schema_type=_FakeInput,
            executor=_fake_executor,
        )


# ===========================================================================
# Lookup
# ===========================================================================

def test_unknown_tool_rejected():
    registry = ToolRegistry()
    with pytest.raises(KeyError, match="not registered"):
        registry.get("nonexistent")


def test_contains_returns_false_for_unknown():
    registry = ToolRegistry()
    assert not registry.contains("nonexistent")


# ===========================================================================
# LLMToolDefinition generation
# ===========================================================================

def test_to_llm_tool_definition():
    registry = ToolRegistry()
    registry.register(_make_tool(description="Get account-level stats"))
    defs = registry.to_llm_tool_definitions()
    assert len(defs) == 1
    d = defs[0]
    assert isinstance(d, LLMToolDefinition)
    assert d.name == "get_account_stats"
    assert d.description == "Get account-level stats"
    assert "properties" in d.input_schema


def test_schema_has_additional_properties_false():
    """The generated JSON schema must reject unknown fields."""
    registry = ToolRegistry()
    registry.register(_make_tool())
    defs = registry.to_llm_tool_definitions()
    schema = defs[0].input_schema
    assert schema.get("additionalProperties") is False


def test_authorization_fields_absent_from_schema():
    """No workspace_id, user_id, organization_id, allowed_account_ids."""
    registry = ToolRegistry()
    registry.register(_make_tool())
    schema = registry.to_llm_tool_definitions()[0].input_schema
    props = schema.get("properties", {})
    forbidden = {
        "workspace_id", "user_id", "organization_id",
        "allowed_account_ids", "slack_user_id", "team_id",
        "channel_id", "permission", "access_token",
    }
    for field in forbidden:
        assert field not in props, f"{field} must not be in tool schema"


# ===========================================================================
# Registry order is deterministic
# ===========================================================================

def test_registry_order_deterministic():
    registry = ToolRegistry()
    registry.register(_make_tool(name="aaa_tool"))
    registry.register(_make_tool(name="bbb_tool", description="B"))
    registry.register(_make_tool(name="ccc_tool", description="C"))
    assert registry.tool_names == ("aaa_tool", "bbb_tool", "ccc_tool")


def test_tool_definitions_preserve_order():
    registry = ToolRegistry()
    registry.register(_make_tool(name="aaa_tool"))
    registry.register(_make_tool(name="bbb_tool", description="B"))
    defs = registry.to_llm_tool_definitions()
    assert defs[0].name == "aaa_tool"
    assert defs[1].name == "bbb_tool"


# ===========================================================================
# Enum inlining ($ref resolution)
# ===========================================================================

def test_enum_ref_resolved_inline():
    """Schemas with enum $ref should be inlined for provider compatibility."""
    from apps.slack_bot.schemas import GetAccountStatsInput

    tool = RegisteredTool(
        name="get_account_stats",
        description="Get stats",
        input_schema_type=GetAccountStatsInput,
        executor=_fake_executor,
    )
    defn = tool.to_llm_tool_definition()
    schema = defn.input_schema
    # $defs should be removed after inlining
    assert "$defs" not in schema
    # platform property should have an inline enum
    platform_schema = schema["properties"]["platform"]
    assert "enum" in platform_schema
    assert "instagram" in platform_schema["enum"]
