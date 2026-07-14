"""Focused unit tests for ToolOrchestrator.

All tests use fake providers, fake tools, and an injected monotonic clock.
No real network, database, Z.AI, Claude, Slack, or BrightBean calls.
Target: <5 seconds total.
"""

from __future__ import annotations

import uuid
from unittest.mock import MagicMock

from ninja import Schema
from pydantic import ConfigDict

from apps.slack_bot.contracts import (
    ToolContext,
    ToolResult,
    ToolResultStatus,
)
from apps.slack_bot.errors import ErrorCode
from apps.slack_bot.llm.base import (
    LLMMessage,
    LLMResponse,
    LLMRole,
    LLMStopReason,
    LLMTokenUsage,
    LLMToolCall,
)
from apps.slack_bot.llm.exceptions import LLMTimeoutError
from apps.slack_bot.llm.router import LLMRouter
from apps.slack_bot.tool_execution import (
    OrchestrationLimits,
    TerminationReason,
    ToolOrchestrator,
)
from apps.slack_bot.tool_registry import RegisteredTool, ToolRegistry

# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

_ACCOUNT_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")


def _make_context() -> ToolContext:
    return ToolContext(
        workspace_id=uuid.UUID("00000000-0000-0000-0000-000000000010"),
        user_id=uuid.UUID("00000000-0000-0000-0000-000000000020"),
        organization_id=uuid.UUID("00000000-0000-0000-0000-000000000030"),
        allowed_account_ids=frozenset({_ACCOUNT_ID}),
        slack_team_id="T1",
        slack_channel_id="C1",
    )


class _FakeInput(Schema):
    model_config = ConfigDict(extra="forbid")
    metric: str = "reach"


def _fake_executor(*, arguments, context):
    return ToolResult(
        status=ToolResultStatus.SUCCESS,
        tool_name="get_test_metric",
        data={"metric": arguments.metric, "value": 100},
    )


def _make_registry() -> ToolRegistry:
    reg = ToolRegistry()
    reg.register(RegisteredTool(
        name="get_test_metric",
        description="Return a test metric.",
        input_schema_type=_FakeInput,
        executor=_fake_executor,
    ))
    return reg


def _make_response(
    text="Done.",
    tool_calls=None,
    provider_name="fake",
):
    return LLMResponse(
        text=text,
        tool_calls=tool_calls or [],
        stop_reason=LLMStopReason.TOOL_USE if tool_calls else LLMStopReason.END_TURN,
        usage=LLMTokenUsage(input_tokens=5, output_tokens=5),
        provider_name=provider_name,
        raw={},
    )


def _make_router(responses: list) -> LLMRouter:
    """Create a router with a mock primary that returns *responses* in order."""
    primary = MagicMock()
    primary.name = "fake"
    primary.complete.side_effect = responses
    return LLMRouter(primary=primary, fallback=None)


def _make_tool_call(name="get_test_metric", args=None, call_id="call_1"):
    return LLMToolCall(
        id=call_id,
        name=name,
        arguments=args or {"metric": "reach"},
    )


# ===========================================================================
# 1. Final text response without a tool call
# ===========================================================================

def test_final_response_without_tool_call():
    router = _make_router([_make_response(text="Here is your answer.")])
    orch = ToolOrchestrator(
        router=router,
        registry=_make_registry(),
        limits=OrchestrationLimits(max_rounds=3),
    )
    result = orch.run(
        messages=[LLMMessage(role=LLMRole.USER, content="What is my reach?")],
        context=_make_context(),
    )
    assert result.final_text == "Here is your answer."
    assert result.termination_reason == TerminationReason.FINAL_RESPONSE
    assert result.tool_call_count == 0
    assert result.rounds == 1


# ===========================================================================
# 2. One valid tool call followed by final text
# ===========================================================================

def test_one_tool_call_then_final_text():
    tc = _make_tool_call()
    router = _make_router([
        _make_response(text="", tool_calls=[tc]),
        _make_response(text="Your reach is 100."),
    ])
    orch = ToolOrchestrator(
        router=router,
        registry=_make_registry(),
        limits=OrchestrationLimits(max_rounds=5),
    )
    result = orch.run(
        messages=[LLMMessage(role=LLMRole.USER, content="What is my reach?")],
        context=_make_context(),
    )
    assert result.final_text == "Your reach is 100."
    assert result.termination_reason == TerminationReason.FINAL_RESPONSE
    assert result.tool_call_count == 1
    assert len(result.tool_results) == 1
    assert result.tool_results[0].tool_name == "get_test_metric"
    assert result.tool_results[0].tool_call_id == "call_1"


