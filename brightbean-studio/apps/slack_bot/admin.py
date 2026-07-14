"""Admin registration for the Slack analytics bot."""

from django.contrib import admin

from .models import SlackChannelMapping, SlackInboundEvent, SlackUserMapping


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
