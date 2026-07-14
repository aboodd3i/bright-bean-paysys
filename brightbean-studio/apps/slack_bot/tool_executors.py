"""Real BrightBean analytics tool executors for the Slack bot.

Each executor is a callable matching the :class:`~apps.slack_bot.tool_registry.ToolExecutor`
protocol.  It receives **validated** Pydantic arguments and an
application-created :class:`~apps.slack_bot.contracts.ToolContext`, and
returns a :class:`~apps.slack_bot.contracts.ToolResult`.

Security invariants:
* Executors never receive raw dicts — arguments are already validated.
* Executors never receive Slack objects or provider-specific objects.
* ``account_id`` from the LLM is checked against
  ``context.allowed_account_ids`` — if not in the set, the executor
  returns a FAILED result with ``ACCOUNT_NOT_ALLOWED``.
* No secrets are returned in ``ToolResult.data``.

These executors wrap the existing read-side analytics services in
:mod:`apps.analytics.services` — they do not modify data, call external
APIs, or trigger syncs.
"""

from __future__ import annotations

import logging
import uuid
from datetime import date, timedelta
from typing import Any

from django.utils import timezone

from apps.analytics.services import (
    account_analytics_bundle,
    all_posts_for,
    engagement_card,
    follower_growth_metric,
    hero_cards,
    post_detail,
)
from apps.composer.models import PlatformPost
from apps.social_accounts.models import SocialAccount

