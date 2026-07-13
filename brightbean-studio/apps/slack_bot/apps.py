"""Django app config for the Slack analytics bot."""

from django.apps import AppConfig


class SlackBotConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.slack_bot"
    verbose_name = "Slack Analytics Bot"
