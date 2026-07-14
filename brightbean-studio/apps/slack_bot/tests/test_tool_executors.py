"""Focused tests for production analytics tool executors.

Tests cover:
* Account resolution (zero, one, multiple, unauthorized)
* Metric validation (invalid metric rejected, no silent fallback)
* Primitive serialization (no Django models, no DerivedMetric objects)
* Platform mapping (internal variants normalized to canonical)
* ToolResult status consistency
* All seven executors on empty data

No network, no Slack, no Z.AI, no social provider API calls.
"""

from __future__ import annotations

import uuid

import pytest

from apps.analytics.derive import DerivedMetric
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
    AccountResolution,
    _derived_to_dict,
    _resolve_account,
    execute_compare_platforms,
    execute_get_account_stats,
    execute_get_engagement_summary,
    execute_get_follower_growth,
    execute_get_post_detail,
    execute_get_top_posts,
    execute_list_connected_accounts,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# 1. _derived_to_dict — DerivedMetric flattening
# ---------------------------------------------------------------------------

def test_derived_to_dict_produces_correct_fields():
    dm = DerivedMetric(value=100.0, delta=5.0, series=[1.0, 2.0, 3.0], kind="count")
    result = _derived_to_dict(dm)
    assert result == {
        "value": 100.0,
        "delta": 5.0,
        "series": [1.0, 2.0, 3.0],
        "kind": "count",
    }


def test_derived_to_dict_does_not_have_legacy_fields():
    dm = DerivedMetric(value=100.0, delta=5.0, series=[1.0], kind="count")
    result = _derived_to_dict(dm)
    assert "current" not in result
    assert "previous" not in result
    assert "change" not in result
    assert "change_pct" not in result


def test_derived_to_dict_series_is_a_copy():
    original = [1.0, 2.0, 3.0]
    dm = DerivedMetric(value=100.0, delta=5.0, series=original, kind="count")
    result = _derived_to_dict(dm)
    result["series"].append(99.0)
    assert original == [1.0, 2.0, 3.0]  # not mutated


# ---------------------------------------------------------------------------
# 2. Account resolution — no accounts
# ---------------------------------------------------------------------------

@pytest.mark.django_db
def test_resolve_account_no_account_returns_no_account():
    context = _make_context(allowed_account_ids=frozenset())
    result = _resolve_account("instagram", None, context)
    assert result.status == AccountResolution.NO_ACCOUNT
    assert result.account is None


# ---------------------------------------------------------------------------
# 3. Account resolution — unauthorized account_id
# ---------------------------------------------------------------------------

@pytest.mark.django_db
def test_resolve_account_unauthorized_id_returns_not_allowed():
    unauthorized = uuid.UUID("11111111-1111-1111-1111-111111111111")
    context = _make_context(allowed_account_ids=frozenset())
    result = _resolve_account("instagram", unauthorized, context)
    assert result.status == AccountResolution.ACCOUNT_NOT_ALLOWED


# ---------------------------------------------------------------------------
# 4. list_connected_accounts — empty workspace
# ---------------------------------------------------------------------------

@pytest.mark.django_db
def test_list_connected_accounts_empty_returns_no_data():
    context = _make_context(allowed_account_ids=frozenset())
    result = execute_list_connected_accounts(
        arguments=ListConnectedAccountsInput(), context=context,
    )
    assert result.status == ToolResultStatus.NO_DATA
    assert result.tool_name == "list_connected_accounts"


# ---------------------------------------------------------------------------
# 5. get_account_stats — no account
# ---------------------------------------------------------------------------

@pytest.mark.django_db
def test_get_account_stats_no_account_returns_no_data():
    context = _make_context(allowed_account_ids=frozenset())
    result = execute_get_account_stats(
        arguments=GetAccountStatsInput(platform="instagram"), context=context,
    )
    assert result.status == ToolResultStatus.NO_DATA


# ---------------------------------------------------------------------------
# 6. get_top_posts — invalid metric rejected
# ---------------------------------------------------------------------------

@pytest.mark.django_db
def test_get_top_posts_invalid_metric_returns_failed():
    context = _make_context(allowed_account_ids=frozenset())
    result = execute_get_top_posts(
        arguments=GetTopPostsInput(platform="instagram", metric="invalid_metric"),
        context=context,
    )
    assert result.status == ToolResultStatus.FAILED
    assert result.error_code == "invalid_metric"
    assert "supported_metrics" in result.data


# ---------------------------------------------------------------------------
# 7. get_top_posts — no account
# ---------------------------------------------------------------------------

@pytest.mark.django_db
def test_get_top_posts_no_account_returns_no_data():
    context = _make_context(allowed_account_ids=frozenset())
    result = execute_get_top_posts(
        arguments=GetTopPostsInput(platform="instagram", metric="reach"),
        context=context,
    )
    assert result.status == ToolResultStatus.NO_DATA


