"""Safe configuration for the LLM provider layer.

Reads environment variables via Django settings / ``os.environ`` and
returns validated :class:`ProviderConfig` dataclasses.

No secrets are logged.  Missing keys are reported as empty strings —
the adapter raises :class:`~apps.slack_bot.llm.exceptions.LLMAuthError`
at call time rather than at import time, so the module is always
importable (important for tests and Django startup).
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from django.conf import settings

# ---------------------------------------------------------------------------
# Provider configuration
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ProviderConfig:
    """Configuration for a single LLM provider.

    Fields
    ------
    api_key : str
        API key for the provider.  Empty string when not configured.
    model : str
        Model identifier to use.  Empty string when not configured.
    base_url : str
        API base URL.  Always set — defaults are baked in.
    timeout_seconds : float
        Request timeout in seconds.
    max_output_tokens : int
        Maximum tokens to generate per request.
    """

    api_key: str
    model: str
    base_url: str
    timeout_seconds: float
    max_output_tokens: int


# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------

ANTHROPIC_BASE_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_API_VERSION = "2023-06-01"

ZAI_BASE_URL = "https://api.z.ai/api/coding/paas/v4/chat/completions"

DEFAULT_TIMEOUT_SECONDS = 15.0
DEFAULT_MAX_OUTPUT_TOKENS = 2000

# Default models for dual-Z.AI configuration.
# Primary uses the most capable model; fallback uses a faster, cheaper one.
ZAI_DEFAULT_PRIMARY_MODEL = "glm-4.7"
ZAI_DEFAULT_FALLBACK_MODEL = "glm-4"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _get_setting(name: str, default: str = "") -> str:
    """Read a setting from Django settings or ``os.environ``."""
    value = getattr(settings, name, os.environ.get(name, default))
    if value is None:
        return default
    return str(value)


def _get_float_setting(name: str, default: float) -> float:
    """Read a float setting, falling back to *default* on parse failure."""
    raw = _get_setting(name, str(default))
    try:
        return float(raw)
    except (ValueError, TypeError):
        return default


def _get_int_setting(name: str, default: int) -> int:
    """Read an int setting, falling back to *default* on parse failure."""
    raw = _get_setting(name, str(default))
    try:
        return int(raw)
    except (ValueError, TypeError):
        return default


# ---------------------------------------------------------------------------
# Public configuration accessors
# ---------------------------------------------------------------------------


def get_claude_config() -> ProviderConfig:
    """Return :class:`ProviderConfig` for the Claude (Anthropic) provider."""
    return ProviderConfig(
        api_key=_get_setting("ANTHROPIC_API_KEY"),
        model=_get_setting("ANTHROPIC_MODEL", "claude-sonnet-4-5-20250929"),
        base_url=ANTHROPIC_BASE_URL,
        timeout_seconds=_get_float_setting(
            "LLM_TIMEOUT_SECONDS", DEFAULT_TIMEOUT_SECONDS
        ),
        max_output_tokens=_get_int_setting(
            "LLM_MAX_OUTPUT_TOKENS", DEFAULT_MAX_OUTPUT_TOKENS
        ),
    )


def get_glm_config() -> ProviderConfig:
    """Return :class:`ProviderConfig` for the GLM (Z.AI) primary provider.

    Uses ``ZAI_MODEL`` (env) or :data:`ZAI_DEFAULT_PRIMARY_MODEL` as the
    model.  In the temporary Z.AI-only configuration both primary and
    fallback point at Z.AI with different models.
    """
    model = _get_setting("ZAI_MODEL", ZAI_DEFAULT_PRIMARY_MODEL)
    if not model:
        model = ZAI_DEFAULT_PRIMARY_MODEL
    return ProviderConfig(
        api_key=_get_setting("ZAI_API_KEY"),
        model=model,
        base_url=ZAI_BASE_URL,
        timeout_seconds=_get_float_setting(
            "LLM_TIMEOUT_SECONDS", DEFAULT_TIMEOUT_SECONDS
        ),
        max_output_tokens=_get_int_setting(
            "LLM_MAX_OUTPUT_TOKENS", DEFAULT_MAX_OUTPUT_TOKENS
        ),
    )


def get_glm_fallback_config() -> ProviderConfig:
    """Return :class:`ProviderConfig` for the GLM (Z.AI) fallback provider.

    Uses ``ZAI_FALLBACK_MODEL`` (env) or :data:`ZAI_DEFAULT_FALLBACK_MODEL`
    as the model.  Shares the same API key and base URL as the primary.
    """
    model = _get_setting("ZAI_FALLBACK_MODEL", ZAI_DEFAULT_FALLBACK_MODEL)
    if not model:
        model = ZAI_DEFAULT_FALLBACK_MODEL
    return ProviderConfig(
        api_key=_get_setting("ZAI_API_KEY"),
        model=model,
        base_url=ZAI_BASE_URL,
        timeout_seconds=_get_float_setting(
            "LLM_TIMEOUT_SECONDS", DEFAULT_TIMEOUT_SECONDS
        ),
        max_output_tokens=_get_int_setting(
            "LLM_MAX_OUTPUT_TOKENS", DEFAULT_MAX_OUTPUT_TOKENS
        ),
    )


def get_primary_provider_name() -> str:
    """Return the configured primary provider name (default ``"glm"``)."""
    return _get_setting("LLM_PRIMARY", "glm").lower()


def get_fallback_provider_name() -> str:
    """Return the configured fallback provider name (default ``"glm"``)."""
    return _get_setting("LLM_FALLBACK", "glm").lower()
