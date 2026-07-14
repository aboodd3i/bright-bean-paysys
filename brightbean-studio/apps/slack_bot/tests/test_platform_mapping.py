"""Tests for the centralized canonical platform mapping.

Verifies that internal BrightBean platform variants are correctly
normalized to the three canonical Slack-facing values (instagram,
facebook, linkedin) and that unsupported platforms are rejected.
"""

from __future__ import annotations

import pytest

from apps.slack_bot.platform_mapping import (
    SUPPORTED_CANONICAL_PLATFORMS,
    SUPPORTED_INTERNAL_PLATFORMS,
    canonical_for,
    filter_supported_internal,
    internal_platforms_for,
    is_supported_internal,
    normalize_to_canonical,
)

# ---------------------------------------------------------------------------
# Canonical → internal
# ---------------------------------------------------------------------------

def test_instagram_canonical_maps_to_internal_variants():
    variants = internal_platforms_for("instagram")
    assert "instagram" in variants
    assert "instagram_login" in variants


def test_facebook_canonical_maps_to_itself():
    variants = internal_platforms_for("facebook")
    assert variants == frozenset({"facebook"})


def test_linkedin_canonical_maps_to_company_and_personal():
    variants = internal_platforms_for("linkedin")
    assert "linkedin_company" in variants
    assert "linkedin_personal" in variants


def test_unknown_canonical_raises():
    with pytest.raises(ValueError, match="Unsupported canonical platform"):
        internal_platforms_for("tiktok")


# ---------------------------------------------------------------------------
# Internal → canonical
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "internal,expected",
    [
        ("instagram", "instagram"),
        ("instagram_login", "instagram"),
        ("facebook", "facebook"),
        ("linkedin_company", "linkedin"),
        ("linkedin_personal", "linkedin"),
    ],
)
def test_canonical_for_known_variants(internal, expected):
    assert canonical_for(internal) == expected


@pytest.mark.parametrize(
    "internal",
    ["tiktok", "youtube", "pinterest", "threads", "bluesky", "mastodon", "devto", "google_business"],
)
def test_canonical_for_unsupported_returns_none(internal):
    assert canonical_for(internal) is None


# ---------------------------------------------------------------------------
# is_supported_internal
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "internal",
    ["instagram", "instagram_login", "facebook", "linkedin_company", "linkedin_personal"],
)
def test_is_supported_internal_true(internal):
    assert is_supported_internal(internal) is True


@pytest.mark.parametrize(
    "internal",
    ["tiktok", "youtube", "pinterest", "threads", "bluesky", "mastodon"],
)
def test_is_supported_internal_false(internal):
    assert is_supported_internal(internal) is False


# ---------------------------------------------------------------------------
# normalize_to_canonical
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "internal,expected",
    [
        ("instagram", "instagram"),
        ("instagram_login", "instagram"),
        ("facebook", "facebook"),
        ("linkedin_company", "linkedin"),
        ("linkedin_personal", "linkedin"),
    ],
)
def test_normalize_to_canonical_known(internal, expected):
    assert normalize_to_canonical(internal) == expected


def test_normalize_to_canonical_unsupported_raises():
    with pytest.raises(ValueError, match="not supported"):
        normalize_to_canonical("tiktok")


# ---------------------------------------------------------------------------
# filter_supported_internal
# ---------------------------------------------------------------------------

def test_filter_supported_internal_excludes_unsupported():
    platforms = ["instagram", "tiktok", "facebook", "youtube", "linkedin_company"]
    result = filter_supported_internal(platforms)
    assert set(result) == {"instagram", "facebook", "linkedin_company"}


def test_filter_supported_internal_empty():
    assert filter_supported_internal([]) == []


# ---------------------------------------------------------------------------
# Constant sets
# ---------------------------------------------------------------------------

def test_supported_canonical_platforms():
    assert frozenset({"instagram", "facebook", "linkedin"}) == SUPPORTED_CANONICAL_PLATFORMS


def test_supported_internal_platforms():
    assert frozenset({
        "instagram", "instagram_login", "facebook", "linkedin_company", "linkedin_personal",
    }) == SUPPORTED_INTERNAL_PLATFORMS
