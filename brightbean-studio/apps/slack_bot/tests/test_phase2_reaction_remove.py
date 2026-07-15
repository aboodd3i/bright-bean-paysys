"""Phase 2 tests — processing reaction (👀) remove behaviour.

Tests cover:
- Eyes reaction removed before successful response
- Eyes reaction removed before controlled error response
- Reaction removal uses original user channel ID and message timestamp
- Reaction-remove failure does not block Slack response
- Enqueue failure triggers best-effort reaction removal
- Response path that never added reaction does not attempt cleanup
- Existing message-thread behaviour unchanged
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest
from django.test import Client, override_settings

from apps.slack_bot.constants import (
    ACCESS_STATUS_APPROVED,
    PERMISSION_READ_ONLY,
    STATUS_RESPONDED,
)
from apps.slack_bot.models import (
    BotUserAccess,
    SlackInboundEvent,
)
from apps.slack_bot.reactions import (
    ReactionResult,
    remove_processing_reaction,
)
from apps.slack_bot.tasks import _deliver_with_reaction_cleanup
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


def _create_approved_user(workspace_id="TTEST123", slack_user_id="UUSER123"):
    return BotUserAccess.objects.create(
        workspace_id=workspace_id,
        slack_user_id=slack_user_id,
        status=ACCESS_STATUS_APPROVED,
        permission=PERMISSION_READ_ONLY,
    )


# ===========================================================================
# Reaction remove — delivery wrapper tests
# ===========================================================================


@pytest.mark.django_db
@patch("apps.slack_bot.tasks.remove_processing_reaction")
@patch("apps.slack_bot.tasks.deliver_slack_response")
def test_reaction_removed_before_successful_response(mock_deliver, mock_remove):
    mock_deliver.return_value = "1720000001.000200"
    mock_remove.return_value = ReactionResult(
        ok=True, channel_id="C123", message_ts="1720000000.000100",
    )
    event = SlackInboundEvent.objects.create(
        event_id="Ev_test_1",
        team_id="TTEST123",
        channel_id="C123",
        user_id="UUSER123",
        event_ts="1720000000.000100",
        message_text="hello",
        thread_ts="",
    )
    _deliver_with_reaction_cleanup(
        channel_id="C123",
        text="Here are your analytics",
        thread_ts="",
        event=event,
    )
    # Reaction removed before delivery
    mock_remove.assert_called_once_with(
        channel_id="C123",
        message_ts="1720000000.000100",
    )
    mock_deliver.assert_called_once()


@pytest.mark.django_db
@patch("apps.slack_bot.tasks.remove_processing_reaction")
@patch("apps.slack_bot.tasks.deliver_slack_response")
def test_reaction_removed_before_controlled_error_response(mock_deliver, mock_remove):
    mock_deliver.return_value = "1720000001.000200"
    mock_remove.return_value = ReactionResult(
        ok=True, channel_id="C123", message_ts="1720000000.000100",
    )
    event = SlackInboundEvent.objects.create(
        event_id="Ev_test_1",
        team_id="TTEST123",
        channel_id="C123",
        user_id="UUSER123",
        event_ts="1720000000.000100",
        message_text="hello",
        thread_ts="",
    )
    _deliver_with_reaction_cleanup(
        channel_id="C123",
        text="I couldn't process your request right now.",
        thread_ts="",
        event=event,
    )
    mock_remove.assert_called_once()
    mock_deliver.assert_called_once()


@pytest.mark.django_db
@patch("apps.slack_bot.tasks.remove_processing_reaction")
@patch("apps.slack_bot.tasks.deliver_slack_response")
def test_removal_uses_original_channel_and_message_ts(mock_deliver, mock_remove):
    mock_deliver.return_value = "1720000001.000200"
    mock_remove.return_value = ReactionResult(
        ok=True, channel_id="C_ORIG", message_ts="1720000000.000100",
    )
    event = SlackInboundEvent.objects.create(
        event_id="Ev_test_1",
        team_id="TTEST123",
        channel_id="C_ORIG",
        user_id="UUSER123",
        event_ts="1720000000.000100",
        message_text="hello",
        thread_ts="1720000000.000050",
    )
    _deliver_with_reaction_cleanup(
        channel_id="C_ORIG",
        text="response",
        thread_ts="1720000000.000050",
        event=event,
    )
    call_kwargs = mock_remove.call_args.kwargs
    assert call_kwargs["channel_id"] == "C_ORIG"
    assert call_kwargs["message_ts"] == "1720000000.000100"


@pytest.mark.django_db
@patch("apps.slack_bot.tasks.remove_processing_reaction")
@patch("apps.slack_bot.tasks.deliver_slack_response")
def test_reaction_remove_failure_does_not_block_response(mock_deliver, mock_remove):
    mock_deliver.return_value = "1720000001.000200"
    mock_remove.return_value = ReactionResult(
        ok=False, channel_id="C123", message_ts="1720000000.000100",
        error="no_reaction",
    )
    event = SlackInboundEvent.objects.create(
        event_id="Ev_test_1",
        team_id="TTEST123",
        channel_id="C123",
        user_id="UUSER123",
        event_ts="1720000000.000100",
        message_text="hello",
        thread_ts="",
    )
    result = _deliver_with_reaction_cleanup(
        channel_id="C123",
        text="response",
        thread_ts="",
        event=event,
    )
    # Delivery still happened despite reaction remove failure
    mock_deliver.assert_called_once()
    assert result == "1720000001.000200"


@pytest.mark.django_db
@patch("apps.slack_bot.tasks.remove_processing_reaction")
@patch("apps.slack_bot.tasks.deliver_slack_response")
def test_removal_order_before_delivery(mock_deliver, mock_remove):
    """Verify reaction is removed BEFORE the response is posted."""
    call_order = []

    def track_remove(*args, **kwargs):
        call_order.append("remove")
        return ReactionResult(ok=True, channel_id="C123", message_ts="ts")

    def track_deliver(**kwargs):
        call_order.append("deliver")
        return "1720000001.000200"

    mock_remove.side_effect = track_remove
    mock_deliver.side_effect = track_deliver

    event = SlackInboundEvent.objects.create(
        event_id="Ev_test_1",
        team_id="TTEST123",
        channel_id="C123",
        user_id="UUSER123",
        event_ts="1720000000.000100",
        message_text="hello",
        thread_ts="",
    )
    _deliver_with_reaction_cleanup(
        channel_id="C123",
        text="response",
        thread_ts="",
        event=event,
    )
    assert call_order == ["remove", "deliver"]


# ===========================================================================
# Enqueue failure cleanup
# ===========================================================================


@pytest.mark.django_db
@override_settings(SLACK_SIGNING_SECRET=SECRET)
@patch("apps.slack_bot.views.enqueue_inbound_event")
@patch("apps.slack_bot.views.add_processing_reaction")
@patch("apps.slack_bot.views.remove_processing_reaction")
def test_enqueue_failure_triggers_reaction_cleanup(
    mock_remove_reaction, mock_add_reaction, mock_enqueue
):
    mock_add_reaction.return_value = MagicMock(ok=True)
    mock_enqueue.side_effect = Exception("queue down")
    _create_approved_user("TTEST123", "UUSER123")
    client = Client()
    response = _post(client, _mention_payload())
    assert response.status_code == 200
    assert response.json()["reason"] == "enqueue_failed"
    mock_remove_reaction.assert_called_once_with(
        channel_id="C123",
        message_ts="1720000000.000100",
    )


@pytest.mark.django_db
@override_settings(SLACK_SIGNING_SECRET=SECRET)
@patch("apps.slack_bot.views.enqueue_inbound_event")
@patch("apps.slack_bot.views.add_processing_reaction")
@patch("apps.slack_bot.views.remove_processing_reaction")
def test_enqueue_failure_no_cleanup_if_reaction_not_added(
    mock_remove_reaction, mock_add_reaction, mock_enqueue
):
    mock_add_reaction.return_value = MagicMock(ok=False)
    mock_enqueue.side_effect = Exception("queue down")
    _create_approved_user("TTEST123", "UUSER123")
    client = Client()
    _post(client, _mention_payload())
    # Reaction was not added, so no cleanup attempt
    mock_remove_reaction.assert_not_called()


# ===========================================================================
# No cleanup for paths that never added reaction
# ===========================================================================


@pytest.mark.django_db
@override_settings(SLACK_SIGNING_SECRET=SECRET)
@patch("apps.slack_bot.views.enqueue_inbound_event")
@patch("apps.slack_bot.views.add_processing_reaction")
@patch("apps.slack_bot.views.remove_processing_reaction")
def test_unauthorized_no_reaction_cleanup(
    mock_remove_reaction, mock_add_reaction, mock_enqueue
):
    mock_add_reaction.return_value = MagicMock(ok=True)
    client = Client()
    _post(client, _mention_payload())  # No approved user
    mock_add_reaction.assert_not_called()
    mock_remove_reaction.assert_not_called()


# ===========================================================================
# Existing thread behaviour unchanged
# ===========================================================================


@pytest.mark.django_db
@override_settings(SLACK_SIGNING_SECRET=SECRET)
@patch("apps.slack_bot.views.enqueue_inbound_event")
@patch("apps.slack_bot.views.add_processing_reaction")
def test_existing_thread_behaviour_unchanged(mock_add_reaction, mock_enqueue):
    """Approved thread reply still works as before, just with access gate + reaction."""
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
        status=STATUS_RESPONDED,
        response_ts="1720000000.000050",
    )
    client = Client()
    payload = {
        "team_id": "TTEST123",
        "type": "event_callback",
        "event_id": "Ev_thread_1",
        "event": {
            "type": "message",
            "user": "UUSER123",
            "text": "follow up question",
            "ts": "1720000000.000200",
            "channel": "C123",
            "thread_ts": "1720000000.000050",
        },
    }
    response = _post(client, payload)
    assert response.status_code == 200
    assert response.json()["status"] == "received"
    mock_enqueue.assert_called_once()
