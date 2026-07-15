"""Admin registration for the Slack analytics bot."""

from django.contrib import admin

from .models import (
    BotAccessAuditLog,
    BotAdministrator,
    BotUserAccess,
    SlackChannelMapping,
    SlackInboundEvent,
    SlackUserMapping,
    UnauthorizedAccessAttempt,
)


@admin.register(SlackInboundEvent)
class SlackInboundEventAdmin(admin.ModelAdmin):
    list_display = ("event_id", "status", "team_id", "channel_id", "created_at")
    list_filter = ("status",)
    search_fields = ("event_id", "team_id", "channel_id", "user_id")
    readonly_fields = ("id", "created_at", "updated_at")
    ordering = ("-created_at",)


@admin.register(SlackChannelMapping)
class SlackChannelMappingAdmin(admin.ModelAdmin):
    list_display = ("team_id", "channel_id", "workspace", "created_at")
    search_fields = ("team_id", "channel_id", "workspace__name")
    readonly_fields = ("id", "created_at", "updated_at")


@admin.register(SlackUserMapping)
class SlackUserMappingAdmin(admin.ModelAdmin):
    list_display = ("slack_user_id", "team_id", "user", "created_at")
    search_fields = ("slack_user_id", "team_id", "user__email")
    readonly_fields = ("id", "created_at", "updated_at")


# ---------------------------------------------------------------------------
# Phase 1 — Bot whitelisting admin registrations
# ---------------------------------------------------------------------------


@admin.register(BotAdministrator)
class BotAdministratorAdmin(admin.ModelAdmin):
    list_display = ("workspace_id", "slack_user_id", "status", "created_at")
    search_fields = ("workspace_id", "slack_user_id")
    list_filter = ("status",)
    readonly_fields = ("id", "created_at", "updated_at")


@admin.register(BotUserAccess)
class BotUserAccessAdmin(admin.ModelAdmin):
    list_display = (
        "workspace_id", "slack_user_id", "status",
        "permission", "granted_at",
    )
    search_fields = ("workspace_id", "slack_user_id")
    list_filter = ("status", "permission")
    readonly_fields = ("id", "created_at", "updated_at", "granted_at")


@admin.register(UnauthorizedAccessAttempt)
class UnauthorizedAccessAttemptAdmin(admin.ModelAdmin):
    list_display = (
        "workspace_id", "slack_user_id", "attempt_count",
        "last_attempt_at", "last_admin_notification_at",
    )
    search_fields = ("workspace_id", "slack_user_id")
    readonly_fields = ("id", "created_at", "updated_at")


@admin.register(BotAccessAuditLog)
class BotAccessAuditLogAdmin(admin.ModelAdmin):
    list_display = (
        "workspace_id", "action", "target_slack_user_id",
        "performed_by_slack_user_id", "created_at",
    )
    search_fields = ("workspace_id", "target_slack_user_id")
    list_filter = ("action", "created_at")
    readonly_fields = (
        "id", "workspace_id", "target_slack_user_id",
        "performed_by_slack_user_id", "action", "metadata", "created_at",
    )
