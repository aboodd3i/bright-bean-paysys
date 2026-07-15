"""Phase 2 tests — access gate (is_user_approved + views.py integration).

Tests cover:
- is_user_approved service behaviour
- Access gate in views.py::slack_events()
- Approved/unregistered/revoked user flows
- Workspace isolation
- Duplicate event handling
- DM handling unchanged
- Unauthorized events return 200 and do not enqueue
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest
from django.test import Client, override_settings

from apps.slack_bot.access_service import is_user_approved
from apps.slack_bot.constants import (
    ACCESS_STATUS_APPROVED,
    ACCESS_STATUS_REVOKED,
    PERMISSION_READ_ONLY,
    STATUS_IGNORED,
)
from apps.slack_bot.models import (
    BotUserAccess,
    SlackInboundEvent,
)
from apps.slack_bot.tests.conftest import signed_slack_headers

SECRET = "test_secret"


def _post(client, body_dict, secret=SECRET, timestamp=None):
    raw = json.dumps(body_dict).encode("utf-8")
    headers = signed_slack_headers(raw, secret=secret, timestamp=timestamp)
    from django.urls import reverse
    url = reverse("slack_bot:events")
    return client.post(url, data=raw, content_type="application/json", **headers)


def _mention_payload(
    event_id="Ev_test_1",
    team_id="TTEST123",
    user_id="UUSER123",
    channel_id="C123",
    text="<@B123> show me analytics",
    ts="1720000000.000100",
):
    return {
        "team_id": team_id,
        "type": "event_callback",
        "event_id": event_id,
        "event": {
            "type": "app_mention",
            "user": user_id,
            "text": text,
            "ts": ts,
            "channel": channel_id,
        },
    }


def _thread_reply_payload(
    event_id="Ev_thread_1",
    team_id="TTEST123",
    user_id="UUSER123",
    channel_id="C123",
    thread_ts="1720000000.000050",
    ts="1720000000.000200",
    text="follow up question",
):
    return {
        "team_id": team_id,
        "type": "event_callback",
        "event_id": event_id,
        "event": {
            "type": "message",
            "user": user_id,
            "text": text,
            "ts": ts,
            "channel": channel_id,
            "thread_ts": thread_ts,
        },
    }


def _create_approved_user(workspace_id="TTEST123", slack_user_id="UUSER123"):
    return BotUserAccess.objects.create(
        workspace_id=workspace_id,
        slack_user_id=slack_user_id,
        status=ACCESS_STATUS_APPROVED,
        permission=PERMISSION_READ_ONLY,
    )


# ===========================================================================
# is_user_approved — unit tests
# ===========================================================================


@pytest.mark.django_db
def test_is_user_approved_true_for_approved_user():
    _create_approved_user("TTEST123", "UUSER123")
    assert is_user_approved("TTEST123", "UUSER123") is True


@pytest.mark.django_db
def test_is_user_approved_false_for_unregistered_user():
    assert is_user_approved("TTEST123", "UUSER123") is False


@pytest.mark.django_db
def test_is_user_approved_false_for_revoked_user():
    BotUserAccess.objects.create(
        workspace_id="TTEST123",
        slack_user_id="UUSER123",
        status=ACCESS_STATUS_REVOKED,
        permission=PERMISSION_READ_ONLY,
    )
    assert is_user_approved("TTEST123", "UUSER123") is False


@pytest.mark.django_db
def test_is_user_approved_workspace_isolation():
    _create_approved_user("TTEST123", "UUSER123")
    assert is_user_approved("TOTHER456", "UUSER123") is False


@pytest.mark.django_db
def test_is_user_approved_different_user_same_workspace():
    _create_approved_user("TTEST123", "UUSER123")
    assert is_user_approved("TTEST123", "UOTHER456") is False


@pytest.mark.django_db
def test_is_user_approved_does_not_create_records():
    is_user_approved("TTEST123", "UUSER123")
    assert BotUserAccess.objects.count() == 0


# ===========================================================================
# Access gate — integration tests via views.py
# ===========================================================================


@pytest.mark.django_db
@override_settings(SLACK_SIGNING_SECRET=SECRET)
@patch("apps.slack_bot.views.enqueue_inbound_event")
@patch("apps.slack_bot.views.add_processing_reaction")
def test_approved_mention_passes_access_gate(mock_add_reaction, mock_enqueue):
    mock_add_reaction.return_value = MagicMock(ok=True)
    _create_approved_user("TTEST123", "UUSER123")
    client = Client()
    response = _post(client, _mention_payload())
    assert response.status_code == 200
    assert response.json()["status"] == "received"
    mock_enqueue.assert_called_once()


@pytest.mark.django_db
@override_settings(SLACK_SIGNING_SECRET=SECRET)
@patch("apps.slack_bot.views.enqueue_inbound_event")
@patch("apps.slack_bot.views.add_processing_reaction")
def test_approved_thread_reply_passes_access_gate(mock_add_reaction, mock_enqueue):
    mock_add_reaction.return_value = MagicMock(ok=True)
    _create_approved_user("TTEST123", "UUSER123")
    # Need a known bot thread for the thread reply to be accepted
    SlackInboundEvent.objects.create(
        event_id="Ev_parent",
        team_id="TTEST123",
        channel_id="C123",
        user_id="UBOT",
        event_ts="1720000000.000050",
        message_text="bot response",
        thread_ts="",
        status="RESPONDED",
        response_ts="1720000000.000050",
    )
    client = Client()
    response = _post(client, _thread_reply_payload())
    assert response.status_code == 200
    assert response.json()["status"] == "received"
    mock_enqueue.assert_called_once()


@pytest.mark.django_db
@override_settings(SLACK_SIGNING_SECRET=SECRET)
@patch("apps.slack_bot.views.enqueue_inbound_event")
@patch("apps.slack_bot.views.add_processing_reaction")
def test_unregistered_mention_does_not_enqueue(mock_add_reaction, mock_enqueue):
    mock_add_reaction.return_value = MagicMock(ok=True)
    client = Client()
    response = _post(client, _mention_payload())
    assert response.status_code == 200
    assert response.json()["status"] == "ignored"
    assert response.json()["reason"] == "access_denied"
    mock_enqueue.assert_not_called()
    mock_add_reaction.assert_not_called()


@pytest.mark.django_db
@override_settings(SLACK_SIGNING_SECRET=SECRET)
@patch("apps.slack_bot.views.enqueue_inbound_event")
@patch("apps.slack_bot.views.add_processing_reaction")
def test_revoked_user_does_not_enqueue(mock_add_reaction, mock_enqueue):
    mock_add_reaction.return_value = MagicMock(ok=True)
    BotUserAccess.objects.create(
        workspace_id="TTEST123",
        slack_user_id="UUSER123",
        status=ACCESS_STATUS_REVOKED,
        permission=PERMISSION_READ_ONLY,
    )
    client = Client()
    response = _post(client, _mention_payload())
    assert response.status_code == 200
    assert response.json()["reason"] == "access_denied"
    mock_enqueue.assert_not_called()


@pytest.mark.django_db
@override_settings(SLACK_SIGNING_SECRET=SECRET)
@patch("apps.slack_bot.views.enqueue_inbound_event")
@patch("apps.slack_bot.views.add_processing_reaction")
def test_user_approved_in_other_workspace_does_not_enqueue(mock_add_reaction, mock_enqueue):
    mock_add_reaction.return_value = MagicMock(ok=True)
    _create_approved_user("TOTHER456", "UUSER123")
    client = Client()
    response = _post(client, _mention_payload(team_id="TTEST123"))
    assert response.status_code == 200
    assert response.json()["reason"] == "access_denied"
    mock_enqueue.assert_not_called()


@pytest.mark.django_db
@override_settings(SLACK_SIGNING_SECRET=SECRET)
@patch("apps.slack_bot.views.enqueue_inbound_event")
@patch("apps.slack_bot.views.add_processing_reaction")
def test_admin_with_approved_access_can_use_bot(mock_add_reaction, mock_enqueue):
    mock_add_reaction.return_value = MagicMock(ok=True)
    from apps.slack_bot.models import BotAdministrator
    BotAdministrator.objects.create(
        workspace_id="TTEST123",
        slack_user_id="UADMIN123",
    )
    _create_approved_user("TTEST123", "UADMIN123")
    client = Client()
    response = _post(client, _mention_payload(user_id="UADMIN123"))
    assert response.status_code == 200
    assert response.json()["status"] == "received"
    mock_enqueue.assert_called_once()


@pytest.mark.django_db
@override_settings(SLACK_SIGNING_SECRET=SECRET)
@patch("apps.slack_bot.views.enqueue_inbound_event")
@patch("apps.slack_bot.views.add_processing_reaction")
def test_unauthorized_returns_successful_ack(mock_add_reaction, mock_enqueue):
    mock_add_reaction.return_value = MagicMock(ok=True)
    client = Client()
    response = _post(client, _mention_payload())
    assert response.status_code == 200
    assert response.json()["ok"] is True


@pytest.mark.django_db
@override_settings(SLACK_SIGNING_SECRET=SECRET)
@patch("apps.slack_bot.views.enqueue_inbound_event")
@patch("apps.slack_bot.views.add_processing_reaction")
def test_unauthorized_does_not_call_llm_or_brightbean(mock_add_reaction, mock_enqueue):
    """Verify no enqueue, no reaction — which means no LLM/BrightBean/tools."""
    mock_add_reaction.return_value = MagicMock(ok=True)
    client = Client()
    _post(client, _mention_payload())
    mock_enqueue.assert_not_called()
    mock_add_reaction.assert_not_called()


@pytest.mark.django_db
@override_settings(SLACK_SIGNING_SECRET=SECRET)
@patch("apps.slack_bot.views.enqueue_inbound_event")
@patch("apps.slack_bot.views.add_processing_reaction")
def test_duplicate_event_does_not_enqueue_twice(mock_add_reaction, mock_enqueue):
    mock_add_reaction.return_value = MagicMock(ok=True)
    _create_approved_user("TTEST123", "UUSER123")
    client = Client()
    payload = _mention_payload()
    r1 = _post(client, payload)
    assert r1.json()["status"] == "received"
    r2 = _post(client, payload)
    assert r2.json()["status"] == "duplicate"
    # enqueue called only once
    assert mock_enqueue.call_count == 1


@pytest.mark.django_db
@override_settings(SLACK_SIGNING_SECRET=SECRET)
@patch("apps.slack_bot.views.enqueue_inbound_event")
@patch("apps.slack_bot.views.add_processing_reaction")
def test_duplicate_event_no_additional_reaction(mock_add_reaction, mock_enqueue):
    mock_add_reaction.return_value = MagicMock(ok=True)
    _create_approved_user("TTEST123", "UUSER123")
    client = Client()
    payload = _mention_payload()
    _post(client, payload)
    _post(client, payload)
    # Reaction added only once (first delivery)
    assert mock_add_reaction.call_count == 1


@pytest.mark.django_db
@override_settings(SLACK_SIGNING_SECRET=SECRET)
def test_dm_event_still_rejected():
    """Direct messages should remain rejected — no access check needed."""
    client = Client()
    payload = {
        "team_id": "TTEST123",
        "type": "event_callback",
        "event_id": "Ev_dm_1",
        "event": {
            "type": "message",
            "user": "UUSER123",
            "text": "hello in DM",
            "ts": "1720000000.000100",
            "channel": "D123",
        },
    }
    response = _post(client, payload)
    assert response.status_code == 200
    assert response.json()["status"] == "ignored"


@pytest.mark.django_db
@override_settings(SLACK_SIGNING_SECRET=SECRET)
@patch("apps.slack_bot.views.enqueue_inbound_event")
@patch("apps.slack_bot.views.add_processing_reaction")
def test_unauthorized_event_marked_ignored_in_db(mock_add_reaction, mock_enqueue):
    mock_add_reaction.return_value = MagicMock(ok=True)
    client = Client()
    _post(client, _mention_payload())
    event = SlackInboundEvent.objects.get(event_id="Ev_test_1")
    assert event.status == STATUS_IGNORED


@pytest.mark.django_db
@override_settings(SLACK_SIGNING_SECRET=SECRET)
@patch("apps.slack_bot.views.enqueue_inbound_event")
@patch("apps.slack_bot.views.add_processing_reaction")
def test_approved_event_not_marked_ignored(mock_add_reaction, mock_enqueue):
    mock_add_reaction.return_value = MagicMock(ok=True)
    _create_approved_user("TTEST123", "UUSER123")
    client = Client()
    _post(client, _mention_payload())
    event = SlackInboundEvent.objects.get(event_id="Ev_test_1")
    assert event.status != STATUS_IGNORED
