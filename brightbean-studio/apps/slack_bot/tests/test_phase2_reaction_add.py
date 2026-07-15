"""Phase 2 tests — processing reaction (👀) add behaviour.

Tests cover:
- Approved mention receives eyes reaction before enqueue
- Approved thread reply receives eyes reaction before enqueue
- Reaction targets exact user message timestamp
- Thread reply targets reply timestamp not thread_ts
- Unregistered/revoked users do not receive reaction
- Invalid thread reply does not receive reaction
- Duplicate event does not add reaction again
- Reaction-add failure is non-blocking
- Reaction add occurs after access approval and before enqueue
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest
from django.test import Client, override_settings

from apps.slack_bot.constants import (
    ACCESS_STATUS_APPROVED,
    ACCESS_STATUS_REVOKED,
    PERMISSION_READ_ONLY,
)
from apps.slack_bot.models import (
    BotUserAccess,
    SlackInboundEvent,
)
from apps.slack_bot.reactions import (
    REACTION_NAME,
    ReactionResult,
    add_processing_reaction,
    remove_processing_reaction,
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
    text="<@B123> show analytics",
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
    text="follow up",
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
# Reaction service unit tests
# ===========================================================================


def test_add_processing_reaction_success():
    mock_response = MagicMock()
    mock_response.text = json.dumps({"ok": True})
    mock_post = MagicMock(return_value=mock_response)
    result = add_processing_reaction(
        "C123", "1720000000.000100",
        token="xoxb-test", http_post=mock_post,
    )
    assert result.ok is True
    call_args = mock_post.call_args
    body = call_args.kwargs["json_body"]
    assert body["name"] == REACTION_NAME
    assert body["channel"] == "C123"
    assert body["timestamp"] == "1720000000.000100"


def test_add_processing_reaction_failure_returns_not_ok():
    mock_response = MagicMock()
    mock_response.text = json.dumps({"ok": False, "error": "already_reacted"})
    mock_post = MagicMock(return_value=mock_response)
    result = add_processing_reaction(
        "C123", "1720000000.000100",
        token="xoxb-test", http_post=mock_post,
    )
    assert result.ok is False
    assert result.error == "already_reacted"


def test_add_processing_reaction_missing_channel():
    result = add_processing_reaction("", "1720000000.000100", token="xoxb-test")
    assert result.ok is False


def test_add_processing_reaction_missing_ts():
    result = add_processing_reaction("C123", "", token="xoxb-test")
    assert result.ok is False


def test_add_processing_reaction_missing_token():
    result = add_processing_reaction("C123", "1720000000.000100", token="")
    assert result.ok is False


def test_add_processing_reaction_http_exception():
    mock_post = MagicMock(side_effect=Exception("connection refused"))
    result = add_processing_reaction(
        "C123", "1720000000.000100",
        token="xoxb-test", http_post=mock_post,
    )
    assert result.ok is False


def test_remove_processing_reaction_success():
    mock_response = MagicMock()
    mock_response.text = json.dumps({"ok": True})
    mock_post = MagicMock(return_value=mock_response)
    result = remove_processing_reaction(
        "C123", "1720000000.000100",
        token="xoxb-test", http_post=mock_post,
    )
    assert result.ok is True


def test_remove_processing_reaction_failure_returns_not_ok():
    mock_response = MagicMock()
    mock_response.text = json.dumps({"ok": False, "error": "no_reaction"})
    mock_post = MagicMock(return_value=mock_response)
    result = remove_processing_reaction(
        "C123", "1720000000.000100",
        token="xoxb-test", http_post=mock_post,
    )
    assert result.ok is False


def test_reaction_name_is_eyes():
    assert REACTION_NAME == "eyes"


# ===========================================================================
# Reaction add — integration via views.py
# ===========================================================================


@pytest.mark.django_db
@override_settings(SLACK_SIGNING_SECRET=SECRET)
@patch("apps.slack_bot.views.enqueue_inbound_event")
@patch("apps.slack_bot.views.add_processing_reaction")
def test_approved_mention_receives_eyes_before_enqueue(mock_add_reaction, mock_enqueue):
    mock_add_reaction.return_value = MagicMock(ok=True)
    _create_approved_user("TTEST123", "UUSER123")
    client = Client()
    _post(client, _mention_payload())
    mock_add_reaction.assert_called_once()
    # Verify reaction called with correct channel and message ts
    call_kwargs = mock_add_reaction.call_args.kwargs
    assert call_kwargs["channel_id"] == "C123"
    assert call_kwargs["message_ts"] == "1720000000.000100"


@pytest.mark.django_db
@override_settings(SLACK_SIGNING_SECRET=SECRET)
@patch("apps.slack_bot.views.enqueue_inbound_event")
@patch("apps.slack_bot.views.add_processing_reaction")
def test_approved_thread_reply_receives_eyes(mock_add_reaction, mock_enqueue):
    mock_add_reaction.return_value = MagicMock(ok=True)
    _create_approved_user("TTEST123", "UUSER123")
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
    _post(client, _thread_reply_payload())
    mock_add_reaction.assert_called_once()


@pytest.mark.django_db
@override_settings(SLACK_SIGNING_SECRET=SECRET)
@patch("apps.slack_bot.views.enqueue_inbound_event")
@patch("apps.slack_bot.views.add_processing_reaction")
def test_reaction_targets_exact_message_ts(mock_add_reaction, mock_enqueue):
    mock_add_reaction.return_value = MagicMock(ok=True)
    _create_approved_user("TTEST123", "UUSER123")
    client = Client()
    _post(client, _mention_payload(ts="1720000000.000999"))
    call_kwargs = mock_add_reaction.call_args.kwargs
    assert call_kwargs["message_ts"] == "1720000000.000999"


@pytest.mark.django_db
@override_settings(SLACK_SIGNING_SECRET=SECRET)
@patch("apps.slack_bot.views.enqueue_inbound_event")
@patch("apps.slack_bot.views.add_processing_reaction")
def test_thread_reply_targets_reply_ts_not_thread_ts(mock_add_reaction, mock_enqueue):
    mock_add_reaction.return_value = MagicMock(ok=True)
    _create_approved_user("TTEST123", "UUSER123")
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
    _post(client, _thread_reply_payload(
        thread_ts="1720000000.000050",
        ts="1720000000.000300",
    ))
    call_kwargs = mock_add_reaction.call_args.kwargs
    # Should target the reply's own ts, not the thread_ts
    assert call_kwargs["message_ts"] == "1720000000.000300"
    assert call_kwargs["message_ts"] != "1720000000.000050"


@pytest.mark.django_db
@override_settings(SLACK_SIGNING_SECRET=SECRET)
@patch("apps.slack_bot.views.enqueue_inbound_event")
@patch("apps.slack_bot.views.add_processing_reaction")
def test_unregistered_user_no_reaction(mock_add_reaction, mock_enqueue):
    mock_add_reaction.return_value = MagicMock(ok=True)
    client = Client()
    _post(client, _mention_payload())
    mock_add_reaction.assert_not_called()


@pytest.mark.django_db
@override_settings(SLACK_SIGNING_SECRET=SECRET)
@patch("apps.slack_bot.views.enqueue_inbound_event")
@patch("apps.slack_bot.views.add_processing_reaction")
def test_revoked_user_no_reaction(mock_add_reaction, mock_enqueue):
    mock_add_reaction.return_value = MagicMock(ok=True)
    BotUserAccess.objects.create(
        workspace_id="TTEST123",
        slack_user_id="UUSER123",
        status=ACCESS_STATUS_REVOKED,
        permission=PERMISSION_READ_ONLY,
    )
    client = Client()
    _post(client, _mention_payload())
    mock_add_reaction.assert_not_called()


@pytest.mark.django_db
@override_settings(SLACK_SIGNING_SECRET=SECRET)
@patch("apps.slack_bot.views.enqueue_inbound_event")
@patch("apps.slack_bot.views.add_processing_reaction")
def test_invalid_thread_reply_no_reaction(mock_add_reaction, mock_enqueue):
    mock_add_reaction.return_value = MagicMock(ok=True)
    _create_approved_user("TTEST123", "UUSER123")
    client = Client()
    # Thread reply where thread_ts doesn't match any bot response
    _post(client, _thread_reply_payload(thread_ts="9999999999.999999"))
    mock_add_reaction.assert_not_called()


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
    assert mock_add_reaction.call_count == 1


@pytest.mark.django_db
@override_settings(SLACK_SIGNING_SECRET=SECRET)
@patch("apps.slack_bot.views.enqueue_inbound_event")
@patch("apps.slack_bot.views.add_processing_reaction")
def test_reaction_add_failure_is_non_blocking(mock_add_reaction, mock_enqueue):
    mock_add_reaction.return_value = MagicMock(ok=False, error="already_reacted")
    _create_approved_user("TTEST123", "UUSER123")
    client = Client()
    response = _post(client, _mention_payload())
    # Processing should still proceed — event enqueued despite reaction failure
    assert response.status_code == 200
    assert response.json()["status"] == "received"
    mock_enqueue.assert_called_once()


@pytest.mark.django_db
@override_settings(SLACK_SIGNING_SECRET=SECRET)
@patch("apps.slack_bot.views.enqueue_inbound_event")
@patch("apps.slack_bot.views.add_processing_reaction")
def test_reaction_add_after_access_before_enqueue(mock_add_reaction, mock_enqueue):
    """Verify ordering: access check → reaction → enqueue.

    We use call ordering via a side_effect that checks enqueue hasn't
    been called yet when reaction is called.
    """
    call_order = []

    def track_add_reaction(*args, **kwargs):
        call_order.append("reaction")
        return MagicMock(ok=True)

    def track_enqueue(*args, **kwargs):
        call_order.append("enqueue")

    mock_add_reaction.side_effect = track_add_reaction
    mock_enqueue.side_effect = track_enqueue
    _create_approved_user("TTEST123", "UUSER123")
    client = Client()
    _post(client, _mention_payload())
    assert call_order == ["reaction", "enqueue"]
