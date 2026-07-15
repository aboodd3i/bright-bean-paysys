"""Slack reaction helper for the processing indicator (👀).

Provides ``add_processing_reaction`` and ``remove_processing_reaction``
using the Slack Web API ``reactions.add`` / ``reactions.remove``
endpoints via ``httpx`` (consistent with :mod:`apps.slack_bot.delivery`).

The reaction name is ``eyes`` (the Slack API reaction name, not the
literal emoji character).

Both functions are **non-blocking** — failures are logged and a
``ReactionResult`` with ``ok=False`` is returned.  The caller should
continue processing regardless of reaction success.
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from django.conf import settings

if TYPE_CHECKING:
    import httpx

logger = logging.getLogger(__name__)

SLACK_REACTIONS_ADD_URL = "https://slack.com/api/reactions.add"
SLACK_REACTIONS_REMOVE_URL = "https://slack.com/api/reactions.remove"

# Slack API reaction name for 👀
REACTION_NAME = "eyes"


# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ReactionResult:
    """Outcome of a Slack reaction API call."""

    ok: bool
    channel_id: str
    message_ts: str
    error: str = ""


# ---------------------------------------------------------------------------
# Token helper (reuse from delivery for consistency)
# ---------------------------------------------------------------------------


def _get_slack_bot_token() -> str:
    """Return the Slack bot token from settings or environment."""
    return getattr(
        settings, "SLACK_BOT_TOKEN", os.environ.get("SLACK_BOT_TOKEN", "")
    )


# ---------------------------------------------------------------------------
# Default HTTP post function (httpx)
# ---------------------------------------------------------------------------


def _default_http_post(url: str, *, json_body: dict, headers: dict) -> "httpx.Response":
    """Default HTTP POST using httpx."""
    import httpx

    return httpx.post(url, json=json_body, headers=headers, timeout=10)


# ---------------------------------------------------------------------------
# Core reaction functions
# ---------------------------------------------------------------------------


def add_processing_reaction(
    channel_id: str,
    message_ts: str,
    *,
    token: str | None = None,
    http_post: Callable | None = None,
) -> ReactionResult:
    """Add the 👀 reaction to a Slack message.

    Args:
        channel_id: Slack channel ID containing the message.
        message_ts: Timestamp of the exact user message to react to.
        token: Slack bot token (defaults to settings/env).
        http_post: Injectable HTTP POST callable for testing.

    Returns:
        ``ReactionResult`` — ``ok=True`` on success, ``ok=False`` on failure.
        Failures are logged but never raised.
    """
    if not channel_id or not message_ts:
        return ReactionResult(
            ok=False, channel_id=channel_id, message_ts=message_ts,
            error="Missing channel_id or message_ts",
        )

    bot_token = token if token is not None else _get_slack_bot_token()
    if not bot_token:
        return ReactionResult(
            ok=False, channel_id=channel_id, message_ts=message_ts,
            error="Missing SLACK_BOT_TOKEN",
        )

    body = {
        "channel": channel_id,
        "timestamp": message_ts,
        "name": REACTION_NAME,
    }
    headers = {
        "Authorization": f"Bearer {bot_token}",
        "Content-Type": "application/json",
    }

    post_fn = http_post if http_post is not None else _default_http_post

    try:
        response = post_fn(
            SLACK_REACTIONS_ADD_URL,
            json_body=body,
            headers=headers,
        )
    except Exception as exc:
        logger.error(
            "processing_reaction_add_failed channel_id=%s message_ts=%s error=%s",
            channel_id, message_ts, exc,
        )
        return ReactionResult(
            ok=False, channel_id=channel_id, message_ts=message_ts,
            error=f"HTTP error: {exc}",
        )

    try:
        raw = json.loads(response.text)
    except (ValueError, AttributeError) as exc:
        logger.error(
            "processing_reaction_add_failed channel_id=%s message_ts=%s error=invalid_json",
            channel_id, message_ts,
        )
        return ReactionResult(
            ok=False, channel_id=channel_id, message_ts=message_ts,
            error=f"Invalid JSON response: {exc}",
        )

    if raw.get("ok"):
        logger.info(
            "processing_reaction_added channel_id=%s message_ts=%s",
            channel_id, message_ts,
        )
        return ReactionResult(
            ok=True, channel_id=channel_id, message_ts=message_ts,
        )

    error = str(raw.get("error", "unknown_slack_error"))
    logger.warning(
        "processing_reaction_add_failed channel_id=%s message_ts=%s error=%s",
        channel_id, message_ts, error,
    )
    return ReactionResult(
        ok=False, channel_id=channel_id, message_ts=message_ts, error=error,
    )


def remove_processing_reaction(
    channel_id: str,
    message_ts: str,
    *,
    token: str | None = None,
    http_post: Callable | None = None,
) -> ReactionResult:
    """Remove the 👀 reaction from a Slack message.

    Args:
        channel_id: Slack channel ID containing the message.
        message_ts: Timestamp of the original user message.
        token: Slack bot token (defaults to settings/env).
        http_post: Injectable HTTP POST callable for testing.

    Returns:
        ``ReactionResult`` — ``ok=True`` on success, ``ok=False`` on failure.
        Failures are logged but never raised.
    """
    if not channel_id or not message_ts:
        return ReactionResult(
            ok=False, channel_id=channel_id, message_ts=message_ts,
            error="Missing channel_id or message_ts",
        )

    bot_token = token if token is not None else _get_slack_bot_token()
    if not bot_token:
        return ReactionResult(
            ok=False, channel_id=channel_id, message_ts=message_ts,
            error="Missing SLACK_BOT_TOKEN",
        )

    body = {
        "channel": channel_id,
        "timestamp": message_ts,
        "name": REACTION_NAME,
    }
    headers = {
        "Authorization": f"Bearer {bot_token}",
        "Content-Type": "application/json",
    }

    post_fn = http_post if http_post is not None else _default_http_post

    try:
        response = post_fn(
            SLACK_REACTIONS_REMOVE_URL,
            json_body=body,
            headers=headers,
        )
    except Exception as exc:
        logger.error(
            "processing_reaction_remove_failed channel_id=%s message_ts=%s error=%s",
            channel_id, message_ts, exc,
        )
        return ReactionResult(
            ok=False, channel_id=channel_id, message_ts=message_ts,
            error=f"HTTP error: {exc}",
        )

    try:
        raw = json.loads(response.text)
    except (ValueError, AttributeError) as exc:
        logger.error(
            "processing_reaction_remove_failed channel_id=%s message_ts=%s error=invalid_json",
            channel_id, message_ts,
        )
        return ReactionResult(
            ok=False, channel_id=channel_id, message_ts=message_ts,
            error=f"Invalid JSON response: {exc}",
        )

    if raw.get("ok"):
        logger.info(
            "processing_reaction_removed channel_id=%s message_ts=%s",
            channel_id, message_ts,
        )
        return ReactionResult(
            ok=True, channel_id=channel_id, message_ts=message_ts,
        )

    error = str(raw.get("error", "unknown_slack_error"))
    logger.warning(
        "processing_reaction_remove_failed channel_id=%s message_ts=%s error=%s",
        channel_id, message_ts, error,
    )
    return ReactionResult(
        ok=False, channel_id=channel_id, message_ts=message_ts, error=error,
    )
