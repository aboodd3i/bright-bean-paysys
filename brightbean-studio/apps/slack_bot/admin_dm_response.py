"""Format administrator DM responses for access-grant results.

Produces the exact text strings sent back to the administrator in
their DM channel after a grant command is processed.

Includes both the legacy Phase 1 formatters (for backward compatibility
with existing tests) and the new Phase C provisioning formatters.
"""

from __future__ import annotations

from .access_provisioning import (
    ProvisioningFailureReason,
    ProvisioningResult,
    ProvisioningStatus,
)
from .access_service import BulkGrantResult, GrantResult


# ---------------------------------------------------------------------------
# Single-user responses
# ---------------------------------------------------------------------------


def format_single_grant_response(
    result: GrantResult,
    notification_failed: bool = False,
) -> str:
    """Format the DM response for a single-user grant operation.

    Args:
        result: The :class:`GrantResult` from ``grant_user_access``.
        notification_failed: If True, the user confirmation DM could not
            be delivered.  Only relevant for ``granted`` and ``restored``.

    Returns:
        Formatted text suitable for a Slack DM message.
    """
    if result.action == "granted":
        base = (
            "Access granted successfully.\n\n"
            f"Member ID: {result.slack_user_id}\n"
            "Permission: Read-only\n"
            "Status: Approved"
        )
        if notification_failed:
            return base + "\n\nUser notification: Failed"
        return base

    if result.action == "restored":
        base = (
            "Access restored successfully.\n\n"
            f"Member ID: {result.slack_user_id}\n"
            "Permission: Read-only\n"
            "Status: Approved"
        )
        if notification_failed:
            return base + "\n\nUser notification: Failed"
        return base

    # already_approved
    return (
        "No change required.\n\n"
        f"Member ID: {result.slack_user_id}\n"
        "Permission: Read-only\n"
        "Status: Already approved"
    )


# ---------------------------------------------------------------------------
# Bulk-user response
# ---------------------------------------------------------------------------


def format_bulk_grant_response(
    result: BulkGrantResult,
    notification_failures: list[str] | None = None,
) -> str:
    """Format the DM response for a bulk grant operation.

    Args:
        result: The :class:`BulkGrantResult` from ``grant_bulk_user_access``.
        notification_failures: List of Slack user IDs where the confirmation
            DM could not be delivered.  Omitted from output when empty.

    Returns:
        Formatted text with grouped sections.  Empty sections are omitted
        except that the result always shows whether anything was approved.
    """
    lines: list[str] = ["Bulk access update completed.", ""]

    if result.approved:
        lines.append("Approved:")
        for uid in result.approved:
            lines.append(f"• {uid}")
        lines.append("")

    if result.restored:
        lines.append("Restored:")
        for uid in result.restored:
            lines.append(f"• {uid}")
        lines.append("")

    if result.already_approved:
        lines.append("Already approved:")
        for uid in result.already_approved:
            lines.append(f"• {uid}")
        lines.append("")

    if result.invalid:
        lines.append("Invalid Member IDs:")
        for uid in result.invalid:
            lines.append(f"• {uid}")
        lines.append("")

    if result.failed:
        lines.append("Failed:")
        for uid in result.failed:
            lines.append(f"• {uid}")
        lines.append("")

    if notification_failures:
        lines.append("User notifications failed:")
        for uid in notification_failures:
            lines.append(f"• {uid}")
        lines.append("")

    lines.append("Permission: Read-only")

    return "\n".join(lines)


# ===========================================================================
# Phase C — Provisioning response formatters
# ===========================================================================


# Map provisioning failure reasons to safe user-facing messages.
_FAILURE_MESSAGES: dict[str, str] = {
    ProvisioningFailureReason.NOT_ADMIN: "You are not an authorised administrator.",
    ProvisioningFailureReason.CHANNEL_NOT_MAPPED: "Source channel not found.",
    ProvisioningFailureReason.WORKSPACE_ARCHIVED: "Workspace is archived.",
    ProvisioningFailureReason.EMAIL_REQUIRED: "A BrightBean login email is required.",
    ProvisioningFailureReason.USER_NOT_FOUND: "BrightBean user not found.",
    ProvisioningFailureReason.USER_INACTIVE: "BrightBean user is inactive.",
    ProvisioningFailureReason.MULTIPLE_USERS: "Multiple BrightBean users match that email.",
    ProvisioningFailureReason.MAPPING_CONFLICT: "Existing mapping points to a different user.",
    ProvisioningFailureReason.EMAIL_MISMATCH: "Supplied email does not match the existing user.",
    ProvisioningFailureReason.NO_VIEW_ANALYTICS: "User lacks analytics permission.",
    ProvisioningFailureReason.POST_CHECK_FAILED: "Authorization verification failed.",
}


