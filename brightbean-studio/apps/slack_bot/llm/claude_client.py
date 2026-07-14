"""Claude (Anthropic Messages API) provider adapter.

Translates normalized :class:`~apps.slack_bot.llm.base.LLMRequest` into
the Anthropic Messages API request format and parses the response back
into :class:`~apps.slack_bot.llm.base.LLMResponse`.

API reference: https://docs.anthropic.com/en/api/messages

* Endpoint: ``POST https://api.anthropic.com/v1/messages``
* Auth: ``x-api-key`` header + ``anthropic-version`` header
* Request body: ``model``, ``max_tokens``, ``system``, ``messages``,
  ``tools``, ``temperature``, ``stop_sequences``
* Response: ``content`` (array of text/tool_use blocks),
  ``stop_reason``, ``usage``

No Anthropic SDK is used — only ``httpx`` (already a project dependency).
All network calls are injectable via ``http_post`` for testing, matching
the pattern in :mod:`apps.slack_bot.delivery`.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from .base import (
    LLMRequest,
    LLMResponse,
    LLMStopReason,
    LLMTokenUsage,
    LLMToolCall,
)
from .config import ANTHROPIC_API_VERSION, get_claude_config
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

PROVIDER_NAME = "claude"

# Anthropic stop_reason → normalized LLMStopReason
_STOP_REASON_MAP: dict[str, LLMStopReason] = {
    "end_turn": LLMStopReason.END_TURN,
    "max_tokens": LLMStopReason.MAX_TOKENS,
    "stop_sequence": LLMStopReason.STOP_SEQUENCE,
    "tool_use": LLMStopReason.TOOL_USE,
}


# ---------------------------------------------------------------------------
# Request translation
# ---------------------------------------------------------------------------


def _build_anthropic_message(msg: LLMMessage) -> dict[str, Any]:
    """Translate a single :class:`LLMMessage` into Anthropic message format.

    Handles three shapes:
    * Text — ``{"role": ..., "content": "text"}``
    * Assistant tool-call — ``{"role": "assistant", "content": [text?, tool_use...]}``
    * Tool-result — ``{"role": "user", "content": [{"type": "tool_result", ...}]}``
    """
    # Tool-result message
    if msg.tool_result is not None:
        return {
            "role": msg.role.value,
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": msg.tool_result.tool_call_id,
                    "content": msg.tool_result.content,
                    "is_error": msg.tool_result.is_error,
                }
            ],
        }

    # Assistant tool-call message
    if msg.tool_calls is not None:
        content: list[dict[str, Any]] = []
        if msg.content:
            content.append({"type": "text", "text": msg.content})
        for tc in msg.tool_calls:
            content.append(
                {
                    "type": "tool_use",
                    "id": tc.id,
                    "name": tc.name,
                    "input": tc.arguments,
                }
            )
        return {"role": msg.role.value, "content": content}

    # Plain text message
    return {"role": msg.role.value, "content": msg.content}


def _build_anthropic_request(request: LLMRequest, model: str) -> dict[str, Any]:
    """Translate :class:`LLMRequest` into the Anthropic Messages API body."""
    body: dict[str, Any] = {
        "model": model,
        "max_tokens": request.max_output_tokens,
        "messages": [
            _build_anthropic_message(msg) for msg in request.messages
        ],
    }

    if request.system_prompt:
        body["system"] = request.system_prompt

    if request.tools:
        body["tools"] = [
            {
                "name": tool.name,
                "description": tool.description,
                "input_schema": tool.input_schema,
            }
            for tool in request.tools
        ]

    if request.temperature != 1.0:
        body["temperature"] = request.temperature

    if request.stop_sequences:
        body["stop_sequences"] = request.stop_sequences

    return body


def _build_headers(api_key: str) -> dict[str, str]:
    """Build Anthropic request headers."""
    return {
        "x-api-key": api_key,
        "anthropic-version": ANTHROPIC_API_VERSION,
        "content-type": "application/json",
    }


# ---------------------------------------------------------------------------
# Response parsing
# ---------------------------------------------------------------------------


def _parse_content_blocks(
    content: list[dict[str, Any]],
) -> tuple[str, list[LLMToolCall]]:
    """Extract text and tool calls from Anthropic content blocks.

    Returns ``(text, tool_calls)``.
    """
    text_parts: list[str] = []
    tool_calls: list[LLMToolCall] = []

    for block in content:
        if not isinstance(block, dict):
            continue
        block_type = block.get("type", "")
        if block_type == "text":
            text_parts.append(block.get("text", ""))
        elif block_type == "tool_use":
            tool_calls.append(
                LLMToolCall(
                    id=block.get("id", ""),
                    name=block.get("name", ""),
                    arguments=block.get("input", {}) if isinstance(
                        block.get("input"), dict
                    ) else {},
                )
            )

    return "".join(text_parts), tool_calls


def _parse_anthropic_response(
    raw: dict[str, Any],
) -> LLMResponse:
    """Parse an Anthropic Messages API response into :class:`LLMResponse`."""
    content = raw.get("content", [])
    if not isinstance(content, list):
        raise LLMResponseParseError(
            "Anthropic response 'content' is not a list",
            provider_name=PROVIDER_NAME,
        )

    text, tool_calls = _parse_content_blocks(content)

    stop_reason_raw = raw.get("stop_reason", "end_turn")
    stop_reason = _STOP_REASON_MAP.get(
        stop_reason_raw, LLMStopReason.END_TURN
    )

    usage_raw = raw.get("usage", {})
    if not isinstance(usage_raw, dict):
        usage_raw = {}
    usage = LLMTokenUsage(
        input_tokens=int(usage_raw.get("input_tokens", 0)),
        output_tokens=int(usage_raw.get("output_tokens", 0)),
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
            f"Anthropic auth failed ({status_code})",
            provider_name=PROVIDER_NAME,
        )
    if status_code == 400:
        return LLMBadRequestError(
            f"Anthropic bad request: {body_text[:500]}",
            provider_name=PROVIDER_NAME,
        )
    if status_code == 429:
        return LLMRateLimitError(provider_name=PROVIDER_NAME)
    if 500 <= status_code < 600:
        return LLMServerError(
            f"Anthropic server error: {body_text[:500]}",
            provider_name=PROVIDER_NAME,
            status_code=status_code,
        )
    # Unexpected status — treat as permanent.
    return LLMBadRequestError(
        f"Anthropic unexpected status {status_code}: {body_text[:500]}",
        provider_name=PROVIDER_NAME,
    )


# ---------------------------------------------------------------------------
# Provider class
# ---------------------------------------------------------------------------


class ClaudeProvider:
    """Anthropic Messages API adapter.

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
                :func:`get_claude_config`.
        """
        self._config = config

    def _resolve_config(self) -> Any:
        if self._config is not None:
            return self._config
        return get_claude_config()

    def complete(
        self,
        request: LLMRequest,
        *,
        http_post: Callable[..., Any] | None = None,
    ) -> LLMResponse:
        """Send *request* to the Anthropic Messages API.

        Args:
            request: Normalized LLM request.
            http_post: Injectable HTTP POST callable for testing.
                Signature: ``http_post(url, json_body=..., headers=...,
                timeout=...) -> response``.

        Returns:
            Normalized :class:`LLMResponse`.

        Raises:
            :class:`~apps.slack_bot.llm.exceptions.LLMProviderError`
            subclass on failure.
        """
        cfg = self._resolve_config()

        if not cfg.api_key:
            raise LLMAuthError(
                "ANTHROPIC_API_KEY is not configured",
                provider_name=PROVIDER_NAME,
            )

        body = _build_anthropic_request(request, cfg.model)
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
            # httpx raises TimeoutException and TransportError.
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
                f"Failed to parse Anthropic response JSON: {exc}",
                provider_name=PROVIDER_NAME,
            ) from exc

        if not isinstance(raw, dict):
            raise LLMResponseParseError(
                "Anthropic response is not a JSON object",
                provider_name=PROVIDER_NAME,
            )

        return _parse_anthropic_response(raw)