from .contracts import (
    AccountReference,
    AnalyticsPeriod,
    ToolContext,
    ToolResult,
    ToolResultStatus,
)
from .errors import ErrorCode
from .schemas import (
    ComparePlatformsInput,
    GetAccountStatsInput,
    GetEngagementSummaryInput,
    GetFollowerGrowthInput,
    GetPostDetailInput,
    GetTopPostsInput,
    ListConnectedAccountsInput,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _resolve_account(
    platform: str,
    account_id: uuid.UUID | None,
    context: ToolContext,
) -> SocialAccount | None:
    """Resolve a single social account for *platform* within *context*.

    If *account_id* is provided, it must be in ``context.allowed_account_ids``.
    If not, auto-resolves the single connected account for the platform.
    Returns ``None`` if no account is found or the account is not allowed.
    """
    qs = SocialAccount.objects.filter(
        workspace_id=context.workspace_id,
        platform=platform,
        connection_status__in=("connected", "token_expiring"),
    )
    if account_id is not None:
        if account_id not in context.allowed_account_ids:
            return None
        qs = qs.filter(id=account_id)
    else:
        qs = qs.filter(id__in=context.allowed_account_ids)

    return qs.first()


def _account_ref(account: SocialAccount) -> AccountReference:
    return AccountReference(
        account_id=account.id,
        platform=account.platform,
        display_name=account.account_name or "",
        handle=account.account_handle or "",
    )


def _period(days: int) -> AnalyticsPeriod:
    end = timezone.now().date()
    start = end - timedelta(days=days - 1)
    return AnalyticsPeriod(start=start, end=end, days=days)


def _no_data(tool_name: str, message: str) -> ToolResult:
    return ToolResult(
        status=ToolResultStatus.NO_DATA,
        tool_name=tool_name,
        error_code=ErrorCode.NO_DATA.value,
        data={"message": message},
    )


def _failed(tool_name: str, message: str) -> ToolResult:
    return ToolResult(
        status=ToolResultStatus.FAILED,
        tool_name=tool_name,
        error_code=ErrorCode.TOOL_EXECUTION_FAILED.value,
        data={"message": message},
    )


# ---------------------------------------------------------------------------
# Executors
# ---------------------------------------------------------------------------


def execute_list_connected_accounts(
    *,
    arguments: ListConnectedAccountsInput,
    context: ToolContext,
) -> ToolResult:
    """List all connected social accounts in the caller's workspace."""
    accounts = SocialAccount.objects.filter(
        workspace_id=context.workspace_id,
        connection_status__in=("connected", "token_expiring"),
        id__in=context.allowed_account_ids,
    ).values_list("id", "platform", "account_name", "account_handle")

    if not accounts:
        return _no_data(
            "list_connected_accounts",
            "No connected social accounts found in your workspace.",
        )

    account_list = [
        {
            "account_id": str(aid),
            "platform": platform,
            "display_name": name or "",
            "handle": handle or "",
        }
        for aid, platform, name, handle in accounts
    ]

    return ToolResult(
        status=ToolResultStatus.SUCCESS,
        tool_name="list_connected_accounts",
        data={"accounts": account_list, "count": len(account_list)},
    )


def execute_get_account_stats(
    *,
    arguments: GetAccountStatsInput,
    context: ToolContext,
) -> ToolResult:
    """Get aggregate analytics stats for a social account."""
    account = _resolve_account(arguments.platform.value, arguments.account_id, context)
    if account is None:
        return _no_data(
            "get_account_stats",
            f"No connected {arguments.platform.value} account found.",
        )

    bundle = account_analytics_bundle(account, arguments.days)
    series_map = bundle["series_map"]
    cards = hero_cards(account, arguments.days, series_map=series_map)
    captured_at = bundle["max_captured_at"]

    stats = {
        card["metric"]: {
            "label": card["label"],
            "current": card["derived"].current,
            "previous": card["derived"].previous,
            "change": card["derived"].change,
            "change_pct": card["derived"].change_pct,
        }
        for card in cards
    }

    return ToolResult(
        status=ToolResultStatus.SUCCESS,
        tool_name="get_account_stats",
        platform=account.platform,
        selected_account=_account_ref(account),
        period=_period(arguments.days),
        data_as_of=captured_at,
        data={"stats": stats},
    )


def execute_get_top_posts(
    *,
    arguments: GetTopPostsInput,
    context: ToolContext,
) -> ToolResult:
    """Get top posts ranked by a metric."""
    account = _resolve_account(arguments.platform.value, arguments.account_id, context)
    if account is None:
        return _no_data(
            "get_top_posts",
            f"No connected {arguments.platform.value} account found.",
        )

    result = all_posts_for(
        account,
        days_filter=arguments.days,
        sort_key=arguments.metric,
        sort_dir="desc",
        page=1,
        page_size=arguments.limit,
    )

    posts = [
        {
            "post_id": str(row["post"].id),
            "caption": row["caption"][:200],
            "date": row["date"],
            "stats": row["stats"],
        }
        for row in result["rows"]
    ]

    if not posts:
        return _no_data("get_top_posts", "No posts found in the specified period.")

    return ToolResult(
        status=ToolResultStatus.SUCCESS,
        tool_name="get_top_posts",
        platform=account.platform,
        selected_account=_account_ref(account),
        period=_period(arguments.days),
        data={"posts": posts, "total": result["total"]},
    )


def execute_get_post_detail(
    *,
    arguments: GetPostDetailInput,
    context: ToolContext,
) -> ToolResult:
    """Get detailed metrics for a single post."""
    try:
        post = PlatformPost.objects.select_related("social_account", "post").get(
            id=arguments.post_id
        )
    except PlatformPost.DoesNotExist:
        return _no_data("get_post_detail", "Post not found.")

    if post.social_account_id not in context.allowed_account_ids:
        return _failed("get_post_detail", "You are not authorized to view this post.")

    detail = post_detail(post)

    return ToolResult(
        status=ToolResultStatus.SUCCESS,
        tool_name="get_post_detail",
        platform=post.social_account.platform,
        selected_account=_account_ref(post.social_account),
        data={"post": detail},
    )


def execute_get_engagement_summary(
    *,
    arguments: GetEngagementSummaryInput,
    context: ToolContext,
) -> ToolResult:
    """Get engagement summary for a social account."""
    account = _resolve_account(arguments.platform.value, arguments.account_id, context)
    if account is None:
        return _no_data(
            "get_engagement_summary",
            f"No connected {arguments.platform.value} account found.",
        )

    bundle = account_analytics_bundle(account, arguments.days)
    series_map = bundle["series_map"]
    eng = engagement_card(account, arguments.days, series_map=series_map)

    if eng is None:
        return _no_data(
            "get_engagement_summary",
            "Engagement data is not available for this platform.",
        )

    return ToolResult(
        status=ToolResultStatus.SUCCESS,
        tool_name="get_engagement_summary",
        platform=account.platform,
        selected_account=_account_ref(account),
        period=_period(arguments.days),
        data_as_of=bundle["max_captured_at"],
        data={"engagement": eng},
    )


def execute_get_follower_growth(
    *,
    arguments: GetFollowerGrowthInput,
    context: ToolContext,
) -> ToolResult:
    """Get follower growth for a social account."""
    account = _resolve_account(arguments.platform.value, arguments.account_id, context)
    if account is None:
        return _no_data(
            "get_follower_growth",
            f"No connected {arguments.platform.value} account found.",
        )

    bundle = account_analytics_bundle(account, arguments.days)
    series_map = bundle["series_map"]
    growth = follower_growth_metric(account, arguments.days, series_map=series_map)

    if growth is None:
        return _no_data(
            "get_follower_growth",
            "Follower growth data is not available for this platform.",
        )

    metric_key, derived = growth

    return ToolResult(
        status=ToolResultStatus.SUCCESS,
        tool_name="get_follower_growth",
        platform=account.platform,
        selected_account=_account_ref(account),
        period=_period(arguments.days),
        data_as_of=bundle["max_captured_at"],
        data={
            "metric": metric_key,
            "current": derived.current,
            "previous": derived.previous,
            "change": derived.change,
            "change_pct": derived.change_pct,
        },
    )


def execute_compare_platforms(
    *,
    arguments: ComparePlatformsInput,
    context: ToolContext,
) -> ToolResult:
    """Compare a metric across multiple platforms."""
    comparison: dict[str, Any] = {}

    for platform in arguments.platforms:
        platform_val = platform.value
        account = _resolve_account(platform_val, None, context)
        if account is None:
            comparison[platform_val] = {"available": False}
            continue

        bundle = account_analytics_bundle(account, arguments.days)
        series_map = bundle["series_map"]
        cards = hero_cards(account, arguments.days, series_map=series_map)

        metric_value = None
        for card in cards:
            if card["metric"] == arguments.metric:
                metric_value = {
                    "current": card["derived"].current,
                    "previous": card["derived"].previous,
                    "change": card["derived"].change,
                    "change_pct": card["derived"].change_pct,
                }
                break

        comparison[platform_val] = {
            "available": True,
            "account": _account_ref(account).display_name,
            arguments.metric: metric_value,
        }

    return ToolResult(
        status=ToolResultStatus.SUCCESS,
        tool_name="compare_platforms",
        period=_period(arguments.days),
        data={"comparison": comparison},
    )
