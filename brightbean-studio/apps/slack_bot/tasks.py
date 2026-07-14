"""Background/core processing pipeline for Slack inbound events.

Takes a stored ``SlackInboundEvent``, normalizes it, resolves
authorization, runs the LLM-backed tool orchestrator, and delivers
the response via a pluggable callback.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from background_task import background

from .authorization import resolve_tool_context
from .constants import (
    RESPONSE_TYPE_ERROR,
    RESPONSE_TYPE_LLM,
    RESPONSE_TYPE_NO_RESPONSE,
    STATUS_FAILED,
    STATUS_IGNORED,
    STATUS_PROCESSING,
    STATUS_RESPONDED,
)
from .delivery import deliver_slack_response
from .exceptions import AuthorizationError, SlackNormalizationError
from .llm.base import LLMMessage, LLMRole
from .llm.router import create_default_router
from .llm_prompt import SYSTEM_PROMPT
from .models import SlackInboundEvent
from .normalization import normalize_inbound_event
from .tool_execution import OrchestrationLimits, ToolOrchestrator
from .tool_registry_prod import build_tool_registry

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Processing result statuses (distinct from DB model statuses)
# ---------------------------------------------------------------------------

RESULT_PROCESSED = "processed"
RESULT_DELIVERED = "delivered"
RESULT_ALREADY_RESPONDED = "already_responded"
RESULT_IGNORED = "ignored"
RESULT_FAILED = "failed"
RESULT_NOT_FOUND = "not_found"


# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ProcessingResult:
    """Outcome of processing a single inbound event."""

    ok: bool
    status: str
    event_id: str
    response_text: str = ""
    response_type: str = ""
    response_ts: str = ""
    error: str = ""
    metadata: dict[str, Any] | None = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _authorization_error_message(exc: AuthorizationError) -> str:
    """Map an :class:`AuthorizationError` to a user-friendly Slack message."""
    from .errors import ErrorCode

    code = exc.error_code
    if code == ErrorCode.CHANNEL_NOT_MAPPED:
        return (
            "This channel isn't connected to a BrightBean workspace. "
            "Ask an admin to map it first."
        )
    if code == ErrorCode.USER_NOT_MAPPED:
        return (
            "Your Slack account isn't linked to BrightBean. "
            "Ask an admin to add you as a workspace member."
        )
    if code == ErrorCode.UNAUTHORIZED:
        return "You don't have permission to view analytics in this workspace."
    if code == ErrorCode.WORKSPACE_UNAVAILABLE:
        return "The BrightBean workspace for this channel is archived."
    if code == ErrorCode.NO_CONNECTED_ACCOUNT:
        return "No social media accounts are connected in this workspace."
    return "I couldn't verify your access. Please try again later."


# ---------------------------------------------------------------------------
# Core processing function
# ---------------------------------------------------------------------------

def process_inbound_event(
    event_id: str,
    deliver_response: Callable | None = None,
) -> ProcessingResult:
    """Process a single ``SlackInboundEvent`` end-to-end.

    Flow:
    1. Load event by ``event_id`` → ``not_found`` if missing.
    2. Skip if already ``RESPONDED`` → ``already_responded``.
    3. Set status to ``PROCESSING``.
    4. Normalize → ``IGNORED`` on ``SlackNormalizationError``.
    5. Route → ``FAILED`` on unexpected error.
    6. If ``deliver_response`` callback provided:
       - Call it, store ``response_ts``, set status ``RESPONDED``.
       - On callback exception → ``FAILED``.
    7. If no callback: leave status as ``PROCESSING`` and return the
       generated response for later delivery.  The caller (or a
       separate delivery phase) is responsible for marking the event
       ``RESPONDED`` after actual delivery succeeds.

    Returns a ``ProcessingResult`` in all cases.
    """
    # --- 1. Load event ---
    try:
        event = SlackInboundEvent.objects.get(event_id=event_id)
    except SlackInboundEvent.DoesNotExist:
        return ProcessingResult(
            ok=False,
            status=RESULT_NOT_FOUND,
            event_id=event_id,
            error="Event not found",
        )

    # --- 2. Skip if already responded ---
    if event.status == STATUS_RESPONDED:
        logger.info(
            "Event already responded: event_id=%s correlation_id=%s",
            event_id, event.correlation_id,
        )
        return ProcessingResult(
            ok=True,
            status=RESULT_ALREADY_RESPONDED,
            event_id=event_id,
            response_ts=event.response_ts,
        )

    # --- 3. Mark processing ---
    logger.info(
        "Processing event: event_id=%s correlation_id=%s",
        event_id, event.correlation_id,
    )
    event.status = STATUS_PROCESSING
    event.save(update_fields=["status", "updated_at"])

    # --- 4. Normalize ---
    try:
        request = normalize_inbound_event(event)
    except SlackNormalizationError:
        logger.info("Event ignored by normalization: event_id=%s", event_id)
        event.status = STATUS_IGNORED
        event.save(update_fields=["status", "updated_at"])
        return ProcessingResult(
            ok=True,
            status=RESULT_IGNORED,
            event_id=event_id,
            error="Message rejected by normalization",
        )

    # --- 5. Resolve authorization ---
    try:
        context = resolve_tool_context(request)
    except AuthorizationError as exc:
        logger.warning(
            "Authorization failed for event %s: code=%s",
            event_id, exc.error_code.value,
        )
        error_text = _authorization_error_message(exc)
        event.status = STATUS_RESPONDED
        event.save(update_fields=["status", "updated_at"])
        if deliver_response is not None:
            try:
                deliver_response(
                    channel_id=request.channel_id,
                    text=error_text,
                    thread_ts=request.thread_ts,
                    event=event,
                )
            except Exception:
                pass
        return ProcessingResult(
            ok=True,
            status=RESULT_DELIVERED if deliver_response else RESULT_PROCESSED,
            event_id=event_id,
            response_text=error_text,
            response_type=RESPONSE_TYPE_ERROR,
        )

    # --- 6. Run LLM orchestration ---
    try:
        router = create_default_router()
        registry = build_tool_registry()
        orchestrator = ToolOrchestrator(
            router=router,
            registry=registry,
            limits=OrchestrationLimits(),
        )
        llm_messages = [
            LLMMessage(role=LLMRole.USER, content=request.text),
        ]
        result = orchestrator.run(
            messages=llm_messages,
            context=context,
            system_prompt=SYSTEM_PROMPT,
            correlation_id=request.correlation_id,
        )
    except Exception as exc:
        logger.exception("LLM orchestration failed for event %s", event_id)
        event.status = STATUS_FAILED
        event.save(update_fields=["status", "updated_at"])
        return ProcessingResult(
            ok=False,
            status=RESULT_FAILED,
            event_id=event_id,
            error=f"LLM error: {exc}",
        )

    # --- 7. Determine response text ---
    response_text = result.final_text
    response_type = RESPONSE_TYPE_LLM

    if not response_text:
        if result.error_message:
            response_text = (
                "I couldn't process your request right now. "
                "Please try again in a moment."
            )
            response_type = RESPONSE_TYPE_ERROR
        else:
            response_text = (
                "I wasn't able to generate a response. "
                "Please try rephrasing your question."
            )
            response_type = RESPONSE_TYPE_NO_RESPONSE

    # --- 8. Deliver (if callback provided) ---
    if deliver_response is not None:
        try:
            response_ts = deliver_response(
                channel_id=request.channel_id,
                text=response_text,
                thread_ts=request.thread_ts,
                event=event,
            )
        except Exception as exc:
            logger.exception("Delivery failed for event %s", event_id)
            event.status = STATUS_FAILED
            event.save(update_fields=["status", "updated_at"])
            return ProcessingResult(
                ok=False,
                status=RESULT_FAILED,
                event_id=event_id,
                response_text=response_text,
                response_type=response_type,
                error=f"Delivery error: {exc}",
            )

        # Delivery succeeded → mark RESPONDED
        ts_str = str(response_ts) if response_ts is not None else ""
        event.status = STATUS_RESPONDED
        event.response_ts = ts_str
        event.save(update_fields=["status", "response_ts", "updated_at"])

        logger.info(
            "Event processed and delivered: event_id=%s response_type=%s response_ts=%s",
            event_id, response_type, ts_str,
        )
        return ProcessingResult(
            ok=True,
            status=RESULT_DELIVERED,
            event_id=event_id,
            response_text=response_text,
            response_type=response_type,
            response_ts=ts_str,
        )

    # --- 9. No delivery callback ---
    logger.info(
        "Event processed (no delivery): event_id=%s response_type=%s",
        event_id, response_type,
    )
    return ProcessingResult(
        ok=True,
        status=RESULT_PROCESSED,
        event_id=event_id,
        response_text=response_text,
        response_type=response_type,
        metadata={"thread_ts": request.thread_ts, "channel_id": request.channel_id},
    )


# ---------------------------------------------------------------------------
# Background task wrapper (django-background-tasks)
# ---------------------------------------------------------------------------


@background(schedule=0)
def process_inbound_event_task(event_id: str):
    """Background-task wrapper for ``process_inbound_event``.

    Scheduled via ``enqueue_inbound_event``.  Runs asynchronously
    in the ``django-background-tasks`` worker process.

    Wired with ``deliver_slack_response`` so events are delivered
    to Slack via ``chat.postMessage`` as part of processing.
    """
    process_inbound_event(event_id, deliver_response=deliver_slack_response)


def enqueue_inbound_event(event_id: str) -> None:
    """Enqueue an inbound event for asynchronous processing.

    Uses ``django-background-tasks`` — the worker process will
    call ``process_inbound_event_task`` which in turn calls
    ``process_inbound_event`` with ``deliver_slack_response``
    as the delivery callback.
    """
    process_inbound_event_task(event_id)
