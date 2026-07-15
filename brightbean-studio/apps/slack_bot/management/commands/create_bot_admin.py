"""Django management command to configure the bot administrator.

Usage:
    python manage.py create_bot_admin --workspace-id T0123 --user-id U0123
"""

from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError

from apps.slack_bot.access_service import configure_administrator
from apps.slack_bot.constants import SYSTEM_ACTOR
from apps.slack_bot.models import BotUserAccess
from apps.slack_bot.slack_id_validation import (
    is_valid_member_id,
    is_valid_workspace_id,
)


class Command(BaseCommand):
    help = "Configure the bot administrator for a workspace."

    def add_arguments(self, parser):
        parser.add_argument(
            "--workspace-id",
            required=True,
            help="Slack workspace/team ID (e.g. T0123456789)",
        )
        parser.add_argument(
            "--user-id",
            required=True,
            help="Slack user/member ID of the administrator (e.g. U0123456789)",
        )

    def handle(self, *args, **options):
        workspace_id = options["workspace_id"].strip()
        user_id = options["user_id"].strip()

        if not is_valid_workspace_id(workspace_id):
            raise CommandError(
                f"Invalid workspace ID: {workspace_id!r}. "
                "Expected format: T followed by uppercase alphanumeric characters."
            )

        if not is_valid_member_id(user_id):
            raise CommandError(
                f"Invalid user ID: {user_id!r}. "
                "Expected format: U or W followed by uppercase alphanumeric characters."
            )

        result = configure_administrator(
            workspace_id=workspace_id,
            slack_user_id=user_id,
        )

        # Check admin's bot access status for reporting
        access = BotUserAccess.objects.filter(
            workspace_id=workspace_id,
            slack_user_id=user_id,
        ).first()

        if result.action == "created":
            self.stdout.write(self.style.SUCCESS(
                "Bot administrator configured successfully.\n\n"
                f"Workspace: {workspace_id}\n"
                f"Administrator: {user_id}\n"
                f"Admin status: Active\n"
                f"Bot access: Approved\n"
                f"Permission: Read-only"
            ))
        elif result.action == "already_active":
            access_status = "Already approved" if access and access.status == "APPROVED" else "Not approved"
            self.stdout.write(self.style.SUCCESS(
                "No change required.\n\n"
                f"Workspace: {workspace_id}\n"
                f"Administrator: {user_id}\n"
                f"Admin status: Already active\n"
                f"Bot access: {access_status}"
            ))
        elif result.action == "updated":
            self.stdout.write(self.style.SUCCESS(
                "Bot administrator updated.\n\n"
                f"Workspace: {workspace_id}\n"
                f"New administrator: {user_id}\n"
                f"Admin status: Active\n"
                f"Bot access: Approved\n"
                f"Permission: Read-only"
            ))
