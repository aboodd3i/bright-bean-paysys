"""Stable machine-readable error codes for the Slack analytics bot.

These codes are the contract between every layer — tools, orchestration,
LLM adapters, formatter, and tests.  They are deliberately ``StrEnum``
so they serialize as plain strings in logs, JSON payloads, and
``StructuredAnswer.warning_codes`` without extra mapping.

Split into two categories:

* **Fatal errors** — the request cannot be fulfilled.  The formatter
  produces an error message and no analytics data is shown.
* **Warnings** — the request succeeded but the result carries a caveat
  (e.g. stale data).  The formatter includes a warning note alongside
  the data.

The distinction is enforced by :meth:`ErrorCode.is_warning` so the
orchestration layer can decide whether to abort or continue without
hard-coding string comparisons.
"""

from __future__ import annotations

from enum import StrEnum

# Codes that represent non-fatal warnings rather than abort-level errors.
_WARNING_CODES: frozenset[str] = frozenset({"no_data", "stale_data"})


class ErrorCode(StrEnum):
    """Stable error / warning codes for the Slack analytics bot."""

    # --- Authorization (fatal) ---
    UNAUTHORIZED = "unauthorized"
    CHANNEL_NOT_MAPPED = "channel_not_mapped"
    USER_NOT_MAPPED = "user_not_mapped"
    WORKSPACE_UNAVAILABLE = "workspace_unavailable"
    ACCOUNT_NOT_ALLOWED = "account_not_allowed"

    # --- Request validation (fatal) ---
    UNSUPPORTED_PLATFORM = "unsupported_platform"
    INVALID_METRIC = "invalid_metric"
    INVALID_PERIOD = "invalid_period"
    INVALID_LIMIT = "invalid_limit"

    # --- Account resolution (fatal) ---
    NO_CONNECTED_ACCOUNT = "no_connected_account"
    MULTIPLE_ACCOUNTS = "multiple_accounts"

    # --- Data state (warning) ---
    NO_DATA = "no_data"
    STALE_DATA = "stale_data"

    # --- Tool execution (fatal) ---
    TOOL_NOT_FOUND = "tool_not_found"
    TOOL_ARGUMENT_VALIDATION_FAILED = "tool_argument_validation_failed"
    TOOL_EXECUTION_FAILED = "tool_execution_failed"
    TOOL_LOOP_LIMIT_REACHED = "tool_loop_limit_reached"

    # --- LLM provider (fatal) ---
    LLM_TEMPORARILY_UNAVAILABLE = "llm_temporarily_unavailable"
    PROVIDER_RESPONSE_INVALID = "provider_response_invalid"
    RESPONSE_REFERENCE_INVALID = "response_reference_invalid"

    @classmethod
    def is_warning(cls, code: str | ErrorCode) -> bool:
        """True when *code* is a non-fatal warning rather than an error."""
        value = code.value if isinstance(code, ErrorCode) else str(code)
        return value in _WARNING_CODES

    @classmethod
    def is_fatal(cls, code: str | ErrorCode) -> bool:
        """True when *code* is a fatal error that aborts the request."""
        return not cls.is_warning(code)
