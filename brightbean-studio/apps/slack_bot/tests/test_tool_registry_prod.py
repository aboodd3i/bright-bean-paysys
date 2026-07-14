"""Tests for the production tool registry and real BrightBean executors.

Validates that:
* All seven expected tools are registered with correct names.
* Each tool has the correct input schema.
* Each executor is callable.
* Duplicate tools are rejected.
* Generated JSON schemas contain no authorization fields.
* Registry order is deterministic.
* No fake executor enters the production registry.
"""

from __future__ import annotations

import uuid

import pytest

from apps.slack_bot.contracts import (
    ToolContext,
    ToolResult,
    ToolResultStatus,
)
from apps.slack_bot.schemas import (
    ComparePlatformsInput,
    GetAccountStatsInput,
    GetEngagementSummaryInput,
    GetFollowerGrowthInput,
    GetPostDetailInput,
    GetTopPostsInput,
    ListConnectedAccountsInput,
)
from apps.slack_bot.tool_executors import (
    execute_compare_platforms,
    execute_get_account_stats,
    execute_get_engagement_summary,
    execute_get_follower_growth,
    execute_get_post_detail,
    execute_get_top_posts,
    execute_list_connected_accounts,
)
from apps.slack_bot.tool_registry_prod import build_tool_registry

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

EXPECTED_TOOLS = (
    "list_connected_accounts",
    "get_account_stats",
    "get_top_posts",
    "get_post_detail",
    "get_engagement_summary",
    "get_follower_growth",
    "compare_platforms",
)

EXPECTED_SCHEMAS = {
    "list_connected_accounts": ListConnectedAccountsInput,
    "get_account_stats": GetAccountStatsInput,
    "get_top_posts": GetTopPostsInput,
    "get_post_detail": GetPostDetailInput,
    "get_engagement_summary": GetEngagementSummaryInput,
    "get_follower_growth": GetFollowerGrowthInput,
    "compare_platforms": ComparePlatformsInput,
}

EXPECTED_EXECUTORS = {
    "list_connected_accounts": execute_list_connected_accounts,
    "get_account_stats": execute_get_account_stats,
    "get_top_posts": execute_get_top_posts,
    "get_post_detail": execute_get_post_detail,
    "get_engagement_summary": execute_get_engagement_summary,
    "get_follower_growth": execute_get_follower_growth,
    "compare_platforms": execute_compare_platforms,
}

FORBIDDEN_SCHEMA_FIELDS = {
    "workspace_id",
    "user_id",
    "organization_id",
    "allowed_account_ids",
    "slack_user_id",
    "slack_team_id",
    "slack_channel_id",
    "team_id",
    "channel_id",
    "permission",
    "access_token",
    "api_key",
}


def _make_context(
    *,
    allowed_account_ids: frozenset[uuid.UUID] | None = None,
) -> ToolContext:
    return ToolContext(
        workspace_id=uuid.UUID("00000000-0000-0000-0000-000000000001"),
        user_id=uuid.UUID("00000000-0000-0000-0000-000000000002"),
        organization_id=uuid.UUID("00000000-0000-0000-0000-000000000003"),
        allowed_account_ids=allowed_account_ids or frozenset(),
        slack_team_id="T0001",
        slack_channel_id="C0001",
    )


# ===========================================================================
# 1. Registry construction — all seven tools registered
# ===========================================================================

def test_all_seven_tools_registered():
    registry = build_tool_registry()
    names = registry.tool_names
    assert len(names) == 7
    for expected in EXPECTED_TOOLS:
        assert expected in names, f"{expected} not registered"


def test_tool_names_match_expected_exactly():
    registry = build_tool_registry()
    assert set(registry.tool_names) == set(EXPECTED_TOOLS)


def test_registry_order_deterministic():
    r1 = build_tool_registry()
    r2 = build_tool_registry()
    assert r1.tool_names == r2.tool_names


# ===========================================================================
# 2. Each tool has the correct input schema
# ===========================================================================

@pytest.mark.parametrize("tool_name", list(EXPECTED_SCHEMAS.keys()))
def test_tool_has_correct_schema(tool_name):
    registry = build_tool_registry()
    tool = registry.get(tool_name)
    assert tool.input_schema_type is EXPECTED_SCHEMAS[tool_name], (
        f"{tool_name} has wrong schema: {tool.input_schema_type}"
    )


# ===========================================================================
# 3. Each executor is callable
# ===========================================================================

