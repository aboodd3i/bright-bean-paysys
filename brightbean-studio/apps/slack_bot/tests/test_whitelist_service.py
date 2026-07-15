"""Tests for Phase 1 bot whitelisting service layer."""

import pytest
from django.db import transaction

from apps.slack_bot.access_service import (
    BulkGrantResult,
    GrantResult,
    AdminResult,
    configure_administrator,
    grant_bulk_user_access,
    grant_user_access,
)
from apps.slack_bot.constants import (
    ACCESS_STATUS_APPROVED,
    ACCESS_STATUS_REVOKED,
    ADMIN_STATUS_ACTIVE,
    AUDIT_ADMIN_BOOTSTRAPPED,
    AUDIT_ADMIN_UPDATED,
    AUDIT_ACCESS_GRANTED,
    AUDIT_ACCESS_RESTORED,
    AUDIT_ACCESS_ALREADY_PRESENT,
    AUDIT_BULK_ACCESS_GRANTED,
    AUDIT_INVALID_MEMBER_ID,
    PERMISSION_READ_ONLY,
    SYSTEM_ACTOR,
)
from apps.slack_bot.models import (
    BotAccessAuditLog,
    BotAdministrator,
    BotUserAccess,
)


# ===========================================================================
# configure_administrator
# ===========================================================================


@pytest.mark.django_db
def test_configure_admin_creates_admin_and_access():
    result = configure_administrator("T0001", "U0001")

    assert result.action == "created"
    assert result.workspace_id == "T0001"
    assert result.slack_user_id == "U0001"

    admin = BotAdministrator.objects.get(workspace_id="T0001")
    assert admin.slack_user_id == "U0001"
    assert admin.status == ADMIN_STATUS_ACTIVE

    access = BotUserAccess.objects.get(workspace_id="T0001", slack_user_id="U0001")
    assert access.status == ACCESS_STATUS_APPROVED
    assert access.permission == PERMISSION_READ_ONLY

    log = BotAccessAuditLog.objects.filter(action=AUDIT_ADMIN_BOOTSTRAPPED)
    assert log.count() == 1


@pytest.mark.django_db
def test_configure_admin_idempotent_same_user():
    configure_administrator("T0001", "U0001")
    result = configure_administrator("T0001", "U0001")

    assert result.action == "already_active"
    assert BotAdministrator.objects.filter(workspace_id="T0001").count() == 1


@pytest.mark.django_db
def test_configure_admin_updates_different_user():
    configure_administrator("T0001", "U0001")
    result = configure_administrator("T0001", "U0002")

    assert result.action == "updated"
    admin = BotAdministrator.objects.get(workspace_id="T0001")
    assert admin.slack_user_id == "U0002"

    # New admin should have access
    access = BotUserAccess.objects.filter(
        workspace_id="T0001", slack_user_id="U0002"
    ).first()
    assert access is not None
    assert access.status == ACCESS_STATUS_APPROVED

    # Audit log for update
    log = BotAccessAuditLog.objects.filter(action=AUDIT_ADMIN_UPDATED)
    assert log.count() == 1
    assert log.first().metadata.get("previous_admin") == "U0001"


@pytest.mark.django_db
def test_configure_admin_isolates_workspaces():
    r1 = configure_administrator("T0001", "U0001")
    r2 = configure_administrator("T0002", "U0002")
    assert r1.action == "created"
    assert r2.action == "created"
    assert BotAdministrator.objects.count() == 2


# ===========================================================================
# grant_user_access
# ===========================================================================


@pytest.mark.django_db
def test_grant_access_new_user():
    result = grant_user_access("T0001", "U0001")

    assert result.action == "granted"
    access = BotUserAccess.objects.get(workspace_id="T0001", slack_user_id="U0001")
    assert access.status == ACCESS_STATUS_APPROVED
    assert access.permission == PERMISSION_READ_ONLY
    assert access.granted_by_slack_user_id == SYSTEM_ACTOR

    log = BotAccessAuditLog.objects.filter(action=AUDIT_ACCESS_GRANTED)
    assert log.count() == 1


@pytest.mark.django_db
def test_grant_access_already_approved():
    grant_user_access("T0001", "U0001")
    result = grant_user_access("T0001", "U0001")

    assert result.action == "already_approved"
    assert BotUserAccess.objects.count() == 1

    log = BotAccessAuditLog.objects.filter(action=AUDIT_ACCESS_ALREADY_PRESENT)
    assert log.count() == 1


