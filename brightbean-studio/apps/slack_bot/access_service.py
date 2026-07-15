"""Service layer for bot whitelisting access management.

Provides atomic operations for configuring the bot administrator
and granting/restoring user access.  All operations write audit
records via :class:`~apps.slack_bot.models.BotAccessAuditLog`.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from django.db import transaction

from .constants import (
    ACCESS_STATUS_APPROVED,
    ACCESS_STATUS_REVOKED,
    ADMIN_STATUS_ACTIVE,
    AUDIT_ACCESS_ALREADY_PRESENT,
    AUDIT_ACCESS_GRANTED,
    AUDIT_ACCESS_RESTORED,
    AUDIT_ADMIN_BOOTSTRAPPED,
    AUDIT_ADMIN_UPDATED,
    AUDIT_BULK_ACCESS_GRANTED,
    AUDIT_INVALID_MEMBER_ID,
    PERMISSION_READ_ONLY,
    SYSTEM_ACTOR,
)
from .models import (
    BotAccessAuditLog,
    BotAdministrator,
    BotUserAccess,
)
from .slack_id_validation import (
    is_valid_member_id,
    is_valid_workspace_id,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Runtime access check (Phase 2)
# ---------------------------------------------------------------------------


def is_user_approved(
    workspace_id: str,
    slack_user_id: str,
) -> bool:
    """Return ``True`` if *slack_user_id* has APPROVED access in *workspace_id*.

    A user is approved only when a :class:`BotUserAccess` record exists
    with matching ``workspace_id``, matching ``slack_user_id``,
    ``status = APPROVED`` and ``permission = READ_ONLY``.

    Returns ``False`` when:
    - no record exists;
    - status is ``REVOKED``;
    - workspace or user ID does not match.

    This is a **read-only** query — it does not call Slack, the LLM,
    BrightBean, or create/modify any access records.
    """
    return BotUserAccess.objects.filter(
        workspace_id=workspace_id,
        slack_user_id=slack_user_id,
        status=ACCESS_STATUS_APPROVED,
        permission=PERMISSION_READ_ONLY,
    ).exists()


# ---------------------------------------------------------------------------
# Result dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AdminResult:
    """Outcome of administrator configuration."""

    action: str  # "created", "already_active", "updated"
    workspace_id: str
    slack_user_id: str


@dataclass(frozen=True)
class GrantResult:
    """Outcome of a single-user access grant."""

    action: str  # "granted", "already_approved", "restored"
    workspace_id: str
    slack_user_id: str


@dataclass(frozen=True)
class BulkGrantResult:
    """Outcome of a bulk access grant."""

    approved: list[str] = field(default_factory=list)
    restored: list[str] = field(default_factory=list)
    already_approved: list[str] = field(default_factory=list)
    invalid: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Administrator configuration
# ---------------------------------------------------------------------------


@transaction.atomic
def configure_administrator(
    workspace_id: str,
    slack_user_id: str,
) -> AdminResult:
    """Configure the bot administrator for *workspace_id*.

    * If no administrator exists → create one + grant READ_ONLY access.
    * If the same administrator exists → return ``already_active``.
    * If a different administrator exists → update the record.
    """
    admin = BotAdministrator.objects.filter(workspace_id=workspace_id).first()

    if admin is None:
        BotAdministrator.objects.create(
            workspace_id=workspace_id,
            slack_user_id=slack_user_id,
            status=ADMIN_STATUS_ACTIVE,
        )
        _ensure_admin_access(workspace_id, slack_user_id)
        _audit(
            workspace_id=workspace_id,
            target=slack_user_id,
            action=AUDIT_ADMIN_BOOTSTRAPPED,
            performed_by=SYSTEM_ACTOR,
        )
        return AdminResult(
            action="created",
            workspace_id=workspace_id,
            slack_user_id=slack_user_id,
        )

    if admin.slack_user_id == slack_user_id:
        return AdminResult(
            action="already_active",
            workspace_id=workspace_id,
            slack_user_id=slack_user_id,
        )

    old_admin_id = admin.slack_user_id
    admin.slack_user_id = slack_user_id
    admin.status = ADMIN_STATUS_ACTIVE
    admin.save(update_fields=["slack_user_id", "status", "updated_at"])
    _ensure_admin_access(workspace_id, slack_user_id)
    _audit(
        workspace_id=workspace_id,
        target=slack_user_id,
        action=AUDIT_ADMIN_UPDATED,
        performed_by=SYSTEM_ACTOR,
        metadata={"previous_admin": old_admin_id},
    )
    return AdminResult(
        action="updated",
        workspace_id=workspace_id,
        slack_user_id=slack_user_id,
    )


def _ensure_admin_access(workspace_id: str, slack_user_id: str) -> None:
    """Ensure the administrator has APPROVED / READ_ONLY bot access."""
    access, created = BotUserAccess.objects.get_or_create(
        workspace_id=workspace_id,
        slack_user_id=slack_user_id,
        defaults={
            "status": ACCESS_STATUS_APPROVED,
            "permission": PERMISSION_READ_ONLY,
            "granted_by_slack_user_id": SYSTEM_ACTOR,
        },
    )
    if not created and access.status == ACCESS_STATUS_REVOKED:
        access.status = ACCESS_STATUS_APPROVED
        access.permission = PERMISSION_READ_ONLY
        access.revoked_at = None
        access.granted_by_slack_user_id = SYSTEM_ACTOR
        access.save(update_fields=[
            "status", "permission", "revoked_at",
            "granted_by_slack_user_id", "updated_at",
        ])


# ---------------------------------------------------------------------------
# Single-user access grant
# ---------------------------------------------------------------------------


@transaction.atomic
def grant_user_access(
    workspace_id: str,
    slack_user_id: str,
    granted_by_slack_user_id: str = SYSTEM_ACTOR,
) -> GrantResult:
    """Grant APPROVED / READ_ONLY access to a single user.

    * New user → create APPROVED row.
    * Already approved → return ``already_approved``.
    * Revoked → restore to APPROVED.
    """
    access = BotUserAccess.objects.filter(
        workspace_id=workspace_id,
        slack_user_id=slack_user_id,
    ).first()

    if access is None:
        BotUserAccess.objects.create(
            workspace_id=workspace_id,
            slack_user_id=slack_user_id,
            status=ACCESS_STATUS_APPROVED,
            permission=PERMISSION_READ_ONLY,
            granted_by_slack_user_id=granted_by_slack_user_id,
        )
        _audit(
            workspace_id=workspace_id,
            target=slack_user_id,
            action=AUDIT_ACCESS_GRANTED,
            performed_by=granted_by_slack_user_id,
        )
        return GrantResult(
            action="granted",
            workspace_id=workspace_id,
            slack_user_id=slack_user_id,
        )

    if access.status == ACCESS_STATUS_APPROVED:
        _audit(
            workspace_id=workspace_id,
            target=slack_user_id,
            action=AUDIT_ACCESS_ALREADY_PRESENT,
            performed_by=granted_by_slack_user_id,
        )
        return GrantResult(
            action="already_approved",
            workspace_id=workspace_id,
            slack_user_id=slack_user_id,
        )

    # Revoked → restore
    access.status = ACCESS_STATUS_APPROVED
    access.permission = PERMISSION_READ_ONLY
    access.revoked_at = None
    access.granted_by_slack_user_id = granted_by_slack_user_id
    access.save(update_fields=[
        "status", "permission", "revoked_at",
        "granted_by_slack_user_id", "updated_at",
    ])
    _audit(
        workspace_id=workspace_id,
        target=slack_user_id,
        action=AUDIT_ACCESS_RESTORED,
        performed_by=granted_by_slack_user_id,
    )
    return GrantResult(
        action="restored",
        workspace_id=workspace_id,
        slack_user_id=slack_user_id,
    )


# ---------------------------------------------------------------------------
# Bulk access grant
# ---------------------------------------------------------------------------


def grant_bulk_user_access(
    workspace_id: str,
    slack_user_ids: list[str],
    granted_by_slack_user_id: str = SYSTEM_ACTOR,
) -> BulkGrantResult:
    """Grant access to multiple users.

    Deduplicates input, separates valid from invalid IDs, and processes
    each valid ID in its own atomic transaction.
    """
    # Deduplicate while preserving order
    seen: set[str] = set()
    unique_ids: list[str] = []
    for uid in slack_user_ids:
        trimmed = uid.strip()
        if trimmed not in seen:
            seen.add(trimmed)
            unique_ids.append(trimmed)

    result = BulkGrantResult()

    for uid in unique_ids:
        if not is_valid_member_id(uid):
            result.invalid.append(uid)
            _audit(
                workspace_id=workspace_id,
                target=uid,
                action=AUDIT_INVALID_MEMBER_ID,
                performed_by=granted_by_slack_user_id,
            )
            continue

        try:
            grant_result = grant_user_access(
                workspace_id=workspace_id,
                slack_user_id=uid,
                granted_by_slack_user_id=granted_by_slack_user_id,
            )
        except Exception:
            logger.exception("Failed to grant access for %s", uid)
            result.failed.append(uid)
            continue

        if grant_result.action == "granted":
            result.approved.append(uid)
        elif grant_result.action == "restored":
            result.restored.append(uid)
        elif grant_result.action == "already_approved":
            result.already_approved.append(uid)

    # One bulk audit record summarising the operation
    if result.approved or result.restored:
        _audit(
            workspace_id=workspace_id,
            target=",".join(result.approved + result.restored),
            action=AUDIT_BULK_ACCESS_GRANTED,
            performed_by=granted_by_slack_user_id,
            metadata={
                "approved": result.approved,
                "restored": result.restored,
                "already_approved": result.already_approved,
                "invalid": result.invalid,
            },
        )

    return result


# ---------------------------------------------------------------------------
# Audit helper
# ---------------------------------------------------------------------------


def _audit(
    *,
    workspace_id: str,
    target: str,
    action: str,
    performed_by: str = SYSTEM_ACTOR,
    metadata: dict[str, Any] | None = None,
) -> None:
    """Write a single audit log entry."""
    BotAccessAuditLog.objects.create(
        workspace_id=workspace_id,
        target_slack_user_id=target,
        performed_by_slack_user_id=performed_by,
        action=action,
        metadata=metadata or {},
    )
