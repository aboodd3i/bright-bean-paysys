"""Models for the Slack analytics bot.

Phase 4: minimal event persistence + deduplication by ``event_id``.
"""

from __future__ import annotations

import uuid

from django.conf import settings
from django.db import models

from .constants import STATUS_CHOICES, STATUS_RECEIVED


class SlackInboundEventManager(models.Manager):
    """Custom manager providing the deduplication helper."""

    def get_or_create_inbound_event(
        self,
        *,
        event_id: str,
        team_id: str,
        channel_id: str,
        user_id: str,
        event_ts: str,
        message_text: str = "",
        thread_ts: str | None = None,
    ) -> tuple[SlackInboundEvent, bool]:
        """Return ``(event, created)`` for the given ``event_id``.

        * If ``event_id`` is new → create with ``status=RECEIVED`` and
          return ``(event, True)``.
        * If ``event_id`` already exists → return the existing record and
          ``(event, False)``.

        No duplicate is ever created.  Database-level uniqueness on
        ``event_id`` is the final safety net.
        """
        defaults = {
            "team_id": team_id,
            "channel_id": channel_id,
            "user_id": user_id,
            "event_ts": event_ts,
            "message_text": message_text,
            "thread_ts": thread_ts or "",
        }
        return self.get_or_create(event_id=event_id, defaults=defaults)


class SlackInboundEvent(models.Model):
    """A single accepted Slack event, persisted for deduplication and audit."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    correlation_id = models.CharField(
        max_length=64, blank=True, default="", db_index=True
    )

    # Main deduplication key — Slack's unique event identifier.
    event_id = models.CharField(max_length=64, unique=True, db_index=True)

    team_id = models.CharField(max_length=64, db_index=True)
    channel_id = models.CharField(max_length=64, db_index=True)
    user_id = models.CharField(max_length=64, db_index=True)

    thread_ts = models.CharField(max_length=32, blank=True, default="")
    event_ts = models.CharField(max_length=32)
    message_text = models.TextField(blank=True, default="")

    status = models.CharField(
        max_length=16,
        choices=STATUS_CHOICES,
        default=STATUS_RECEIVED,
        db_index=True,
    )

    response_ts = models.CharField(max_length=32, blank=True, default="")

    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = SlackInboundEventManager()

    class Meta:
        db_table = "slack_bot_inbound_event"
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["channel_id", "thread_ts"], name="slack_bot_chan_thread"),
        ]

    def __str__(self) -> str:
        return f"{self.event_id} [{self.status}]"


# ---------------------------------------------------------------------------
# Phase 2 — Slack ↔ BrightBean identity mappings
# ---------------------------------------------------------------------------


class SlackChannelMapping(models.Model):
    """Maps a Slack ``(team_id, channel_id)`` pair to a BrightBean workspace.

    Created by an admin during onboarding.  When a Slack analytics request
    arrives, the resolver looks up this mapping to determine which
    workspace's social accounts are in scope.

    Security: the resolver **fails closed** — if no mapping exists, the
    request is rejected with ``ErrorCode.CHANNEL_NOT_MAPPED``.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    team_id = models.CharField(max_length=64, db_index=True)
    channel_id = models.CharField(max_length=64, db_index=True)

    workspace = models.ForeignKey(
        "workspaces.Workspace",
        on_delete=models.CASCADE,
        related_name="slack_channel_mappings",
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "slack_bot_channel_mapping"
        unique_together = [("team_id", "channel_id")]

    def __str__(self):
        return f"{self.team_id}/{self.channel_id} → {self.workspace.name}"


class SlackUserMapping(models.Model):
    """Maps a Slack ``(slack_user_id, team_id)`` pair to a BrightBean user.

    Created by an admin during onboarding.  When a Slack analytics request
    arrives, the resolver looks up this mapping to determine which
    BrightBean user is making the request — used for membership and
    permission checks.

    Security: the resolver **fails closed** — if no mapping exists, the
    request is rejected with ``ErrorCode.USER_NOT_MAPPED``.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    slack_user_id = models.CharField(max_length=64, db_index=True)
    team_id = models.CharField(max_length=64, db_index=True)

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="slack_user_mappings",
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "slack_bot_user_mapping"
        unique_together = [("slack_user_id", "team_id")]

    def __str__(self):
        return f"{self.slack_user_id} ({self.team_id}) → {self.user.email}"
