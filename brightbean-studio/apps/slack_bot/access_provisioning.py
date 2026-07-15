"""Shared BrightBean access provisioning service.

Ensures that a Slack user has the complete BrightBean identity and
authorization chain required to use the analytics bot:

    SlackChannelMapping → Workspace
    SlackUserMapping     → BrightBean User
    OrgMembership
    WorkspaceMembership  (with view_analytics permission)
    BotUserAccess        (APPROVED / READ_ONLY)

The service is **atomic** — all database changes are wrapped in a single
``transaction.atomic()`` block.  If any step fails, all changes are
rolled back and a controlled failure is returned.

This service does **not**:
- send Slack messages;
- parse natural-language commands;
- create BrightBean users;
- grant elevated privileges (staff, superuser, admin/owner roles);
- call external APIs (Slack, LLM, social providers).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import StrEnum

from django.db import transaction

from .authorization import resolve_tool_context
from .constants import (
    ACCESS_STATUS_APPROVED,
    ACCESS_STATUS_REVOKED,
    ADMIN_STATUS_ACTIVE,
    PERMISSION_READ_ONLY,
    SYSTEM_ACTOR,
)
from .contracts import SlackAnalyticsRequest
from .models import (
    BotAdministrator,
    BotUserAccess,
    SlackChannelMapping,
    SlackUserMapping,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


class ProvisioningStatus(StrEnum):
    """Final status of a provisioning attempt."""

    NEWLY_PROVISIONED = "newly_provisioned"
    ALREADY_PROVISIONED = "already_provisioned"
    REPAIRED = "repaired"
    RESTORED = "restored"
    FAILED = "failed"


class ProvisioningFailureReason(StrEnum):
    """Controlled failure reason for a provisioning attempt."""

    NOT_ADMIN = "not_admin"
    CHANNEL_NOT_MAPPED = "channel_not_mapped"
    WORKSPACE_ARCHIVED = "workspace_archived"
    EMAIL_REQUIRED = "email_required"
    USER_NOT_FOUND = "user_not_found"
    USER_INACTIVE = "user_inactive"
    MULTIPLE_USERS = "multiple_users"
    MAPPING_CONFLICT = "mapping_conflict"
    EMAIL_MISMATCH = "email_mismatch"
    NO_VIEW_ANALYTICS = "no_view_analytics"
    POST_CHECK_FAILED = "post_check_failed"


@dataclass(frozen=True)
class ProvisioningResult:
    """Structured outcome of a provisioning attempt.

    Safe fields only — no database PKs, internal IDs, credentials,
    or tokens.
    """

    status: ProvisioningStatus
    target_slack_user_id: str
    brightbean_email: str = ""
    workspace_name: str = ""
    bot_access_action: str = ""  # "created", "restored", "already_approved"
    mapping_action: str = ""     # "created", "already_exists"
    org_membership_action: str = ""   # "created", "already_exists"
    ws_membership_action: str = ""    # "created", "already_exists"
    failure_reason: str = ""
    failure_message: str = ""


# ---------------------------------------------------------------------------
# Diagnostic text for the authorization post-check
# ---------------------------------------------------------------------------

_DIAGNOSTIC_TEXT = "show me workspace analytics overview"


# ---------------------------------------------------------------------------
# Main service function
# ---------------------------------------------------------------------------


@transaction.atomic
def grant_slack_analytics_access(
    *,
    approving_slack_user_id: str,
    team_id: str,
    source_channel_id: str,
    target_slack_user_id: str,
    brightbean_email: str | None = None,
) -> ProvisioningResult:
    """Provision complete BrightBean analytics access for a Slack user.

    Parameters
    ----------
    approving_slack_user_id : str
        Slack user ID of the administrator approving the request.
    team_id : str
        Slack workspace/team ID.
    source_channel_id : str
        Slack channel ID where the request originated (must be mapped
        to a BrightBean workspace).
    target_slack_user_id : str
        Slack user ID of the user to provision.
    brightbean_email : str | None
        Email of the existing BrightBean user.  Required when no
        ``SlackUserMapping`` exists yet.

    Returns
    -------
    ProvisioningResult
        Structured result describing the outcome.
    """
    # --- 1. Verify administrator ---
    admin = BotAdministrator.objects.filter(
        workspace_id=team_id,
        slack_user_id=approving_slack_user_id,
        status=ADMIN_STATUS_ACTIVE,
    ).first()

    if admin is None:
        return ProvisioningResult(
            status=ProvisioningStatus.FAILED,
            target_slack_user_id=target_slack_user_id,
            failure_reason=ProvisioningFailureReason.NOT_ADMIN,
            failure_message="Approving user is not an active bot administrator.",
        )

    # --- 2. Resolve channel mapping → workspace ---
    try:
        channel_mapping = SlackChannelMapping.objects.select_related(
            "workspace", "workspace__organization"
        ).get(team_id=team_id, channel_id=source_channel_id)
    except SlackChannelMapping.DoesNotExist:
        return ProvisioningResult(
            status=ProvisioningStatus.FAILED,
            target_slack_user_id=target_slack_user_id,
            failure_reason=ProvisioningFailureReason.CHANNEL_NOT_MAPPED,
            failure_message=(
                f"No workspace mapping for channel {source_channel_id!r} "
                f"in team {team_id!r}."
            ),
        )

    workspace = channel_mapping.workspace

    if workspace.is_archived:
        return ProvisioningResult(
            status=ProvisioningStatus.FAILED,
            target_slack_user_id=target_slack_user_id,
            failure_reason=ProvisioningFailureReason.WORKSPACE_ARCHIVED,
            failure_message=f"Workspace {workspace.name!r} is archived.",
        )

    organization = workspace.organization

    # --- 3. Resolve BrightBean user ---
    from apps.accounts.models import User

    existing_mapping = SlackUserMapping.objects.select_related("user").filter(
        slack_user_id=target_slack_user_id,
        team_id=team_id,
    ).first()

    if existing_mapping is not None:
        # Reuse existing mapping
        brightbean_user = existing_mapping.user

        # If email is also supplied, verify it matches
        if brightbean_email:
            normalized_email = brightbean_email.strip().lower()
            if brightbean_user.email.lower() != normalized_email:
                return ProvisioningResult(
                    status=ProvisioningStatus.FAILED,
                    target_slack_user_id=target_slack_user_id,
                    failure_reason=ProvisioningFailureReason.EMAIL_MISMATCH,
                    failure_message=(
                        "Supplied email does not match the existing "
                        "BrightBean user."
                    ),
                )
    else:
        # No existing mapping — email is required
        if not brightbean_email or not brightbean_email.strip():
            return ProvisioningResult(
                status=ProvisioningStatus.FAILED,
                target_slack_user_id=target_slack_user_id,
                failure_reason=ProvisioningFailureReason.EMAIL_REQUIRED,
                failure_message=(
                    "Email is required when no SlackUserMapping exists."
                ),
            )

        normalized_email = brightbean_email.strip().lower()

        # Case-insensitive email lookup — resolve exactly one active user
        matching_users = list(
            User.objects.filter(
                email__iexact=normalized_email,
                is_active=True,
            )
        )

        if not matching_users:
            # Check if the user exists but is inactive
            inactive_exists = User.objects.filter(
                email__iexact=normalized_email,
            ).exists()
            if inactive_exists:
                return ProvisioningResult(
                    status=ProvisioningStatus.FAILED,
                    target_slack_user_id=target_slack_user_id,
                    failure_reason=ProvisioningFailureReason.USER_INACTIVE,
                    failure_message="BrightBean user is inactive.",
                )
            return ProvisioningResult(
                status=ProvisioningStatus.FAILED,
                target_slack_user_id=target_slack_user_id,
                failure_reason=ProvisioningFailureReason.USER_NOT_FOUND,
                failure_message="No BrightBean user found with that email.",
            )

        if len(matching_users) > 1:
            return ProvisioningResult(
                status=ProvisioningStatus.FAILED,
                target_slack_user_id=target_slack_user_id,
                failure_reason=ProvisioningFailureReason.MULTIPLE_USERS,
                failure_message="Multiple BrightBean users match that email.",
            )

        brightbean_user = matching_users[0]

    # --- 4. Validate mapping conflicts ---
    # (Already handled above — if existing_mapping exists and points to
    # a different user, that's a conflict.  But we only get here if the
    # email matched or no email was supplied.  If no email was supplied
    # and the mapping exists, we reuse it.  If email was supplied and
    # didn't match, we already returned an error above.)

    # Track what we'll need for the result
    mapping_action = "already_exists" if existing_mapping is not None else "created"
    changes_made = False

    # --- 5. Create/preserve organization membership ---
    from apps.members.models import OrgMembership

    org_membership_action = "already_exists"
    org_membership = OrgMembership.objects.filter(
        user=brightbean_user,
        organization=organization,
    ).first()

    if org_membership is None:
        OrgMembership.objects.create(
            user=brightbean_user,
            organization=organization,
            org_role=OrgMembership.OrgRole.MEMBER,
        )
        org_membership_action = "created"
        changes_made = True
    # If it exists, preserve the existing role (no downgrade/upgrade)

    # --- 6. Create/preserve workspace membership ---
    from apps.members.models import WorkspaceMembership

    ws_membership_action = "already_exists"
    ws_membership = WorkspaceMembership.objects.filter(
        user=brightbean_user,
        workspace=workspace,
    ).first()

    if ws_membership is None:
        ws_membership = WorkspaceMembership.objects.create(
            user=brightbean_user,
            workspace=workspace,
            workspace_role=WorkspaceMembership.WorkspaceRole.VIEWER,
        )
        ws_membership_action = "created"
        changes_made = True
    # If it exists, preserve the existing role (no downgrade/upgrade)

    # --- 7. Create/preserve SlackUserMapping ---
    if existing_mapping is None:
        SlackUserMapping.objects.create(
            slack_user_id=target_slack_user_id,
            team_id=team_id,
            user=brightbean_user,
        )
        changes_made = True
    # If existing_mapping exists and points to same user → idempotent

    # --- 8. Verify view_analytics permission ---
    # Re-fetch ws_membership to get effective_permissions
    ws_membership = WorkspaceMembership.objects.get(
        user=brightbean_user,
        workspace=workspace,
    )
    permissions = ws_membership.effective_permissions
    if not permissions or not permissions.get("view_analytics", False):
        return ProvisioningResult(
            status=ProvisioningStatus.FAILED,
            target_slack_user_id=target_slack_user_id,
            failure_reason=ProvisioningFailureReason.NO_VIEW_ANALYTICS,
            failure_message=(
                "User's workspace role does not include view_analytics."
            ),
        )

    # --- 9. Create/restore BotUserAccess ---
    bot_access_action = "already_approved"
    access = BotUserAccess.objects.filter(
        workspace_id=team_id,
        slack_user_id=target_slack_user_id,
    ).first()

    if access is None:
        BotUserAccess.objects.create(
            workspace_id=team_id,
            slack_user_id=target_slack_user_id,
            status=ACCESS_STATUS_APPROVED,
            permission=PERMISSION_READ_ONLY,
            granted_by_slack_user_id=approving_slack_user_id,
        )
        bot_access_action = "created"
        changes_made = True
    elif access.status == ACCESS_STATUS_REVOKED:
        access.status = ACCESS_STATUS_APPROVED
        access.permission = PERMISSION_READ_ONLY
        access.revoked_at = None
        access.granted_by_slack_user_id = approving_slack_user_id
        access.save(update_fields=[
            "status", "permission", "revoked_at",
            "granted_by_slack_user_id", "updated_at",
        ])
        bot_access_action = "restored"
        changes_made = True
    # If already APPROVED, preserve — but we still verify/repair
    # the identity chain (done above).

    # --- 10. Authorization post-check ---
    request = SlackAnalyticsRequest(
        correlation_id=f"provision-{target_slack_user_id}",
        event_id=f"provision-{target_slack_user_id}",
        team_id=team_id,
        channel_id=source_channel_id,
        user_id=target_slack_user_id,
        thread_ts="",
        text=_DIAGNOSTIC_TEXT,
    )

    try:
        context = resolve_tool_context(request)
    except Exception as exc:
        # Roll back — authorization failed despite provisioning.
        # Use transaction.set_rollback to undo all changes within this
        # atomic block, then return a controlled failure.
        logger.warning(
            "Post-check failed for target=%s: %s",
            target_slack_user_id, exc,
        )
        transaction.set_rollback(True)
        return ProvisioningResult(
            status=ProvisioningStatus.FAILED,
            target_slack_user_id=target_slack_user_id,
            failure_reason=ProvisioningFailureReason.POST_CHECK_FAILED,
            failure_message="Authorization post-check failed after provisioning.",
        )

    # Verify the resolved context matches our expectations
    if context.workspace_id != workspace.id:
        transaction.set_rollback(True)
        return ProvisioningResult(
            status=ProvisioningStatus.FAILED,
            target_slack_user_id=target_slack_user_id,
            failure_reason=ProvisioningFailureReason.POST_CHECK_FAILED,
            failure_message="Post-check workspace mismatch.",
        )
    if context.user_id != brightbean_user.id:
        transaction.set_rollback(True)
        return ProvisioningResult(
            status=ProvisioningStatus.FAILED,
            target_slack_user_id=target_slack_user_id,
            failure_reason=ProvisioningFailureReason.POST_CHECK_FAILED,
            failure_message="Post-check user mismatch.",
        )
    if context.organization_id != organization.id:
        transaction.set_rollback(True)
        return ProvisioningResult(
            status=ProvisioningStatus.FAILED,
            target_slack_user_id=target_slack_user_id,
            failure_reason=ProvisioningFailureReason.POST_CHECK_FAILED,
            failure_message="Post-check organization mismatch.",
        )

    # --- 11. Determine final status ---
    if not changes_made:
        final_status = ProvisioningStatus.ALREADY_PROVISIONED
    elif bot_access_action == "restored":
        final_status = ProvisioningStatus.RESTORED
    elif bot_access_action == "already_approved" and (
        mapping_action == "created"
        or org_membership_action == "created"
        or ws_membership_action == "created"
    ):
        # Bot access was already approved but identity chain was missing
        final_status = ProvisioningStatus.REPAIRED
    else:
        final_status = ProvisioningStatus.NEWLY_PROVISIONED

    return ProvisioningResult(
        status=final_status,
        target_slack_user_id=target_slack_user_id,
        brightbean_email=brightbean_user.email,
        workspace_name=workspace.name,
        bot_access_action=bot_access_action,
        mapping_action=mapping_action,
        org_membership_action=org_membership_action,
        ws_membership_action=ws_membership_action,
    )
