"""Tests for the Claude (Anthropic Messages API) provider adapter."""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest
from django.test import override_settings

from apps.slack_bot.llm.base import (
    LLMMessage,
    LLMRequest,
    LLMRole,
    LLMStopReason,
    LLMToolDefinition,
)
from apps.slack_bot.llm.claude_client import ClaudeProvider
from apps.slack_bot.llm.config import ProviderConfig
from apps.slack_bot.llm.exceptions import (
    LLMAuthError,
    LLMBadRequestError,
    LLMRateLimitError,
    LLMResponseParseError,
    LLMServerError,
    LLMTimeoutError,
    LLMTransportError,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _mock_response(status_code=200, body_dict=None, text=None):
    mock = MagicMock()
    mock.status_code = status_code
    if text is not None:
        mock.text = text
    elif body_dict is not None:
        mock.text = json.dumps(body_dict)
        mock.json = lambda: body_dict
    else:
        mock.text = "{}"
        mock.json = lambda: {}
    return mock


def _mock_http_post(response=None):
    if response is None:
        response = _mock_response()

    def _post(url, *, json_body, headers, timeout):
        _post.last_url = url
        _post.last_json_body = json_body
        _post.last_headers = headers
        _post.last_timeout = timeout
        return response

    _post.last_url = None
    _post.last_json_body = None
    _post.last_headers = None
    _post.last_timeout = None
    return _post


def _make_request(**kwargs):
    defaults = dict(
        system_prompt="You are an analytics assistant.",
        messages=[LLMMessage(role=LLMRole.USER, content="What are our Instagram stats?")],
        max_output_tokens=1000,
    )
    defaults.update(kwargs)
    return LLMRequest(**defaults)


def _make_config(api_key="sk-ant-test"):
    return ProviderConfig(
        api_key=api_key,
        model="claude-sonnet-4-5-20250929",
        base_url="https://api.anthropic.com/v1/messages",
        timeout_seconds=15.0,
        max_output_tokens=2000,
    )


def _make_provider(api_key="sk-ant-test"):
    return ClaudeProvider(config=_make_config(api_key=api_key))


# ---------------------------------------------------------------------------
# Anthropic response factory
# ---------------------------------------------------------------------------

def _anthropic_response(
    text="Here are your stats.",
    tool_use=None,
    stop_reason="end_turn",
    input_tokens=100,
    output_tokens=50,
):
    content = [{"type": "text", "text": text}]
    if tool_use:
        content.append(tool_use)
    return {
        "id": "msg_01abc",
        "type": "message",
        "role": "assistant",
        "content": content,
        "model": "claude-sonnet-4-5-20250929",
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
        },
    }


# ===========================================================================
# 1. Successful text-only response
# ===========================================================================

def test_success_text_only():
    raw = _anthropic_response(text="Reach is 18,420.", stop_reason="end_turn")
    resp = _mock_response(body_dict=raw)
    post = _mock_http_post(resp)
    provider = _make_provider()

    result = provider.complete(_make_request(), http_post=post)

    assert result.text == "Reach is 18,420."
    assert result.tool_calls == []
    assert result.stop_reason == LLMStopReason.END_TURN
    assert result.usage.input_tokens == 100
    assert result.usage.output_tokens == 50
    assert result.provider_name == "claude"


# ===========================================================================
# 2. Request body construction
# ===========================================================================

def test_request_body_has_model_and_max_tokens():
    resp = _mock_response(body_dict=_anthropic_response())
    post = _mock_http_post(resp)
    provider = _make_provider()

    provider.complete(_make_request(max_output_tokens=500), http_post=post)

    body = post.last_json_body
    assert body["model"] == "claude-sonnet-4-5-20250929"
    assert body["max_tokens"] == 500
    assert body["system"] == "You are an analytics assistant."
    assert body["messages"] == [
        {"role": "user", "content": "What are our Instagram stats?"}
    ]


def test_request_body_temperature_omitted_when_default():
    resp = _mock_response(body_dict=_anthropic_response())
    post = _mock_http_post(resp)
    provider = _make_provider()

    provider.complete(_make_request(), http_post=post)

    assert "temperature" not in post.last_json_body


def test_request_body_temperature_included_when_non_default():
    resp = _mock_response(body_dict=_anthropic_response())
    post = _mock_http_post(resp)
    provider = _make_provider()

    provider.complete(_make_request(temperature=0.5), http_post=post)

    assert post.last_json_body["temperature"] == 0.5


def test_request_body_tools_translated():
    resp = _mock_response(body_dict=_anthropic_response())
    post = _mock_http_post(resp)
    provider = _make_provider()

    tool = LLMToolDefinition(
        name="get_account_stats",
        description="Get account-level analytics",
        input_schema={"type": "object", "properties": {"platform": {"type": "string"}}},
    )
    provider.complete(_make_request(tools=[tool]), http_post=post)

    assert post.last_json_body["tools"] == [
        {
            "name": "get_account_stats",
            "description": "Get account-level analytics",
            "input_schema": {"type": "object", "properties": {"platform": {"type": "string"}}},
        }
    ]


def test_request_headers():
    resp = _mock_response(body_dict=_anthropic_response())
    post = _mock_http_post(resp)
    provider = _make_provider(api_key="sk-ant-abc123")

    provider.complete(_make_request(), http_post=post)

    headers = post.last_headers
    assert headers["x-api-key"] == "sk-ant-abc123"
    assert headers["anthropic-version"] == "2023-06-01"
    assert headers["content-type"] == "application/json"


# ===========================================================================
# 3. Tool use response
# ===========================================================================

