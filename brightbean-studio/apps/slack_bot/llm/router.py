"""Fallback router for the LLM provider layer.

Tries the primary provider first.  When the primary raises a
**retryable** error (timeout, 429, 5xx, transport), the router falls
back to the next provider.  When the primary raises a **permanent**
error (auth, bad request, parse failure), the router re-raises
immediately — retrying on a different provider will not help and may
mask a real bug.

Fallback policy matches :file:`docs/Bot_Workflow.md` §14.2:

* Fallback on: timeout, network failure, HTTP 429, provider 5xx.
* No fallback for: auth failure, invalid request, parse failure.

The router is deliberately simple — no retries within a single provider,
no exponential backoff.  Those concerns belong in the background-task
layer (Phase 3B+).
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from .base import LLMRequest, LLMResponse
from .exceptions import LLMProviderError

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Router result
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LLMRouterResult:
    """Outcome of a routed LLM call.

    Fields
    ------
    response : LLMResponse | None
        The successful response, or ``None`` when all providers failed.
    provider_name : str
        Name of the provider that succeeded.  Empty when all failed.
    used_fallback : bool
        True when the fallback provider was used.
    primary_error : str | None
        Error message from the primary provider, if it failed.
    fallback_error : str | None
        Error message from the fallback provider, if it was tried and
        also failed.
    """

    response: LLMResponse | None = None
    provider_name: str = ""
    used_fallback: bool = False
    primary_error: str | None = None
    fallback_error: str | None = None
    errors: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------


class LLMRouter:
    """Primary/fallback LLM provider router.

    Fields
    ------
    primary : LLMProvider
        Primary provider (typically Claude).
    fallback : LLMProvider | None
        Fallback provider (typically GLM).  ``None`` disables fallback.
    """

    def __init__(
        self,
        primary: Any,
        fallback: Any | None = None,
    ) -> None:
        self.primary = primary
        self.fallback = fallback

    def route(
        self,
        request: LLMRequest,
        *,
        http_post: Callable[..., Any] | None = None,
    ) -> LLMRouterResult:
        """Try the primary provider, fall back on retryable errors.

        Args:
            request: Normalized LLM request.
            http_post: Injectable HTTP POST callable for testing.

        Returns:
            :class:`LLMRouterResult` with either a successful response
            or error details from all attempted providers.
        """
        errors: list[str] = []

        # --- Try primary ---
        try:
            response = self.primary.complete(request, http_post=http_post)
            return LLMRouterResult(
                response=response,
                provider_name=self.primary.name,
                used_fallback=False,
            )
        except LLMProviderError as exc:
            errors.append(f"{exc.provider_name}: {exc}")
            primary_error = str(exc)

            if not exc.is_retryable:
                # Permanent error — do not fall back.
                logger.warning(
                    "LLM primary %s failed permanently: %s",
                    exc.provider_name,
                    exc,
                )
                return LLMRouterResult(
                    response=None,
                    provider_name="",
                    used_fallback=False,
                    primary_error=primary_error,
                    errors=errors,
                )

            logger.info(
                "LLM primary %s failed (retryable), trying fallback: %s",
                exc.provider_name,
                exc,
            )
        except Exception as exc:
            # Unexpected non-LLM exception — treat as retryable transport.
            errors.append(f"{getattr(self.primary, 'name', 'primary')}: {exc}")
            primary_error = str(exc)
            logger.exception(
                "LLM primary raised unexpected exception, trying fallback"
            )

        # --- Try fallback ---
        if self.fallback is None:
            return LLMRouterResult(
                response=None,
                provider_name="",
                used_fallback=False,
                primary_error=primary_error,
                errors=errors,
            )

        try:
            response = self.fallback.complete(request, http_post=http_post)
            return LLMRouterResult(
                response=response,
                provider_name=self.fallback.name,
                used_fallback=True,
                primary_error=primary_error,
                errors=errors,
            )
        except LLMProviderError as exc:
            errors.append(f"{exc.provider_name}: {exc}")
            logger.warning(
                "LLM fallback %s also failed: %s",
                exc.provider_name,
                exc,
            )
            return LLMRouterResult(
                response=None,
                provider_name="",
                used_fallback=True,
                primary_error=primary_error,
                fallback_error=str(exc),
                errors=errors,
            )
        except Exception as exc:
            errors.append(f"{getattr(self.fallback, 'name', 'fallback')}: {exc}")
            logger.exception("LLM fallback raised unexpected exception")
            return LLMRouterResult(
                response=None,
                provider_name="",
                used_fallback=True,
                primary_error=primary_error,
                fallback_error=str(exc),
                errors=errors,
            )


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def create_default_router() -> LLMRouter:
    """Create an :class:`LLMRouter` from settings.

    Reads ``LLM_PRIMARY`` and ``LLM_FALLBACK`` to decide which providers
    to wire.  Defaults to GLM primary + GLM fallback (dual-Z.AI mode).

    When both primary and fallback are ``"glm"``, two separate
    :class:`GLMProvider` instances are created — the primary uses
    :func:`get_glm_config` (best model) and the fallback uses
    :func:`get_glm_fallback_config` (next-best model).
    """
    from .claude_client import ClaudeProvider
    from .config import (
        get_fallback_provider_name,
        get_glm_config,
        get_glm_fallback_config,
        get_primary_provider_name,
    )
    from .glm_client import GLMProvider

    primary_name = get_primary_provider_name()
    fallback_name = get_fallback_provider_name()

    # --- Build primary provider ---
    if primary_name == "glm":
        primary = GLMProvider(config=get_glm_config())
    elif primary_name == "claude":
        primary = ClaudeProvider()
    else:
        # Unknown provider — fall back to GLM primary.
        primary = GLMProvider(config=get_glm_config())

    # --- Build fallback provider ---
    if not fallback_name:
        fallback = None
    elif fallback_name == "glm":
        if primary_name == "glm":
            # Dual-Z.AI: fallback uses a different (cheaper) model.
            fallback = GLMProvider(config=get_glm_fallback_config())
        else:
            fallback = GLMProvider(config=get_glm_config())
    elif fallback_name == "claude":
        fallback = ClaudeProvider()
    else:
        # Unknown fallback — use GLM fallback.
        fallback = GLMProvider(config=get_glm_fallback_config())

    return LLMRouter(primary=primary, fallback=fallback)
