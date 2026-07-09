"""Analytics report generation and email delivery.

Compiles analytics data from the existing services layer into a report
payload, renders an HTML + text email, and sends it via Django's email
backend. Designed to be called from a management command or a scheduled
background task.
"""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

from django.conf import settings
from django.core.mail import EmailMultiAlternatives
from django.template.loader import render_to_string
from django.utils import timezone

from apps.analytics.constants import NO_ANALYTICS_PLATFORMS
from apps.analytics.services import (
    account_analytics_bundle,
    all_posts_for,
    engagement_card,
    follower_growth,
    hero_cards,
)
from apps.social_accounts.models import AnalyticsPlatformConfig, SocialAccount

logger = logging.getLogger(__name__)

DEFAULT_DAYS = 1  # daily report covers the past 1 day
TOP_POSTS_LIMIT = 5


def _enabled_connected_accounts() -> list[SocialAccount]:
    """All connected accounts on platforms with analytics enabled."""
    enabled = set(AnalyticsPlatformConfig.enabled_platforms())
    return list(
        SocialAccount.objects.filter(
            connection_status=SocialAccount.ConnectionStatus.CONNECTED,
            platform__in=enabled,
        )
        .exclude(platform__in=NO_ANALYTICS_PLATFORMS)
        .order_by("platform", "account_name")
    )


def _build_account_report(account: SocialAccount, days: int) -> dict[str, Any]:
    """Compile the report payload for a single social account."""
    bundle = account_analytics_bundle(account, days)
    series_map = bundle["series_map"]

    # Follower growth
    fg = follower_growth(account, days, series_map=series_map)
    follower_data = None
    if fg and fg.value > 0:
        follower_data = {"value": fg.value, "delta": fg.delta}

    # Hero cards (account-level insights)
    cards = hero_cards(account, days, series_map=series_map)
    hero_data = [
        {
            "metric": c["metric"],
            "label": c["label"],
            "derived": {
                "value": c["derived"].value,
                "delta": c["derived"].delta,
                "kind": c["derived"].kind,
            },
        }
        for c in cards
        if c["derived"].value > 0
    ]

    # Engagement card
    eng = engagement_card(account, days, series_map=series_map)
    engagement_data = None
    if eng and eng["rate"].value > 0:
        engagement_data = {
            "rate": {
                "value": eng["rate"].value,
                "delta": eng["rate"].delta,
            },
            "parts": [
                {
                    "metric": p["metric"],
                    "label": p["label"],
                    "derived": {
                        "value": p["derived"].value,
                        "delta": p["derived"].delta,
                        "kind": p["derived"].kind,
                    },
                }
                for p in eng["parts"]
            ],
        }

    # All posts table
    table = all_posts_for(
        account,
        days_filter=days,
        sort_key=None,
        sort_dir="desc",
        type_filter="all",
        page=1,
        page_size=50,  # include up to 50 posts in the email
    )

    # Top performing posts — sort by primary metric, take top N
    all_rows = table.get("rows", [])
    primary = table.get("primary", "")
    if primary and all_rows:
        top_sorted = sorted(
            all_rows,
            key=lambda r: r["stats"].get(primary, 0),
            reverse=True,
        )[:TOP_POSTS_LIMIT]
    else:
        top_sorted = all_rows[:TOP_POSTS_LIMIT]

    metric_labels = table.get("metric_labels", [])

    def _stats_list(stats_dict):
        """Convert a stats dict to a list of (key, label, value) for templates."""
        return [
            {
                "key": ml["key"],
                "label": ml["label"],
                "value": float(stats_dict.get(ml["key"], 0)),
            }
            for ml in metric_labels
        ]

    top_posts = [
        {
            "caption": r["caption"],
            "date": r["date"],
            "media_kind": r["media_kind"],
            "stats": _stats_list(r["stats"]),
        }
        for r in top_sorted
    ]

    # Flatten all posts for the table section
    all_posts = [
        {
            "caption": r["caption"],
            "date": r["date"],
            "stats": _stats_list(r["stats"]),
        }
        for r in all_rows
    ]

    return {
        "account_name": account.account_name,
        "platform": account.platform,
        "follower_growth": follower_data,
        "hero_cards": hero_data,
        "engagement": engagement_data,
        "top_posts": top_posts,
        "all_posts": all_posts,
        "metric_labels": table.get("metric_labels", []),
    }


def generate_report(days: int = DEFAULT_DAYS) -> dict[str, Any]:
    """Compile analytics data for all connected accounts into a report payload.

    Returns a dict with ``report_date``, ``days``, and ``accounts`` (list of
    per-account report dicts). Safe to call even with zero connected accounts.
    """
    accounts = _enabled_connected_accounts()
    account_reports = []

    for account in accounts:
        try:
            report = _build_account_report(account, days)
            # Skip accounts with no data at all
            if (
                not report["follower_growth"]
                and not report["hero_cards"]
                and not report["engagement"]
                and not report["top_posts"]
            ):
                continue
            account_reports.append(report)
        except Exception:
            logger.exception("Failed to build report for account %s", account.id)

    return {
        "report_date": timezone.now().date(),
        "days": days,
        "accounts": account_reports,
    }


def send_report_email(
    recipient: str | None = None,
    days: int = DEFAULT_DAYS,
) -> bool:
    """Generate the analytics report and email it.

    Args:
        recipient: Email address to send to. Falls back to
            ``settings.ANALYTICS_REPORT_RECIPIENT`` then
            ``settings.DEFAULT_FROM_EMAIL``.
        days: Lookback window in days (default 1 for daily reports).

    Returns ``True`` if the email was sent, ``False`` if there was no data
    or no recipient.
    """
    recipient = recipient or getattr(
        settings, "ANALYTICS_REPORT_RECIPIENT", None
    ) or getattr(settings, "DEFAULT_FROM_EMAIL", None)

    if not recipient:
        logger.warning("No recipient configured for analytics report email.")
        return False

    report = generate_report(days=days)

    # Don't send an empty report
    if not report["accounts"]:
        logger.info("No analytics data to report — skipping email.")
        return False

    context = {
        **report,
        "app_url": getattr(settings, "APP_URL", "http://localhost:8000"),
    }

    subject = f"Daily Analytics Report — {report['report_date'].strftime('%b %d, %Y')}"

    text_content = render_to_string("analytics/email/report.txt", context)
    html_content = render_to_string("analytics/email/report.html", context)

    msg = EmailMultiAlternatives(
        subject=subject,
        body=text_content,
        from_email=getattr(settings, "DEFAULT_FROM_EMAIL", "noreply@localhost"),
        to=[recipient],
    )
    msg.attach_alternative(html_content, "text/html")
    msg.send(fail_silently=False)

    logger.info("Analytics report email sent to %s (%d accounts)", recipient, len(report["accounts"]))
    return True
