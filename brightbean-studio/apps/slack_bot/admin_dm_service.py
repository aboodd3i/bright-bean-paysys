"""Service layer for administrator DM access-grant commands.

Handles the full flow:
1. Check if the DM sender is the active BotAdministrator for the workspace.
2. Parse the DM text for a grant intent.
3. Execute the grant via the Phase B provisioning service.
4. Format the response text.

This module does NOT send the Slack DM — the caller (views.py) is
responsible for delivering the response via ``send_slack_message``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from .access_provisioning import (
    ProvisioningFailureReason,
    ProvisioningResult,
    ProvisioningStatus,
    grant_slack_analytics_access,
)
from .admin_dm_parser import GrantCommandResult, USAGE_MESSAGE, parse_grant_command
from .admin_dm_response import (
    format_bulk_provisioning_response,
    format_provisioning_response,
)
from .constants import ADMIN_STATUS_ACTIVE
from .models import BotAdministrator, UnauthorizedAccessAttempt
from .user_confirmation_service import (
    send_bulk_user_confirmation_dms,
    send_user_confirmation_dm,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AdminDMResult:
    """Outcome of processing an admin DM.

    Attributes:
        is_admin_dm: True if the sender is the active admin for the workspace.
        handled: True if a grant command was recognised and processed.
        response_text: The text to send back to the admin (empty if not handled).
        workspace_id: The workspace ID from the event.
        admin_slack_user_id: The admin's Slack user ID.
    """

    is_admin_dm: bool
    handled: bool
    response_text: str
    workspace_id: str
    admin_slack_user_id: str


# ---------------------------------------------------------------------------
# DM detection
# ---------------------------------------------------------------------------


def is_direct_message_channel(channel_id: str) -> bool:
    """Return ``True`` if *channel_id* is a Slack DM channel.

    Slack DM channel IDs start with ``D``.
    """
    return bool(channel_id) and channel_id.startswith("D")


def is_active_admin(workspace_id: str, slack_user_id: str) -> bool:
    """Return ``True`` if *slack_user_id* is the ACTIVE admin for *workspace_id*."""
    return BotAdministrator.objects.filter(
        workspace_id=workspace_id,
        slack_user_id=slack_user_id,
        status=ADMIN_STATUS_ACTIVE,
    ).exists()


# ---------------------------------------------------------------------------
# Command processing
# ---------------------------------------------------------------------------


def process_admin_dm(
    workspace_id: str,
    sender_slack_user_id: str,
    dm_text: str,
) -> AdminDMResult:
    """Process a potential admin DM access-grant command.

    Returns an :class:`AdminDMResult`.  If the sender is not the active
    admin, ``is_admin_dm`` is ``False`` and the caller should ignore the DM.

    If the sender is the admin but the message does not contain a grant
    intent, ``handled`` is ``False`` and ``response_text`` is empty.

    If the sender is the admin and a grant intent is found but no valid
    Member IDs are present, ``handled`` is ``True`` and ``response_text``
    contains the usage message.
    """
    # --- Check if sender is the active admin ---
    if not is_active_admin(workspace_id, sender_slack_user_id):
        return AdminDMResult(
            is_admin_dm=False,
            handled=False,
            response_text="",
            workspace_id=workspace_id,
            admin_slack_user_id=sender_slack_user_id,
        )

    logger.info(
        "admin_dm_received workspace_id=%s admin_user_id=%s",
        workspace_id, sender_slack_user_id,
    )

    # --- Parse the DM text ---
    parsed = parse_grant_command(dm_text)

    if not parsed.is_grant_intent:
        # Not a grant command — ignore silently
        return AdminDMResult(
            is_admin_dm=True,
            handled=False,
            response_text="",
            workspace_id=workspace_id,
            admin_slack_user_id=sender_slack_user_id,
        )

    # --- Grant intent found but no IDs at all ---
    if not parsed.member_ids and not parsed.invalid_ids:
        return AdminDMResult(
            is_admin_dm=True,
            handled=True,
            response_text=USAGE_MESSAGE,
            workspace_id=workspace_id,
            admin_slack_user_id=sender_slack_user_id,
        )

    # --- Report email conflicts if any ---
    if parsed.email_conflicts:
        conflict_lines = ["Email conflict detected.\n\nThe same email is used for multiple Member IDs:"]
        for conflict in parsed.email_conflicts:
            conflict_lines.append(f"• {conflict}")
        conflict_lines.append("\nPlease use a unique email per Member ID.")
        if not parsed.entries:
            return AdminDMResult(
                is_admin_dm=True,
                handled=True,
                response_text="\n".join(conflict_lines),
                workspace_id=workspace_id,
                admin_slack_user_id=sender_slack_user_id,
            )

    # --- Build the list of entries to provision ---
    # Each entry is a GrantCommandEntry with member_id and optional email.
    entries = parsed.entries

    # --- If no valid entries but invalid IDs exist, report them ---
    if not entries and parsed.invalid_ids:
        invalid_lines = ["Invalid Member IDs:\n"]
        for uid in parsed.invalid_ids:
            invalid_lines.append(f"• {uid}")
        invalid_lines.append(f"\n{USAGE_MESSAGE}")
        return AdminDMResult(
            is_admin_dm=True,
            handled=True,
            response_text="\n".join(invalid_lines),
            workspace_id=workspace_id,
            admin_slack_user_id=sender_slack_user_id,
        )

    # --- Provision each entry via the Phase B service ---
    provisioning_results: list[tuple[ProvisioningResult, bool]] = []

    for entry in entries:
        # Resolve source channel from UnauthorizedAccessAttempt
        attempt = UnauthorizedAccessAttempt.objects.filter(
            workspace_id=workspace_id,
            slack_user_id=entry.member_id,
        ).first()

        source_channel_id = ""
        if attempt and attempt.last_source_channel_id:
            source_channel_id = attempt.last_source_channel_id

        # If no source channel, create a FAILED result without calling the service
        if not source_channel_id:
            failed_result = ProvisioningResult(
                status=ProvisioningStatus.FAILED,
                target_slack_user_id=entry.member_id,
                brightbean_email=entry.email,
                workspace_name=None,
                bot_access_action=None,
                mapping_action=None,
                org_membership_action=None,
                ws_membership_action=None,
                failure_reason=ProvisioningFailureReason.CHANNEL_NOT_MAPPED,
                failure_message="No source channel found for this user.",
            )
            provisioning_results.append((failed_result, False))
            continue

        # Call the Phase B provisioning service
        try:
            result = grant_slack_analytics_access(
                approving_slack_user_id=sender_slack_user_id,
                team_id=workspace_id,
                source_channel_id=source_channel_id,
                target_slack_user_id=entry.member_id,
                brightbean_email=entry.email,
            )
        except Exception as exc:  # pragma: no cover — defensive
            logger.exception(
                "provisioning_error workspace=%s target=%s: %s",
                workspace_id, entry.member_id, exc,
            )
            result = ProvisioningResult(
                status=ProvisioningStatus.FAILED,
                target_slack_user_id=entry.member_id,
                brightbean_email=entry.email,
                workspace_name=None,
                bot_access_action=None,
                mapping_action=None,
                org_membership_action=None,
                ws_membership_action=None,
                failure_reason=None,
                failure_message="An unexpected error occurred.",
            )

        # --- Send user confirmation DM for actionable statuses ---
        notification_failed = False
        if result.status in (
            ProvisioningStatus.NEWLY_PROVISIONED,
            ProvisioningStatus.RESTORED,
            ProvisioningStatus.REPAIRED,
        ):
            notification_failed = not send_user_confirmation_dm(
                workspace_id=workspace_id,
                slack_user_id=result.target_slack_user_id,
                admin_slack_user_id=sender_slack_user_id,
                result_type="approved",
            )

        provisioning_results.append((result, notification_failed))

    # --- Format the response ---
    if len(provisioning_results) == 1 and not parsed.invalid_ids and not parsed.email_conflicts:
        result, notification_failed = provisioning_results[0]
        response_text = format_provisioning_response(
            result,
            notification_failed=notification_failed,
        )
    else:
        response_text = format_bulk_provisioning_response(provisioning_results)

    # --- Append invalid IDs to the response if any ---
    if parsed.invalid_ids:
        invalid_section = "\n\nInvalid Member IDs:\n"
        for uid in parsed.invalid_ids:
            invalid_section += f"• {uid}\n"
        response_text += invalid_section.rstrip()

    # --- Append email conflicts to the response if any ---
    if parsed.email_conflicts:
        conflict_section = "\n\nEmail conflicts:\n"
        for conflict in parsed.email_conflicts:
            conflict_section += f"• {conflict}\n"
        response_text += conflict_section.rstrip()

    logger.info(
        "admin_dm_provisioning_completed workspace_id=%s admin_user_id=%s entries=%d",
        workspace_id, sender_slack_user_id, len(entries),
    )

    return AdminDMResult(
        is_admin_dm=True,
        handled=True,
        response_text=response_text,
        workspace_id=workspace_id,
        admin_slack_user_id=sender_slack_user_id,
    )
