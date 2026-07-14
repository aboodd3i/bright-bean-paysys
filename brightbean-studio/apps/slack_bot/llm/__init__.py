"""LLM provider layer for the Slack analytics bot.

Public API:

* :class:`LLMProvider` — protocol every adapter satisfies
* :class:`LLMRequest` / :class:`LLMResponse` — normalized contracts
* :class:`ClaudeProvider` — Anthropic Messages API adapter
* :class:`GLMProvider` — Z.AI / Zhipu BigModel adapter
* :class:`LLMRouter` — primary/fallback router
* :func:`create_default_router` — settings-driven factory
"""

from __future__ import annotations

from .base import (
    LLMMessage,
    LLMProvider,
    LLMRequest,
    LLMResponse,
    LLMRole,
    LLMStopReason,
    LLMTokenUsage,
    LLMToolCall,
    LLMToolCallContent,
    LLMToolDefinition,
    LLMToolResultContent,
)
from .claude_client import ClaudeProvider
from .config import (
    ProviderConfig,
    get_claude_config,
    get_fallback_provider_name,
    get_glm_config,
    get_primary_provider_name,
)
from .exceptions import (
    LLMAuthError,
    LLMBadRequestError,
    LLMProviderError,
    LLMRateLimitError,
    LLMResponseParseError,
    LLMServerError,
    LLMTimeoutError,
    LLMTransportError,
)
from .glm_client import GLMProvider
from .router import LLMRouter, LLMRouterResult, create_default_router

__all__ = [
    # Base contracts
    "LLMMessage",
    "LLMProvider",
    "LLMRequest",
    "LLMResponse",
    "LLMRole",
    "LLMStopReason",
    "LLMTokenUsage",
    "LLMToolCall",
    "LLMToolCallContent",
    "LLMToolDefinition",
    "LLMToolResultContent",
    # Config
    "ProviderConfig",
    "get_claude_config",
    "get_fallback_provider_name",
    "get_glm_config",
    "get_primary_provider_name",
    # Exceptions
    "LLMAuthError",
    "LLMBadRequestError",
    "LLMProviderError",
    "LLMRateLimitError",
    "LLMResponseParseError",
    "LLMServerError",
    "LLMTimeoutError",
    "LLMTransportError",
    # Providers
    "ClaudeProvider",
    "GLMProvider",
    # Router
    "LLMRouter",
    "LLMRouterResult",
    "create_default_router",
]
