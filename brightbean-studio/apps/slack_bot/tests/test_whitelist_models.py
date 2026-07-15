"""Tests for Phase 1 bot whitelisting models."""

import pytest
from django.db import IntegrityError

from apps.slack_bot.constants import (
    ACCESS_STATUS_APPROVED,
    ACCESS_STATUS_REVOKED,
    ADMIN_STATUS_ACTIVE,
    AUDIT_ADMIN_BOOTSTRAPPED,
    AUDIT_ACCESS_GRANTED,
    PERMISSION_READ_ONLY,
    SYSTEM_ACTOR,
)
from apps.slack_bot.models import (
    BotAccessAuditLog,
    BotAdministrator,
    BotUserAccess,
    UnauthorizedAccessAttempt,
)


# ---------------------------------------------------------------------------
# BotAdministrator
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_administrator_can_be_created():
    admin = BotAdministrator.objects.create(
        workspace_id="T0001",
        slack_user_id="U0001",
    )
    assert admin.pk is not None
    assert admin.status == ADMIN_STATUS_ACTIVE
    assert admin.created_at is not None


@pytest.mark.django_db
def test_only_one_administrator_per_workspace():
    BotAdministrator.objects.create(
        workspace_id="T0001",
        slack_user_id="U0001",
    )
    with pytest.raises(IntegrityError):
        BotAdministrator.objects.create(
            workspace_id="T0001",
            slack_user_id="U0002",
        )


@pytest.mark.django_db
def test_different_workspaces_can_have_different_admins():
    a1 = BotAdministrator.objects.create(workspace_id="T0001", slack_user_id="U0001")
    a2 = BotAdministrator.objects.create(workspace_id="T0002", slack_user_id="U0002")
    assert a1.pk != a2.pk


# ---------------------------------------------------------------------------
# BotUserAccess
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_user_access_can_be_created():
    access = BotUserAccess.objects.create(
        workspace_id="T0001",
        slack_user_id="U0001",
    )
    assert access.status == ACCESS_STATUS_APPROVED
    assert access.permission == PERMISSION_READ_ONLY
    assert access.granted_by_slack_user_id == SYSTEM_ACTOR
    assert access.revoked_at is None


@pytest.mark.django_db
def test_user_access_uniqueness():
    BotUserAccess.objects.create(workspace_id="T0001", slack_user_id="U0001")
    with pytest.raises(IntegrityError):
        BotUserAccess.objects.create(workspace_id="T0001", slack_user_id="U0001")


@pytest.mark.django_db
def test_revoked_state():
    access = BotUserAccess.objects.create(
        workspace_id="T0001",
        slack_user_id="U0001",
        status=ACCESS_STATUS_REVOKED,
    )
    assert access.status == ACCESS_STATUS_REVOKED


@pytest.mark.django_db
def test_different_workspaces_isolated():
    BotUserAccess.objects.create(workspace_id="T0001", slack_user_id="U0001")
    access2 = BotUserAccess.objects.create(workspace_id="T0002", slack_user_id="U0001")
    assert access2.pk is not None


# ---------------------------------------------------------------------------
# UnauthorizedAccessAttempt
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_unauth_attempt_can_be_created():
    attempt = UnauthorizedAccessAttempt.objects.create(
        workspace_id="T0001",
        slack_user_id="U0001",
    )
    assert attempt.attempt_count == 0
    assert attempt.first_attempt_at is None
    assert attempt.last_attempt_at is None
    assert attempt.last_admin_notification_at is None


@pytest.mark.django_db
def test_unauth_attempt_uniqueness():
    UnauthorizedAccessAttempt.objects.create(
        workspace_id="T0001",
        slack_user_id="U0001",
    )
    with pytest.raises(IntegrityError):
        UnauthorizedAccessAttempt.objects.create(
            workspace_id="T0001",
            slack_user_id="U0001",
        )


@pytest.mark.django_db
def test_unauth_attempt_nullable_notification_timestamp():
    attempt = UnauthorizedAccessAttempt.objects.create(
        workspace_id="T0001",
        slack_user_id="U0001",
    )
    assert attempt.last_admin_notification_at is None


# ---------------------------------------------------------------------------
# BotAccessAuditLog
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_audit_log_can_be_created():
    log = BotAccessAuditLog.objects.create(
        workspace_id="T0001",
        target_slack_user_id="U0001",
        action=AUDIT_ADMIN_BOOTSTRAPPED,
    )
    assert log.pk is not None
    assert log.performed_by_slack_user_id == SYSTEM_ACTOR
    assert log.metadata == {}


@pytest.mark.django_db
def test_audit_log_with_metadata():
    log = BotAccessAuditLog.objects.create(
        workspace_id="T0001",
        target_slack_user_id="U0001",
        action=AUDIT_ACCESS_GRANTED,
        metadata={"key": "value"},
    )
    assert log.metadata == {"key": "value"}