def _safe_failure_message(reason: str, fallback: str = "") -> str:
    """Return a safe user-facing message for a provisioning failure reason."""
    return _FAILURE_MESSAGES.get(reason, fallback or "Provisioning failed.")


def format_provisioning_response(
    result: ProvisioningResult,
    notification_failed: bool = False,
) -> str:
    """Format the DM response for a single-user provisioning operation.

    Args:
        result: The :class:`ProvisioningResult` from
            ``grant_slack_analytics_access``.
        notification_failed: If True, the user confirmation DM could not
            be delivered.

    Returns:
        Formatted text suitable for a Slack DM message.
    """
    member = result.target_slack_user_id
    email = result.brightbean_email or "—"
    ws_name = result.workspace_name or "—"

    if result.status == ProvisioningStatus.NEWLY_PROVISIONED:
        base = (
            "Access granted successfully.\n\n"
            f"Member: {member}\n"
            f"BrightBean user: {email}\n"
            f"Workspace: {ws_name}\n"
            "Permission: Analytics read-only\n"
            "Status: Approved"
        )
        if notification_failed:
            return base + "\n\nUser notification: Failed"
        return base

    if result.status == ProvisioningStatus.RESTORED:
        base = (
            "Access restored successfully.\n\n"
            f"Member: {member}\n"
            f"BrightBean user: {email}\n"
            f"Workspace: {ws_name}\n"
            "Permission: Analytics read-only\n"
            "Status: Approved"
        )
        if notification_failed:
            return base + "\n\nUser notification: Failed"
        return base

    if result.status == ProvisioningStatus.REPAIRED:
        base = (
            "Access provisioning completed.\n\n"
            f"Member: {member}\n"
            f"BrightBean user: {email}\n"
            f"Workspace: {ws_name}\n"
            "Permission: Analytics read-only\n"
            "Status: BrightBean access linked"
        )
        if notification_failed:
            return base + "\n\nUser notification: Failed"
        return base

    if result.status == ProvisioningStatus.ALREADY_PROVISIONED:
        return (
            "No change required.\n\n"
            f"Member: {member}\n"
            f"BrightBean user: {email}\n"
            f"Workspace: {ws_name}\n"
            "Permission: Analytics read-only\n"
            "Status: Already approved and linked"
        )

    # FAILED
    reason_msg = _safe_failure_message(
        result.failure_reason, result.failure_message,
    )
    return (
        "Access was not granted.\n\n"
        f"Member: {member}\n"
        f"Reason: {reason_msg}"
    )


# ---------------------------------------------------------------------------
# Bulk provisioning response
# ---------------------------------------------------------------------------


def format_bulk_provisioning_response(
    results: list[tuple[ProvisioningResult, bool]],
) -> str:
    """Format the DM response for a bulk provisioning operation.

    Args:
        results: List of ``(ProvisioningResult, notification_failed)`` tuples.

    Returns:
        Formatted text with grouped sections.
    """
    approved: list[str] = []
    repaired: list[str] = []
    restored: list[str] = []
    already_linked: list[str] = []
    failed: list[str] = []
    notification_failures: list[str] = []

    for result, notif_failed in results:
        member = result.target_slack_user_id
        email = result.brightbean_email

        if notif_failed:
            notification_failures.append(member)

        if result.status == ProvisioningStatus.NEWLY_PROVISIONED:
            label = f"• {member} — {email}" if email else f"• {member}"
            approved.append(label)
        elif result.status == ProvisioningStatus.REPAIRED:
            label = f"• {member} — {email}" if email else f"• {member}"
            repaired.append(label)
        elif result.status == ProvisioningStatus.RESTORED:
            label = f"• {member} — {email}" if email else f"• {member}"
            restored.append(label)
        elif result.status == ProvisioningStatus.ALREADY_PROVISIONED:
            label = f"• {member} — {email}" if email else f"• {member}"
            already_linked.append(label)
        else:
            # FAILED
            reason_msg = _safe_failure_message(
                result.failure_reason, result.failure_message,
            )
            failed.append(f"• {member} — {reason_msg}")

    lines: list[str] = ["Bulk access update completed.", ""]

    if approved:
        lines.append("Approved and linked:")
        lines.extend(approved)
        lines.append("")

    if repaired:
        lines.append("Repaired:")
        lines.extend(repaired)
        lines.append("")

    if restored:
        lines.append("Restored:")
        lines.extend(restored)
        lines.append("")

    if already_linked:
        lines.append("Already approved and linked:")
        lines.extend(already_linked)
        lines.append("")

    if failed:
        lines.append("Failed:")
        lines.extend(failed)
        lines.append("")

    if notification_failures:
        lines.append("User notifications failed:")
        for uid in notification_failures:
            lines.append(f"• {uid}")
        lines.append("")

    lines.append("Permission: Analytics read-only")

    return "\n".join(lines)
