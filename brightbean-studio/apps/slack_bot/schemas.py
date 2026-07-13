"""LLM-facing tool input schemas for the Slack analytics bot.

These schemas define what the LLM is allowed to pass as arguments to
each approved analytics tool.  They are **strict** — unknown fields are
rejected via ``model_config = ConfigDict(extra="forbid")`` so the LLM
cannot inject authorization scope.

Security rules enforced by these schemas:
* No ``workspace_id``, ``user_id``, ``organization_id``, or
  ``allowed_account_ids`` fields — authorization comes from
  :class:`apps.slack_bot.contracts.ToolContext` which is
  application-created, never LLM-created.
* ``days`` is bounded to ``[7, 90]`` matching the existing Agent API
  constraint in ``apps/api/routers/analytics.py``.
* ``limit`` is bounded to ``[1, 20]`` to prevent unbounded result sets.
* ``platform`` uses :class:`SupportedPlatform` so unsupported values
  are rejected at validation time.

Technology: ``ninja.Schema`` (Pydantic-backed) — matches
``apps/api/schemas.py`` conventions.
"""

from __future__ import annotations

import uuid

from ninja import Field, Schema
from pydantic import ConfigDict

from .contracts import SupportedPlatform

# Bounded ranges — match the existing Agent API constraints.
MIN_DAYS = 7
MAX_DAYS = 90
MIN_LIMIT = 1
MAX_LIMIT = 20


class _BaseToolInput(Schema):
    """Base for all tool inputs — rejects unknown fields."""

    model_config = ConfigDict(extra="forbid")


class ListConnectedAccountsInput(_BaseToolInput):
    """Input for ``list_connected_accounts``.

    Takes no LLM-controlled arguments — the tool returns all accounts
    in the caller's :class:`ToolContext.allowed_account_ids`.
    """


class GetAccountStatsInput(_BaseToolInput):
    """Input for ``get_account_stats``.

    Fields
    ------
    platform : SupportedPlatform
        Which social platform to query.
    account_id : uuid.UUID | None
        Specific account to query.  If ``None``, the tool resolves
        the single connected account for the platform (or asks for
        clarification if multiple exist).
    days : int
        Rolling window size in days.  Bounded to ``[7, 90]``.
    """

    platform: SupportedPlatform
    account_id: uuid.UUID | None = None
    days: int = Field(30, ge=MIN_DAYS, le=MAX_DAYS)


class GetTopPostsInput(_BaseToolInput):
    """Input for ``get_top_posts``.

    Fields
    ------
    platform : SupportedPlatform
    account_id : uuid.UUID | None
        Specific account, or ``None`` for auto-resolution.
    metric : str
        Metric to rank by (e.g. ``"reach"``, ``"engagement"``, ``"views"``).
        Validated against the platform's metric catalog at execution time.
    limit : int
        Maximum number of posts to return.  Bounded to ``[1, 20]``.
    days : int
        Rolling window.  Bounded to ``[7, 90]``.
    """

    platform: SupportedPlatform
    account_id: uuid.UUID | None = None
    metric: str
    limit: int = Field(5, ge=MIN_LIMIT, le=MAX_LIMIT)
    days: int = Field(30, ge=MIN_DAYS, le=MAX_DAYS)


class GetPostDetailInput(_BaseToolInput):
    """Input for ``get_post_detail``.

    Fields
    ------
    post_id : uuid.UUID
        The BrightBean ``PlatformPost.id`` to fetch details for.
        Scoped to :class:`ToolContext.allowed_account_ids` at execution
        time.
    """

    post_id: uuid.UUID


class GetEngagementSummaryInput(_BaseToolInput):
    """Input for ``get_engagement_summary``.

    Fields
    ------
    platform : SupportedPlatform
    account_id : uuid.UUID | None
    days : int
        Rolling window.  Bounded to ``[7, 90]``.
    """

    platform: SupportedPlatform
    account_id: uuid.UUID | None = None
    days: int = Field(30, ge=MIN_DAYS, le=MAX_DAYS)


class GetFollowerGrowthInput(_BaseToolInput):
    """Input for ``get_follower_growth``.

    Fields
    ------
    platform : SupportedPlatform
    account_id : uuid.UUID | None
    days : int
        Rolling window.  Bounded to ``[7, 90]``.
    """

    platform: SupportedPlatform
    account_id: uuid.UUID | None = None
    days: int = Field(30, ge=MIN_DAYS, le=MAX_DAYS)


class ComparePlatformsInput(_BaseToolInput):
    """Input for ``compare_platforms``.

    Fields
    ------
    platforms : list[SupportedPlatform]
        Two or more platforms to compare.  Each must be a
        :class:`SupportedPlatform`.
    metric : str
        Metric to compare across platforms (e.g. ``"reach"``).
    days : int
        Rolling window.  Bounded to ``[7, 90]``.
    """

    platforms: list[SupportedPlatform]
    metric: str
    days: int = Field(30, ge=MIN_DAYS, le=MAX_DAYS)