# ===========================================================================
# 3. Unknown tool rejected
# ===========================================================================

def test_unknown_tool_rejected():
    tc = _make_tool_call(name="nonexistent_tool")
    router = _make_router([_make_response(text="", tool_calls=[tc])])
    orch = ToolOrchestrator(
        router=router,
        registry=_make_registry(),
        limits=OrchestrationLimits(max_rounds=5),
    )
    result = orch.run(
        messages=[LLMMessage(role=LLMRole.USER, content="test")],
        context=_make_context(),
    )
    assert result.termination_reason == TerminationReason.TOOL_VALIDATION_FAILED
    assert result.error_code == ErrorCode.TOOL_NOT_FOUND


# ===========================================================================
# 4. Invalid tool arguments rejected
# ===========================================================================

def test_invalid_tool_arguments_rejected():
    tc = _make_tool_call(args={"unknown_field": "bad"})
    router = _make_router([_make_response(text="", tool_calls=[tc])])
    orch = ToolOrchestrator(
        router=router,
        registry=_make_registry(),
        limits=OrchestrationLimits(max_rounds=5),
    )
    result = orch.run(
        messages=[LLMMessage(role=LLMRole.USER, content="test")],
        context=_make_context(),
    )
    assert result.termination_reason == TerminationReason.TOOL_VALIDATION_FAILED
    assert result.error_code == ErrorCode.TOOL_ARGUMENT_VALIDATION_FAILED


# ===========================================================================
# 5. ToolContext passed separately and cannot be overridden
# ===========================================================================

def test_context_not_in_llm_request():
    """The LLM request must not contain ToolContext fields."""
    primary = MagicMock()
    primary.name = "fake"
    primary.complete.return_value = _make_response(text="Done.")
    router = LLMRouter(primary=primary, fallback=None)
    orch = ToolOrchestrator(
        router=router,
        registry=_make_registry(),
        limits=OrchestrationLimits(),
    )
    ctx = _make_context()
    orch.run(
        messages=[LLMMessage(role=LLMRole.USER, content="test")],
        context=ctx,
    )
    # Inspect the request passed to the provider
    call_args = primary.complete.call_args
    request = call_args[0][0]
    # The request is an LLMRequest — check no context fields leaked
    assert not hasattr(request, "workspace_id")
    assert not hasattr(request, "allowed_account_ids")
    # Check the messages don't contain context data
    for msg in request.messages:
        if msg.content:
            assert "workspace_id" not in msg.content
            assert "allowed_account_ids" not in msg.content


# ===========================================================================
# 6. Repeated identical tool call triggers loop protection
# ===========================================================================

def test_repeated_call_triggers_loop_detection():
    tc = _make_tool_call()
    # Model keeps requesting the same tool every round
    router = _make_router([_make_response(text="", tool_calls=[tc])] * 5)
    orch = ToolOrchestrator(
        router=router,
        registry=_make_registry(),
        limits=OrchestrationLimits(max_rounds=10, max_repeated_calls=1),
    )
    result = orch.run(
        messages=[LLMMessage(role=LLMRole.USER, content="test")],
        context=_make_context(),
    )
    assert result.termination_reason == TerminationReason.LOOP_DETECTED


# ===========================================================================
# 7. Maximum rounds enforced
# ===========================================================================

def test_max_rounds_enforced():
    tc = _make_tool_call()
    # Model always requests a tool, never gives final text
    router = _make_router([_make_response(text="", tool_calls=[tc])] * 10)
    orch = ToolOrchestrator(
        router=router,
        registry=_make_registry(),
        limits=OrchestrationLimits(max_rounds=3, max_total_calls=20, max_repeated_calls=20),
    )
    result = orch.run(
        messages=[LLMMessage(role=LLMRole.USER, content="test")],
        context=_make_context(),
    )
    assert result.termination_reason == TerminationReason.MAX_ROUNDS_REACHED
    assert result.rounds == 3


