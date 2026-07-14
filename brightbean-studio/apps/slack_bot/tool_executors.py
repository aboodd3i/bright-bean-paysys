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
from datetime import timedelta
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
from .metric_validation import validate_metric
from .platform_mapping import (
    is_supported_internal,
    normalize_to_canonical,
)
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


class AccountResolution:
    """Outcome of account resolution.

    Attributes
    ----------
    status : str
        One of ``RESOLVED``, ``NO_ACCOUNT``, ``MULTIPLE_ACCOUNTS``,
        ``ACCOUNT_NOT_ALLOWED``.
    account : SocialAccount | None
        The resolved account when status is ``RESOLVED``.
    choices : list[dict[str, str]] | None
        Safe account references when status is ``MULTIPLE_ACCOUNTS``.
    """

    RESOLVED = "resolved"
    NO_ACCOUNT = "no_account"
    MULTIPLE_ACCOUNTS = "multiple_accounts"
    ACCOUNT_NOT_ALLOWED = "account_not_allowed"

    def __init__(
        self,
        *,
        status: str,
        account: SocialAccount | None = None,
        choices: list[dict[str, str]] | None = None,
    ) -> None:
        self.status = status
        self.account = account
        self.choices = choices


def _resolve_account(
    canonical_platform: str,
    account_id: uuid.UUID | None,
    context: ToolContext,
) -> AccountResolution:
    """Resolve a single social account for *canonical_platform* within *context*.

    Uses :mod:`apps.slack_bot.platform_mapping` to match internal platform
    variants (e.g. ``instagram_login`` → ``instagram``).

    Returns an :class:`AccountResolution` with one of:
    - ``RESOLVED`` — exactly one account found
    - ``NO_ACCOUNT`` — zero accounts found
    - ``MULTIPLE_ACCOUNTS`` — more than one account, no explicit ``account_id``
    - ``ACCOUNT_NOT_ALLOWED`` — ``account_id`` not in allowed set
    """
    from .platform_mapping import internal_platforms_for

    internal_variants = internal_platforms_for(canonical_platform)

    if account_id is not None:
        if account_id not in context.allowed_account_ids:
            return AccountResolution(status=AccountResolution.ACCOUNT_NOT_ALLOWED)
        account = SocialAccount.objects.filter(
            workspace_id=context.workspace_id,
            platform__in=internal_variants,
            connection_status__in=("connected", "token_expiring"),
            id=account_id,
        ).first()
        if account is None:
            return AccountResolution(status=AccountResolution.NO_ACCOUNT)
        return AccountResolution(status=AccountResolution.RESOLVED, account=account)

    accounts = list(
        SocialAccount.objects.filter(
            workspace_id=context.workspace_id,
            platform__in=internal_variants,
            connection_status__in=("connected", "token_expiring"),
            id__in=context.allowed_account_ids,
        )
    )

    if not accounts:
        return AccountResolution(status=AccountResolution.NO_ACCOUNT)
    if len(accounts) == 1:
        return AccountResolution(status=AccountResolution.RESOLVED, account=accounts[0])

    choices = [
        {
            "account_id": str(a.id),
            "platform": normalize_to_canonical(a.platform),
            "display_name": a.account_name or "",
            "handle": a.account_handle or "",
        }
        for a in accounts
    ]
    return AccountResolution(
        status=AccountResolution.MULTIPLE_ACCOUNTS,
        choices=choices,
    )


def _account_ref(account: SocialAccount) -> AccountReference:
    return AccountReference(
        account_id=account.id,
        platform=normalize_to_canonical(account.platform),
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
        data={"message": message},
    )


def _failed(tool_name: str, message: str) -> ToolResult:
    return ToolResult(
        status=ToolResultStatus.FAILED,
        tool_name=tool_name,
        error_code=ErrorCode.TOOL_EXECUTION_FAILED.value,
        data={"message": message},
    )


def _invalid_metric(
    tool_name: str,
    platform: str,
    metric: str,
    supported: frozenset[str],
) -> ToolResult:
    return ToolResult(
        status=ToolResultStatus.FAILED,
        tool_name=tool_name,
        error_code=ErrorCode.INVALID_METRIC.value,
        data={
            "message": f"Metric {metric!r} is not valid for {platform}.",
            "supported_metrics": sorted(supported),
        },
    )


def _clarification(
    tool_name: str,
    platform: str,
    choices: list[dict[str, str]],
) -> ToolResult:
    return ToolResult(
        status=ToolResultStatus.CLARIFICATION_REQUIRED,
        tool_name=tool_name,
        data={
            "message": f"Multiple {platform} accounts found. Specify account_id.",
            "accounts": choices,
        },
    )


