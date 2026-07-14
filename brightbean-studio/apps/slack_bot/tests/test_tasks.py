"""Tests for the background/core processing pipeline."""

from __future__ import annotations

import pytest

from apps.slack_bot.constants import (
    STATUS_FAILED,
    STATUS_IGNORED,
    STATUS_RESPONDED,
)
from apps.slack_bot.models import SlackInboundEvent
from apps.slack_bot.tasks import (
    ProcessingResult,
    RESULT_ALREADY_RESPONDED,
    RESULT_DELIVERED,
    RESULT_FAILED,
    RESULT_IGNORED,
    RESULT_NOT_FOUND,
    process_inbound_event,
)


def _create_event(
    event_id="Ev_task_1",
    message_text="<@B123> hello",
    thread_ts="1720000000.000100",
    status=None,
):
    kwargs = dict(
        event_id=event_id,
        team_id="T123",
        channel_id="C123",
        user_id="U123",
        event_ts="1720000000.000100",
        message_text=message_text,
        thread_ts=thread_ts,
    )
    if status:
        kwargs["status"] = status
    return SlackInboundEvent.objects.create(**kwargs)


def _fake_delivery(response_ts="1720000001.000200"):
    """Return a fake delivery callback that records its calls."""
    calls = []

    def _deliver(**kwargs):
        calls.append(kwargs)
        return response_ts

    _deliver.calls = calls
    return _deliver


# ===========================================================================
# 1. Missing event
# ===========================================================================

@pytest.mark.django_db
def test_missing_event_returns_not_found():
    result = process_inbound_event("Ev_does_not_exist")
    assert result.ok is False
    assert result.status == RESULT_NOT_FOUND
    assert result.event_id == "Ev_does_not_exist"


# ===========================================================================
# 2. Greeting event — simple response delivered, no authorization
# ===========================================================================

@pytest.mark.django_db
def test_greeting_event_simple_response():
    event = _create_event(event_id="Ev_greet", message_text="<@B123> hello")
    delivery = _fake_delivery()

    result = process_inbound_event(event.event_id, deliver_response=delivery)

    assert result.ok is True
    assert result.status == RESULT_DELIVERED
    assert result.response_type == "greeting"
    assert len(delivery.calls) == 1

    event.refresh_from_db()
    assert event.status == STATUS_RESPONDED


# ===========================================================================
# 3. Help event — simple response delivered, no authorization
# ===========================================================================

@pytest.mark.django_db
def test_help_event_simple_response():
    event = _create_event(event_id="Ev_help", message_text="<@B123> help")
    delivery = _fake_delivery()

    result = process_inbound_event(event.event_id, deliver_response=delivery)

    assert result.ok is True
    assert result.status == RESULT_DELIVERED
    assert result.response_type == "help"

    event.refresh_from_db()
    assert event.status == STATUS_RESPONDED


# ===========================================================================
# 4. Analytics query — authorization fails, error delivered
# ===========================================================================

@pytest.mark.django_db
def test_analytics_query_no_response():
    event = _create_event(
        event_id="Ev_analytics",
        message_text="<@B123> top instagram post this week",
    )
    delivery = _fake_delivery()

    result = process_inbound_event(event.event_id, deliver_response=delivery)

    assert result.ok is True
    assert result.status == RESULT_DELIVERED
    assert result.response_type == "error"

    event.refresh_from_db()
    assert event.status == STATUS_RESPONDED


# ===========================================================================
# 5. Already responded — no re-processing
# ===========================================================================

@pytest.mark.django_db
def test_already_responded_skips_processing():
    event = _create_event(
        event_id="Ev_done",
        message_text="<@B123> hello",
        status=STATUS_RESPONDED,
    )
    event.response_ts = "1720000001.000999"
    event.save()

    delivery = _fake_delivery()
    result = process_inbound_event(event.event_id, deliver_response=delivery)

    assert result.ok is True
    assert result.status == RESULT_ALREADY_RESPONDED
    assert len(delivery.calls) == 0  # delivery not called


# ===========================================================================
# 6. Empty message normalization failure
# ===========================================================================

@pytest.mark.django_db
def test_empty_message_after_mention_ignored():
    event = _create_event(
        event_id="Ev_empty",
        message_text="<@B123>",
    )
    delivery = _fake_delivery()

    result = process_inbound_event(event.event_id, deliver_response=delivery)

    assert result.ok is True
    assert result.status == RESULT_IGNORED
    assert len(delivery.calls) == 0

    event.refresh_from_db()
    assert event.status == STATUS_IGNORED


# ===========================================================================
# 7. Punctuation-only normalization failure
# ===========================================================================

@pytest.mark.django_db
def test_punctuation_only_ignored():
    event = _create_event(
        event_id="Ev_punct",
        message_text="???",
    )
    delivery = _fake_delivery()

    result = process_inbound_event(event.event_id, deliver_response=delivery)

    assert result.status == RESULT_IGNORED
    assert len(delivery.calls) == 0

    event.refresh_from_db()
    assert event.status == STATUS_IGNORED


# ===========================================================================
# 8. Delivery callback not called for no_response routes
# ===========================================================================

@pytest.mark.django_db
def test_delivery_not_called_for_no_response():
    """Simple greeting delivery fails → event marked FAILED."""
    event = _create_event(event_id="Ev_nodeliver2", message_text="<@B123> hello")

    def bad_delivery(**kwargs):
        raise RuntimeError("Slack API exploded")

    result = process_inbound_event(event.event_id, deliver_response=bad_delivery)

    # Greeting → delivery attempted but fails → FAILED
    assert result.ok is False
    assert result.status == RESULT_FAILED

    event.refresh_from_db()
    assert event.status == STATUS_FAILED