@pytest.mark.parametrize("tool_name", list(EXPECTED_EXECUTORS.keys()))
def test_executor_is_callable(tool_name):
    registry = build_tool_registry()
    tool = registry.get(tool_name)
    assert callable(tool.executor), f"{tool_name} executor is not callable"
    assert tool.executor is EXPECTED_EXECUTORS[tool_name], (
        f"{tool_name} has wrong executor"
    )


# ===========================================================================
# 4. No authorization fields in generated JSON schemas
# ===========================================================================

@pytest.mark.parametrize("tool_name", list(EXPECTED_SCHEMAS.keys()))
def test_no_authorization_fields_in_schema(tool_name):
    registry = build_tool_registry()
    tool = registry.get(tool_name)
    schema = tool.to_llm_tool_definition().input_schema
    props = schema.get("properties", {})
    for field in FORBIDDEN_SCHEMA_FIELDS:
        assert field not in props, (
            f"{field} must not be in {tool_name} schema"
        )


# ===========================================================================
# 5. All schemas reject extra fields (extra="forbid")
# ===========================================================================

@pytest.mark.parametrize("tool_name", list(EXPECTED_SCHEMAS.keys()))
def test_schema_rejects_extra_fields(tool_name):
    registry = build_tool_registry()
    tool = registry.get(tool_name)
    schema = tool.to_llm_tool_definition().input_schema
    assert schema.get("additionalProperties") is False, (
        f"{tool_name} schema must set additionalProperties=false"
    )


# ===========================================================================
# 6. Tool names match system prompt
# ===========================================================================

def test_tool_names_match_system_prompt():
    from apps.slack_bot.llm_prompt import SYSTEM_PROMPT

    registry = build_tool_registry()
    for name in registry.tool_names:
        assert name in SYSTEM_PROMPT, (
            f"Tool {name!r} not mentioned in system prompt"
        )


# ===========================================================================
# 7. No fake executor in production registry
# ===========================================================================

def test_no_fake_executor_in_production():
    registry = build_tool_registry()
    for name in registry.tool_names:
        tool = registry.get(name)
        # All production executors must be from tool_executors module
        module = tool.executor.__module__
        assert "tool_executors" in module, (
            f"{name} executor is from {module}, not tool_executors"
        )


# ===========================================================================
# 8. Duplicate registration rejected
# ===========================================================================

def test_duplicate_registration_rejected():
    from apps.slack_bot.tool_registry import RegisteredTool, ToolRegistry

    registry = ToolRegistry()
    tool = RegisteredTool(
        name="get_account_stats",
        description="Get stats",
        input_schema_type=GetAccountStatsInput,
        executor=execute_get_account_stats,
    )
    registry.register(tool)
    with pytest.raises(ValueError, match="already registered"):
        registry.register(tool)


# ===========================================================================
# 9. LLM tool definitions generated correctly
# ===========================================================================

def test_to_llm_tool_definitions_count():
    registry = build_tool_registry()
    defs = registry.to_llm_tool_definitions()
    assert len(defs) == 7


def test_to_llm_tool_definitions_names():
    registry = build_tool_registry()
    defs = registry.to_llm_tool_definitions()
    names = [d.name for d in defs]
    assert set(names) == set(EXPECTED_TOOLS)


# ===========================================================================
# 10. Executor account scoping — _resolve_account
# ===========================================================================

@pytest.mark.django_db
def test_resolve_account_rejects_unauthorized_account():
    """_resolve_account returns ACCOUNT_NOT_ALLOWED when account_id not in allowed_account_ids."""
    from apps.slack_bot.tool_executors import AccountResolution, _resolve_account

    unauthorized_id = uuid.UUID("11111111-1111-1111-1111-111111111111")
    context = _make_context(allowed_account_ids=frozenset())  # empty allowlist

    result = _resolve_account("instagram", unauthorized_id, context)
    assert result.status == AccountResolution.ACCOUNT_NOT_ALLOWED


@pytest.mark.django_db
def test_resolve_account_rejects_when_no_accounts_allowed():
    """_resolve_account returns NO_ACCOUNT when allowed_account_ids is empty."""
    from apps.slack_bot.tool_executors import AccountResolution, _resolve_account

    context = _make_context(allowed_account_ids=frozenset())
    result = _resolve_account("instagram", None, context)
    assert result.status == AccountResolution.NO_ACCOUNT


# ===========================================================================
# 11. Executor returns ToolResult — list_connected_accounts
# ===========================================================================

