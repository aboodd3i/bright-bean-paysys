"""GLM (Z.AI / Zhipu BigModel) provider adapter.

Z.AI exposes an OpenAI-compatible chat completions API.  This adapter
translates normalized :class:`~apps.slack_bot.llm.base.LLMRequest` into
that format and parses the response back into
:class:`~apps.slack_bot.llm.base.LLMResponse`.

API reference: https://open.bigmodel.cn/dev/api#glm-4

* Endpoint: ``POST https://open.bigmodel.cn/api/paas/v4/chat/completions``
* Auth: ``Authorization: Bearer <key>`` header
* Request body: ``model``, ``messages`` (with ``system`` role inline),
  ``tools``, ``tool_choice``, ``temperature``, ``max_tokens``,
  ``stop``
* Response: ``choices[0].message.content`` (text),
  ``choices[0].message.tool_calls`` (tool calls),
  ``choices[0].finish_reason``, ``usage``

No Z.AI SDK is used — only ``httpx`` (already a project dependency).
All network calls are injectable via ``http_post`` for testing.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from .base import (
    LLMMessage,
    LLMRequest,
    LLMResponse,
    LLMStopReason,
    LLMTokenUsage,
    LLMToolCall,
)
from .config import get_glm_config
from .exceptions import (
    LLMAuthError,
    LLMBadRequestError,
    LLMRateLimitError,
    LLMResponseParseError,
    LLMServerError,
    LLMTimeoutError,
    LLMTransportError,
)

if TYPE_CHECKING:
    import httpx

logger = logging.getLogger(__name__)

PROVIDER_NAME = "glm"

# OpenAI-compatible finish_reason → normalized LLMStopReason
_FINISH_REASON_MAP: dict[str, LLMStopReason] = {
    "stop": LLMStopReason.END_TURN,
    "length": LLMStopReason.MAX_TOKENS,
    "tool_calls": LLMStopReason.TOOL_USE,
}


# ---------------------------------------------------------------------------
# Request translation
# ---------------------------------------------------------------------------


def _build_zai_message(msg: LLMMessage) -> dict[str, Any]:
    """Translate a single :class:`LLMMessage` into Z.AI/OpenAI message format.

    Handles three shapes:
    * Text — ``{"role": ..., "content": "text"}``
    * Assistant tool-call — ``{"role": "assistant", "content": "text",
      "tool_calls": [...]}``
    * Tool-result — ``{"role": "tool", "tool_call_id": "...,
      "content": "..."}``
    """
    # Tool-result message → OpenAI "tool" role
    if msg.tool_result is not None:
        return {
            "role": "tool",
            "tool_call_id": msg.tool_result.tool_call_id,
            "content": msg.tool_result.content,
        }

    # Assistant tool-call message
    if msg.tool_calls is not None:
        message: dict[str, Any] = {
            "role": msg.role.value,
            "content": msg.content,
        }
        message["tool_calls"] = [
            {
                "id": tc.id,
                "type": "function",
                "function": {
                    "name": tc.name,
                    "arguments": json.dumps(tc.arguments, sort_keys=True),
                },
            }
            for tc in msg.tool_calls
        ]
        return message

    # Plain text message
    return {"role": msg.role.value, "content": msg.content}


def _build_zai_messages(request: LLMRequest) -> list[dict[str, Any]]:
    """Build the ``messages`` array for the Z.AI request.

    Z.AI uses the OpenAI convention where the system prompt is the first
    message with ``role: "system"``.
    """
    messages: list[dict[str, Any]] = []

    if request.system_prompt:
        messages.append({"role": "system", "content": request.system_prompt})

    for msg in request.messages:
        messages.append(_build_zai_message(msg))

    return messages


def _build_zai_tools(tools: list) -> list[dict[str, Any]]:
    """Translate tool definitions into the OpenAI function-calling format."""
    result: list[dict[str, Any]] = []
    for tool in tools:
        result.append(
            {
                "type": "function",
                "function": {
                    "name": tool.name,
                    "description": tool.description,
                    "parameters": tool.input_schema,
                },
            }
        )
    return result


def _build_zai_request(request: LLMRequest, model: str) -> dict[str, Any]:
    """Translate :class:`LLMRequest` into the Z.AI chat completions body."""
    body: dict[str, Any] = {
        "model": model,
        "messages": _build_zai_messages(request),
        "max_tokens": request.max_output_tokens,
    }

    if request.tools:
        body["tools"] = _build_zai_tools(request.tools)

    if request.temperature != 1.0:
        body["temperature"] = request.temperature

    if request.stop_sequences:
        body["stop"] = request.stop_sequences

    return body


def _build_headers(api_key: str) -> dict[str, str]:
    """Build Z.AI request headers."""
    return {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }


# ---------------------------------------------------------------------------
# Response parsing
# ---------------------------------------------------------------------------


def _parse_tool_calls(
    tool_calls_raw: list[dict[str, Any]],
) -> list[LLMToolCall]:
    """Parse OpenAI-style tool_calls from the response."""
    result: list[LLMToolCall] = []
    for tc in tool_calls_raw:
        if not isinstance(tc, dict):
            continue
        function = tc.get("function", {})
        if not isinstance(function, dict):
            function = {}
        arguments_raw = function.get("arguments", "{}")
        if isinstance(arguments_raw, dict):
            arguments = arguments_raw
        else:
            try:
                arguments = json.loads(arguments_raw) if arguments_raw else {}
            except (json.JSONDecodeError, TypeError):
                arguments = {}
        result.append(
            LLMToolCall(
                id=tc.get("id", ""),
                name=function.get("name", ""),
                arguments=arguments,
            )
        )
    return result


def _parse_zai_response(raw: dict[str, Any]) -> LLMResponse:
    """Parse a Z.AI chat completions response into :class:`LLMResponse`."""
    choices = raw.get("choices", [])
    if not isinstance(choices, list) or not choices:
        raise LLMResponseParseError(
            "Z.AI response has no 'choices' array",
            provider_name=PROVIDER_NAME,
        )

    choice = choices[0]
    if not isinstance(choice, dict):
        raise LLMResponseParseError(
            "Z.AI response choice[0] is not an object",
            provider_name=PROVIDER_NAME,
        )

    message = choice.get("message", {})
    if not isinstance(message, dict):
        message = {}

    text = message.get("content", "") or ""

    tool_calls: list[LLMToolCall] = []
    tool_calls_raw = message.get("tool_calls", [])
    if isinstance(tool_calls_raw, list):
        tool_calls = _parse_tool_calls(tool_calls_raw)

    finish_reason_raw = choice.get("finish_reason", "stop")
    stop_reason = _FINISH_REASON_MAP.get(
        finish_reason_raw, LLMStopReason.END_TURN
    )

    usage_raw = raw.get("usage", {})
    if not isinstance(usage_raw, dict):
        usage_raw = {}
    usage = LLMTokenUsage(
        input_tokens=int(usage_raw.get("prompt_tokens", 0)),
        output_tokens=int(usage_raw.get("completion_tokens", 0)),
    )

    return LLMResponse(
        text=text,
        tool_calls=tool_calls,
        stop_reason=stop_reason,
        usage=usage,
        provider_name=PROVIDER_NAME,
        raw=raw,
    )


# ---------------------------------------------------------------------------
# Default HTTP post
# ---------------------------------------------------------------------------


def _default_http_post(
    url: str,
    *,
    json_body: dict[str, Any],
    headers: dict[str, str],
    timeout: float,
) -> httpx.Response:
    """Default HTTP POST using httpx.  Imported lazily."""
    import httpx

    return httpx.post(url, json=json_body, headers=headers, timeout=timeout)


# ---------------------------------------------------------------------------
# Error classification
# ---------------------------------------------------------------------------


def _classify_http_error(
    status_code: int,
    body_text: str,
) -> Exception:
    """Map an HTTP error status to the appropriate exception."""
    if status_code == 401 or status_code == 403:
        return LLMAuthError(
            f"Z.AI auth failed ({status_code})",
            provider_name=PROVIDER_NAME,
        )
    if status_code == 400:
        return LLMBadRequestError(
            f"Z.AI bad request: {body_text[:500]}",
            provider_name=PROVIDER_NAME,
        )
    if status_code == 429:
        return LLMRateLimitError(provider_name=PROVIDER_NAME)
    if 500 <= status_code < 600:
        return LLMServerError(
            f"Z.AI server error: {body_text[:500]}",
            provider_name=PROVIDER_NAME,
            status_code=status_code,
        )
    return LLMBadRequestError(
        f"Z.AI unexpected status {status_code}: {body_text[:500]}",
        provider_name=PROVIDER_NAME,
    )


# ---------------------------------------------------------------------------
# Provider class
# ---------------------------------------------------------------------------


class GLMProvider:
    """Z.AI / Zhipu BigModel chat completions adapter.

    Implements the :class:`~apps.slack_bot.llm.base.LLMProvider` protocol.
    """

    name = PROVIDER_NAME

    def __init__(
        self,
        config: Any | None = None,
    ) -> None:
        """Initialise the provider.

        Args:
            config: Optional :class:`ProviderConfig`.  When ``None``,
                reads from settings/env at call time via
                :func:`get_glm_config`.
        """
        self._config = config

    def _resolve_config(self) -> Any:
        if self._config is not None:
            return self._config
        return get_glm_config()

    def complete(
        self,
        request: LLMRequest,
        *,
        http_post: Callable[..., Any] | None = None,
    ) -> LLMResponse:
        """Send *request* to the Z.AI chat completions API.

        Args:
            request: Normalized LLM request.
            http_post: Injectable HTTP POST callable for testing.

        Returns:
            Normalized :class:`LLMResponse`.

        Raises:
            :class:`~apps.slack_bot.llm.exceptions.LLMProviderError`
            subclass on failure.
        """
        cfg = self._resolve_config()

        if not cfg.api_key:
            raise LLMAuthError(
                "ZAI_API_KEY is not configured",
                provider_name=PROVIDER_NAME,
            )

        body = _build_zai_request(request, cfg.model)
        headers = _build_headers(cfg.api_key)
        post_fn = http_post if http_post is not None else _default_http_post

        try:
            response = post_fn(
                cfg.base_url,
                json_body=body,
                headers=headers,
                timeout=cfg.timeout_seconds,
            )
        except Exception as exc:
            exc_name = type(exc).__name__
            if "Timeout" in exc_name:
                raise LLMTimeoutError(
                    str(exc), provider_name=PROVIDER_NAME
                ) from exc
            raise LLMTransportError(
                str(exc), provider_name=PROVIDER_NAME
            ) from exc

        status_code = getattr(response, "status_code", 0)

        if status_code != 200:
            body_text = getattr(response, "text", "")
            raise _classify_http_error(status_code, body_text)

        try:
            raw = response.json()
        except Exception as exc:
            raise LLMResponseParseError(
                f"Failed to parse Z.AI response JSON: {exc}",
                provider_name=PROVIDER_NAME,
            ) from exc

        if not isinstance(raw, dict):
            raise LLMResponseParseError(
                "Z.AI response is not a JSON object",
                provider_name=PROVIDER_NAME,
            )

        return _parse_zai_response(raw)