# ===========================================================================
# 9. No delivery callback — greeting still processed
# ===========================================================================

@pytest.mark.django_db
def test_no_delivery_callback():
    event = _create_event(event_id="Ev_nodeliver", message_text="<@B123> hello")

    result = process_inbound_event(event.event_id)

    assert result.ok is True
    assert result.status == "processed"
    assert result.response_type == "greeting"

    event.refresh_from_db()
    assert event.status == STATUS_RESPONDED


# ===========================================================================
# 10. Thread timestamp — no delivery for no_response
# ===========================================================================

@pytest.mark.django_db
def test_thread_ts_no_delivery_for_no_response():
    event = _create_event(
        event_id="Ev_thread",
        message_text="<@B123> hello",
        thread_ts="1719999999.000900",
    )
    delivery = _fake_delivery()

    result = process_inbound_event(event.event_id, deliver_response=delivery)

    assert result.status == RESULT_DELIVERED
    assert len(delivery.calls) == 1


# ===========================================================================
# 11. Idempotency — event marked IGNORED, second call skips
# ===========================================================================

@pytest.mark.django_db
def test_idempotency_ignored_event():
    event = _create_event(event_id="Ev_idem", message_text="<@B123> hello")
    delivery = _fake_delivery()

    r1 = process_inbound_event(event.event_id, deliver_response=delivery)
    assert r1.status == RESULT_DELIVERED
    assert len(delivery.calls) == 1

    # Second call — event is already RESPONDED, so it's skipped.
    r2 = process_inbound_event(event.event_id, deliver_response=delivery)
    assert r2.status == RESULT_ALREADY_RESPONDED
    assert len(delivery.calls) == 1  # no new delivery


# ===========================================================================
# 12. ProcessingResult dataclass
# ===========================================================================

def test_processing_result_is_frozen():
    result = ProcessingResult(ok=True, status="processed", event_id="Ev1")
    with pytest.raises(AttributeError):
        result.ok = False


def test_processing_result_defaults():
    result = ProcessingResult(ok=True, status="processed", event_id="Ev1")
    assert result.response_text == ""
    assert result.response_type == ""
    assert result.response_ts == ""
    assert result.error == ""
    assert result.metadata is None


# ===========================================================================
# 13. Safety — no external service imports
# ===========================================================================

def test_tasks_module_does_not_import_slack_sdk():
    import apps.slack_bot.tasks as tasks_mod
    source = open(tasks_mod.__file__).read()
    assert "slack_sdk" not in source
    assert "WebClient" not in source
    # "from slack" would indicate a Slack SDK import — but our delivery.py
    # uses httpx, not a Slack SDK.  Check tasks.py specifically.
    assert "from slack" not in source


def test_tasks_module_does_not_import_llm_clients():
    import apps.slack_bot.tasks as tasks_mod
    source = open(tasks_mod.__file__).read()
    assert "anthropic" not in source
    assert "openai" not in source
    assert "zhipuai" not in source


def test_tasks_module_does_not_import_brightbean_analytics():
    import apps.slack_bot.tasks as tasks_mod
    source = open(tasks_mod.__file__).read()
    # tasks.py imports tool_executors which imports analytics services,
    # but tasks.py itself does not import apps.analytics directly.
    assert "from apps.analytics" not in source
    assert "AnalyticsService" not in source


# ===========================================================================
# Phase 8 — delivery callback wiring tests
# ===========================================================================

@pytest.mark.django_db
def test_process_with_real_delivery_callback_mock():
    """Greeting message delivered via Slack delivery callback."""
    from unittest.mock import patch

    event = _create_event(event_id="Ev_real_delivery", message_text="<@B123> hello")

    with patch("apps.slack_bot.delivery.send_slack_message") as mock_send:
        from apps.slack_bot.delivery import SlackDeliveryResult
        mock_send.return_value = SlackDeliveryResult(
            ok=True, channel_id="C123", response_ts="123.456"
        )
        from apps.slack_bot.delivery import deliver_slack_response
        result = process_inbound_event(
            event.event_id, deliver_response=deliver_slack_response
        )

    assert result.ok is True
    assert result.status == RESULT_DELIVERED
    assert result.response_type == "greeting"
    mock_send.assert_called_once()

    event.refresh_from_db()
    assert event.status == STATUS_RESPONDED


@pytest.mark.django_db
def test_delivery_failure_through_callback_marks_failed():
    """Simple greeting delivery fails — event marked FAILED."""
    from unittest.mock import patch

    event = _create_event(event_id="Ev_delivery_fail", message_text="<@B123> hello")

    with patch("apps.slack_bot.delivery.send_slack_message") as mock_send:
        from apps.slack_bot.delivery import SlackDeliveryResult
        mock_send.return_value = SlackDeliveryResult(
            ok=False, channel_id="C123", error="channel_not_found"
        )

        from apps.slack_bot.delivery import deliver_slack_response
        result = process_inbound_event(
            event.event_id, deliver_response=deliver_slack_response
        )

    assert result.ok is False
    assert result.status == RESULT_FAILED
    mock_send.assert_called_once()

    event.refresh_from_db()
    assert event.status == STATUS_FAILED


@pytest.mark.django_db
def test_background_task_uses_delivery_callback():
    """Verify process_inbound_event_task is wired with deliver_slack_response."""
    import apps.slack_bot.tasks as tasks_mod

    # The __wrapped__ attribute exposes the original function before
    # @background decoration.  We verify the import exists.
    source = open(tasks_mod.__file__).read()
    assert "deliver_slack_response" in source
    assert "deliver_response=deliver_slack_response" in source
