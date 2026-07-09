"""Management command to run post discovery manually.

Usage::

    python manage.py sync_posts                # all connected accounts
    python manage.py sync_posts --account <id>  # single account
    python manage.py sync_posts --limit 50      # fetch up to 50 per account
"""

from __future__ import annotations

import logging

from django.core.management.base import BaseCommand

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Discover externally-published posts and import them as PlatformPost records."

    def add_arguments(self, parser):
        parser.add_argument(
            "--account",
            type=str,
            default=None,
            help="UUID of a specific SocialAccount to discover posts for.",
        )
        parser.add_argument(
            "--limit",
            type=int,
            default=25,
            help="Maximum number of recent posts to fetch per account (default 25).",
        )

    def handle(self, *args, **options):
        from apps.analytics.post_discovery import discover_all_posts, discover_posts_for_account

        limit = options["limit"]
        account_id = options["account"]

        if account_id:
            from apps.social_accounts.models import SocialAccount

            try:
                account = SocialAccount.objects.get(pk=account_id)
            except SocialAccount.DoesNotExist:
                self.stderr.write(self.style.ERROR(f"SocialAccount {account_id} not found."))
                return

            self.stdout.write(f"Discovering posts for {account.platform} account {account_id} (limit={limit}) …")
            created = discover_posts_for_account(account, limit=limit)
            self.stdout.write(
                self.style.SUCCESS(f"Done — {created} new post(s) imported.")
            )
        else:
            self.stdout.write(f"Discovering posts for all connected accounts (limit={limit}) …")
            results = discover_all_posts(limit=limit)
            total = sum(results.values())
            for key, count in results.items():
                self.stdout.write(f"  {key}: {count} new post(s)")
            self.stdout.write(
                self.style.SUCCESS(f"Done — {total} new post(s) imported across {len(results)} account(s).")
            )
