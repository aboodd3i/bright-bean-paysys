"""Phase 2 — Authorization and ToolContext resolution.

Resolves a :class:`~apps.slack_bot.contracts.SlackAnalyticsRequest` into
an immutable :class:`~apps.slack_bot.contracts.ToolContext` through a
strict, fail-closed chain:

1. **Team allowlist** — ``SLACK_ALLOWED_TEAM_ID`` must match.
2. **Channel mapping** — ``SlackChannelMapping`` → workspace.
3. **Workspace active** — ``workspace.is_archived`` must be False.
4. **User mapping** — ``SlackUserMapping`` → BrightBean user.
5. **User active** — ``user.is_active`` must be True.
6. **Org membership** — user must be a member of the workspace's org.
7. **Workspace membership** — user must be a member of the workspace.
8. **Permission** — ``view_analytics`` must be True in effective permissions.
9. **Account scoping** — connected ``SocialAccount`` rows in the workspace.

No LLM calls, no analytics queries, no external API calls.  Every failure
raises :class:`~apps.slack_bot.exceptions.AuthorizationError` with a
stable :class:`~apps.slack_bot.errors.ErrorCode`.
"""

from __future__ import annotations

import logging
import os

from django.conf import settings

from .contracts import SlackAnalyticsRequest, ToolContext
from .errors import ErrorCode
from .exceptions import AuthorizationError
from .models import SlackChannelMapping, SlackUserMapping

logger = logging.getLogger(__name__)

# Connection statuses that qualify an account for analytics queries.
_USABLE_CONNECTION_STATUSES = frozenset({"connected", "token_expiring"})


def _get_allowed_team_id() -> str:
    """Return the configured ``SLACK_ALLOWED_TEAM_ID`` (or empty string)."""
    return getattr(
        settings,
        "SLACK_ALLOWED_TEAM_ID",
        os.environ.get("SLACK_ALLOWED_TEAM_ID", ""),
    ).strip()


def _fail(error_code: ErrorCode, message: str) -> AuthorizationError:
    """Log and return an :class:`AuthorizationError` (does not raise)."""
    logger.warning("Authorization failed: code=%s msg=%s", error_code.value, message)
    return AuthorizationError(error_code, message)


def resolve_tool_context(request: SlackAnalyticsRequest) -> ToolContext:
    """Resolve *request* into an authorized :class:`ToolContext`.

    Raises
    ------
    AuthorizationError
        With a stable :class:`ErrorCode` for any failure.  The caller
        must catch this and produce an error response — never let it
        propagate to the user as a 500.

    Returns
    -------
    ToolContext
        Immutable, application-created authorization context.  No secrets.
    """
    # 1 — Team allowlist
    allowed_team_id = _get_allowed_team_id()
    if allowed_team_id and request.team_id != allowed_team_id:
        raise _fail(
            ErrorCode.UNAUTHORIZED,
            f"Team {request.team_id!r} is not in the allowlist",
        )

    # 2 — Channel mapping → workspace
    try:
        channel_mapping = SlackChannelMapping.objects.select_related(
            "workspace", "workspace__organization"
        ).get(team_id=request.team_id, channel_id=request.channel_id)
    except SlackChannelMapping.DoesNotExist:
        raise _fail(
            ErrorCode.CHANNEL_NOT_MAPPED,
            f"No workspace mapping for channel {request.channel_id!r} in team {request.team_id!r}",
        ) from None

    workspace = channel_mapping.workspace

    # 3 — Workspace must not be archived
    if workspace.is_archived:
        raise _fail(
            ErrorCode.WORKSPACE_UNAVAILABLE,
            f"Workspace {workspace.id} is archived",
        )

    # 4 — User mapping → BrightBean user
    try:
        user_mapping = SlackUserMapping.objects.select_related("user").get(
            slack_user_id=request.user_id,
            team_id=request.team_id,
        )
    except SlackUserMapping.DoesNotExist:
        raise _fail(
            ErrorCode.USER_NOT_MAPPED,
            f"No user mapping for Slack user {request.user_id!r} in team {request.team_id!r}",
        ) from None

    user = user_mapping.user

    # 5 — User must be active
    if not user.is_active:
        raise _fail(
            ErrorCode.UNAUTHORIZED,
            f"User {user.id} is not active",
        )

    # 6 — Org membership
    from apps.members.models import OrgMembership

    if not OrgMembership.objects.filter(
        user=user,
        organization=workspace.organization,
    ).exists():
        raise _fail(
            ErrorCode.UNAUTHORIZED,
            f"User {user.id} is not a member of organization {workspace.organization_id}",
        )

    # 7 — Workspace membership
    from apps.members.models import WorkspaceMembership

    try:
        ws_membership = WorkspaceMembership.objects.get(
            user=user,
            workspace=workspace,
        )
    except WorkspaceMembership.DoesNotExist:
        raise _fail(
            ErrorCode.UNAUTHORIZED,
            f"User {user.id} is not a member of workspace {workspace.id}",
        ) from None

    # 8 — Permission check
    permissions = ws_membership.effective_permissions
    if not permissions or not permissions.get("view_analytics", False):
        raise _fail(
            ErrorCode.UNAUTHORIZED,
            f"User {user.id} lacks view_analytics permission in workspace {workspace.id}",
        )

    # 9 — Account scoping: connected social accounts in this workspace
    from apps.social_accounts.models import SocialAccount

    allowed_accounts = SocialAccount.objects.filter(
        workspace=workspace,
        connection_status__in=_USABLE_CONNECTION_STATUSES,
    ).values_list("id", flat=True)

    allowed_account_ids = frozenset(allowed_accounts)

    logger.info(
        "Authorization resolved: user=%s workspace=%s org=%s accounts=%d",
        user.id,
        workspace.id,
        workspace.organization_id,
        len(allowed_account_ids),
    )

    return ToolContext(
        workspace_id=workspace.id,
        user_id=user.id,
        organization_id=workspace.organization_id,
        allowed_account_ids=allowed_account_ids,
        slack_team_id=request.team_id,
        slack_channel_id=request.channel_id,
    )
