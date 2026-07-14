"""Tests for ToolResult serialization."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime

import pytest

from apps.slack_bot.contracts import (
    AccountReference,
    AnalyticsPeriod,
    ToolResult,
    ToolResultStatus,
)
from apps.slack_bot.tool_execution import serialize_tool_result


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

import uuid

_ACCOUNT_ID = uuid.UUID("12345678-1234-5678-1234-567812345678")


def _make_result(**kwargs):
    defaults = dict(
        status=ToolResultStatus.SUCCESS,
        tool_name="get_account_stats",
    )
    defaults.update(kwargs)
    return ToolResult(**defaults)


# ===========================================================================
# Valid serialization
# ===========================================================================

def test_valid_result_serializes():
    result = _make_result(
        platform="instagram",
        data={"reach": 1000, "engagement": 50},
    )
    serialized = serialize_tool_result(result)
    payload = json.loads(serialized)
    assert payload["status"] == "success"
    assert payload["tool_name"] == "get_account_stats"
    assert payload["platform"] == "instagram"
    assert payload["data"]["reach"] == 1000


def test_serialization_is_deterministic():
    result = _make_result(
        platform="instagram",
        data={"reach": 1000, "engagement": 50},
    )
    s1 = serialize_tool_result(result)
    s2 = serialize_tool_result(result)
    assert s1 == s2


def test_account_reference_serialized():
    result = _make_result(
        selected_account=AccountReference(
            account_id=_ACCOUNT_ID,
            platform="instagram",
            display_name="My Account",
            handle="@myaccount",
        ),
    )
    payload = json.loads(serialize_tool_result(result))
    assert payload["account"]["account_id"] == str(_ACCOUNT_ID)
    assert payload["account"]["display_name"] == "My Account"


def test_period_serialized():
    result = _make_result(
        period=AnalyticsPeriod(
            start=date(2025, 1, 1),
            end=date(2025, 1, 7),
            days=7,
        ),
    )
    payload = json.loads(serialize_tool_result(result))
    assert payload["period"]["start"] == "2025-01-01"
    assert payload["period"]["end"] == "2025-01-07"
    assert payload["period"]["days"] == 7


# ===========================================================================
# Timezone-aware datetime serialization
# ===========================================================================

def test_timezone_aware_datetime_serializes():
    result = _make_result(
        data_as_of=datetime(2025, 1, 1, 12, 0, 0, tzinfo=UTC),
    )
    payload = json.loads(serialize_tool_result(result))
    assert "2025-01-01T12:00:00" in payload["data_as_of"]
    assert "+00:00" in payload["data_as_of"]


def test_warnings_serialized():
    result = _make_result(warnings=["stale_data"])
    payload = json.loads(serialize_tool_result(result))
    assert payload["warnings"] == ["stale_data"]


def test_error_code_serialized():
    result = _make_result(
        status=ToolResultStatus.FAILED,
        error_code="tool_execution_failed",
    )
    payload = json.loads(serialize_tool_result(result))
    assert payload["error_code"] == "tool_execution_failed"


# ===========================================================================
# Rejection of unsupported objects
# ===========================================================================

def test_non_serializable_object_rejected():
    result = _make_result(
        data={"callback": lambda x: x},
    )
    with pytest.raises(ValueError, match="not JSON-serializable"):
        serialize_tool_result(result)


def test_django_model_rejected():
    """A Django model instance in data should be rejected."""

    class FakeModel:
        _meta = None

    result = _make_result(data={"model": FakeModel()})
    with pytest.raises(ValueError, match="not JSON-serializable"):
        serialize_tool_result(result)


# ===========================================================================
# Size bounding
# ===========================================================================

def test_oversized_result_rejected():
    """Result exceeding 32 KiB should be rejected."""
    big_data = {"key_" + str(i): "x" * 100 for i in range(500)}
    result = _make_result(data=big_data)
    with pytest.raises(ValueError, match="exceeds"):
        serialize_tool_result(result)


# ===========================================================================
# Original ToolResult unchanged
# ===========================================================================

def test_original_result_unchanged():
    result = _make_result(data={"reach": 1000})
    original_data = dict(result.data)
    serialize_tool_result(result)
    assert result.data == original_data


# ===========================================================================
# No secrets or context in serialized payload
# ===========================================================================

def test_no_secrets_in_payload():
    result = _make_result(data={"reach": 1000})
    serialized = serialize_tool_result(result)
    forbidden = ["token", "secret", "password", "api_key", "credential"]
    lower = serialized.lower()
    for word in forbidden:
        assert word not in lower, f"{word} must not appear in serialized result"
