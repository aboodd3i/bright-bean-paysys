"""Syntactic validation for Slack IDs used in management commands.

Phase 1 validation is syntactic only — a valid ID does not guarantee
the ID exists in Slack.
"""

from __future__ import annotations

import re

# Slack workspace/team IDs start with T followed by alphanumeric chars.
_WORKSPACE_RE = re.compile(r"^T[A-Z0-9]+$")

# Slack user/member IDs start with U (or W for guest/external) followed
# by alphanumeric chars.  Channel (C) and group (G) IDs are rejected.
_MEMBER_RE = re.compile(r"^[UW][A-Z0-9]+$")


def is_valid_workspace_id(value: str) -> bool:
    """Return ``True`` if *value* looks like a valid Slack workspace/team ID."""
    if not value:
        return False
    return bool(_WORKSPACE_RE.match(value.strip()))


def is_valid_member_id(value: str) -> bool:
    """Return ``True`` if *value* looks like a valid Slack user/member ID.

    Accepts IDs starting with ``U`` (standard users) or ``W``
    (guest/external users).  Rejects IDs starting with ``C``
    (channels) or ``G`` (groups/MPIMs).
    """
    if not value:
        return False
    return bool(_MEMBER_RE.match(value.strip()))


def deduplicate_ids(ids: list[str]) -> list[str]:
    """Trim whitespace and remove duplicates while preserving order."""
    seen: set[str] = set()
    result: list[str] = []
    for uid in ids:
        trimmed = uid.strip()
        if trimmed not in seen:
            seen.add(trimmed)
            result.append(trimmed)
    return result
