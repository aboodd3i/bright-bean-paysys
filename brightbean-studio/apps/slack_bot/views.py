"""Slack Events API endpoint.

Phase 9: signature verification, URL verification, event parsing,
deduplication, persistence, and background-task enqueue for new events.
Phase 2: bot whitelisting access gate and processing reaction (👀).
Phase 3: administrator DM access-grant commands.
"""

from __future__ import annotations

import logging

from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt

from .access_service import is_user_approved
from .admin_dm_service import is_direct_message_channel, process_admin_dm
from .constants import (
    ERROR_INVALID_JSON,
    ERROR_INVALID_SIGNATURE,
    RESPONSE_DUPLICATE,
    RESPONSE_IGNORED,
    RESPONSE_RECEIVED,
    STATUS_IGNORED,
)
from .delivery import send_slack_message
from .events import (
    extract_event_payload,
    extract_persistence_fields,
    get_url_verification_challenge,
    is_url_verification,
    parse_slack_payload,
    should_accept_event,
)
from .models import SlackInboundEvent
from .reactions import add_processing_reaction, remove_processing_reaction
from .signing import verify_slack_request
from .tasks import enqueue_inbound_event
from .unauthorized_notification_service import (
    classify_user,
    handle_unauthorized_access,
)

logger = logging.getLogger(__name__)


