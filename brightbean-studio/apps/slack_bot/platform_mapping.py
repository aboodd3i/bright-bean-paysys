"""Centralized canonical platform mapping for the Slack analytics bot.

Maps between the three canonical Slack-facing platform values
(``instagram``, ``facebook``, ``linkedin``) and the internal BrightBean
platform identifiers used in ``PlatformCredential.Platform`` and
``SocialAccount.platform``.

Canonical platform values are the only values the LLM and the user ever
see.  Internal variants (``instagram_login``, ``linkedin_company``,
``linkedin_personal``) are resolved here and never leak to the LLM.

Source of truth:
* :class:`apps.credentials.models.PlatformCredential.Platform`
* :class:`apps.social_accounts.models.SocialAccount.platform`
* :class:`apps.slack_bot.contracts.SupportedPlatform`
"""

from __future__ import annotations

from collections.abc import Iterable

# ---------------------------------------------------------------------------
# Canonical → internal variants
# ---------------------------------------------------------------------------

_CANONICAL_TO_INTERNAL: dict[str, frozenset[str]] = {
    "instagram": frozenset({"instagram", "instagram_login"}),
    "facebook": frozenset({"facebook"}),
    "linkedin": frozenset({"linkedin_company", "linkedin_personal"}),
}

# Reverse: every internal variant → its canonical value.
_INTERNAL_TO_CANONICAL: dict[str, str] = {
    internal: canonical
    for canonical, internals in _CANONICAL_TO_INTERNAL.items()
    for internal in internals
}

# Platforms the Slack bot supports (canonical).
SUPPORTED_CANONICAL_PLATFORMS: frozenset[str] = frozenset(_CANONICAL_TO_INTERNAL.keys())

# All internal platform variants the bot recognizes.
SUPPORTED_INTERNAL_PLATFORMS: frozenset[str] = frozenset(_INTERNAL_TO_CANONICAL.keys())


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def internal_platforms_for(canonical: str) -> frozenset[str]:
    """Return all internal BrightBean platform variants for *canonical*.

    Raises ``ValueError`` if *canonical* is not a supported canonical platform.
    """
    variants = _CANONICAL_TO_INTERNAL.get(canonical)
    if variants is None:
        raise ValueError(f"Unsupported canonical platform: {canonical!r}")
    return variants


def canonical_for(internal: str) -> str | None:
    """Return the canonical platform for an internal *internal* value.

    Returns ``None`` if the internal platform is not one the bot supports
    (e.g. ``"tiktok"``, ``"youtube"``).
    """
    return _INTERNAL_TO_CANONICAL.get(internal)


def is_supported_internal(internal: str) -> bool:
    """True if *internal* is a recognized internal platform variant."""
    return internal in _INTERNAL_TO_CANONICAL


def normalize_to_canonical(internal: str) -> str:
    """Normalize an internal platform to its canonical value.

    Raises ``ValueError`` if the platform is not supported by the bot.
    """
    canonical = _INTERNAL_TO_CANONICAL.get(internal)
    if canonical is None:
        raise ValueError(f"Platform {internal!r} is not supported by the Slack bot")
    return canonical


def filter_supported_internal(platforms: Iterable[str]) -> list[str]:
    """Filter *platforms* to only bot-supported internal variants."""
    return [p for p in platforms if p in _INTERNAL_TO_CANONICAL]
