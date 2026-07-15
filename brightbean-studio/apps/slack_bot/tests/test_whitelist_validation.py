"""Tests for Phase 1 Slack ID validation helpers."""

import pytest

from apps.slack_bot.slack_id_validation import (
    deduplicate_ids,
    is_valid_member_id,
    is_valid_workspace_id,
)


# ===========================================================================
# is_valid_workspace_id
# ===========================================================================


def test_valid_workspace_id():
    assert is_valid_workspace_id("T0123456789") is True


def test_valid_workspace_id_short():
    assert is_valid_workspace_id("T1") is True


def test_invalid_workspace_id_lowercase():
    assert is_valid_workspace_id("t0123") is False


def test_invalid_workspace_id_wrong_prefix():
    assert is_valid_workspace_id("U0123") is False


def test_invalid_workspace_id_empty():
    assert is_valid_workspace_id("") is False


def test_invalid_workspace_id_with_special_chars():
    assert is_valid_workspace_id("T-123") is False


# ===========================================================================
# is_valid_member_id
# ===========================================================================


def test_valid_member_id_user():
    assert is_valid_member_id("U0123456789") is True


def test_valid_member_id_guest():
    assert is_valid_member_id("W0123456789") is True


def test_invalid_member_id_channel():
    assert is_valid_member_id("C0123") is False


def test_invalid_member_id_group():
    assert is_valid_member_id("G0123") is False


def test_invalid_member_id_lowercase():
    assert is_valid_member_id("u0123") is False


def test_invalid_member_id_empty():
    assert is_valid_member_id("") is False


def test_invalid_member_id_team_prefix():
    assert is_valid_member_id("T0123") is False


# ===========================================================================
# deduplicate_ids
# ===========================================================================


def test_deduplicate_preserves_order():
    result = deduplicate_ids(["U0003", "U0001", "U0002"])
    assert result == ["U0003", "U0001", "U0002"]


def test_deduplicate_removes_duplicates():
    result = deduplicate_ids(["U0001", "U0002", "U0001", "U0002"])
    assert result == ["U0001", "U0002"]


def test_deduplicate_trims_whitespace():
    result = deduplicate_ids(["  U0001  ", "U0001"])
    assert result == ["U0001"]


def test_deduplicate_empty_list():
    assert deduplicate_ids([]) == []