@csrf_exempt
def slack_events(request):
    """Handle incoming Slack Events API requests.

    Flow:
    1. Read raw body.
    2. Verify Slack signature + timestamp → 401 if invalid.
    3. Parse JSON → 400 if invalid.
    4. URL verification → return challenge.
    5. Unsupported/ignored event → 200 with status=ignored.
    6. Accepted event → persist via dedup helper → enqueue if new → 200.
    """
    raw_body = request.body

    # --- 1. Signature verification ---
    timestamp = request.headers.get("X-Slack-Request-Timestamp")
    signature = request.headers.get("X-Slack-Signature")

    if not verify_slack_request(raw_body, timestamp, signature):
        logger.warning("Slack request rejected: invalid signature")
        return JsonResponse(
            {"ok": False, "error": ERROR_INVALID_SIGNATURE},
            status=401,
        )

    # --- 2. Parse JSON ---
    try:
        payload = parse_slack_payload(raw_body)
    except Exception:
        logger.warning("Slack request rejected: invalid JSON body")
        return JsonResponse(
            {"ok": False, "error": ERROR_INVALID_JSON},
            status=400,
        )

    # --- 3. URL verification ---
    if is_url_verification(payload):
        try:
            challenge = get_url_verification_challenge(payload)
        except Exception:
            return JsonResponse(
                {"ok": False, "error": ERROR_INVALID_JSON},
                status=400,
            )
        return JsonResponse({"challenge": challenge})

    # --- 3b. Phase 3: Administrator DM interception ---
    # Check for direct-message events BEFORE should_accept_event()
    # so admin commands don't enter normal analytics routing.
    inner_event = extract_event_payload(payload)
    if inner_event is not None:
        event_type = inner_event.get("type", "")
        channel_id = inner_event.get("channel", "")
        # Skip bot-generated messages and subtypes
        if (
            event_type == "message"
            and not inner_event.get("bot_id")
            and not inner_event.get("subtype")
            and is_direct_message_channel(channel_id)
        ):
            team_id = payload.get("team_id", "")
            event_id = payload.get("event_id", "")
            user_id = inner_event.get("user", "")
            dm_text = inner_event.get("text", "")
            dm_ts = inner_event.get("ts", "")

            # Use existing event idempotency — get_or_create on event_id
            if not event_id or not team_id or not user_id:
                # Missing required fields — ignore
                return JsonResponse(
                    {"ok": True, "status": RESPONSE_IGNORED,
                     "reason": "missing_required_fields"},
                    status=200,
                )

            event_obj, dm_created = SlackInboundEvent.objects.get_or_create_inbound_event(
                event_id=str(event_id),
                team_id=str(team_id),
                channel_id=str(channel_id),
                user_id=str(user_id),
                event_ts=str(dm_ts or ""),
                message_text=dm_text,
                thread_ts="",
            )

            if not dm_created:
                # Duplicate DM — don't process twice
                logger.info("Admin DM duplicate: event_id=%s", event_id)
                return JsonResponse(
                    {"ok": True, "status": RESPONSE_DUPLICATE},
                    status=200,
                )

            # Process the admin DM
            result = process_admin_dm(
                workspace_id=str(team_id),
                sender_slack_user_id=str(user_id),
                dm_text=dm_text,
            )

            if result.is_admin_dm and result.handled:
                # Send the response back in the DM
                try:
                    send_slack_message(
                        channel_id=str(channel_id),
                        text=result.response_text,
                    )
                except Exception:
                    logger.exception(
                        "Admin DM response failed: event_id=%s", event_id,
                    )
                return JsonResponse(
                    {"ok": True, "status": RESPONSE_RECEIVED},
                    status=200,
                )

            if result.is_admin_dm and not result.handled:
                # Admin sent a DM but no grant intent recognised — ignore
                logger.info(
                    "Admin DM not a grant command: event_id=%s", event_id,
                )
                return JsonResponse(
                    {"ok": True, "status": RESPONSE_IGNORED,
                     "reason": "not_grant_command"},
                    status=200,
                )

            # Non-admin DM — mark ignored, preserve existing rejection behaviour
            logger.info(
                "Non-admin DM ignored: event_id=%s team_id=%s user_id=%s",
                event_id, team_id, user_id,
            )
            event_obj.status = STATUS_IGNORED
            event_obj.save(update_fields=["status", "updated_at"])
            return JsonResponse(
                {"ok": True, "status": RESPONSE_IGNORED,
                 "reason": "non_admin_dm"},
                status=200,
            )

    # --- 4. Event filtering ---
    accepted, reason = should_accept_event(payload)
    if not accepted:
        logger.info("Slack event ignored: reason=%s", reason)
        return JsonResponse(
            {"ok": True, "status": RESPONSE_IGNORED, "reason": reason},
            status=200,
        )

    # --- 5. Persist accepted event ---
    fields = extract_persistence_fields(payload)
    if fields is None:
        logger.info("Slack event ignored: missing required fields")
        return JsonResponse(
            {"ok": True, "status": RESPONSE_IGNORED, "reason": "missing_required_fields"},
            status=200,
        )

    event, created = SlackInboundEvent.objects.get_or_create_inbound_event(
        event_id=fields["event_id"],
        team_id=fields["team_id"],
        channel_id=fields["channel_id"],
        user_id=fields["user_id"],
        event_ts=fields["event_ts"],
        message_text=fields["message_text"],
        thread_ts=fields["thread_ts"],
    )

    if created:
        logger.info(
            "Slack event received: event_id=%s team_id=%s channel_id=%s",
            event.event_id, event.team_id, event.channel_id,
        )

        # --- Phase 2: Access gate ---
        # Check BotUserAccess AFTER persistence but BEFORE enqueue.
        # Unapproved users get a successful 200 so Slack does not retry,
        # but no task is queued and no LLM/BrightBean/tools run.
        if not is_user_approved(event.team_id, event.user_id):
            logger.info(
                "access_gate_denied event_id=%s team_id=%s user_id=%s",
                event.event_id, event.team_id, event.user_id,
            )

            # --- Phase 4: Unregistered-user notification flow ---
            # Distinguish unregistered (no BotUserAccess) from revoked.
            # Revoked users stay blocked via the existing Phase 2 path
            # without entering the notification flow.
            is_unregistered, is_revoked = classify_user(
                event.team_id, event.user_id,
            )

            if is_unregistered:
                handle_unauthorized_access(
                    workspace_id=event.team_id,
                    slack_user_id=event.user_id,
                    source_channel_id=event.channel_id,
                    message_ts=event.event_ts,
                    thread_ts=event.thread_ts,
                )

            event.status = STATUS_IGNORED
            event.save(update_fields=["status", "updated_at"])
            return JsonResponse(
                {"ok": True, "status": RESPONSE_IGNORED,
                 "reason": "access_denied"},
                status=200,
            )

        logger.info(
            "access_gate_allowed event_id=%s team_id=%s user_id=%s",
            event.event_id, event.team_id, event.user_id,
        )

        # --- Phase 2: Processing reaction (👀) ---
        # Add the eyes reaction to the exact user message before enqueue.
        # Reaction failure is non-blocking — processing continues regardless.
        reaction_added = False
        reaction_result = add_processing_reaction(
            channel_id=event.channel_id,
            message_ts=event.event_ts,
        )
        if reaction_result.ok:
            reaction_added = True
        # If reaction add failed, we log it inside the reaction service
        # and continue enqueueing anyway.

        # --- Enqueue for background processing ---
        try:
            enqueue_inbound_event(event.event_id)
        except Exception:
            logger.exception(
                "Slack event enqueue failed: event_id=%s", event.event_id,
            )
            # Best-effort reaction cleanup if we added the reaction
            if reaction_added:
                remove_processing_reaction(
                    channel_id=event.channel_id,
                    message_ts=event.event_ts,
                )
                logger.info(
                    "processing_reaction_cleanup_after_enqueue_failure "
                    "event_id=%s", event.event_id,
                )
            return JsonResponse(
                {"ok": True, "status": RESPONSE_IGNORED,
                 "reason": "enqueue_failed"},
                status=200,
            )

        logger.info("Slack event enqueued: event_id=%s", event.event_id)
        return JsonResponse(
            {"ok": True, "status": RESPONSE_RECEIVED},
            status=200,
        )

    logger.info("Slack event duplicate: event_id=%s", event.event_id)
    return JsonResponse(
        {"ok": True, "status": RESPONSE_DUPLICATE},
        status=200,
    )
