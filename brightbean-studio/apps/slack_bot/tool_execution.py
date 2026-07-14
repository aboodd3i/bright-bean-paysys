"""Bounded tool-calling orchestration for the Slack analytics bot.

Sits between the :class:`~apps.slack_bot.llm.router.LLMRouter` and the
tool registry/executor layer.  The orchestrator:

1. Sends messages and tool definitions through the LLM router.
2. Interprets normalized tool calls from the LLM response.
3. Validates tool names against the registry.
4. Validates tool arguments using the registered Pydantic schema.
5. Executes approved tools with application-created
   :class:`~apps.slack_bot.contracts.ToolContext`.
6. Appends tool-call and tool-result messages to the conversation.
7. Enforces round, call, loop, and time limits.
8. Returns a normalized :class:`ToolOrchestrationResult`.

No real BrightBean analytics tools are used — the registry is injected.
No Slack formatting, no structured answer generation, no task pipeline
integration.  Those belong to later phases.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from .contracts import ToolContext, ToolResult, ToolResultStatus
from .errors import ErrorCode
from .llm.base import (
    LLMMessage,
    LLMRequest,
    LLMResponse,
    LLMRole,
    LLMToolCall,
    LLMToolResultContent,
)
from .llm.router import LLMRouter, LLMRouterResult
from .tool_registry import RegisteredTool, ToolRegistry

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Orchestration result
# ---------------------------------------------------------------------------


class TerminationReason(StrEnum):
    """Why the orchestration stopped."""

    FINAL_RESPONSE = "final_response"
    MAX_ROUNDS_REACHED = "max_rounds_reached"
    MAX_CALLS_REACHED = "max_calls_reached"
    LOOP_DETECTED = "loop_detected"
    TIMEOUT = "timeout"
    TOOL_VALIDATION_FAILED = "tool_validation_failed"
    TOOL_EXECUTION_FAILED = "tool_execution_failed"
    PROVIDER_FAILURE = "provider_failure"
    MAX_CALLS_PER_RESPONSE = "max_calls_per_response"


@dataclass(frozen=True)
class PreservedToolResult:
    """A successful tool result preserved for later phases.

    Fields
    ------
    tool_name : str
        Name of the tool that produced this result.
    tool_call_id : str
        Provider-assigned call ID.
    result : ToolResult
        The original typed ToolResult — available to structured-response
        and formatter phases.
    round_number : int
        Which orchestration round produced this result.
    """

    tool_name: str
    tool_call_id: str
    result: ToolResult
    round_number: int


@dataclass(frozen=True)
class ToolOrchestrationResult:
    """Normalized outcome of a tool orchestration run.

    Fields
    ------
    final_text : str
        Final text response from the LLM (may be empty on failure).
    provider_name : str
        Name of the provider that produced the final response.
    used_fallback : bool
        Whether the fallback provider was used in the final round.
    tool_results : list[PreservedToolResult]
        Successful tool results in execution order.
    tool_call_count : int
        Total tool calls made across all rounds.
    rounds : int
        Number of orchestration rounds completed.
    termination_reason : TerminationReason
        Why the orchestration stopped.
    error_code : ErrorCode | None
        Stable error code when the result is a failure.
    error_message : str | None
        Human-readable error detail (no secrets, no stack traces).
    """

    final_text: str = ""
    provider_name: str = ""
    used_fallback: bool = False
    tool_results: list[PreservedToolResult] = field(default_factory=list)
    tool_call_count: int = 0
    rounds: int = 0
    termination_reason: TerminationReason = TerminationReason.FINAL_RESPONSE
    error_code: ErrorCode | None = None
    error_message: str | None = None


# ---------------------------------------------------------------------------
# Orchestration limits
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class OrchestrationLimits:
    """Bounded execution limits for the orchestrator.

    Fields
    ------
    max_rounds : int
        Maximum LLM round-trips (1 = no tool calls).
    max_total_calls : int
        Maximum total tool calls across all rounds.
    max_calls_per_response : int
        Maximum tool calls accepted in a single LLM response.
    max_repeated_calls : int
        Maximum consecutive identical calls before loop detection.
    timeout_seconds : float
        Total orchestration time budget.
    """

    max_rounds: int = 5
    max_total_calls: int = 10
    max_calls_per_response: int = 3
    max_repeated_calls: int = 2
    timeout_seconds: float = 30.0

    def __post_init__(self) -> None:
        if self.max_rounds < 1:
            raise ValueError("max_rounds must be at least 1")
        if self.max_total_calls < 1:
            raise ValueError("max_total_calls must be at least 1")
        if self.max_calls_per_response < 1:
            raise ValueError("max_calls_per_response must be at least 1")
        if self.max_repeated_calls < 1:
            raise ValueError("max_repeated_calls must be at least 1")
        if self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")


# ---------------------------------------------------------------------------
# Tool result serialization
# ---------------------------------------------------------------------------

_MAX_SERIALIZED_BYTES = 32_768  # 32 KiB cap on tool result payload


def serialize_tool_result(result: ToolResult) -> str:
    """Serialize a :class:`ToolResult` to a bounded JSON string for the LLM.

    Excludes internal-only fields, secrets, and Django models.  Dates and
    timezone-aware datetimes are serialized as ISO 8601 strings.

    Raises :class:`ValueError` if the result is non-serializable or
    exceeds the size cap.
    """
    payload: dict[str, Any] = {
        "status": result.status.value,
        "tool_name": result.tool_name,
    }

    if result.platform:
        payload["platform"] = result.platform

    if result.selected_account:
        payload["account"] = {
            "account_id": str(result.selected_account.account_id),
            "platform": result.selected_account.platform,
            "display_name": result.selected_account.display_name,
            "handle": result.selected_account.handle,
        }

    if result.period:
        payload["period"] = {
            "start": result.period.start.isoformat(),
            "end": result.period.end.isoformat(),
            "days": result.period.days,
        }

    if result.data_as_of:
        payload["data_as_of"] = result.data_as_of.isoformat()

    if result.last_synced_at:
        payload["last_synced_at"] = result.last_synced_at.isoformat()

    if result.is_stale:
        payload["is_stale"] = True

    if result.data:
        payload["data"] = result.data

    if result.warnings:
        payload["warnings"] = result.warnings

    if result.error_code:
        payload["error_code"] = result.error_code

    try:
        serialized = json.dumps(payload, sort_keys=True, default=_json_default)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"ToolResult is not JSON-serializable: {exc}"
        ) from exc

    if len(serialized.encode("utf-8")) > _MAX_SERIALIZED_BYTES:
        raise ValueError(
            f"Serialized ToolResult exceeds {_MAX_SERIALIZED_BYTES} bytes"
        )

    return serialized


def _json_default(obj: Any) -> Any:
    """JSON serializer for dates and datetimes.  Rejects unknown types."""
    from datetime import date, datetime

    if isinstance(obj, datetime):
        if obj.tzinfo is None:
            raise ValueError("naive datetime is not allowed in tool result")
        return obj.isoformat()
    if isinstance(obj, date):
        return obj.isoformat()
    raise TypeError(f"Object of type {type(obj).__name__} is not serializable")


# ---------------------------------------------------------------------------
# Loop detection
# ---------------------------------------------------------------------------


def _call_identity(tool_call: LLMToolCall) -> str:
    """Canonical identity for a tool call (name + sorted arguments).

    Same arguments with different JSON key order produce the same identity.
    """
    canonical_args = json.dumps(tool_call.arguments, sort_keys=True)
    return f"{tool_call.name}:{canonical_args}"


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------


class ToolOrchestrator:
    """Bounded tool-calling orchestrator.

    Fields
    ------
    router : LLMRouter
        LLM provider router (primary + fallback).
    registry : ToolRegistry
        Explicit allowlist of approved tools.
    limits : OrchestrationLimits
        Bounded execution limits.
    """

    def __init__(
        self,
        *,
        router: LLMRouter,
        registry: ToolRegistry,
        limits: OrchestrationLimits | None = None,
        monotonic_clock: Callable[[], float] | None = None,
    ) -> None:
        self.router = router
        self.registry = registry
        self.limits = limits or OrchestrationLimits()
        self._clock = monotonic_clock or time.monotonic

    def run(
        self,
        *,
        messages: list[LLMMessage],
        context: ToolContext,
        system_prompt: str = "",
        correlation_id: str = "",
    ) -> ToolOrchestrationResult:
        """Run the bounded tool-calling loop.

        Args:
            messages: Initial conversation messages (user turn).
            context: Application-created authorization context.
            system_prompt: Optional system prompt for the LLM.
            correlation_id: Optional correlation ID for logging.

        Returns:
            :class:`ToolOrchestrationResult` with final text, preserved
            tool results, and termination metadata.
        """
        limits = self.limits
        start_time = self._clock()

        tool_definitions = self.registry.to_llm_tool_definitions()
        conversation: list[LLMMessage] = list(messages)
        preserved_results: list[PreservedToolResult] = []
        total_calls = 0
        last_call_identities: list[str] = []

        for round_num in range(1, limits.max_rounds + 1):
            # --- Check timeout ---
            elapsed = self._clock() - start_time
            if elapsed >= limits.timeout_seconds:
                return ToolOrchestrationResult(
                    rounds=round_num - 1,
                    tool_call_count=total_calls,
                    tool_results=preserved_results,
                    termination_reason=TerminationReason.TIMEOUT,
                    error_code=ErrorCode.LLM_TEMPORARILY_UNAVAILABLE,
                    error_message="Orchestration timed out",
                )

            # --- Call the LLM ---
            request = LLMRequest(
                system_prompt=system_prompt,
                messages=conversation,
                tools=tool_definitions,
            )

            router_result = self.router.route(request)

            # --- Handle provider failure ---
            if router_result.response is None:
                return ToolOrchestrationResult(
                    rounds=round_num - 1,
                    tool_call_count=total_calls,
                    tool_results=preserved_results,
                    termination_reason=TerminationReason.PROVIDER_FAILURE,
                    error_code=ErrorCode.LLM_TEMPORARILY_UNAVAILABLE,
                    error_message=router_result.primary_error or "Provider failed",
                )

            response = router_result.response

            # --- No tool calls → final response ---
            if not response.tool_calls:
                return ToolOrchestrationResult(
                    final_text=response.text,
                    provider_name=response.provider_name,
                    used_fallback=router_result.used_fallback,
                    tool_results=preserved_results,
                    tool_call_count=total_calls,
                    rounds=round_num,
                    termination_reason=TerminationReason.FINAL_RESPONSE,
                )

            # --- Tool calls present ---
            calls = response.tool_calls

            # Check max calls per response
            if len(calls) > limits.max_calls_per_response:
                return ToolOrchestrationResult(
                    rounds=round_num,
                    tool_call_count=total_calls,
                    tool_results=preserved_results,
                    termination_reason=TerminationReason.MAX_CALLS_PER_RESPONSE,
                    error_code=ErrorCode.TOOL_EXECUTION_FAILED,
                    error_message=(
                        f"LLM requested {len(calls)} tool calls in one "
                        f"response, max is {limits.max_calls_per_response}"
                    ),
                )

            # Check total calls budget
            if total_calls + len(calls) > limits.max_total_calls:
                return ToolOrchestrationResult(
                    rounds=round_num,
                    tool_call_count=total_calls,
                    tool_results=preserved_results,
                    termination_reason=TerminationReason.MAX_CALLS_REACHED,
                    error_code=ErrorCode.TOOL_LOOP_LIMIT_REACHED,
                    error_message="Maximum total tool calls reached",
                )

            # --- Validate all calls before executing any ---
            validated: list[tuple[LLMToolCall, RegisteredTool, Any]] = []
            for tc in calls:
                # Unknown tool
                if not self.registry.contains(tc.name):
                    return ToolOrchestrationResult(
                        rounds=round_num,
                        tool_call_count=total_calls,
                        tool_results=preserved_results,
                        termination_reason=TerminationReason.TOOL_VALIDATION_FAILED,
                        error_code=ErrorCode.TOOL_NOT_FOUND,
                        error_message=f"Unknown tool: {tc.name!r}",
                    )

                tool = self.registry.get(tc.name)

                # Validate arguments using the registered schema
                try:
                    validated_args = tool.input_schema_type.model_validate(
                        tc.arguments
                    )
                except Exception as exc:
                    return ToolOrchestrationResult(
                        rounds=round_num,
                        tool_call_count=total_calls,
                        tool_results=preserved_results,
                        termination_reason=TerminationReason.TOOL_VALIDATION_FAILED,
                        error_code=ErrorCode.TOOL_ARGUMENT_VALIDATION_FAILED,
                        error_message=(
                            f"Tool {tc.name!r} argument validation failed: {exc}"
                        ),
                    )

                validated.append((tc, tool, validated_args))

            # --- Loop detection ---
            current_identities = [_call_identity(tc) for tc, _, _ in validated]
            consecutive_count = 0
            for identity in current_identities:
                if identity in last_call_identities:
                    consecutive_count += 1

            if consecutive_count >= limits.max_repeated_calls:
                return ToolOrchestrationResult(
                    rounds=round_num,
                    tool_call_count=total_calls,
                    tool_results=preserved_results,
                    termination_reason=TerminationReason.LOOP_DETECTED,
                    error_code=ErrorCode.TOOL_LOOP_LIMIT_REACHED,
                    error_message="Repeated identical tool calls detected",
                )

            last_call_identities = current_identities

            # --- Append assistant tool-call message ---
            conversation.append(
                LLMMessage(
                    role=LLMRole.ASSISTANT,
                    content=response.text,
                    tool_calls=calls,
                )
            )

            # --- Execute tools ---
            for tc, tool, validated_args in validated:
                # Check timeout before each execution
                elapsed = self._clock() - start_time
                if elapsed >= limits.timeout_seconds:
                    return ToolOrchestrationResult(
                        rounds=round_num,
                        tool_call_count=total_calls,
                        tool_results=preserved_results,
                        termination_reason=TerminationReason.TIMEOUT,
                        error_code=ErrorCode.LLM_TEMPORARILY_UNAVAILABLE,
                        error_message="Orchestration timed out during tool execution",
                    )

                try:
                    result = tool.executor(
                        arguments=validated_args,
                        context=context,
                    )
                except Exception as exc:
                    logger.exception(
                        "Tool %s execution failed: %s", tc.name, exc
                    )
                    return ToolOrchestrationResult(
                        rounds=round_num,
                        tool_call_count=total_calls + 1,
                        tool_results=preserved_results,
                        termination_reason=TerminationReason.TOOL_EXECUTION_FAILED,
                        error_code=ErrorCode.TOOL_EXECUTION_FAILED,
                        error_message=f"Tool {tc.name!r} execution failed",
                    )

                # Verify executor returned a ToolResult
                if not isinstance(result, ToolResult):
                    return ToolOrchestrationResult(
                        rounds=round_num,
                        tool_call_count=total_calls + 1,
                        tool_results=preserved_results,
                        termination_reason=TerminationReason.TOOL_EXECUTION_FAILED,
                        error_code=ErrorCode.TOOL_EXECUTION_FAILED,
                        error_message=(
                            f"Tool {tc.name!r} executor returned "
                            f"{type(result).__name__}, expected ToolResult"
                        ),
                    )

                total_calls += 1

                # Preserve successful results
                if result.status == ToolResultStatus.SUCCESS:
                    preserved_results.append(
                        PreservedToolResult(
                            tool_name=tc.name,
                            tool_call_id=tc.id,
                            result=result,
                            round_number=round_num,
                        )
                    )

                # Serialize and append tool-result message
                try:
                    serialized = serialize_tool_result(result)
                    is_error = result.status == ToolResultStatus.FAILED
                except ValueError:
                    serialized = json.dumps({
                        "status": "failed",
                        "tool_name": tc.name,
                        "error_code": ErrorCode.TOOL_EXECUTION_FAILED.value,
                    })
                    is_error = True

                conversation.append(
                    LLMMessage(
                        role=LLMRole.USER,
                        tool_result=LLMToolResultContent(
                            tool_call_id=tc.id,
                            content=serialized,
                            is_error=is_error,
                        ),
                    )
                )

        # --- Max rounds reached ---
        return ToolOrchestrationResult(
            rounds=limits.max_rounds,
            tool_call_count=total_calls,
            tool_results=preserved_results,
            termination_reason=TerminationReason.MAX_ROUNDS_REACHED,
            error_code=ErrorCode.TOOL_LOOP_LIMIT_REACHED,
            error_message="Maximum orchestration rounds reached",
        )
