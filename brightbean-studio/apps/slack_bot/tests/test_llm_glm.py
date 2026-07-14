"""Tests for the GLM (Z.AI / Zhipu BigModel) provider adapter."""

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
from apps.slack_bot.llm.glm_client import GLMProvider

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


def _make_config(api_key="zai-test-key"):
    return ProviderConfig(
        api_key=api_key,
        model="glm-4",
        base_url="https://open.bigmodel.cn/api/paas/v4/chat/completions",
        timeout_seconds=15.0,
        max_output_tokens=2000,
    )


def _make_provider(api_key="zai-test-key"):
    return GLMProvider(config=_make_config(api_key=api_key))


# ---------------------------------------------------------------------------
# Z.AI (OpenAI-compatible) response factory
# ---------------------------------------------------------------------------

def _zai_response(
    text="Here are your stats.",
    tool_calls=None,
    finish_reason="stop",
    prompt_tokens=100,
    completion_tokens=50,
):
    message = {"role": "assistant", "content": text}
    if tool_calls:
        message["tool_calls"] = tool_calls
    return {
        "id": "chatcmpl-abc",
        "object": "chat.completion",
        "model": "glm-4",
        "choices": [
            {
                "index": 0,
                "message": message,
                "finish_reason": finish_reason,
            }
        ],
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
        },
    }


# ===========================================================================
# 1. Successful text-only response
# ===========================================================================

def test_success_text_only():
    raw = _zai_response(text="Reach is 18,420.", finish_reason="stop")
    resp = _mock_response(body_dict=raw)
    post = _mock_http_post(resp)
    provider = _make_provider()

    result = provider.complete(_make_request(), http_post=post)

    assert result.text == "Reach is 18,420."
    assert result.tool_calls == []
    assert result.stop_reason == LLMStopReason.END_TURN
    assert result.usage.input_tokens == 100
    assert result.usage.output_tokens == 50
    assert result.provider_name == "glm"


# ===========================================================================
# 2. Request body construction
# ===========================================================================

def test_request_body_has_model_and_max_tokens():
    resp = _mock_response(body_dict=_zai_response())
    post = _mock_http_post(resp)
    provider = _make_provider()

    provider.complete(_make_request(max_output_tokens=500), http_post=post)

    body = post.last_json_body
    assert body["model"] == "glm-4"
    assert body["max_tokens"] == 500


def test_request_body_system_prompt_as_first_message():
    resp = _mock_response(body_dict=_zai_response())
    post = _mock_http_post(resp)
    provider = _make_provider()

    provider.complete(_make_request(), http_post=post)

    messages = post.last_json_body["messages"]
    assert messages[0] == {"role": "system", "content": "You are an analytics assistant."}
    assert messages[1] == {"role": "user", "content": "What are our Instagram stats?"}


def test_request_body_no_system_prompt_omits_system_message():
    resp = _mock_response(body_dict=_zai_response())
    post = _mock_http_post(resp)
    provider = _make_provider()

    provider.complete(_make_request(system_prompt=""), http_post=post)

    messages = post.last_json_body["messages"]
    assert messages[0]["role"] == "user"


def test_request_body_tools_translated_to_openai_format():
    resp = _mock_response(body_dict=_zai_response())
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
            "type": "function",
            "function": {
                "name": "get_account_stats",
                "description": "Get account-level analytics",
                "parameters": {"type": "object", "properties": {"platform": {"type": "string"}}},
            },
        }
    ]


def test_request_headers():
    resp = _mock_response(body_dict=_zai_response())
    post = _mock_http_post(resp)
    provider = _make_provider(api_key="zai-abc123")

    provider.complete(_make_request(), http_post=post)

    headers = post.last_headers
    assert headers["Authorization"] == "Bearer zai-abc123"
    assert headers["Content-Type"] == "application/json"


# ===========================================================================
# 3. Tool use response
# ===========================================================================

def test_tool_use_response_parsed():
    tool_calls = [
        {
            "id": "call_abc",
            "type": "function",
            "function": {
                "name": "get_account_stats",
                "arguments": json.dumps({"platform": "instagram", "days": 30}),
            },
        }
    ]
    raw = _zai_response(
        text="",
        tool_calls=tool_calls,
        finish_reason="tool_calls",
    )
    resp = _mock_response(body_dict=raw)
    post = _mock_http_post(resp)
    provider = _make_provider()

    result = provider.complete(_make_request(), http_post=post)

    assert len(result.tool_calls) == 1
    tc = result.tool_calls[0]
    assert tc.id == "call_abc"
    assert tc.name == "get_account_stats"
    assert tc.arguments == {"platform": "instagram", "days": 30}
    assert result.stop_reason == LLMStopReason.TOOL_USE


def test_tool_use_with_dict_arguments():
    """Some providers return arguments as a dict instead of a JSON string."""
    tool_calls = [
        {
            "id": "call_xyz",
            "type": "function",
            "function": {
                "name": "get_top_posts",
                "arguments": {"platform": "facebook"},
            },
        }
    ]
    raw = _zai_response(text="", tool_calls=tool_calls, finish_reason="tool_calls")
    resp = _mock_response(body_dict=raw)
    provider = _make_provider()

    result = provider.complete(_make_request(), http_post=_mock_http_post(resp))

    assert result.tool_calls[0].arguments == {"platform": "facebook"}


# ===========================================================================
# 4. Stop reason mapping
# ===========================================================================

def test_stop_reason_length():
    raw = _zai_response(finish_reason="length")
    resp = _mock_response(body_dict=raw)
    provider = _make_provider()

    result = provider.complete(_make_request(), http_post=_mock_http_post(resp))

    assert result.stop_reason == LLMStopReason.MAX_TOKENS


# ===========================================================================
# 5. Error classification
# ===========================================================================

def test_error_401_raises_auth():
    resp = _mock_response(status_code=401, text="invalid key")
    provider = _make_provider()

    with pytest.raises(LLMAuthError) as exc_info:
        provider.complete(_make_request(), http_post=_mock_http_post(resp))

    assert exc_info.value.is_retryable is False


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

    with pytest.raises(LLMAuthError, match="ZAI_API_KEY"):
        provider.complete(_make_request())


# ===========================================================================
# 8. Response parse errors
# ===========================================================================

def test_empty_choices_raises_parse_error():
    raw = {"choices": []}
    resp = _mock_response(body_dict=raw)
    provider = _make_provider()

    with pytest.raises(LLMResponseParseError):
        provider.complete(_make_request(), http_post=_mock_http_post(resp))


def test_non_dict_response_raises_parse_error():
    resp = _mock_response(status_code=200, text="[1, 2, 3]")
    resp.json = lambda: [1, 2, 3]
    provider = _make_provider()

    with pytest.raises(LLMResponseParseError):
        provider.complete(_make_request(), http_post=_mock_http_post(resp))


# ===========================================================================
# 9. Config from settings
# ===========================================================================

@override_settings(
    ZAI_API_KEY="zai-from-settings",
    ZAI_MODEL="glm-4",
)
def test_config_from_settings():
    resp = _mock_response(body_dict=_zai_response())
    post = _mock_http_post(resp)
    provider = GLMProvider()  # no explicit config

    provider.complete(_make_request(), http_post=post)

    assert post.last_headers["Authorization"] == "Bearer zai-from-settings"
    assert post.last_json_body["model"] == "glm-4"
