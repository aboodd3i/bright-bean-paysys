"""Tests for the LLM fallback router."""

from __future__ import annotations

from unittest.mock import MagicMock

from apps.slack_bot.llm.base import (
    LLMMessage,
    LLMRequest,
    LLMResponse,
    LLMRole,
    LLMStopReason,
    LLMTokenUsage,
)
from apps.slack_bot.llm.exceptions import (
    LLMAuthError,
    LLMRateLimitError,
    LLMServerError,
    LLMTimeoutError,
)
from apps.slack_bot.llm.router import LLMRouter, LLMRouterResult

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_request():
    return LLMRequest(
        system_prompt="You are an analytics assistant.",
        messages=[LLMMessage(role=LLMRole.USER, content="Stats?")],
    )


def _make_response(provider_name="claude", text="Done."):
    return LLMResponse(
        text=text,
        tool_calls=[],
        stop_reason=LLMStopReason.END_TURN,
        usage=LLMTokenUsage(input_tokens=10, output_tokens=5),
        provider_name=provider_name,
        raw={},
    )


def _make_provider(name="claude", response=None, raises=None):
    """Create a mock provider that either returns *response* or raises *raises*."""
    provider = MagicMock()
    provider.name = name
    if raises is not None:
        provider.complete.side_effect = raises
    else:
        provider.complete.return_value = response or _make_response(provider_name=name)
    return provider


# ===========================================================================
# 1. Primary succeeds — no fallback
# ===========================================================================

def test_primary_succeeds_no_fallback():
    primary = _make_provider(name="claude", response=_make_response("claude"))
    fallback = _make_provider(name="glm")
    router = LLMRouter(primary=primary, fallback=fallback)

    result = router.route(_make_request())

    assert result.response is not None
    assert result.provider_name == "claude"
    assert result.used_fallback is False
    assert result.primary_error is None
    assert result.fallback_error is None
    fallback.complete.assert_not_called()


# ===========================================================================
# 2. Primary fails retryable → fallback succeeds
# ===========================================================================

def test_primary_retryable_fallback_succeeds():
    primary = _make_provider(
        name="claude",
        raises=LLMTimeoutError("timed out", provider_name="claude"),
    )
    fallback = _make_provider(name="glm", response=_make_response("glm"))
    router = LLMRouter(primary=primary, fallback=fallback)

    result = router.route(_make_request())

    assert result.response is not None
    assert result.provider_name == "glm"
    assert result.used_fallback is True
    assert result.primary_error is not None
    assert result.fallback_error is None


def test_primary_rate_limit_fallback_succeeds():
    primary = _make_provider(
        name="claude",
        raises=LLMRateLimitError("429 rate limited", provider_name="claude"),
    )
    fallback = _make_provider(name="glm", response=_make_response("glm"))
    router = LLMRouter(primary=primary, fallback=fallback)

    result = router.route(_make_request())

    assert result.used_fallback is True
    assert result.provider_name == "glm"


def test_primary_server_error_fallback_succeeds():
    primary = _make_provider(
        name="claude",
        raises=LLMServerError("500 server error", provider_name="claude", status_code=500),
    )
    fallback = _make_provider(name="glm", response=_make_response("glm"))
    router = LLMRouter(primary=primary, fallback=fallback)

    result = router.route(_make_request())

    assert result.used_fallback is True
    assert result.provider_name == "glm"


# ===========================================================================
# 3. Primary fails permanent → no fallback
# ===========================================================================

def test_primary_permanent_no_fallback():
    primary = _make_provider(
        name="claude",
        raises=LLMAuthError("invalid key", provider_name="claude"),
    )
    fallback = _make_provider(name="glm")
    router = LLMRouter(primary=primary, fallback=fallback)

    result = router.route(_make_request())

    assert result.response is None
    assert result.used_fallback is False
    assert result.primary_error is not None
    fallback.complete.assert_not_called()


# ===========================================================================
# 4. Both fail
# ===========================================================================

def test_both_fail_returns_no_response():
    primary = _make_provider(
        name="claude",
        raises=LLMTimeoutError("timed out", provider_name="claude"),
    )
    fallback = _make_provider(
        name="glm",
        raises=LLMServerError("503 unavailable", provider_name="glm", status_code=503),
    )
    router = LLMRouter(primary=primary, fallback=fallback)

    result = router.route(_make_request())

    assert result.response is None
    assert result.provider_name == ""
    assert result.used_fallback is True
    assert result.primary_error is not None
    assert result.fallback_error is not None
    assert len(result.errors) == 2


# ===========================================================================
# 5. No fallback configured
# ===========================================================================

def test_no_fallback_configured():
    primary = _make_provider(
        name="claude",
        raises=LLMTimeoutError("timed out", provider_name="claude"),
    )
    router = LLMRouter(primary=primary, fallback=None)

    result = router.route(_make_request())

    assert result.response is None
    assert result.used_fallback is False
    assert result.primary_error is not None


# ===========================================================================
# 6. http_post is forwarded
# ===========================================================================

def test_http_post_forwarded_to_primary():
    primary = _make_provider(name="claude", response=_make_response("claude"))
    router = LLMRouter(primary=primary, fallback=None)

    custom_post = MagicMock()
    router.route(_make_request(), http_post=custom_post)

    primary.complete.assert_called_once()
    _, kwargs = primary.complete.call_args
    assert kwargs["http_post"] is custom_post


def test_http_post_forwarded_to_fallback():
    primary = _make_provider(
        name="claude",
        raises=LLMTimeoutError("timed out", provider_name="claude"),
    )
    fallback = _make_provider(name="glm", response=_make_response("glm"))
    router = LLMRouter(primary=primary, fallback=fallback)

    custom_post = MagicMock()
    router.route(_make_request(), http_post=custom_post)

    fallback.complete.assert_called_once()
    _, kwargs = fallback.complete.call_args
    assert kwargs["http_post"] is custom_post


# ===========================================================================
# 7. Unexpected (non-LLM) exception treated as retryable
# ===========================================================================

def test_unexpected_exception_triggers_fallback():
    primary = _make_provider(
        name="claude",
        raises=RuntimeError("unexpected"),
    )
    fallback = _make_provider(name="glm", response=_make_response("glm"))
    router = LLMRouter(primary=primary, fallback=fallback)

    result = router.route(_make_request())

    assert result.used_fallback is True
    assert result.provider_name == "glm"


# ===========================================================================
# 8. LLMRouterResult defaults
# ===========================================================================

def test_router_result_defaults():
    r = LLMRouterResult()
    assert r.response is None
    assert r.provider_name == ""
    assert r.used_fallback is False
    assert r.primary_error is None
    assert r.fallback_error is None
    assert r.errors == []
