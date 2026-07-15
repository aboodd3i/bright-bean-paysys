"""Django management command to pre-approve bot users.

Usage:
    python manage.py grant_bot_access --workspace-id T0123 --user-ids U001 U002 U003
"""

from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError

from apps.slack_bot.access_service import grant_bulk_user_access
from apps.slack_bot.constants import SYSTEM_ACTOR
from apps.slack_bot.slack_id_validation import (
    is_valid_workspace_id,
)


class Command(BaseCommand):
    help = "Pre-approve one or more Slack users for bot access."

    def add_arguments(self, parser):
        parser.add_argument(
            "--workspace-id",
            required=True,
            help="Slack workspace/team ID (e.g. T0123456789)",
        )
        parser.add_argument(
            "--user-ids",
            nargs="+",
            required=True,
            help="One or more Slack user/member IDs (e.g. U001 U002 U003)",
        )

    def handle(self, *args, **options):
        workspace_id = options["workspace_id"].strip()
        user_ids = options["user_ids"]

        if not is_valid_workspace_id(workspace_id):
            raise CommandError(
                f"Invalid workspace ID: {workspace_id!r}. "
                "Expected format: T followed by uppercase alphanumeric characters."
            )

        result = grant_bulk_user_access(
            workspace_id=workspace_id,
            slack_user_ids=user_ids,
            granted_by_slack_user_id=SYSTEM_ACTOR,
        )

        lines: list[str] = []

        if result.approved:
            lines.append("Approved:")
            for uid in result.approved:
                lines.append(f"  - {uid}")

        if result.restored:
            lines.append("Restored:")
            for uid in result.restored:
                lines.append(f"  - {uid}")

        if result.already_approved:
            lines.append("Already approved:")
            for uid in result.already_approved:
                lines.append(f"  - {uid}")

        if result.invalid:
            lines.append("Invalid Member IDs:")
            for uid in result.invalid:
                lines.append(f"  - {uid}")

        if result.failed:
            lines.append("Failed:")
            for uid in result.failed:
                lines.append(f"  - {uid}")

        if not lines:
            lines.append("No changes.")

        self.stdout.write(self.style.SUCCESS("\n".join(lines)))