# ===========================================================================
# 8. ToolResult preserved
# ===========================================================================

def test_tool_result_preserved():
    tc = _make_tool_call()
    router = _make_router([
        _make_response(text="", tool_calls=[tc]),
        _make_response(text="Done."),
    ])
    orch = ToolOrchestrator(
        router=router,
        registry=_make_registry(),
        limits=OrchestrationLimits(max_rounds=5),
    )
    result = orch.run(
        messages=[LLMMessage(role=LLMRole.USER, content="test")],
        context=_make_context(),
    )
    assert len(result.tool_results) == 1
    preserved = result.tool_results[0]
    assert preserved.result.status == ToolResultStatus.SUCCESS
    assert preserved.result.data == {"metric": "reach", "value": 100}
    assert preserved.round_number == 1


# ===========================================================================
# 9. Tool executor exception handled safely
# ===========================================================================

def test_executor_exception_handled():
    def bad_executor(*, arguments, context):
        raise RuntimeError("Executor crashed")

    reg = ToolRegistry()
    reg.register(RegisteredTool(
        name="get_test_metric",
        description="Test",
        input_schema_type=_FakeInput,
        executor=bad_executor,
    ))

    tc = _make_tool_call()
    router = _make_router([_make_response(text="", tool_calls=[tc])])
    orch = ToolOrchestrator(
        router=router,
        registry=reg,
        limits=OrchestrationLimits(max_rounds=5),
    )
    result = orch.run(
        messages=[LLMMessage(role=LLMRole.USER, content="test")],
        context=_make_context(),
    )
    assert result.termination_reason == TerminationReason.TOOL_EXECUTION_FAILED
    assert result.error_code == ErrorCode.TOOL_EXECUTION_FAILED


# ===========================================================================
# 10. Provider failure → PROVIDER_FAILURE termination
# ===========================================================================

def test_provider_failure_terminates():
    primary = MagicMock()
    primary.name = "fake"
    primary.complete.side_effect = LLMTimeoutError("timed out", provider_name="fake")
    # No fallback configured
    router = LLMRouter(primary=primary, fallback=None)
    orch = ToolOrchestrator(
        router=router,
        registry=_make_registry(),
        limits=OrchestrationLimits(max_rounds=5),
    )
    result = orch.run(
        messages=[LLMMessage(role=LLMRole.USER, content="test")],
        context=_make_context(),
    )
    assert result.termination_reason == TerminationReason.PROVIDER_FAILURE
    assert result.final_text == ""


# ===========================================================================
# 11. Timeout via injected clock
# ===========================================================================

def test_timeout_via_injected_clock():
    # Clock that always reports elapsed >= timeout
    tick = [0.0]

    def fast_clock():
        tick[0] += 100.0
        return tick[0]

    router = _make_router([_make_response(text="Done.")])
    orch = ToolOrchestrator(
        router=router,
        registry=_make_registry(),
        limits=OrchestrationLimits(timeout_seconds=1.0),
        monotonic_clock=fast_clock,
    )
    result = orch.run(
        messages=[LLMMessage(role=LLMRole.USER, content="test")],
        context=_make_context(),
    )
    assert result.termination_reason == TerminationReason.TIMEOUT


# ===========================================================================
# 12. Primary retryable failure uses fallback
# ===========================================================================

def test_primary_retryable_uses_fallback():
    fallback_response = _make_response(text="Fallback answer.", provider_name="fallback")
    primary = MagicMock()
    primary.name = "primary"
    primary.complete.side_effect = LLMTimeoutError("timed out", provider_name="primary")
    fallback = MagicMock()
    fallback.name = "fallback"
    fallback.complete.return_value = fallback_response
    router = LLMRouter(primary=primary, fallback=fallback)

    orch = ToolOrchestrator(
        router=router,
        registry=_make_registry(),
        limits=OrchestrationLimits(max_rounds=5),
    )
    result = orch.run(
        messages=[LLMMessage(role=LLMRole.USER, content="test")],
        context=_make_context(),
    )
    assert result.final_text == "Fallback answer."
    assert result.used_fallback is True
    assert result.termination_reason == TerminationReason.FINAL_RESPONSE
