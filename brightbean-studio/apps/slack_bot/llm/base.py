"""Provider-neutral contracts for the LLM layer.

These dataclasses define the **normalized** request and response that
every provider adapter must accept and produce.  Provider-specific
formats (Anthropic Messages API, Z.AI/OpenAI-compatible chat completions)
live entirely inside their respective adapter modules.

Conventions
-----------
* ``@dataclass(frozen=True)`` for immutability — matches the pattern in
  :mod:`apps.slack_bot.contracts`.
* ``enum.StrEnum`` for role and stop-reason enums.
* No Slack-specific types, no Block Kit, no tool execution — this module
  is pure data.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol

# ---------------------------------------------------------------------------
# Message role
# ---------------------------------------------------------------------------


class LLMRole(StrEnum):
    """Conversational role for a chat message."""

    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"


# ---------------------------------------------------------------------------
# Tool call requested by the LLM
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LLMToolCall:
    """A tool call requested by the LLM in its response.

    Fields
    ------
    id : str
        Provider-assigned call ID (used to match the result in a
        subsequent turn).
    name : str
        Name of the tool to call.
    arguments : dict[str, Any]
        Parsed JSON arguments for the tool call.
    """

    id: str
    name: str
    arguments: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.id:
            raise ValueError("LLMToolCall.id must not be empty")
        if not self.name:
            raise ValueError("LLMToolCall.name must not be empty")


# ---------------------------------------------------------------------------
# Tool-result content block (provider-neutral)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LLMToolResultContent:
    """A tool-result content block carried inside an :class:`LLMMessage`.

    When the orchestrator executes a tool and sends the result back to
    the LLM, it creates an ``LLMMessage`` with ``role=USER`` and
    ``tool_result`` set to this object.  Each adapter translates this
    into the provider-native tool-result format.

    Fields
    ------
    tool_call_id : str
        ID of the tool call this result corresponds to.
    content : str
        Serialized tool result (JSON string).  Bounded and safe — no
        secrets, no Django models, no unrestricted ORM objects.
    is_error : bool
        When ``True`` the result represents a tool execution failure.
    """

    tool_call_id: str
    content: str
    is_error: bool = False

    def __post_init__(self) -> None:
        if not self.tool_call_id:
            raise ValueError("LLMToolResultContent.tool_call_id must not be empty")


# ---------------------------------------------------------------------------
# Tool-call content block (provider-neutral)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LLMToolCallContent:
    """A tool-call content block carried inside an :class:`LLMMessage`.

    When the orchestrator appends the assistant's tool-call turn to the
    conversation, it creates an ``LLMMessage`` with ``role=ASSISTANT``
    and ``tool_calls`` set to this list.  Each adapter translates this
    into the provider-native assistant tool-call format.

    Fields
    ------
    tool_calls : list[LLMToolCall]
        Tool calls the assistant requested in this turn.
    """

    tool_calls: list[LLMToolCall]


# ---------------------------------------------------------------------------
# Normalized message
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LLMMessage:
    """A single chat message in the normalized conversation.

    A message is one of three shapes:

    * **Text** — ``content`` is a non-empty string, ``tool_calls`` and
      ``tool_result`` are ``None``.  Used for user/assistant text turns.
    * **Assistant tool-call** — ``role=ASSISTANT``, ``tool_calls`` is
      set, ``content`` may be empty (the model only requested tools).
    * **Tool-result** — ``role=USER``, ``tool_result`` is set,
      ``content`` may be empty.

    Fields
    ------
    role : LLMRole
        Who produced this message.
    content : str
        The message text.  May be empty when the message is purely a
        tool-call or tool-result turn.
    tool_calls : list[LLMToolCall] | None
        Tool calls requested by the assistant in this turn.
    tool_result : LLMToolResultContent | None
        Tool result sent back to the LLM in this turn.
    """

    role: LLMRole
    content: str = ""
    tool_calls: list[LLMToolCall] | None = None
    tool_result: LLMToolResultContent | None = None

    def __post_init__(self) -> None:
        has_text = bool(self.content)
        has_tool_calls = self.tool_calls is not None
        has_tool_result = self.tool_result is not None

        if not has_text and not has_tool_calls and not has_tool_result:
            raise ValueError(
                "LLMMessage must have content, tool_calls, or tool_result"
            )


# ---------------------------------------------------------------------------
# Tool definition (provider-neutral)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LLMToolDefinition:
    """A tool the LLM may request to call.

    The ``input_schema`` is a JSON-schema dict — the same shape used by
    both the Anthropic Messages API (``input_schema``) and the
    OpenAI-compatible API (``parameters``).

    Fields
    ------
    name : str
        Tool name as the LLM will reference it.
    description : str
        Human-readable description for the LLM.
    input_schema : dict[str, Any]
        JSON schema for the tool's input parameters.
    """

    name: str
    description: str
    input_schema: dict[str, Any]

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("LLMToolDefinition.name must not be empty")


# ---------------------------------------------------------------------------
# Stop reason
# ---------------------------------------------------------------------------


class LLMStopReason(StrEnum):
    """Why the LLM stopped generating.

    Normalized across providers:

    * ``END_TURN`` — the model finished naturally.
    * ``MAX_TOKENS`` — the response was truncated by the token limit.
    * ``STOP_SEQUENCE`` — a configured stop sequence was hit.
    * ``TOOL_USE`` — the model requested one or more tool calls.
    """

    END_TURN = "end_turn"
    MAX_TOKENS = "max_tokens"
    STOP_SEQUENCE = "stop_sequence"
    TOOL_USE = "tool_use"


# ---------------------------------------------------------------------------
# Token usage
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LLMTokenUsage:
    """Token usage reported by the provider.

    Fields
    ------
    input_tokens : int
        Tokens consumed by the prompt.
    output_tokens : int
        Tokens generated by the model.
    """

    input_tokens: int = 0
    output_tokens: int = 0

    def __post_init__(self) -> None:
        if self.input_tokens < 0:
            raise ValueError("input_tokens must be non-negative")
        if self.output_tokens < 0:
            raise ValueError("output_tokens must be non-negative")


# ---------------------------------------------------------------------------
# Normalized LLM response
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LLMResponse:
    """Normalized response from any LLM provider.

    Fields
    ------
    text : str
        Concatenated text content from the response.  May be empty when
        the model only requested tool calls.
    tool_calls : list[LLMToolCall]
        Tool calls requested by the model (may be empty).
    stop_reason : LLMStopReason
        Why the model stopped.
    usage : LLMTokenUsage
        Token counts for billing/observability.
    provider_name : str
        Name of the provider that produced this response.
    raw : dict[str, Any] | None
        The raw provider response dict, kept for debugging and logging.
        Must not contain secrets.
    """

    text: str
    tool_calls: list[LLMToolCall] = field(default_factory=list)
    stop_reason: LLMStopReason = LLMStopReason.END_TURN
    usage: LLMTokenUsage = field(default_factory=LLMTokenUsage)
    provider_name: str = ""
    raw: dict[str, Any] | None = None


# ---------------------------------------------------------------------------
# Normalized LLM request
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LLMRequest:
    """Provider-neutral request to an LLM.

    Fields
    ------
    system_prompt : str
        System instructions.  May be empty.
    messages : list[LLMMessage]
        Conversation turns (user/assistant alternating).
    tools : list[LLMToolDefinition]
        Tools the model may call.  May be empty.
    max_output_tokens : int
        Maximum tokens to generate.
    temperature : float
        Sampling temperature (0.0–1.0).
    stop_sequences : list[str]
        Custom stop sequences.
    """

    system_prompt: str
    messages: list[LLMMessage]
    tools: list[LLMToolDefinition] = field(default_factory=list)
    max_output_tokens: int = 2000
    temperature: float = 1.0
    stop_sequences: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.messages:
            raise ValueError("LLMRequest.messages must not be empty")
        if self.max_output_tokens < 1:
            raise ValueError("max_output_tokens must be at least 1")
        if not 0.0 <= self.temperature <= 1.0:
            raise ValueError("temperature must be between 0.0 and 1.0")


# ---------------------------------------------------------------------------
# Provider protocol
# ---------------------------------------------------------------------------


class LLMProvider(Protocol):
    """Protocol every provider adapter must satisfy.

    Implementations receive a normalized :class:`LLMRequest` and return a
    normalized :class:`LLMResponse`.  Provider-specific request/response
    mapping happens inside the adapter.

    The ``http_post`` parameter is an injectable callable with the
    signature ``http_post(url, *, json_body, headers, timeout) ->
    response`` where ``response`` has ``.status_code`` (int),
    ``.json()`` (dict), and ``.text`` (str).  This mirrors the pattern
    in :mod:`apps.slack_bot.delivery`.
    """

    name: str

    def complete(
        self,
        request: LLMRequest,
        *,
        http_post: Callable[..., Any] | None = None,
    ) -> LLMResponse: ...