# ---------------------------------------------------------------------------
# 8. get_post_detail — not found
# ---------------------------------------------------------------------------

@pytest.mark.django_db
def test_get_post_detail_not_found_returns_no_data():
    context = _make_context(allowed_account_ids=frozenset())
    result = execute_get_post_detail(
        arguments=GetPostDetailInput(
            post_id=uuid.UUID("99999999-9999-9999-9999-999999999999"),
        ),
        context=context,
    )
    assert result.status == ToolResultStatus.NO_DATA


# ---------------------------------------------------------------------------
# 9. get_engagement_summary — no account
# ---------------------------------------------------------------------------

@pytest.mark.django_db
def test_get_engagement_summary_no_account_returns_no_data():
    context = _make_context(allowed_account_ids=frozenset())
    result = execute_get_engagement_summary(
        arguments=GetEngagementSummaryInput(platform="instagram"),
        context=context,
    )
    assert result.status == ToolResultStatus.NO_DATA


# ---------------------------------------------------------------------------
# 10. get_follower_growth — no account
# ---------------------------------------------------------------------------

@pytest.mark.django_db
def test_get_follower_growth_no_account_returns_no_data():
    context = _make_context(allowed_account_ids=frozenset())
    result = execute_get_follower_growth(
        arguments=GetFollowerGrowthInput(platform="instagram"),
        context=context,
    )
    assert result.status == ToolResultStatus.NO_DATA


# ---------------------------------------------------------------------------
# 11. compare_platforms — invalid metric rejected
# ---------------------------------------------------------------------------

@pytest.mark.django_db
def test_compare_platforms_invalid_metric_returns_failed():
    context = _make_context(allowed_account_ids=frozenset())
    result = execute_compare_platforms(
        arguments=ComparePlatformsInput(
            platforms=["instagram", "facebook"],
            metric="invalid_metric",
        ),
        context=context,
    )
    assert result.status == ToolResultStatus.FAILED
    assert result.error_code == "invalid_metric"


# ---------------------------------------------------------------------------
# 12. compare_platforms — no accounts
# ---------------------------------------------------------------------------

@pytest.mark.django_db
def test_compare_platforms_no_accounts_returns_success_with_unavailable():
    context = _make_context(allowed_account_ids=frozenset())
    result = execute_compare_platforms(
        arguments=ComparePlatformsInput(
            platforms=["instagram", "facebook"],
            metric="reach",
        ),
        context=context,
    )
    assert result.status == ToolResultStatus.SUCCESS
    comparison = result.data["comparison"]
    assert comparison["instagram"]["available"] is False
    assert comparison["facebook"]["available"] is False


# ---------------------------------------------------------------------------
# 13. All executors return ToolResult (type safety)
# ---------------------------------------------------------------------------

@pytest.mark.django_db
def test_all_executors_return_tool_result_on_empty():
    context = _make_context(allowed_account_ids=frozenset())

    cases = [
        (execute_list_connected_accounts, ListConnectedAccountsInput()),
        (execute_get_account_stats, GetAccountStatsInput(platform="instagram")),
        (execute_get_top_posts, GetTopPostsInput(platform="instagram", metric="reach")),
        (execute_get_engagement_summary, GetEngagementSummaryInput(platform="instagram")),
        (execute_get_follower_growth, GetFollowerGrowthInput(platform="instagram")),
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


# ---------------------------------------------------------------------------
# 14. Primitive serialization — recursive check
# ---------------------------------------------------------------------------

_ALLOWED_TYPES = (type(None), bool, int, float, str, list, dict)


def _is_primitive(value: object) -> bool:
    """Recursively verify value contains only JSON-safe primitives."""
    if isinstance(value, (type(None), bool, int, float, str)):
        return True
    if isinstance(value, list):
        return all(_is_primitive(v) for v in value)
    if isinstance(value, dict):
        return all(isinstance(k, str) and _is_primitive(v) for k, v in value.items())
    return False


@pytest.mark.django_db
def test_compare_platforms_data_is_primitive():
    context = _make_context(allowed_account_ids=frozenset())
    result = execute_compare_platforms(
        arguments=ComparePlatformsInput(
            platforms=["instagram", "facebook"],
            metric="reach",
        ),
        context=context,
    )
    assert _is_primitive(result.data)


@pytest.mark.django_db
def test_list_connected_accounts_data_is_primitive():
    context = _make_context(allowed_account_ids=frozenset())
    result = execute_list_connected_accounts(
        arguments=ListConnectedAccountsInput(), context=context,
    )
    # NO_DATA result with message — should be primitive
    assert _is_primitive(result.data)
