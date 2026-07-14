"""Tests for provider-neutral tool-result and tool-call message translation.

Verifies that both Claude and GLM adapters correctly translate
:class:`LLMMessage` objects carrying tool-call and tool-result content
into their provider-native request formats.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock

from apps.slack_bot.llm.base import (
    LLMMessage,
    LLMRequest,
    LLMRole,
    LLMToolCall,
    LLMToolResultContent,
)
from apps.slack_bot.llm.claude_client import ClaudeProvider
from apps.slack_bot.llm.config import ProviderConfig
from apps.slack_bot.llm.glm_client import GLMProvider


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _mock_response(body_dict):
    mock = MagicMock()
    mock.status_code = 200
    mock.text = json.dumps(body_dict)
    mock.json = lambda: body_dict
    return mock


def _mock_post(response):
    def _post(url, *, json_body, headers, timeout):
        _post.last_json_body = json_body
        return response
    _post.last_json_body = None
    return _post


def _claude_provider():
    return ClaudeProvider(config=ProviderConfig(
        api_key="sk-test",
        model="claude-test",
        base_url="https://api.anthropic.com/v1/messages",
        timeout_seconds=15.0,
        max_output_tokens=2000,
    ))


def _glm_provider():
    return GLMProvider(config=ProviderConfig(
        api_key="zai-test",
        model="glm-4",
        base_url="https://open.bigmodel.cn/api/paas/v4/chat/completions",
        timeout_seconds=15.0,
        max_output_tokens=2000,
    ))


def _anthropic_ok():
    return {
        "content": [{"type": "text", "text": "Done."}],
        "stop_reason": "end_turn",
        "usage": {"input_tokens": 10, "output_tokens": 5},
    }


def _zai_ok():
    return {
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": "Done."},
            "finish_reason": "stop",
        }],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5},
    }


# ===========================================================================
# Claude: tool-call message translation
# ===========================================================================

def test_claude_assistant_tool_call_message():
    """Assistant message with tool_calls → Anthropic content blocks."""
    messages = [
        LLMMessage(role=LLMRole.USER, content="What are our stats?"),
        LLMMessage(
            role=LLMRole.ASSISTANT,
            content="Let me check.",
            tool_calls=[
                LLMToolCall(id="call_1", name="get_account_stats",
                            arguments={"platform": "instagram", "days": 30}),
            ],
        ),
    ]
    resp = _mock_response(_anthropic_ok())
    post = _mock_post(resp)
    provider = _claude_provider()

    provider.complete(
        LLMRequest(system_prompt="sys", messages=messages),
        http_post=post,
    )

    msgs = post.last_json_body["messages"]
    assert msgs[1]["role"] == "assistant"
    content = msgs[1]["content"]
    assert isinstance(content, list)
    assert content[0] == {"type": "text", "text": "Let me check."}
    assert content[1]["type"] == "tool_use"
    assert content[1]["id"] == "call_1"
    assert content[1]["name"] == "get_account_stats"
    assert content[1]["input"] == {"platform": "instagram", "days": 30}


def test_claude_tool_call_no_text():
    """Assistant tool-call with no text → only tool_use block."""
    messages = [
        LLMMessage(
            role=LLMRole.ASSISTANT,
            tool_calls=[LLMToolCall(id="c1", name="list_connected_accounts")],
        ),
    ]
    resp = _mock_response(_anthropic_ok())
    post = _mock_post(resp)
    provider = _claude_provider()

    provider.complete(
        LLMRequest(system_prompt="sys", messages=messages),
        http_post=post,
    )

    content = post.last_json_body["messages"][0]["content"]
    assert len(content) == 1
    assert content[0]["type"] == "tool_use"


# ===========================================================================
# Claude: tool-result message translation
# ===========================================================================

def test_claude_tool_result_message():
    """Tool-result message → Anthropic tool_result content block."""
    messages = [
        LLMMessage(role=LLMRole.USER, content="Stats?"),
        LLMMessage(
            role=LLMRole.ASSISTANT,
            tool_calls=[LLMToolCall(id="call_1", name="get_account_stats")],
        ),
        LLMMessage(
            role=LLMRole.USER,
            tool_result=LLMToolResultContent(
                tool_call_id="call_1",
                content='{"status": "success", "data": {"reach": 100}}',
            ),
        ),
    ]
    resp = _mock_response(_anthropic_ok())
    post = _mock_post(resp)
    provider = _claude_provider()

    provider.complete(
        LLMRequest(system_prompt="sys", messages=messages),
        http_post=post,
    )

    msgs = post.last_json_body["messages"]
    result_msg = msgs[2]
    assert result_msg["role"] == "user"
    content = result_msg["content"]
    assert isinstance(content, list)
    assert content[0]["type"] == "tool_result"
    assert content[0]["tool_use_id"] == "call_1"
    assert "reach" in content[0]["content"]
    assert content[0]["is_error"] is False


def test_claude_tool_result_error():
    """Tool-result with is_error=True → Anthropic is_error flag."""
    messages = [
        LLMMessage(
            role=LLMRole.USER,
            tool_result=LLMToolResultContent(
                tool_call_id="call_1",
                content='{"status": "failed"}',
                is_error=True,
            ),
        ),
    ]
    resp = _mock_response(_anthropic_ok())
    post = _mock_post(resp)
    provider = _claude_provider()

    provider.complete(
        LLMRequest(system_prompt="sys", messages=messages),
        http_post=post,
    )

    content = post.last_json_body["messages"][0]["content"]
    assert content[0]["is_error"] is True


# ===========================================================================
# GLM: tool-call message translation
# ===========================================================================

def test_glm_assistant_tool_call_message():
    """Assistant message with tool_calls → OpenAI tool_calls format."""
    messages = [
        LLMMessage(role=LLMRole.USER, content="What are our stats?"),
        LLMMessage(
            role=LLMRole.ASSISTANT,
            content="Let me check.",
            tool_calls=[
                LLMToolCall(id="call_1", name="get_account_stats",
                            arguments={"platform": "instagram", "days": 30}),
            ],
        ),
    ]
    resp = _mock_response(_zai_ok())
    post = _mock_post(resp)
    provider = _glm_provider()

    provider.complete(
        LLMRequest(system_prompt="sys", messages=messages),
        http_post=post,
    )

    msgs = post.last_json_body["messages"]
    # msgs[0] is system, msgs[1] is user, msgs[2] is assistant
    assistant_msg = msgs[2]
    assert assistant_msg["role"] == "assistant"
    assert assistant_msg["content"] == "Let me check."
    assert "tool_calls" in assistant_msg
    tc = assistant_msg["tool_calls"][0]
    assert tc["id"] == "call_1"
    assert tc["type"] == "function"
    assert tc["function"]["name"] == "get_account_stats"
    args = json.loads(tc["function"]["arguments"])
    assert args == {"platform": "instagram", "days": 30}


# ===========================================================================
# GLM: tool-result message translation
# ===========================================================================

def test_glm_tool_result_message():
    """Tool-result message → OpenAI 'tool' role message."""
    messages = [
        LLMMessage(role=LLMRole.USER, content="Stats?"),
        LLMMessage(
            role=LLMRole.ASSISTANT,
            tool_calls=[LLMToolCall(id="call_1", name="get_account_stats")],
        ),
        LLMMessage(
            role=LLMRole.USER,
            tool_result=LLMToolResultContent(
                tool_call_id="call_1",
                content='{"status": "success", "data": {"reach": 100}}',
            ),
        ),
    ]
    resp = _mock_response(_zai_ok())
    post = _mock_post(resp)
    provider = _glm_provider()

    provider.complete(
        LLMRequest(system_prompt="sys", messages=messages),
        http_post=post,
    )

    msgs = post.last_json_body["messages"]
    # Last message should be the tool result
    tool_msg = msgs[-1]
    assert tool_msg["role"] == "tool"
    assert tool_msg["tool_call_id"] == "call_1"
    assert "reach" in tool_msg["content"]


def test_glm_tool_result_error():
    """Tool-result with is_error=True → still sent as tool role (OpenAI convention)."""
    messages = [
        LLMMessage(
            role=LLMRole.USER,
            tool_result=LLMToolResultContent(
                tool_call_id="call_1",
                content='{"status": "failed"}',
                is_error=True,
            ),
        ),
    ]
    resp = _mock_response(_zai_ok())
    post = _mock_post(resp)
    provider = _glm_provider()

    provider.complete(
        LLMRequest(system_prompt="sys", messages=messages),
        http_post=post,
    )

    tool_msg = post.last_json_body["messages"][-1]
    assert tool_msg["role"] == "tool"
    assert tool_msg["tool_call_id"] == "call_1"


# ===========================================================================
# Plain text messages still work
# ===========================================================================

def test_claude_plain_text_message_unchanged():
    """Plain text messages still produce simple string content."""
    messages = [
        LLMMessage(role=LLMRole.USER, content="Hello"),
        LLMMessage(role=LLMRole.ASSISTANT, content="Hi there"),
    ]
    resp = _mock_response(_anthropic_ok())
    post = _mock_post(resp)
    provider = _claude_provider()

    provider.complete(
        LLMRequest(system_prompt="sys", messages=messages),
        http_post=post,
    )

    msgs = post.last_json_body["messages"]
    assert msgs[0] == {"role": "user", "content": "Hello"}
    assert msgs[1] == {"role": "assistant", "content": "Hi there"}


def test_glm_plain_text_message_unchanged():
    """Plain text messages still produce simple string content."""
    messages = [
        LLMMessage(role=LLMRole.USER, content="Hello"),
    ]
    resp = _mock_response(_zai_ok())
    post = _mock_post(resp)
    provider = _glm_provider()

    provider.complete(
        LLMRequest(system_prompt="sys", messages=messages),
        http_post=post,
    )

    msgs = post.last_json_body["messages"]
    # msgs[0] is system, msgs[1] is user
    assert msgs[1] == {"role": "user", "content": "Hello"}
