"""Simple deterministic command routing for Slack bot messages.

Receives a normalized ``SlackAnalyticsRequest`` and returns a
``SimpleBotResponse`` with a response type and text.

Greetings, help requests, and basic conversational messages are handled
here deterministically — before authorization or LLM orchestration.
Only analytics-relevant messages return ``no_response`` to continue
to the authorization/LLM pipeline.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .constants import (
    RESPONSE_TYPE_GREETING,
    RESPONSE_TYPE_HELP,
    RESPONSE_TYPE_NO_RESPONSE,
    RESPONSE_TYPE_STATUS,
)
from .contracts import SlackAnalyticsRequest

# ---------------------------------------------------------------------------
# Response dataclass
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SimpleBotResponse:
    """Structured response from the routing layer."""

    response_type: str
    text: str
    metadata: dict = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Command keyword sets (retained for future LLM routing context)
# ---------------------------------------------------------------------------

_GREETING_KEYWORDS = frozenset({
    "hi", "hello", "hey", "salam", "assalam o alaikum",
})

_HELP_KEYWORDS = frozenset({
    "help", "what can you do", "what do you do", "commands", "examples",
})

_STATUS_KEYWORDS = frozenset({
    "status", "how are you", "how are you doing",
})


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def normalize_command_text(text: str) -> str:
    """Lowercase and strip trailing punctuation for command matching."""
    lowered = text.lower().strip()
    # Strip trailing punctuation for exact-match commands
    return re.sub(r"[!?.,;:]+$", "", lowered).strip()


def is_greeting(text: str) -> bool:
    """Return ``True`` if *text* matches a greeting command."""
    return normalize_command_text(text) in _GREETING_KEYWORDS


def is_help_command(text: str) -> bool:
    """Return ``True`` if *text* matches a help command."""
    return normalize_command_text(text) in _HELP_KEYWORDS


def is_status_command(text: str) -> bool:
    """Return ``True`` if *text* matches a status command."""
    return normalize_command_text(text) in _STATUS_KEYWORDS


# ---------------------------------------------------------------------------
# Main routing function
# ---------------------------------------------------------------------------

def route_simple_command(request: SlackAnalyticsRequest) -> SimpleBotResponse:
    """Route a normalized Slack analytics request to a simple response.

    Greetings, help, and status commands return a deterministic response
    that bypasses authorization and LLM orchestration entirely.

    Everything else returns ``no_response`` to continue to the
    authorization/LLM analytics pipeline.
    """
    text = normalize_command_text(request.text)

    if text in _GREETING_KEYWORDS:
        return SimpleBotResponse(
            response_type=RESPONSE_TYPE_GREETING,
            text=(
                "Hi. Ask me about Instagram, Facebook, or LinkedIn analytics."
            ),
        )

    if text in _HELP_KEYWORDS:
        return SimpleBotResponse(
            response_type=RESPONSE_TYPE_HELP,
            text=(
                "I can help with social media analytics. Try asking:\n"
                "• \"Show Instagram reach for the last 30 days\"\n"
                "• \"Top Facebook posts this week\"\n"
                "• \"Compare LinkedIn and Instagram engagement\"\n"
                "• \"Follower growth for the last 7 days\""
            ),
        )

    if text in _STATUS_KEYWORDS:
        return SimpleBotResponse(
            response_type=RESPONSE_TYPE_STATUS,
            text=(
                "I'm ready to help with Instagram, Facebook, "
                "or LinkedIn analytics."
            ),
        )

    return SimpleBotResponse(
        response_type=RESPONSE_TYPE_NO_RESPONSE,
        text="",
    )
