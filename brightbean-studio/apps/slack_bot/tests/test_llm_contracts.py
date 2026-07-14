"""Tests for LLM base contracts and exception hierarchy."""

from __future__ import annotations

import pytest

from apps.slack_bot.llm.base import (
    LLMMessage,
    LLMRequest,
    LLMResponse,
    LLMRole,
    LLMStopReason,
    LLMTokenUsage,
    LLMToolCall,
    LLMToolDefinition,
)
from apps.slack_bot.llm.exceptions import (
    LLMAuthError,
    LLMBadRequestError,
    LLMProviderError,
    LLMRateLimitError,
    LLMResponseParseError,
    LLMServerError,
    LLMTimeoutError,
    LLMTransportError,
)

# ===========================================================================
# LLMMessage
# ===========================================================================

def test_llm_message_creation():
    msg = LLMMessage(role=LLMRole.USER, content="Hello")
    assert msg.role == LLMRole.USER
    assert msg.content == "Hello"


def test_llm_message_empty_content_rejected():
    with pytest.raises(ValueError, match="must have content, tool_calls, or tool_result"):
        LLMMessage(role=LLMRole.USER, content="")


def test_llm_message_tool_calls_only():
    """Assistant message with tool_calls but no text content."""
    from apps.slack_bot.llm.base import LLMToolCall

    msg = LLMMessage(
        role=LLMRole.ASSISTANT,
        tool_calls=[LLMToolCall(id="call_1", name="get_stats")],
    )
    assert msg.content == ""
    assert msg.tool_calls is not None
    assert len(msg.tool_calls) == 1


def test_llm_message_tool_result_only():
    """User message with tool_result but no text content."""
    from apps.slack_bot.llm.base import LLMToolResultContent

    msg = LLMMessage(
        role=LLMRole.USER,
        tool_result=LLMToolResultContent(tool_call_id="call_1", content='{"ok": true}'),
    )
    assert msg.content == ""
    assert msg.tool_result is not None


# ===========================================================================
# LLMToolDefinition
# ===========================================================================

def test_tool_definition_creation():
    tool = LLMToolDefinition(
        name="get_stats",
        description="Get account stats",
        input_schema={"type": "object", "properties": {}},
    )
    assert tool.name == "get_stats"


def test_tool_definition_empty_name_rejected():
    with pytest.raises(ValueError, match="name must not be empty"):
        LLMToolDefinition(name="", description="d", input_schema={})


# ===========================================================================
# LLMToolCall
# ===========================================================================

def test_tool_call_creation():
    tc = LLMToolCall(id="call_1", name="get_stats", arguments={"platform": "instagram"})
    assert tc.id == "call_1"
    assert tc.arguments == {"platform": "instagram"}


def test_tool_call_empty_id_rejected():
    with pytest.raises(ValueError, match="id must not be empty"):
        LLMToolCall(id="", name="get_stats")


def test_tool_call_empty_name_rejected():
    with pytest.raises(ValueError, match="name must not be empty"):
        LLMToolCall(id="call_1", name="")


def test_tool_call_default_arguments():
    tc = LLMToolCall(id="call_1", name="get_stats")
    assert tc.arguments == {}


# ===========================================================================
# LLMTokenUsage
# ===========================================================================

def test_token_usage_defaults():
    usage = LLMTokenUsage()
    assert usage.input_tokens == 0
    assert usage.output_tokens == 0


def test_token_usage_negative_rejected():
    with pytest.raises(ValueError, match="input_tokens"):
        LLMTokenUsage(input_tokens=-1)
    with pytest.raises(ValueError, match="output_tokens"):
        LLMTokenUsage(output_tokens=-1)


# ===========================================================================
# LLMResponse
# ===========================================================================

def test_llm_response_defaults():
    resp = LLMResponse(text="Hello")
    assert resp.text == "Hello"
    assert resp.tool_calls == []
    assert resp.stop_reason == LLMStopReason.END_TURN
    assert resp.usage == LLMTokenUsage()
    assert resp.provider_name == ""
    assert resp.raw is None


# ===========================================================================
# LLMRequest
# ===========================================================================

def test_llm_request_creation():
    req = LLMRequest(
        system_prompt="You are a helpful assistant.",
        messages=[LLMMessage(role=LLMRole.USER, content="What are our Instagram stats?")],
    )
    assert len(req.messages) == 1
    assert req.max_output_tokens == 2000
    assert req.temperature == 1.0


def test_llm_request_empty_messages_rejected():
    with pytest.raises(ValueError, match="messages must not be empty"):
        LLMRequest(system_prompt="sys", messages=[])


def test_llm_request_invalid_max_tokens_rejected():
    with pytest.raises(ValueError, match="max_output_tokens"):
        LLMRequest(
            system_prompt="sys",
            messages=[LLMMessage(role=LLMRole.USER, content="hi")],
            max_output_tokens=0,
        )


def test_llm_request_invalid_temperature_rejected():
    with pytest.raises(ValueError, match="temperature"):
        LLMRequest(
            system_prompt="sys",
            messages=[LLMMessage(role=LLMRole.USER, content="hi")],
            temperature=1.5,
        )
    with pytest.raises(ValueError, match="temperature"):
        LLMRequest(
            system_prompt="sys",
            messages=[LLMMessage(role=LLMRole.USER, content="hi")],
            temperature=-0.1,
        )


# ===========================================================================
# Exception hierarchy
# ===========================================================================

def test_all_exceptions_inherit_from_slack_bot_error():
    from apps.slack_bot.exceptions import SlackBotError

    for exc_cls in [
        LLMProviderError,
        LLMTimeoutError,
        LLMRateLimitError,
        LLMServerError,
        LLMTransportError,
        LLMAuthError,
        LLMBadRequestError,
        LLMResponseParseError,
    ]:
        assert issubclass(exc_cls, SlackBotError), f"{exc_cls} must inherit SlackBotError"
        assert issubclass(exc_cls, LLMProviderError), f"{exc_cls} must inherit LLMProviderError"


def test_retryable_exceptions():
    assert LLMTimeoutError(provider_name="claude").is_retryable is True
    assert LLMRateLimitError(provider_name="claude").is_retryable is True
    assert LLMServerError(provider_name="claude", status_code=500).is_retryable is True
    assert LLMTransportError(provider_name="claude").is_retryable is True


def test_permanent_exceptions():
    assert LLMAuthError(provider_name="claude").is_retryable is False
    assert LLMBadRequestError(provider_name="claude").is_retryable is False
    assert LLMResponseParseError(provider_name="claude").is_retryable is False


def test_exception_carries_provider_name():
    exc = LLMTimeoutError(provider_name="glm")
    assert exc.provider_name == "glm"


def test_exception_default_messages():
    assert "timed out" in str(LLMTimeoutError(provider_name="claude"))
    assert "rate limit" in str(LLMRateLimitError(provider_name="glm"))
    assert "auth" in str(LLMAuthError(provider_name="claude")).lower()


def test_server_error_carries_status_code():
    exc = LLMServerError(provider_name="claude", status_code=503)
    assert exc.status_code == 503
    assert "503" in str(exc)
