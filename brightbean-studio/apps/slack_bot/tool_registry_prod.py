"""Production tool registry for the Slack analytics bot.

Builds a :class:`~apps.slack_bot.tool_registry.ToolRegistry` with all
approved BrightBean analytics tools wired to their real executors in
:mod:`apps.slack_bot.tool_executors`.

This is the explicit allowlist — only tools registered here can be
called by the LLM.  No dynamic discovery.
"""

from __future__ import annotations

from .schemas import (
    ComparePlatformsInput,
    GetAccountStatsInput,
    GetEngagementSummaryInput,
    GetFollowerGrowthInput,
    GetPostDetailInput,
    GetTopPostsInput,
    ListConnectedAccountsInput,
)
from .tool_executors import (
    execute_compare_platforms,
    execute_get_account_stats,
    execute_get_engagement_summary,
    execute_get_follower_growth,
    execute_get_post_detail,
    execute_get_top_posts,
    execute_list_connected_accounts,
)
from .tool_registry import RegisteredTool, ToolRegistry


def build_tool_registry() -> ToolRegistry:
    """Build the production tool registry with all analytics tools.

    Returns a :class:`ToolRegistry` with the following tools registered:

    * ``list_connected_accounts`` — list accounts in the workspace
    * ``get_account_stats`` — aggregate metrics for one account
    * ``get_top_posts`` — top posts ranked by a metric
    * ``get_post_detail`` — detailed metrics for one post
    * ``get_engagement_summary`` — engagement rate summary
    * ``get_follower_growth`` — follower/subscriber growth
    * ``compare_platforms`` — compare a metric across platforms
    """
    registry = ToolRegistry()

    registry.register(RegisteredTool(
        name="list_connected_accounts",
        description=(
            "List all connected social media accounts in the caller's workspace. "
            "Takes no arguments — returns all accounts the user is authorized to see."
        ),
        input_schema_type=ListConnectedAccountsInput,
        executor=execute_list_connected_accounts,
    ))

    registry.register(RegisteredTool(
        name="get_account_stats",
        description=(
            "Get aggregate analytics stats (reach, impressions, engagement, etc.) "
            "for a social media account over a rolling window. "
            "Specify platform and optionally account_id. "
            "Days defaults to 30, bounded to [7, 90]."
        ),
        input_schema_type=GetAccountStatsInput,
        executor=execute_get_account_stats,
    ))

    registry.register(RegisteredTool(
        name="get_top_posts",
        description=(
            "Get top-performing posts ranked by a specific metric. "
            "Specify platform, metric (e.g. 'reach', 'engagement', 'views'), "
            "and optionally account_id. "
            "Limit defaults to 5 (max 20), days defaults to 30 (max 90)."
        ),
        input_schema_type=GetTopPostsInput,
        executor=execute_get_top_posts,
    ))

    registry.register(RegisteredTool(
        name="get_post_detail",
        description=(
            "Get detailed metrics for a single post by its ID. "
            "Requires post_id (UUID)."
        ),
        input_schema_type=GetPostDetailInput,
        executor=execute_get_post_detail,
    ))

    registry.register(RegisteredTool(
        name="get_engagement_summary",
        description=(
            "Get engagement rate summary for a social media account. "
            "Specify platform and optionally account_id. "
            "Days defaults to 30, bounded to [7, 90]."
        ),
        input_schema_type=GetEngagementSummaryInput,
        executor=execute_get_engagement_summary,
    ))

    registry.register(RegisteredTool(
        name="get_follower_growth",
        description=(
            "Get follower/subscriber growth for a social media account. "
            "Specify platform and optionally account_id. "
            "Days defaults to 30, bounded to [7, 90]."
        ),
        input_schema_type=GetFollowerGrowthInput,
        executor=execute_get_follower_growth,
    ))

    registry.register(RegisteredTool(
        name="compare_platforms",
        description=(
            "Compare a specific metric across multiple platforms. "
            "Specify a list of platforms (at least 2) and a metric. "
            "Days defaults to 30, bounded to [7, 90]."
        ),
        input_schema_type=ComparePlatformsInput,
        executor=execute_compare_platforms,
    ))

    return registry
