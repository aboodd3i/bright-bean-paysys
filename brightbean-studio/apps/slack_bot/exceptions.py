"""Custom exceptions for the Slack analytics bot."""


class SlackBotError(Exception):
    """Base exception for all Slack bot errors."""


class SlackSignatureError(SlackBotError):
    """Raised when Slack request signature verification fails."""


class SlackEventParseError(SlackBotError):
    """Raised when a Slack event payload cannot be parsed."""


class SlackNormalizationError(SlackBotError):
    """Raised when a Slack message cannot be normalized to meaningful text."""


class SlackDeliveryError(SlackBotError):
    """Raised when Slack message delivery fails."""


class AuthorizationError(SlackBotError):
    """Raised when authorization resolution fails.

    Carries a stable :class:`~apps.slack_bot.errors.ErrorCode` so the
    caller can produce a machine-readable error response without
    inspecting the exception message.

    Attributes
    ----------
    error_code : ErrorCode
        Stable machine-readable code for the failure.
    """

    def __init__(self, error_code, message=""):
        self.error_code = error_code
        super().__init__(message or str(error_code))
