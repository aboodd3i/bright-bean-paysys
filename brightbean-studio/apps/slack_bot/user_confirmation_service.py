"""Phase 5 — User access confirmation DM helper.

Sends a confirmation DM to users whose access was newly granted or restored.
Does NOT send to already-approved, invalid, or failed users.

Uses the existing Slack delivery abstraction (``send_slack_message``).
A delivery failure is logged and returned — it never raises and never
rolls back the database grant.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from .delivery import SlackDeliveryResult, send_slack_message

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

USER_CONFIRMATION_DM_TEXT = (
    "Your access to Social Media Analytics Bot has been enabled.\n\n"
    "Permission: Analytics read-only\n\n"
    "You can now mention the bot in Slack."
)


# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class UserConfirmationResult:
    """Outcome of sending confirmation DMs to granted/restored users.

    Attributes:
        notified: List of Slack user IDs that received the confirmation DM.
        failed: List of Slack user IDs where delivery failed.
    """

    notified: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Single-user confirmation
# ---------------------------------------------------------------------------


def send_user_confirmation_dm(
    *,
    workspace_id: str,
    slack_user_id: str,
    admin_slack_user_id: str = "",
    result_type: str = "approved",
) -> bool:
    """Send a confirmation DM to a single user.

    Returns ``True`` on success, ``False`` on failure.
    Never raises — failures are logged and returned.
    """
    result = send_slack_message(
        channel_id=slack_user_id,
        text=USER_CONFIRMATION_DM_TEXT,
    )

    if result.ok:
        logger.info(
            "access_enabled_user_dm_sent "
            "workspace_id=%s slack_user_id=%s "
            "administrator_slack_user_id=%s result_type=%s",
            workspace_id, slack_user_id,
            admin_slack_user_id, result_type,
        )
        return True

    logger.error(
        "access_enabled_user_dm_failed "
        "workspace_id=%s slack_user_id=%s "
        "administrator_slack_user_id=%s result_type=%s error=%s",
        workspace_id, slack_user_id,
        admin_slack_user_id, result_type, result.error,
    )
    return False


# ---------------------------------------------------------------------------
# Bulk confirmation
# ---------------------------------------------------------------------------


def send_bulk_user_confirmation_dms(
    *,
    workspace_id: str,
    slack_user_ids: list[str],
    admin_slack_user_id: str = "",
    result_type: str = "approved",
) -> UserConfirmationResult:
    """Send confirmation DMs to multiple users.

    A failure for one user does not prevent notifications to others.

    Returns a :class:`UserConfirmationResult` with notified/failed lists.
    """
    notified: list[str] = []
    failed: list[str] = []

    for uid in slack_user_ids:
        ok = send_user_confirmation_dm(
            workspace_id=workspace_id,
            slack_user_id=uid,
            admin_slack_user_id=admin_slack_user_id,
            result_type=result_type,
        )
        if ok:
            notified.append(uid)
        else:
            failed.append(uid)

    return UserConfirmationResult(notified=notified, failed=failed)
