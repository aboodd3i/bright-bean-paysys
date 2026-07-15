"""Tests for Phase 1 bot whitelisting management commands."""

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError

from apps.slack_bot.models import (
    BotAccessAuditLog,
    BotAdministrator,
    BotUserAccess,
)


# ===========================================================================
# create_bot_admin
# ===========================================================================


@pytest.mark.django_db
def test_create_bot_admin_first_time(capsys):
    call_command(
        "create_bot_admin",
        "--workspace-id", "T0001",
        "--user-id", "U0001",
    )
    out = capsys.readouterr().out
    assert "configured successfully" in out
    assert "T0001" in out
    assert "U0001" in out

    admin = BotAdministrator.objects.get(workspace_id="T0001")
    assert admin.slack_user_id == "U0001"

    access = BotUserAccess.objects.get(workspace_id="T0001", slack_user_id="U0001")
    assert access.status == "APPROVED"


@pytest.mark.django_db
def test_create_bot_admin_repeated_same_user(capsys):
    call_command("create_bot_admin", "--workspace-id", "T0001", "--user-id", "U0001")
    call_command("create_bot_admin", "--workspace-id", "T0001", "--user-id", "U0001")
    out = capsys.readouterr().out
    assert "No change required" in out
    assert BotAdministrator.objects.count() == 1


@pytest.mark.django_db
def test_create_bot_admin_different_user(capsys):
    call_command("create_bot_admin", "--workspace-id", "T0001", "--user-id", "U0001")
    call_command("create_bot_admin", "--workspace-id", "T0001", "--user-id", "U0002")
    out = capsys.readouterr().out
    assert "updated" in out
    admin = BotAdministrator.objects.get(workspace_id="T0001")
    assert admin.slack_user_id == "U0002"


@pytest.mark.django_db
def test_create_bot_admin_invalid_workspace_id():
    with pytest.raises(CommandError, match="Invalid workspace ID"):
        call_command("create_bot_admin", "--workspace-id", "X0001", "--user-id", "U0001")


@pytest.mark.django_db
def test_create_bot_admin_invalid_user_id():
    with pytest.raises(CommandError, match="Invalid user ID"):
        call_command("create_bot_admin", "--workspace-id", "T0001", "--user-id", "C0001")


# ===========================================================================
# grant_bot_access
# ===========================================================================


@pytest.mark.django_db
def test_grant_bot_access_single_user(capsys):
    call_command(
        "grant_bot_access",
        "--workspace-id", "T0001",
        "--user-ids", "U0001",
    )
    out = capsys.readouterr().out
    assert "U0001" in out
    assert BotUserAccess.objects.filter(workspace_id="T0001").count() == 1


@pytest.mark.django_db
def test_grant_bot_access_multiple_users(capsys):
    call_command(
        "grant_bot_access",
        "--workspace-id", "T0001",
        "--user-ids", "U0001", "U0002", "U0003",
    )
    out = capsys.readouterr().out
    assert "U0001" in out
    assert "U0002" in out
    assert "U0003" in out
    assert BotUserAccess.objects.filter(workspace_id="T0001").count() == 3


@pytest.mark.django_db
def test_grant_bot_access_duplicate_users(capsys):
    call_command(
        "grant_bot_access",
        "--workspace-id", "T0001",
        "--user-ids", "U0001", "U0001",
    )
    assert BotUserAccess.objects.filter(workspace_id="T0001").count() == 1


@pytest.mark.django_db
def test_grant_bot_access_mixed_valid_invalid(capsys):
    call_command(
        "grant_bot_access",
        "--workspace-id", "T0001",
        "--user-ids", "U0001", "C0001", "U0002", "G0001",
    )
    out = capsys.readouterr().out
    assert "C0001" in out
    assert "G0001" in out
    assert BotUserAccess.objects.filter(workspace_id="T0001").count() == 2


@pytest.mark.django_db
def test_grant_bot_access_channel_id_rejected(capsys):
    call_command(
        "grant_bot_access",
        "--workspace-id", "T0001",
        "--user-ids", "C0001",
    )
    out = capsys.readouterr().out
    assert "C0001" in out
    assert BotUserAccess.objects.count() == 0


@pytest.mark.django_db
def test_grant_bot_access_invalid_workspace_id():
    with pytest.raises(CommandError, match="Invalid workspace ID"):
        call_command(
            "grant_bot_access",
            "--workspace-id", "X0001",
            "--user-ids", "U0001",
        )


@pytest.mark.django_db
def test_grant_bot_access_already_approved(capsys):
    call_command("grant_bot_access", "--workspace-id", "T0001", "--user-ids", "U0001")
    call_command("grant_bot_access", "--workspace-id", "T0001", "--user-ids", "U0001")
    out = capsys.readouterr().out
    assert "Already approved" in out
    assert BotUserAccess.objects.count() == 1


@pytest.mark.django_db
def test_grant_bot_access_restores_revoked(capsys):
    BotUserAccess.objects.create(
        workspace_id="T0001",
        slack_user_id="U0001",
        status="REVOKED",
    )
    call_command("grant_bot_access", "--workspace-id", "T0001", "--user-ids", "U0001")
    out = capsys.readouterr().out
    assert "U0001" in out
    access = BotUserAccess.objects.get(workspace_id="T0001", slack_user_id="U0001")
    assert access.status == "APPROVED"
