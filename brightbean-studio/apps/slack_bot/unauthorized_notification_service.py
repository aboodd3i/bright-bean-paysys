"""Phase 4 — Unauthorized access notification service.

Handles the full flow when an unregistered user attempts to use the bot:
1. Record / update the UnauthorizedAccessAttempt row.
2. Send a DM to the unregistered user.
3. Send a generic response in the originating channel/thread.
4. Send a DM to the active administrator (with 24-hour cooldown).
5. Write audit log entries.

This module does NOT call the LLM, BrightBean, analytics tools,
conversation memory, or processing reactions.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import timedelta

from django.db import transaction
from django.utils import timezone

from .constants import (
    ACCESS_STATUS_REVOKED,
    ADMIN_STATUS_ACTIVE,
    AUDIT_ADMIN_NOTIFICATION_SENT,
    AUDIT_ADMIN_NOTIFICATION_SUPPRESSED,
    AUDIT_UNAUTHORIZED_ACCESS_ATTEMPT,
    SYSTEM_ACTOR,
)
from .delivery import SlackDeliveryResult, send_slack_message
from .models import (
    BotAccessAuditLog,
    BotAdministrator,
    BotUserAccess,
    UnauthorizedAccessAttempt,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

ADMIN_NOTIFICATION_COOLDOWN = timedelta(hours=24)

USER_DM_TEXT = (
    "Social Media Analytics Bot Access\n\n"
    "You do not currently have access to this bot.\n\n"
    "The bot administrator has been notified."
)

GENERIC_CHANNEL_RESPONSE = "I've sent you a direct message regarding access."


# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class UnauthorizedNotificationResult:
    """Outcome of the Phase 4 unauthorized-access notification flow.

    Attributes:
        handled: Always True — the flow ran to completion.
        is_unregistered: True if the user has no BotUserAccess record.
        is_revoked: True if the user has a REVOKED BotUserAccess record.
        user_dm_sent: True if the user DM was delivered successfully.
        channel_response_sent: True if the channel response was delivered.
        admin_dm_sent: True if the admin DM was sent (not suppressed).
        admin_dm_suppressed: True if the admin DM was suppressed by cooldown.
        admin_not_configured: True if no active admin exists for the workspace.
        attempt_count: The updated attempt_count for this user.
    """

    handled: bool
    is_unregistered: bool
    is_revoked: bool
    user_dm_sent: bool
    channel_response_sent: bool
    admin_dm_sent: bool
    admin_dm_suppressed: bool
    admin_not_configured: bool
    attempt_count: int


# ---------------------------------------------------------------------------
# User classification
# ---------------------------------------------------------------------------


def classify_user(workspace_id: str, slack_user_id: str) -> tuple[bool, bool]:
    """Classify a user's access status.

    Returns ``(is_unregistered, is_revoked)`` where:
    - ``is_unregistered``: no BotUserAccess record exists.
    - ``is_revoked``: a BotUserAccess record exists with status REVOKED.

    Exactly one of the two will be True when the user is not approved.
    If the user is approved, both will be False.
    """
    access = BotUserAccess.objects.filter(
        workspace_id=workspace_id,
        slack_user_id=slack_user_id,
    ).first()

    if access is None:
        return True, False
    if access.status == ACCESS_STATUS_REVOKED:
        return False, True
    return False, False


# ---------------------------------------------------------------------------
# Attempt recording
# ---------------------------------------------------------------------------


def record_unauthorized_attempt(
    *,
    workspace_id: str,
    slack_user_id: str,
    source_channel_id: str,
    message_ts: str,
) -> UnauthorizedAccessAttempt:
    """Create or update the UnauthorizedAccessAttempt row.

    Uses ``select_for_update`` inside a transaction to prevent
    duplicate admin notifications from concurrent attempts.

    Returns the updated attempt record.
    """
    now = timezone.now()

    with transaction.atomic():
        attempt, created = (
            UnauthorizedAccessAttempt.objects
            .select_for_update()
            .get_or_create(
                workspace_id=workspace_id,
                slack_user_id=slack_user_id,
                defaults={
                    "attempt_count": 1,
                    "first_attempt_at": now,
                    "last_attempt_at": now,
                    "last_source_channel_id": source_channel_id,
                    "last_message_ts": message_ts,
                },
            )
        )

        if not created:
            attempt.attempt_count += 1
            attempt.last_attempt_at = now
            attempt.last_source_channel_id = source_channel_id
            attempt.last_message_ts = message_ts
            attempt.save(update_fields=[
                "attempt_count",
                "last_attempt_at",
                "last_source_channel_id",
                "last_message_ts",
                "updated_at",
            ])

    logger.info(
        "unauthorized_attempt_recorded "
        "workspace_id=%s slack_user_id=%s attempt_count=%d",
        workspace_id, slack_user_id, attempt.attempt_count,
    )

    return attempt


# ---------------------------------------------------------------------------
# Audit helper
# ---------------------------------------------------------------------------


def _write_audit(
    *,
    workspace_id: str,
    target_slack_user_id: str,
    action: str,
    performed_by: str = SYSTEM_ACTOR,
    metadata: dict | None = None,
) -> None:
    """Write a BotAccessAuditLog entry."""
    BotAccessAuditLog.objects.create(
        workspace_id=workspace_id,
        target_slack_user_id=target_slack_user_id,
        performed_by_slack_user_id=performed_by,
        action=action,
        metadata=metadata or {},
    )


# ---------------------------------------------------------------------------
# Admin DM formatting
# ---------------------------------------------------------------------------


def format_admin_notification_dm(
    *,
    slack_user_id: str,
    source_channel_id: str,
    attempted_at,
) -> str:
    """Format the administrator notification DM text."""
    formatted_ts = attempted_at.strftime("%Y-%m-%d %H:%M:%S %Z").strip()
    return (
        "Unauthorized Bot Access Attempt\n\n"
        f"Member ID: {slack_user_id}\n"
        f"Source Channel ID: {source_channel_id}\n"
        f"Attempted At: {formatted_ts}\n\n"
        "This user does not currently have access to the bot.\n"
        "To grant analytics read-only access, reply:\n\n"
        f"Give {slack_user_id} access as user@company.com"
    )


# ---------------------------------------------------------------------------
# Admin notification with 24-hour cooldown
# ---------------------------------------------------------------------------


def _should_notify_admin(attempt: UnauthorizedAccessAttempt) -> bool:
    """Return True if the 24-hour cooldown has elapsed (or no prior notification)."""
    if attempt.last_admin_notification_at is None:
        return True
    return timezone.now() - attempt.last_admin_notification_at >= ADMIN_NOTIFICATION_COOLDOWN


def send_admin_notification(
    *,
    workspace_id: str,
    slack_user_id: str,
    source_channel_id: str,
    attempt: UnauthorizedAccessAttempt,
) -> tuple[bool, bool, bool]:
    """Send admin DM with 24-hour cooldown.

    Returns ``(admin_dm_sent, admin_dm_suppressed, admin_not_configured)``.
    """
    admin = BotAdministrator.objects.filter(
        workspace_id=workspace_id,
        status=ADMIN_STATUS_ACTIVE,
    ).first()

    if admin is None:
        logger.info(
            "admin_not_configured workspace_id=%s slack_user_id=%s",
            workspace_id, slack_user_id,
        )
        return False, False, True

    if not _should_notify_admin(attempt):
        logger.info(
            "admin_notification_suppressed "
            "workspace_id=%s slack_user_id=%s administrator_slack_user_id=%s "
            "attempt_count=%d",
            workspace_id, slack_user_id, admin.slack_user_id,
            attempt.attempt_count,
        )
        _write_audit(
            workspace_id=workspace_id,
            target_slack_user_id=slack_user_id,
            action=AUDIT_ADMIN_NOTIFICATION_SUPPRESSED,
            metadata={
                "attempt_count": attempt.attempt_count,
                "administrator_slack_user_id": admin.slack_user_id,
            },
        )
        return False, True, False

    # Send the admin DM
    dm_text = format_admin_notification_dm(
        slack_user_id=slack_user_id,
        source_channel_id=source_channel_id,
        attempted_at=attempt.last_attempt_at or timezone.now(),
    )

    result = send_slack_message(
        channel_id=admin.slack_user_id,
        text=dm_text,
    )

    if not result.ok:
        logger.error(
            "admin_notification_failed "
            "workspace_id=%s slack_user_id=%s administrator_slack_user_id=%s "
            "error=%s",
            workspace_id, slack_user_id, admin.slack_user_id,
            result.error,
        )
        # Do NOT update last_admin_notification_at — allow retry
        return False, False, False

    # Success — update timestamp atomically
    with transaction.atomic():
        UnauthorizedAccessAttempt.objects.filter(
            pk=attempt.pk,
        ).update(last_admin_notification_at=timezone.now())

    logger.info(
        "admin_notification_sent "
        "workspace_id=%s slack_user_id=%s administrator_slack_user_id=%s "
        "channel_id=%s attempt_count=%d",
        workspace_id, slack_user_id, admin.slack_user_id,
        source_channel_id, attempt.attempt_count,
    )
    _write_audit(
        workspace_id=workspace_id,
        target_slack_user_id=slack_user_id,
        action=AUDIT_ADMIN_NOTIFICATION_SENT,
        performed_by=admin.slack_user_id,
        metadata={
            "attempt_count": attempt.attempt_count,
            "source_channel_id": source_channel_id,
        },
    )
    return True, False, False


# ---------------------------------------------------------------------------
# Main orchestrator
# ---------------------------------------------------------------------------


def handle_unauthorized_access(
    *,
    workspace_id: str,
    slack_user_id: str,
    source_channel_id: str,
    message_ts: str,
    thread_ts: str = "",
) -> UnauthorizedNotificationResult:
    """Run the full Phase 4 unauthorized-access notification flow.

    This function is called only when the access gate has rejected
    the user AND the user is unregistered (no BotUserAccess record).

    Steps:
    1. Record / update UnauthorizedAccessAttempt.
    2. Write UNAUTHORIZED_ACCESS_ATTEMPT audit.
    3. Send user DM.
    4. Send generic channel/thread response.
    5. Send admin DM (with 24-hour cooldown).

    Returns an UnauthorizedNotificationResult.
    """
    # --- 1. Record the attempt ---
    attempt = record_unauthorized_attempt(
        workspace_id=workspace_id,
        slack_user_id=slack_user_id,
        source_channel_id=source_channel_id,
        message_ts=message_ts,
    )

    # --- 2. Audit ---
    _write_audit(
        workspace_id=workspace_id,
        target_slack_user_id=slack_user_id,
        action=AUDIT_UNAUTHORIZED_ACCESS_ATTEMPT,
        metadata={
            "attempt_count": attempt.attempt_count,
            "source_channel_id": source_channel_id,
        },
    )

    # --- 3. Send user DM ---
    user_dm_sent = False
    user_result = send_slack_message(
        channel_id=slack_user_id,
        text=USER_DM_TEXT,
    )
    if user_result.ok:
        user_dm_sent = True
        logger.info(
            "unauthorized_user_dm_sent "
            "workspace_id=%s slack_user_id=%s",
            workspace_id, slack_user_id,
        )
    else:
        logger.error(
            "unauthorized_user_dm_failed "
            "workspace_id=%s slack_user_id=%s error=%s",
            workspace_id, slack_user_id, user_result.error,
        )

    # --- 4. Send generic channel/thread response ---
    channel_response_sent = False
    channel_result = send_slack_message(
        channel_id=source_channel_id,
        text=GENERIC_CHANNEL_RESPONSE,
        thread_ts=thread_ts or "",
    )
    if channel_result.ok:
        channel_response_sent = True
    else:
        logger.error(
            "unauthorized_channel_response_failed "
            "workspace_id=%s slack_user_id=%s channel_id=%s error=%s",
            workspace_id, slack_user_id, source_channel_id,
            channel_result.error,
        )

    # --- 5. Admin notification with 24-hour cooldown ---
    admin_dm_sent, admin_dm_suppressed, admin_not_configured = send_admin_notification(
        workspace_id=workspace_id,
        slack_user_id=slack_user_id,
        source_channel_id=source_channel_id,
        attempt=attempt,
    )

    return UnauthorizedNotificationResult(
        handled=True,
        is_unregistered=True,
        is_revoked=False,
        user_dm_sent=user_dm_sent,
        channel_response_sent=channel_response_sent,
        admin_dm_sent=admin_dm_sent,
        admin_dm_suppressed=admin_dm_suppressed,
        admin_not_configured=admin_not_configured,
        attempt_count=attempt.attempt_count,
    )
