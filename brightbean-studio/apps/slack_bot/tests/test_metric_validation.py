"""Tests for centralized metric validation.

Verifies that metric names are validated against the real BrightBean
metric catalog with no silent fallback.
"""

from __future__ import annotations

import pytest

from apps.slack_bot.metric_validation import (
    is_valid_metric,
    supported_metrics_for_canonical,
    validate_metric,
)

# ---------------------------------------------------------------------------
# supported_metrics_for_canonical
# ---------------------------------------------------------------------------

def test_instagram_metrics_include_reach_and_engagement():
    metrics = supported_metrics_for_canonical("instagram")
    assert "reach" in metrics
    assert "engagement" in metrics
    assert "likes" in metrics
    assert "comments" in metrics


def test_facebook_metrics_include_reach_and_reactions():
    metrics = supported_metrics_for_canonical("facebook")
    assert "reach" in metrics
    assert "reactions" in metrics
    assert "shares" in metrics


def test_linkedin_metrics_include_impressions():
    metrics = supported_metrics_for_canonical("linkedin")
    assert "impressions" in metrics
    assert "reactions" in metrics
    # LinkedIn Personal has likes, comments, shares
    assert "likes" in metrics


# ---------------------------------------------------------------------------
# is_valid_metric
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "platform,metric",
    [
        ("instagram", "reach"),
        ("instagram", "engagement"),
        ("facebook", "reactions"),
        ("linkedin", "impressions"),
        ("linkedin", "likes"),  # from linkedin_personal
    ],
)
def test_is_valid_metric_true(platform, metric):
    assert is_valid_metric(platform, metric) is True


@pytest.mark.parametrize(
    "platform,metric",
    [
        ("instagram", "invalid_metric"),
        ("facebook", "watch_time"),
        ("linkedin", "saves"),
        ("instagram", ""),
    ],
)
def test_is_valid_metric_false(platform, metric):
    assert is_valid_metric(platform, metric) is False


# ---------------------------------------------------------------------------
# validate_metric
# ---------------------------------------------------------------------------

def test_validate_metric_valid_returns_true_and_supported_set():
    valid, supported = validate_metric("instagram", "reach")
    assert valid is True
    assert "reach" in supported
    assert "engagement" in supported


def test_validate_metric_invalid_returns_false_and_supported_set():
    valid, supported = validate_metric("instagram", "invalid_metric")
    assert valid is False
    assert "reach" in supported  # supported set is still returned for helpful error


def test_validate_metric_supported_set_is_frozenset():
    _, supported = validate_metric("instagram", "reach")
    assert isinstance(supported, frozenset)
