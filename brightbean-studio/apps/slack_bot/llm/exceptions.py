"""Exception hierarchy for the LLM provider layer.

All exceptions inherit from :class:`~apps.slack_bot.exceptions.SlackBotError`
so callers can catch the entire family with a single ``except SlackBotError``.

Split into two categories:

* **Retryable** — transient failures (timeout, 429, 5xx, network).
  The fallback router catches these and tries the next provider.
* **Permanent** — the provider rejected the request in a way that
  retrying on a different provider will not fix (invalid API key,
  malformed request, bad response shape).  These abort the request.

The distinction is enforced by :meth:`LLMProviderError.is_retryable`.
"""

from __future__ import annotations

from apps.slack_bot.exceptions import SlackBotError


class LLMProviderError(SlackBotError):
    """Base exception for all LLM provider failures.

    Attributes
    ----------
    provider_name : str
        Name of the provider that raised the error (``"claude"``,
        ``"glm"``).
    is_retryable : bool
        When ``True`` the fallback router should try the next provider.
        When ``False`` the request must be aborted.
    """

    def __init__(
        self,
        message: str = "",
        *,
        provider_name: str = "",
        retryable: bool = False,
    ) -> None:
        self.provider_name = provider_name
        self.is_retryable = retryable
        super().__init__(message)


# ---------------------------------------------------------------------------
# Retryable errors — trigger fallback
# ---------------------------------------------------------------------------


class LLMTimeoutError(LLMProviderError):
    """The provider did not respond within the configured timeout."""

    def __init__(self, message: str = "", *, provider_name: str = "") -> None:
        super().__init__(
            message or f"{provider_name} request timed out",
            provider_name=provider_name,
            retryable=True,
        )


class LLMRateLimitError(LLMProviderError):
    """The provider returned HTTP 429."""

    def __init__(self, message: str = "", *, provider_name: str = "") -> None:
        super().__init__(
            message or f"{provider_name} rate limit (429)",
            provider_name=provider_name,
            retryable=True,
        )


class LLMServerError(LLMProviderError):
    """The provider returned an HTTP 5xx response."""

    def __init__(
        self,
        message: str = "",
        *,
        provider_name: str = "",
        status_code: int | None = None,
    ) -> None:
        self.status_code = status_code
        super().__init__(
            message or f"{provider_name} server error ({status_code})",
            provider_name=provider_name,
            retryable=True,
        )


class LLMTransportError(LLMProviderError):
    """Network-level failure (DNS, connection refused, TLS, etc.)."""

    def __init__(self, message: str = "", *, provider_name: str = "") -> None:
        super().__init__(
            message or f"{provider_name} transport error",
            provider_name=provider_name,
            retryable=True,
        )


# ---------------------------------------------------------------------------
# Permanent errors — abort the request
# ---------------------------------------------------------------------------


class LLMAuthError(LLMProviderError):
    """The provider rejected the API key (HTTP 401/403)."""

    def __init__(self, message: str = "", *, provider_name: str = "") -> None:
        super().__init__(
            message or f"{provider_name} authentication failed",
            provider_name=provider_name,
            retryable=False,
        )


class LLMBadRequestError(LLMProviderError):
    """The provider rejected the request body (HTTP 400).

    This is permanent — retrying on a fallback provider with the same
    payload is unlikely to help and may mask a real bug.
    """

    def __init__(self, message: str = "", *, provider_name: str = "") -> None:
        super().__init__(
            message or f"{provider_name} bad request (400)",
            provider_name=provider_name,
            retryable=False,
        )


class LLMResponseParseError(LLMProviderError):
    """The provider returned a 2xx response that could not be parsed.

    The HTTP call succeeded but the JSON shape does not match the
    expected contract.  This is permanent for the current provider —
    the fallback router may still try the next one.
    """

    def __init__(self, message: str = "", *, provider_name: str = "") -> None:
        super().__init__(
            message or f"{provider_name} response could not be parsed",
            provider_name=provider_name,
            retryable=False,
        )