@pytest.mark.django_db
def test_grant_access_restores_revoked():
    access = BotUserAccess.objects.create(
        workspace_id="T0001",
        slack_user_id="U0001",
        status=ACCESS_STATUS_REVOKED,
    )
    result = grant_user_access("T0001", "U0001")

    assert result.action == "restored"
    access.refresh_from_db()
    assert access.status == ACCESS_STATUS_APPROVED
    assert access.revoked_at is None

    log = BotAccessAuditLog.objects.filter(action=AUDIT_ACCESS_RESTORED)
    assert log.count() == 1


@pytest.mark.django_db
def test_grant_access_with_custom_granted_by():
    grant_user_access("T0001", "U0001", granted_by_slack_user_id="U0099")
    access = BotUserAccess.objects.get(workspace_id="T0001", slack_user_id="U0001")
    assert access.granted_by_slack_user_id == "U0099"


@pytest.mark.django_db
def test_grant_access_workspace_isolation():
    grant_user_access("T0001", "U0001")
    grant_user_access("T0002", "U0001")
    assert BotUserAccess.objects.count() == 2


# ===========================================================================
# grant_bulk_user_access
# ===========================================================================


@pytest.mark.django_db
def test_bulk_grant_single_user():
    result = grant_bulk_user_access("T0001", ["U0001"])
    assert result.approved == ["U0001"]
    assert result.restored == []
    assert result.already_approved == []
    assert result.invalid == []
    assert result.failed == []


@pytest.mark.django_db
def test_bulk_grant_multiple_users():
    result = grant_bulk_user_access("T0001", ["U0001", "U0002", "U0003"])
    assert sorted(result.approved) == ["U0001", "U0002", "U0003"]
    assert BotUserAccess.objects.count() == 3


@pytest.mark.django_db
def test_bulk_grant_deduplicates():
    result = grant_bulk_user_access("T0001", ["U0001", "U0001", "U0001"])
    assert result.approved == ["U0001"]
    assert BotUserAccess.objects.count() == 1


@pytest.mark.django_db
def test_bulk_grant_mixed_valid_invalid():
    result = grant_bulk_user_access("T0001", ["U0001", "C0001", "U0002", "G0001"])
    assert sorted(result.approved) == ["U0001", "U0002"]
    assert sorted(result.invalid) == ["C0001", "G0001"]

    invalid_logs = BotAccessAuditLog.objects.filter(action=AUDIT_INVALID_MEMBER_ID)
    assert invalid_logs.count() == 2


@pytest.mark.django_db
def test_bulk_grant_already_approved():
    grant_user_access("T0001", "U0001")
    result = grant_bulk_user_access("T0001", ["U0001", "U0002"])
    assert result.already_approved == ["U0001"]
    assert result.approved == ["U0002"]


@pytest.mark.django_db
def test_bulk_grant_restores_revoked():
    BotUserAccess.objects.create(
        workspace_id="T0001",
        slack_user_id="U0001",
        status=ACCESS_STATUS_REVOKED,
    )
    result = grant_bulk_user_access("T0001", ["U0001"])
    assert result.restored == ["U0001"]


@pytest.mark.django_db
def test_bulk_grant_w_guest_ids_accepted():
    result = grant_bulk_user_access("T0001", ["W0001"])
    assert result.approved == ["W0001"]


@pytest.mark.django_db
def test_bulk_grant_channel_ids_rejected():
    result = grant_bulk_user_access("T0001", ["C0001", "G0001"])
    assert result.approved == []
    assert sorted(result.invalid) == ["C0001", "G0001"]


@pytest.mark.django_db
def test_bulk_grant_workspace_isolation():
    grant_bulk_user_access("T0001", ["U0001"])
    grant_bulk_user_access("T0002", ["U0001"])
    assert BotUserAccess.objects.count() == 2


@pytest.mark.django_db
def test_bulk_grant_writes_summary_audit():
    grant_bulk_user_access("T0001", ["U0001", "U0002"])
    log = BotAccessAuditLog.objects.filter(action=AUDIT_BULK_ACCESS_GRANTED)
    assert log.count() == 1
    meta = log.first().metadata
    assert sorted(meta["approved"]) == ["U0001", "U0002"]


@pytest.mark.django_db
def test_bulk_grant_empty_list():
    result = grant_bulk_user_access("T0001", [])
    assert result.approved == []
    assert result.restored == []
    assert result.already_approved == []
    assert result.invalid == []
    assert result.failed == []


@pytest.mark.django_db
def test_bulk_grant_trims_whitespace():
    result = grant_bulk_user_access("T0001", ["  U0001  ", "U0001"])
    assert result.approved == ["U0001"]
    assert BotUserAccess.objects.count() == 1