@pytest.mark.django_db
def test_list_connected_accounts_no_data():
    """Empty workspace returns NO_DATA result."""
    context = _make_context(allowed_account_ids=frozenset())
    args = ListConnectedAccountsInput()
    result = execute_list_connected_accounts(arguments=args, context=context)
    assert isinstance(result, ToolResult)
    assert result.status == ToolResultStatus.NO_DATA
    assert result.tool_name == "list_connected_accounts"


# ===========================================================================
# 12. Executor returns ToolResult — get_account_stats with no account
# ===========================================================================

@pytest.mark.django_db
def test_get_account_stats_no_account():
    """No connected account returns NO_DATA."""
    context = _make_context(allowed_account_ids=frozenset())
    args = GetAccountStatsInput(platform="instagram")
    result = execute_get_account_stats(arguments=args, context=context)
    assert isinstance(result, ToolResult)
    assert result.status == ToolResultStatus.NO_DATA
    assert result.tool_name == "get_account_stats"


# ===========================================================================
# 13. Executor returns ToolResult — get_top_posts with no account
# ===========================================================================

@pytest.mark.django_db
def test_get_top_posts_no_account():
    context = _make_context(allowed_account_ids=frozenset())
    args = GetTopPostsInput(platform="instagram", metric="reach")
    result = execute_get_top_posts(arguments=args, context=context)
    assert isinstance(result, ToolResult)
    assert result.status == ToolResultStatus.NO_DATA


# ===========================================================================
# 14. Executor returns ToolResult — get_engagement_summary with no account
# ===========================================================================

@pytest.mark.django_db
def test_get_engagement_summary_no_account():
    context = _make_context(allowed_account_ids=frozenset())
    args = GetEngagementSummaryInput(platform="instagram")
    result = execute_get_engagement_summary(arguments=args, context=context)
    assert isinstance(result, ToolResult)
    assert result.status == ToolResultStatus.NO_DATA


# ===========================================================================
# 15. Executor returns ToolResult — get_follower_growth with no account
# ===========================================================================

@pytest.mark.django_db
def test_get_follower_growth_no_account():
    context = _make_context(allowed_account_ids=frozenset())
    args = GetFollowerGrowthInput(platform="instagram")
    result = execute_get_follower_growth(arguments=args, context=context)
    assert isinstance(result, ToolResult)
    assert result.status == ToolResultStatus.NO_DATA


# ===========================================================================
# 16. Executor returns ToolResult — get_post_detail not found
# ===========================================================================

@pytest.mark.django_db
def test_get_post_detail_not_found():
    context = _make_context(allowed_account_ids=frozenset())
    args = GetPostDetailInput(
        post_id=uuid.UUID("99999999-9999-9999-9999-999999999999")
    )
    result = execute_get_post_detail(arguments=args, context=context)
    assert isinstance(result, ToolResult)
    assert result.status == ToolResultStatus.NO_DATA


# ===========================================================================
# 17. Executor returns ToolResult — compare_platforms with no accounts
# ===========================================================================

@pytest.mark.django_db
def test_compare_platforms_no_accounts():
    context = _make_context(allowed_account_ids=frozenset())
    args = ComparePlatformsInput(
        platforms=["instagram", "facebook"],
        metric="reach",
    )
    result = execute_compare_platforms(arguments=args, context=context)
    assert isinstance(result, ToolResult)
    assert result.status == ToolResultStatus.SUCCESS
    comparison = result.data["comparison"]
    assert comparison["instagram"]["available"] is False
    assert comparison["facebook"]["available"] is False


# ===========================================================================
# 18. All executors return ToolResult instances (type safety)
# ===========================================================================

@pytest.mark.django_db
def test_all_executors_return_tool_result_on_empty():
    """Every executor must return a ToolResult, not a dict or other type."""
    context = _make_context(allowed_account_ids=frozenset())

    cases = [
        (execute_list_connected_accounts, ListConnectedAccountsInput()),
        (execute_get_account_stats, GetAccountStatsInput(platform="instagram")),
        (execute_get_top_posts, GetTopPostsInput(platform="instagram", metric="reach")),
        (
            execute_get_engagement_summary,
            GetEngagementSummaryInput(platform="instagram"),
        ),
        (
            execute_get_follower_growth,
            GetFollowerGrowthInput(platform="instagram"),
        ),
        (
            execute_compare_platforms,
            ComparePlatformsInput(platforms=["instagram", "facebook"], metric="reach"),
        ),
    ]

    for executor, args in cases:
        result = executor(arguments=args, context=context)
        assert isinstance(result, ToolResult), (
            f"{executor.__name__} returned {type(result).__name__}"
        )