def _derived_to_dict(derived: Any) -> dict[str, Any]:
    """Flatten a DerivedMetric into a safe primitive dict.

    DerivedMetric fields (from apps.analytics.derive):
    - value : float
    - delta : float  (percent change vs previous period)
    - series : list[float]
    - kind : str
    """
    return {
        "value": derived.value,
        "delta": derived.delta,
        "series": list(derived.series),
        "kind": derived.kind,
    }


# ---------------------------------------------------------------------------
# Executors
# ---------------------------------------------------------------------------


def execute_list_connected_accounts(
    *,
    arguments: ListConnectedAccountsInput,
    context: ToolContext,
) -> ToolResult:
    """List all connected social accounts in the caller's workspace.

    Only returns accounts on bot-supported platforms (Instagram, Facebook,
    LinkedIn).  Internal platform variants are normalized to canonical names.
    """
    accounts = SocialAccount.objects.filter(
        workspace_id=context.workspace_id,
        connection_status__in=("connected", "token_expiring"),
        id__in=context.allowed_account_ids,
    ).values_list("id", "platform", "account_name", "account_handle")

    account_list = [
        {
            "account_id": str(aid),
            "platform": normalize_to_canonical(platform),
            "display_name": name or "",
            "handle": handle or "",
        }
        for aid, platform, name, handle in accounts
        if is_supported_internal(platform)
    ]

    if not account_list:
        return _no_data(
            "list_connected_accounts",
            "No connected social accounts found in your workspace.",
        )

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
    canonical = arguments.platform.value
    resolution = _resolve_account(canonical, arguments.account_id, context)

    if resolution.status == AccountResolution.NO_ACCOUNT:
        return _no_data(
            "get_account_stats",
            f"No connected {canonical} account found.",
        )
    if resolution.status == AccountResolution.ACCOUNT_NOT_ALLOWED:
        return _failed("get_account_stats", "Account is not authorized.")
    if resolution.status == AccountResolution.MULTIPLE_ACCOUNTS:
        return _clarification("get_account_stats", canonical, resolution.choices or [])

    account = resolution.account
    assert account is not None  # noqa: S101 — guaranteed by RESOLVED

    bundle = account_analytics_bundle(account, arguments.days)
    series_map = bundle["series_map"]
    cards = hero_cards(account, arguments.days, series_map=series_map)
    captured_at = bundle["max_captured_at"]

    stats = {
        card["metric"]: {
            "label": card["label"],
            **_derived_to_dict(card["derived"]),
        }
        for card in cards
    }

    return ToolResult(
        status=ToolResultStatus.SUCCESS,
        tool_name="get_account_stats",
        platform=canonical,
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
    canonical = arguments.platform.value

    # Metric validation — no silent fallback.
    valid, supported = validate_metric(canonical, arguments.metric)
    if not valid:
        return _invalid_metric("get_top_posts", canonical, arguments.metric, supported)

    resolution = _resolve_account(canonical, arguments.account_id, context)

    if resolution.status == AccountResolution.NO_ACCOUNT:
        return _no_data(
            "get_top_posts",
            f"No connected {canonical} account found.",
        )
    if resolution.status == AccountResolution.ACCOUNT_NOT_ALLOWED:
        return _failed("get_top_posts", "Account is not authorized.")
    if resolution.status == AccountResolution.MULTIPLE_ACCOUNTS:
        return _clarification("get_top_posts", canonical, resolution.choices or [])

    account = resolution.account
    assert account is not None  # noqa: S101

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
            "caption": (row["caption"] or "")[:200],
            "date": row["date"],
            "stats": dict(row["stats"]),
        }
        for row in result["rows"]
    ]

    if not posts:
        return _no_data("get_top_posts", "No posts found in the specified period.")

    return ToolResult(
        status=ToolResultStatus.SUCCESS,
        tool_name="get_top_posts",
        platform=canonical,
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

    if not is_supported_internal(post.social_account.platform):
        return _no_data("get_post_detail", "Post is on an unsupported platform.")

    detail = post_detail(post)
    canonical = normalize_to_canonical(post.social_account.platform)

    # Flatten the service result into safe primitives — no Django models.
    safe_detail = {
        "post_id": str(post.id),
        "caption": detail.get("caption", ""),
        "date": detail.get("date", ""),
        "days_ago": detail.get("days_ago"),
        "media_kind": detail.get("media_kind", ""),
        "captured_at": detail.get("captured_at").isoformat()
        if detail.get("captured_at") is not None
        else None,
        "metric_tiles": [
            {
                "key": t["key"],
                "label": t["label"],
                "value": t["value"],
                "kind": t["kind"],
                "sparkline": list(t.get("sparkline", [])),
                "is_primary": t["is_primary"],
            }
            for t in detail.get("metric_tiles", [])
        ],
    }

    return ToolResult(
        status=ToolResultStatus.SUCCESS,
        tool_name="get_post_detail",
        platform=canonical,
        selected_account=_account_ref(post.social_account),
        data={"post": safe_detail},
    )


def execute_get_engagement_summary(
    *,
    arguments: GetEngagementSummaryInput,
    context: ToolContext,
) -> ToolResult:
    """Get engagement summary for a social account."""
    canonical = arguments.platform.value
    resolution = _resolve_account(canonical, arguments.account_id, context)

    if resolution.status == AccountResolution.NO_ACCOUNT:
        return _no_data(
            "get_engagement_summary",
            f"No connected {canonical} account found.",
        )
    if resolution.status == AccountResolution.ACCOUNT_NOT_ALLOWED:
        return _failed("get_engagement_summary", "Account is not authorized.")
    if resolution.status == AccountResolution.MULTIPLE_ACCOUNTS:
        return _clarification("get_engagement_summary", canonical, resolution.choices or [])

    account = resolution.account
    assert account is not None  # noqa: S101

    bundle = account_analytics_bundle(account, arguments.days)
    series_map = bundle["series_map"]
    eng = engagement_card(account, arguments.days, series_map=series_map)

    if eng is None:
        return _no_data(
            "get_engagement_summary",
            "Engagement data is not available for this platform.",
        )

    # Flatten DerivedMetric objects into safe primitives.
    safe_engagement = {
        "rate": _derived_to_dict(eng["rate"]),
        "parts": [
            {
                "metric": p["metric"],
                "label": p["label"],
                **_derived_to_dict(p["derived"]),
            }
            for p in eng.get("parts", [])
        ],
    }

    return ToolResult(
        status=ToolResultStatus.SUCCESS,
        tool_name="get_engagement_summary",
        platform=canonical,
        selected_account=_account_ref(account),
        period=_period(arguments.days),
        data_as_of=bundle["max_captured_at"],
        data={"engagement": safe_engagement},
    )


def execute_get_follower_growth(
    *,
    arguments: GetFollowerGrowthInput,
    context: ToolContext,
) -> ToolResult:
    """Get follower growth for a social account."""
    canonical = arguments.platform.value
    resolution = _resolve_account(canonical, arguments.account_id, context)

    if resolution.status == AccountResolution.NO_ACCOUNT:
        return _no_data(
            "get_follower_growth",
            f"No connected {canonical} account found.",
        )
    if resolution.status == AccountResolution.ACCOUNT_NOT_ALLOWED:
        return _failed("get_follower_growth", "Account is not authorized.")
    if resolution.status == AccountResolution.MULTIPLE_ACCOUNTS:
        return _clarification("get_follower_growth", canonical, resolution.choices or [])

    account = resolution.account
    assert account is not None  # noqa: S101

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
        platform=canonical,
        selected_account=_account_ref(account),
        period=_period(arguments.days),
        data_as_of=bundle["max_captured_at"],
        data={
            "metric": metric_key,
            **_derived_to_dict(derived),
        },
    )


def execute_compare_platforms(
    *,
    arguments: ComparePlatformsInput,
    context: ToolContext,
) -> ToolResult:
    """Compare a metric across multiple platforms."""
    # Validate metric against all requested platforms.
    for platform in arguments.platforms:
        valid, supported = validate_metric(platform.value, arguments.metric)
        if not valid:
            return _invalid_metric(
                "compare_platforms", platform.value, arguments.metric, supported,
            )

    comparison: dict[str, Any] = {}

    for platform in arguments.platforms:
        platform_val = platform.value
        resolution = _resolve_account(platform_val, None, context)

        if resolution.status != AccountResolution.RESOLVED:
            comparison[platform_val] = {"available": False}
            continue

        account = resolution.account
        assert account is not None  # noqa: S101

        bundle = account_analytics_bundle(account, arguments.days)
        series_map = bundle["series_map"]
        cards = hero_cards(account, arguments.days, series_map=series_map)

        metric_value = None
        for card in cards:
            if card["metric"] == arguments.metric:
                metric_value = _derived_to_dict(card["derived"])
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
