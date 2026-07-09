import logging
from datetime import datetime, timedelta, timezone

from django.apps import AppConfig

logger = logging.getLogger(__name__)

SYNC_INTERVAL_SECONDS = 3600  # hourly

# Target send time for the daily report: 5:37 PM Pakistan time (PKT = UTC+5)
# → 12:37 UTC. Stored as (hour, minute) in UTC.
REPORT_SEND_TIME_UTC = (12, 37)  # 5:37 PM PKT = 12:37 UTC


class AnalyticsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.analytics"
    verbose_name = "Analytics"

    def ready(self):
        from django.db.models.signals import post_migrate

        from . import signals  # noqa: F401

        # Register the recurring sync task with django-background-tasks. We
        # use ``post_migrate`` so the task table exists before we touch it;
        # mirrors the pattern in apps/publisher/apps.py.
        post_migrate.connect(self._register_sync_task, sender=self)
        post_migrate.connect(self._register_daily_report_task, sender=self)
        post_migrate.connect(self._register_post_discovery_task, sender=self)

    @staticmethod
    def _register_sync_task(sender, **kwargs):
        """Idempotently register the hourly analytics sync cron."""
        try:
            from background_task.models import Task

            from apps.analytics.tasks import sync_all_account_analytics

            if not Task.objects.filter(verbose_name="sync_all_account_analytics").exists():
                sync_all_account_analytics(
                    repeat=SYNC_INTERVAL_SECONDS,
                    verbose_name="sync_all_account_analytics",
                )
                logger.info(
                    "Registered recurring analytics sync (every %ss)",
                    SYNC_INTERVAL_SECONDS,
                )
        except Exception:
            logger.debug("Skipping analytics sync registration (database not ready)")

    @staticmethod
    def _register_daily_report_task(sender, **kwargs):
        """Idempotently register the daily analytics report email cron.

        Scheduled for 10:30 AM Pakistan time (05:30 UTC) every day.
        """
        try:
            from background_task.models import Task

            from apps.analytics.tasks import (
                DAILY_REPORT_INTERVAL_SECONDS,
                send_daily_analytics_report,
            )

            if not Task.objects.filter(verbose_name="send_daily_analytics_report").exists():
                # Compute seconds until the next 05:30 UTC.
                now = datetime.now(timezone.utc)
                target = now.replace(hour=REPORT_SEND_TIME_UTC[0], minute=REPORT_SEND_TIME_UTC[1], second=0, microsecond=0)
                if target <= now:
                    target += timedelta(days=1)
                delay_seconds = int((target - now).total_seconds())

                send_daily_analytics_report(
                    schedule=delay_seconds,
                    repeat=DAILY_REPORT_INTERVAL_SECONDS,
                    verbose_name="send_daily_analytics_report",
                )
                logger.info(
                    "Registered daily analytics report email — first send in %ss (at 5:37 PM PKT), repeating every %ss",
                    delay_seconds,
                    DAILY_REPORT_INTERVAL_SECONDS,
                )
        except Exception:
            logger.debug("Skipping daily report registration (database not ready)")

    @staticmethod
    def _register_post_discovery_task(sender, **kwargs):
        """Idempotently register the hourly post-discovery cron."""
        try:
            from background_task.models import Task

            from apps.analytics.tasks import (
                POST_DISCOVERY_INTERVAL_SECONDS,
                sync_platform_posts,
            )

            if not Task.objects.filter(verbose_name="sync_platform_posts").exists():
                sync_platform_posts(
                    repeat=POST_DISCOVERY_INTERVAL_SECONDS,
                    verbose_name="sync_platform_posts",
                )
                logger.info(
                    "Registered recurring post discovery (every %ss)",
                    POST_DISCOVERY_INTERVAL_SECONDS,
                )
        except Exception:
            logger.debug("Skipping post discovery registration (database not ready)")