def test_tool_use_response_parsed():
    tool_use_block = {
        "type": "tool_use",
        "id": "toolu_01abc",
        "name": "get_account_stats",
        "input": {"platform": "instagram", "days": 30},
    }
    raw = _anthropic_response(
        text="",
        tool_use=tool_use_block,
        stop_reason="tool_use",
    )
    resp = _mock_response(body_dict=raw)
    post = _mock_http_post(resp)
    provider = _make_provider()

    result = provider.complete(_make_request(), http_post=post)

    assert len(result.tool_calls) == 1
    tc = result.tool_calls[0]
    assert tc.id == "toolu_01abc"
    assert tc.name == "get_account_stats"
    assert tc.arguments == {"platform": "instagram", "days": 30}
    assert result.stop_reason == LLMStopReason.TOOL_USE


# ===========================================================================
# 4. Stop reason mapping
# ===========================================================================

def test_stop_reason_max_tokens():
    raw = _anthropic_response(stop_reason="max_tokens")
    resp = _mock_response(body_dict=raw)
    provider = _make_provider()

    result = provider.complete(_make_request(), http_post=_mock_http_post(resp))

    assert result.stop_reason == LLMStopReason.MAX_TOKENS


def test_stop_reason_stop_sequence():
    raw = _anthropic_response(stop_reason="stop_sequence")
    resp = _mock_response(body_dict=raw)
    provider = _make_provider()

    result = provider.complete(_make_request(), http_post=_mock_http_post(resp))

    assert result.stop_reason == LLMStopReason.STOP_SEQUENCE


# ===========================================================================
# 5. Error classification
# ===========================================================================

def test_error_401_raises_auth():
    resp = _mock_response(status_code=401, text='{"error": "invalid key"}')
    provider = _make_provider()

    with pytest.raises(LLMAuthError) as exc_info:
        provider.complete(_make_request(), http_post=_mock_http_post(resp))

    assert exc_info.value.is_retryable is False


def test_error_403_raises_auth():
    resp = _mock_response(status_code=403, text="forbidden")
    provider = _make_provider()

    with pytest.raises(LLMAuthError):
        provider.complete(_make_request(), http_post=_mock_http_post(resp))


def test_error_400_raises_bad_request():
    resp = _mock_response(status_code=400, text="bad request")
    provider = _make_provider()

    with pytest.raises(LLMBadRequestError) as exc_info:
        provider.complete(_make_request(), http_post=_mock_http_post(resp))

    assert exc_info.value.is_retryable is False


def test_error_429_raises_rate_limit():
    resp = _mock_response(status_code=429, text="rate limited")
    provider = _make_provider()

    with pytest.raises(LLMRateLimitError) as exc_info:
        provider.complete(_make_request(), http_post=_mock_http_post(resp))

    assert exc_info.value.is_retryable is True


def test_error_500_raises_server_error():
    resp = _mock_response(status_code=500, text="internal error")
    provider = _make_provider()

    with pytest.raises(LLMServerError) as exc_info:
        provider.complete(_make_request(), http_post=_mock_http_post(resp))

    assert exc_info.value.is_retryable is True
    assert exc_info.value.status_code == 500


def test_error_503_raises_server_error():
    resp = _mock_response(status_code=503, text="unavailable")
    provider = _make_provider()

    with pytest.raises(LLMServerError):
        provider.complete(_make_request(), http_post=_mock_http_post(resp))


# ===========================================================================
# 6. Transport errors
# ===========================================================================

def test_timeout_raises_llm_timeout():
    def _post(url, *, json_body, headers, timeout):
        raise TimeoutError("connection timed out")

    provider = _make_provider()

    with pytest.raises(LLMTimeoutError) as exc_info:
        provider.complete(_make_request(), http_post=_post)

    assert exc_info.value.is_retryable is True


def test_transport_error_raises_llm_transport():
    def _post(url, *, json_body, headers, timeout):
        raise ConnectionError("connection refused")

    provider = _make_provider()

    with pytest.raises(LLMTransportError) as exc_info:
        provider.complete(_make_request(), http_post=_post)

    assert exc_info.value.is_retryable is True


# ===========================================================================
# 7. Missing API key
# ===========================================================================

def test_missing_api_key_raises_auth():
    provider = _make_provider(api_key="")

    with pytest.raises(LLMAuthError, match="ANTHROPIC_API_KEY"):
        provider.complete(_make_request())


# ===========================================================================
# 8. Response parse errors
# ===========================================================================

def test_non_dict_response_raises_parse_error():
    resp = _mock_response(status_code=200, text="[1, 2, 3]")
    resp.json = lambda: [1, 2, 3]
    provider = _make_provider()

    with pytest.raises(LLMResponseParseError):
        provider.complete(_make_request(), http_post=_mock_http_post(resp))


def test_malformed_content_raises_parse_error():
    raw = {"content": "not a list", "stop_reason": "end_turn"}
    resp = _mock_response(body_dict=raw)
    provider = _make_provider()

    with pytest.raises(LLMResponseParseError):
        provider.complete(_make_request(), http_post=_mock_http_post(resp))


# ===========================================================================
# 9. Config from settings (no explicit config)
# ===========================================================================

@override_settings(
    ANTHROPIC_API_KEY="sk-ant-from-settings",
    ANTHROPIC_MODEL="claude-sonnet-4-5-20250929",
)
def test_config_from_settings():
    resp = _mock_response(body_dict=_anthropic_response())
    post = _mock_http_post(resp)
    provider = ClaudeProvider()  # no explicit config

    provider.complete(_make_request(), http_post=post)

    assert post.last_headers["x-api-key"] == "sk-ant-from-settings"
    assert post.last_json_body["model"] == "claude-sonnet-4-5-20250929"
