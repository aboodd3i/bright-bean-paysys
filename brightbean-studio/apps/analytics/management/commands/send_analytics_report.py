"""Send an analytics report email immediately or on a schedule.

Usage:
    python manage.py send_analytics_report              # daily (1 day lookback)
    python manage.py send_analytics_report --days 7     # weekly (7 day lookback)
    python manage.py send_analytics_report --email foo@bar.com
"""

from __future__ import annotations

from django.core.management.base import BaseCommand

from apps.analytics.report import send_report_email


class Command(BaseCommand):
    help = "Generate and send an analytics report email."

    def add_arguments(self, parser):
        parser.add_argument(
            "--days",
            type=int,
            default=1,
            help="Lookback window in days (default: 1 for daily reports).",
        )
        parser.add_argument(
            "--email",
            type=str,
            default=None,
            help="Recipient email address (default: ANALYTICS_REPORT_RECIPIENT from .env).",
        )

    def handle(self, *args, **opts):
        self.stdout.write("Generating analytics report…")

        sent = send_report_email(
            recipient=opts["email"],
            days=opts["days"],
        )

        if sent:
            self.stdout.write(self.style.SUCCESS("Analytics report email sent successfully."))
        else:
            self.stdout.write(
                self.style.WARNING(
                    "No email sent — either no analytics data was found "
                    "or no recipient was configured."
                )
            )
