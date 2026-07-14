"""Centralized metric validation for the Slack analytics bot.

Validates requested metric names against the real BrightBean metric
catalog in :mod:`apps.analytics.metrics`.  No silent fallback — an
invalid metric returns a typed failure, never a substitute.

Source of truth:
* :data:`apps.analytics.metrics.PLATFORM_METRICS`
* :data:`apps.analytics.metrics.METRICS`
"""

from __future__ import annotations

from apps.analytics.metrics import PLATFORM_METRICS

from .platform_mapping import internal_platforms_for


def supported_metrics_for_canonical(canonical_platform: str) -> frozenset[str]:
    """Return all valid metric keys for a canonical platform.

    Aggregates metrics across all internal variants (e.g. ``instagram``
    and ``instagram_login`` may have the same metrics, but this function
    unions them to be safe).
    """
    metrics: set[str] = set()
    for internal in internal_platforms_for(canonical_platform):
        metrics.update(PLATFORM_METRICS.get(internal, []))
    return frozenset(metrics)


def is_valid_metric(canonical_platform: str, metric: str) -> bool:
    """True if *metric* is a valid metric for *canonical_platform*."""
    return metric in supported_metrics_for_canonical(canonical_platform)


def validate_metric(
    canonical_platform: str,
    metric: str,
) -> tuple[bool, frozenset[str]]:
    """Validate *metric* against the platform's metric catalog.

    Returns ``(True, supported_metrics)`` if valid, ``(False, supported_metrics)``
    if invalid.  The caller uses the supported set to produce a helpful error.
    """
    supported = supported_metrics_for_canonical(canonical_platform)
    return (metric in supported, supported)
