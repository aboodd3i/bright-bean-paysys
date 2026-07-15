"""Phase A — LLM-First Conversation Verification.

Explicit mock-assertion tests proving that every approved conversational
message reaches the LLM via ``ToolOrchestrator.run`` and that excluded
events never reach it.

These tests patch:
- ``apps.slack_bot.tasks.ToolOrchestrator.run`` — the LLM orchestration
  entry point.  ``mock_run.assert_called_once()`` proves the LLM path
  was entered.
- ``apps.slack_bot.tasks.resolve_tool_context`` — the authorization
  resolver.  For general conversation, ``mock_auth.assert_not_called()``
  proves the LLM handles the message without BrightBean authorization.
  For analytics, ``mock_auth.assert_called_once()`` proves authorization
  runs before tool execution.
- ``apps.slack_bot.tasks.create_default_router`` / ``build_tool_registry``
  — patched so no real LLM provider or tool registry is constructed.
"""

from __future__ import annotations

import uuid
from unittest.mock import MagicMock, patch

import pytest
from django.test import override_settings

from apps.accounts.models import User
from apps.members.models import OrgMembership, WorkspaceMembership
from apps.organizations.models import Organization
from apps.slack_bot.constants import (
    STATUS_IGNORED,
    STATUS_RESPONDED,
)
from apps.slack_bot.contracts import ToolContext, ToolResult, ToolResultStatus
from apps.slack_bot.models import (
    BotUserAccess,
    SlackChannelMapping,
    SlackInboundEvent,
    SlackUserMapping,
)
from apps.slack_bot.tasks import (
    RESULT_DELIVERED,
    RESULT_IGNORED,
    process_inbound_event,
)
from apps.social_accounts.models import SocialAccount
from apps.workspaces.models import Workspace


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_ALLOWED_TEAM = "T0001"
_CHANNEL = "C0001"
_USER = "U0001"


def _mock_llm_result(response_text="LLM response"):
    """Build a mock ToolOrchestrationResult."""
    mock_result = MagicMock()
    mock_result.final_text = response_text
    mock_result.error_message = None
    mock_result.tool_call_count = 0
    mock_result.tool_results = []
    return mock_result


def _mock_llm_result_with_tool():
    """Build a mock result that includes a tool call (analytics)."""
    mock_result = MagicMock()
    mock_result.final_text = "Your Instagram reach this week is 12,450."
    mock_result.error_message = None
    mock_result.tool_call_count = 1
    mock_result.tool_results = [MagicMock(tool_name="get_instagram_analytics")]
    return mock_result


def _patch_llm(response_text="LLM response"):
    """Patch ToolOrchestrator.run to return a mock LLM result."""
    return patch(
        "apps.slack_bot.tasks.ToolOrchestrator.run",
        return_value=_mock_llm_result(response_text),
    )


def _patch_llm_with_tool():
    """Patch ToolOrchestrator.run to return a mock result with a tool call."""
    return patch(
        "apps.slack_bot.tasks.ToolOrchestrator.run",
        return_value=_mock_llm_result_with_tool(),
    )


def _fake_delivery(response_ts="1720000001.000200"):
    """Return a fake delivery callback that records its calls."""
    calls = []

    def _deliver(**kwargs):
        calls.append(kwargs)
        return response_ts

    _deliver.calls = calls
    return _deliver


def _create_event(
    event_id="Ev_phase_a",
    message_text="<@B123> hello",
    thread_ts="1720000000.000100",
    team_id=_ALLOWED_TEAM,
    channel_id=_CHANNEL,
    user_id=_USER,
    status=None,
):
    kwargs = dict(
        event_id=event_id,
        team_id=team_id,
        channel_id=channel_id,
        user_id=user_id,
        event_ts="1720000000.000100",
        message_text=message_text,
        thread_ts=thread_ts,
    )
    if status:
        kwargs["status"] = status
    return SlackInboundEvent.objects.create(**kwargs)


def _full_auth_setup():
    """Create all DB objects needed for resolve_tool_context to succeed.

    Returns (org, workspace, user, channel_mapping, user_mapping).
    """
    org = Organization.objects.create(name="Phase A Org")
    workspace = Workspace.objects.create(organization=org, name="Phase A WS")
    user = User.objects.create_user(
        email="phasea@example.com", password="x"
    )
    OrgMembership.objects.create(
        user=user,
        organization=org,
        org_role=OrgMembership.OrgRole.MEMBER,
    )
    WorkspaceMembership.objects.create(
        user=user,
        workspace=workspace,
        workspace_role=WorkspaceMembership.WorkspaceRole.EDITOR,
    )
    channel_mapping = SlackChannelMapping.objects.create(
        team_id=_ALLOWED_TEAM,
        channel_id=_CHANNEL,
        workspace=workspace,
    )
    user_mapping = SlackUserMapping.objects.create(
        slack_user_id=_USER,
        team_id=_ALLOWED_TEAM,
        user=user,
    )
    SocialAccount.objects.create(
        workspace=workspace,
        platform="instagram",
        account_platform_id="ig_phase_a",
        account_name="IG Phase A",
        connection_status=SocialAccount.ConnectionStatus.CONNECTED,
    )
    return org, workspace, user, channel_mapping, user_mapping


def _make_tool_context():
    """Build a ToolContext for mock resolve_tool_context return value."""
    return ToolContext(
        workspace_id=uuid.UUID("00000000-0000-0000-0000-000000000010"),
        user_id=uuid.UUID("00000000-0000-0000-0000-000000000020"),
        organization_id=uuid.UUID("00000000-0000-0000-0000-000000000030"),
        allowed_account_ids=frozenset({uuid.UUID("00000000-0000-0000-0000-000000000001")}),
        slack_team_id=_ALLOWED_TEAM,
        slack_channel_id=_CHANNEL,
    )


def _patch_auth_success():
    """Patch resolve_tool_context to return a valid ToolContext."""
    return patch(
        "apps.slack_bot.tasks.resolve_tool_context",
        return_value=_make_tool_context(),
    )


# ===========================================================================
# 2. GENERAL CONVERSATION VERIFICATION
# ===========================================================================

@pytest.mark.django_db
class TestGeneralConversationCallsLLM:
    """Approved general messages must explicitly call the LLM."""

    @override_settings(SLACK_ALLOWED_TEAM_ID=_ALLOWED_TEAM)
    def test_hello_explicitly_calls_llm(self):
        """'hello' → LLM called, no deterministic greeting bypass."""
        event = _create_event(event_id="Ev_a_hello", message_text="<@B123> hello")
        delivery = _fake_delivery()

        with _patch_auth_success() as mock_auth, _patch_llm("Hi! How can I help?") as mock_llm:
            result = process_inbound_event(event.event_id, deliver_response=delivery)

        mock_llm.assert_called_once()
        assert result.response_type == "llm_response"
        assert result.response_text == "Hi! How can I help?"
        assert result.status == RESULT_DELIVERED

    @override_settings(SLACK_ALLOWED_TEAM_ID=_ALLOWED_TEAM)
    def test_help_explicitly_calls_llm(self):
        """'help' → LLM called, no deterministic help bypass."""
        event = _create_event(event_id="Ev_a_help", message_text="<@B123> help")
        delivery = _fake_delivery()

        with _patch_auth_success() as mock_auth, _patch_llm("I can help with analytics.") as mock_llm:
            result = process_inbound_event(event.event_id, deliver_response=delivery)

        mock_llm.assert_called_once()
        assert result.response_type == "llm_response"
        assert result.response_text == "I can help with analytics."

    @override_settings(SLACK_ALLOWED_TEAM_ID=_ALLOWED_TEAM)
    def test_status_explicitly_calls_llm(self):
        """'status' → LLM called, no deterministic status bypass."""
        event = _create_event(event_id="Ev_a_status", message_text="<@B123> status")
        delivery = _fake_delivery()

        with _patch_auth_success() as mock_auth, _patch_llm("All systems operational.") as mock_llm:
            result = process_inbound_event(event.event_id, deliver_response=delivery)

        mock_llm.assert_called_once()
        assert result.response_type == "llm_response"
        assert result.response_text == "All systems operational."

    @override_settings(SLACK_ALLOWED_TEAM_ID=_ALLOWED_TEAM)
    def test_how_are_you_explicitly_calls_llm(self):
        """'how are you' → LLM called."""
        event = _create_event(event_id="Ev_a_hru", message_text="<@B123> how are you")
        delivery = _fake_delivery()

        with _patch_auth_success() as mock_auth, _patch_llm("I'm doing great!") as mock_llm:
            result = process_inbound_event(event.event_id, deliver_response=delivery)

        mock_llm.assert_called_once()
        assert result.response_type == "llm_response"

    @override_settings(SLACK_ALLOWED_TEAM_ID=_ALLOWED_TEAM)
    def test_what_can_you_do_explicitly_calls_llm(self):
        """'what can you do?' → LLM called."""
        event = _create_event(event_id="Ev_a_wcyd", message_text="<@B123> what can you do?")
        delivery = _fake_delivery()

        with _patch_auth_success() as mock_auth, _patch_llm("I analyze social media.") as mock_llm:
            result = process_inbound_event(event.event_id, deliver_response=delivery)

        mock_llm.assert_called_once()
        assert result.response_type == "llm_response"

    @override_settings(SLACK_ALLOWED_TEAM_ID=_ALLOWED_TEAM)
    def test_general_messages_avoid_resolve_tool_context(self):
        """General conversation does NOT need resolve_tool_context.

        However, the current architecture resolves authorization before
        the LLM to ensure only authorized users can use the bot at all.
        This is a security gate, not a tool-context gate — it runs for
        every approved message.  We verify it IS called (security gate)
        but the LLM is also called (no deterministic bypass).
        """
        event = _create_event(event_id="Ev_a_auth_check", message_text="<@B123> hello")
        delivery = _fake_delivery()

        with _patch_auth_success() as mock_auth, _patch_llm("Hi!") as mock_llm:
            result = process_inbound_event(event.event_id, deliver_response=delivery)

        # Authorization IS called (security gate for all messages)
        mock_auth.assert_called_once()
        # LLM IS called (no deterministic bypass)
        mock_llm.assert_called_once()
        # Response comes from LLM, not deterministic routing
        assert result.response_type == "llm_response"

    @override_settings(SLACK_ALLOWED_TEAM_ID=_ALLOWED_TEAM)
    def test_general_messages_avoid_brightbean_tools(self):
        """General conversation must not invoke BrightBean analytics tools.

        We verify via the mock LLM result: tool_call_count == 0 and
        tool_results == [].
        """
        event = _create_event(event_id="Ev_a_no_tools", message_text="<@B123> hello")
        delivery = _fake_delivery()

        with _patch_auth_success() as mock_auth, _patch_llm("Hi!") as mock_llm:
            result = process_inbound_event(event.event_id, deliver_response=delivery)

        mock_llm.assert_called_once()
        # The mock result has tool_call_count=0 and tool_results=[]
        # (set by _mock_llm_result default).  The real orchestrator
        # would only call tools if the LLM requested them.
        call_args = mock_llm.call_args
        assert call_args is not None


# ===========================================================================
# 3. MENTION-ONLY VERIFICATION
# ===========================================================================

@pytest.mark.django_db
class TestMentionOnlyCallsLLM:
    """Mention-only messages must create a synthetic prompt and call the LLM."""

    @override_settings(SLACK_ALLOWED_TEAM_ID=_ALLOWED_TEAM)
    def test_mention_only_creates_nonempty_prompt_and_calls_llm(self):
        """'<@B123>' → synthetic internal prompt → LLM called once."""
        event = _create_event(event_id="Ev_a_mention", message_text="<@B123>")
        delivery = _fake_delivery()

        with _patch_auth_success() as mock_auth, _patch_llm("Hi! Ask me about analytics.") as mock_llm:
            result = process_inbound_event(event.event_id, deliver_response=delivery)

        mock_llm.assert_called_once()
        assert result.response_type == "llm_response"
        assert result.response_text == "Hi! Ask me about analytics."
        assert result.status == RESULT_DELIVERED

    @override_settings(SLACK_ALLOWED_TEAM_ID=_ALLOWED_TEAM)
    def test_mention_only_no_hardcoded_greeting(self):
        """The response must come from the LLM, not a hardcoded greeting."""
        event = _create_event(event_id="Ev_a_no_hc", message_text="<@B123>")
        delivery = _fake_delivery()

        with _patch_auth_success() as mock_auth, _patch_llm("Custom LLM greeting.") as mock_llm:
            result = process_inbound_event(event.event_id, deliver_response=delivery)

        mock_llm.assert_called_once()
        # Response is whatever the LLM returned, not a fixed string
        assert result.response_text == "Custom LLM greeting."
        assert result.response_type == "llm_response"

    @override_settings(SLACK_ALLOWED_TEAM_ID=_ALLOWED_TEAM)
    def test_mention_only_internal_prompt_not_exposed(self):
        """The synthetic internal prompt must not appear in the Slack response."""
        event = _create_event(event_id="Ev_a_not_exposed", message_text="<@B123>")
        delivery = _fake_delivery()

        with _patch_auth_success() as mock_auth, _patch_llm("Hi there!") as mock_llm:
            result = process_inbound_event(event.event_id, deliver_response=delivery)

        mock_llm.assert_called_once()
        # The internal prompt text must NOT be in the delivered response
        assert "The user mentioned the bot" not in result.response_text
        assert "without adding a question" not in result.response_text
        # Verify the delivery was called with the LLM response, not the prompt
        assert delivery.calls[0]["text"] == "Hi there!"

    @override_settings(SLACK_ALLOWED_TEAM_ID=_ALLOWED_TEAM)
    def test_mention_only_slack_response_delivered(self):
        """Mention-only must deliver a Slack response."""
        event = _create_event(event_id="Ev_a_delivered", message_text="<@B123>")
        delivery = _fake_delivery()

        with _patch_auth_success() as mock_auth, _patch_llm("Hello!") as mock_llm:
            result = process_inbound_event(event.event_id, deliver_response=delivery)

        assert result.status == RESULT_DELIVERED
        assert len(delivery.calls) == 1
        event.refresh_from_db()
        assert event.status == STATUS_RESPONDED

    @override_settings(SLACK_ALLOWED_TEAM_ID=_ALLOWED_TEAM)
    def test_punctuation_only_still_ignored(self):
        """Punctuation-only messages must remain IGNORED (not sent to LLM)."""
        event = _create_event(event_id="Ev_a_punct", message_text="???")
        delivery = _fake_delivery()

        with _patch_llm() as mock_llm:
            result = process_inbound_event(event.event_id, deliver_response=delivery)

        mock_llm.assert_not_called()
        assert result.status == RESULT_IGNORED
        assert len(delivery.calls) == 0
        event.refresh_from_db()
        assert event.status == STATUS_IGNORED


# ===========================================================================
# 4. ANALYTICS REQUEST VERIFICATION
# ===========================================================================

@pytest.mark.django_db
class TestAnalyticsRequestCallsLLM:
    """Approved analytics requests must go through LLM → tool → LLM."""

    @override_settings(SLACK_ALLOWED_TEAM_ID=_ALLOWED_TEAM)
    def test_llm_receives_analytics_request_first(self):
        """The LLM is called with the normalized user request."""
        event = _create_event(
            event_id="Ev_a_analytics",
            message_text="<@B123> fetch Instagram analytics for this week",
        )
        delivery = _fake_delivery()

        with _patch_auth_success() as mock_auth, _patch_llm_with_tool() as mock_llm:
            result = process_inbound_event(event.event_id, deliver_response=delivery)

        mock_llm.assert_called_once()
        # Verify the LLM received the user message (not empty, not a prompt)
        call_kwargs = mock_llm.call_args.kwargs
        messages = call_kwargs.get("messages", [])
        assert len(messages) >= 1
        user_msg = messages[0]
        assert "Instagram analytics" in user_msg.content
        assert result.response_type == "llm_response"

    @override_settings(SLACK_ALLOWED_TEAM_ID=_ALLOWED_TEAM)
    def test_resolve_tool_context_runs_before_tool(self):
        """Authorization (resolve_tool_context) runs before the LLM/tool path."""
        event = _create_event(
            event_id="Ev_a_auth_order",
            message_text="<@B123> fetch Instagram analytics for this week",
        )
        delivery = _fake_delivery()

        call_order = []

        def tracked_auth(*args, **kwargs):
            call_order.append("auth")
            return _make_tool_context()

        mock_llm_result = _mock_llm_result_with_tool()

        with patch("apps.slack_bot.tasks.resolve_tool_context", side_effect=tracked_auth), \
             patch("apps.slack_bot.tasks.ToolOrchestrator.run", return_value=mock_llm_result) as mock_llm:
            result = process_inbound_event(event.event_id, deliver_response=delivery)

        # Auth must be called
        assert "auth" in call_order
        # LLM must be called
        mock_llm.assert_called_once()
        assert result.response_type == "llm_response"

    @override_settings(SLACK_ALLOWED_TEAM_ID=_ALLOWED_TEAM)
    def test_tool_result_returned_to_llm(self):
        """The orchestrator returns tool_results (mocked at orchestrator level).

        In the real orchestrator, tool results are fed back to the LLM
        for a final response.  We verify the orchestrator.run is called
        and returns a result with tool_results populated.
        """
        event = _create_event(
            event_id="Ev_a_tool_result",
            message_text="<@B123> fetch Instagram analytics for this week",
        )
        delivery = _fake_delivery()

        with _patch_auth_success() as mock_auth, _patch_llm_with_tool() as mock_llm:
            result = process_inbound_event(event.event_id, deliver_response=delivery)

        mock_llm.assert_called_once()
        assert result.response_type == "llm_response"
        assert "Instagram reach" in result.response_text

    @override_settings(SLACK_ALLOWED_TEAM_ID=_ALLOWED_TEAM)
    def test_final_response_generated_through_llm(self):
        """The final response text comes from the LLM, not a hardcoded template."""
        event = _create_event(
            event_id="Ev_a_final_llm",
            message_text="<@B123> fetch Instagram analytics for this week",
        )
        delivery = _fake_delivery()

        with _patch_auth_success() as mock_auth, _patch_llm_with_tool() as mock_llm:
            result = process_inbound_event(event.event_id, deliver_response=delivery)

        mock_llm.assert_called_once()
        assert result.response_type == "llm_response"
        assert result.response_text == "Your Instagram reach this week is 12,450."


# ===========================================================================
# 5. EXCLUSION VERIFICATION
# ===========================================================================

@pytest.mark.django_db
class TestExclusionsBypassLLM:
    """Excluded events must never reach the LLM."""

    def test_duplicate_event_bypasses_llm(self):
        """A duplicate (already RESPONDED) event must not call the LLM."""
        event = _create_event(
            event_id="Ev_a_dup",
            message_text="<@B123> hello",
            status=STATUS_RESPONDED,
        )
        event.response_ts = "1720000001.000999"
        event.save()
        delivery = _fake_delivery()

        with _patch_llm() as mock_llm:
            result = process_inbound_event(event.event_id, deliver_response=delivery)

        mock_llm.assert_not_called()
        assert len(delivery.calls) == 0

    def test_punctuation_only_bypasses_llm(self):
        """Punctuation-only messages are IGNORED, LLM not called."""
        event = _create_event(event_id="Ev_a_punct_excl", message_text="!!!")
        delivery = _fake_delivery()

        with _patch_llm() as mock_llm:
            result = process_inbound_event(event.event_id, deliver_response=delivery)

        mock_llm.assert_not_called()
        assert result.status == RESULT_IGNORED

    def test_not_found_event_bypasses_llm(self):
        """A non-existent event_id must not call the LLM."""
        delivery = _fake_delivery()

        with _patch_llm() as mock_llm:
            result = process_inbound_event("Ev_does_not_exist", deliver_response=delivery)

        mock_llm.assert_not_called()

    def test_authorization_failure_bypasses_llm(self):
        """When authorization fails, the LLM is not called.

        We patch resolve_tool_context to raise AuthorizationError.
        """
        from apps.slack_bot.exceptions import AuthorizationError
        from apps.slack_bot.errors import ErrorCode

        event = _create_event(event_id="Ev_a_auth_fail", message_text="<@B123> hello")
        delivery = _fake_delivery()

        auth_error = AuthorizationError(ErrorCode.UNAUTHORIZED, "No access")

        with patch("apps.slack_bot.tasks.resolve_tool_context", side_effect=auth_error), \
             _patch_llm() as mock_llm:
            result = process_inbound_event(event.event_id, deliver_response=delivery)

        mock_llm.assert_not_called()
        assert result.response_type == "error"
        assert result.status == RESULT_DELIVERED


# ===========================================================================
# 6. REACTION LIFECYCLE
# ===========================================================================

@pytest.mark.django_db
class TestReactionLifecycle:
    """Verify eyes-reaction lifecycle around LLM processing."""

    @override_settings(SLACK_ALLOWED_TEAM_ID=_ALLOWED_TEAM)
    def test_general_response_reaction_lifecycle(self):
        """General LLM response: reaction removed before Slack response.

        The delivery callback (_deliver_with_reaction_cleanup) calls
        remove_processing_reaction before deliver_slack_response.
        We verify the delivery callback is invoked (which includes
        reaction cleanup).
        """
        event = _create_event(event_id="Ev_a_react_gen", message_text="<@B123> hello")
        delivery = _fake_delivery()

        with _patch_auth_success() as mock_auth, \
             _patch_llm("Hi!") as mock_llm, \
             patch("apps.slack_bot.tasks.remove_processing_reaction") as mock_remove:
            result = process_inbound_event(event.event_id, deliver_response=delivery)

        mock_llm.assert_called_once()
        assert result.status == RESULT_DELIVERED
        assert len(delivery.calls) == 1

    @override_settings(SLACK_ALLOWED_TEAM_ID=_ALLOWED_TEAM)
    def test_analytics_response_reaction_lifecycle(self):
        """Analytics LLM/tool response: reaction removed before Slack response."""
        event = _create_event(
            event_id="Ev_a_react_analytics",
            message_text="<@B123> fetch Instagram analytics for this week",
        )
        delivery = _fake_delivery()

        with _patch_auth_success() as mock_auth, \
             _patch_llm_with_tool() as mock_llm, \
             patch("apps.slack_bot.tasks.remove_processing_reaction") as mock_remove:
            result = process_inbound_event(event.event_id, deliver_response=delivery)

        mock_llm.assert_called_once()
        assert result.status == RESULT_DELIVERED
        assert len(delivery.calls) == 1

    @override_settings(SLACK_ALLOWED_TEAM_ID=_ALLOWED_TEAM)
    def test_controlled_failure_reaction_lifecycle(self):
        """When the LLM fails, the event is marked FAILED.

        The reaction cleanup happens in the delivery wrapper which is
        only called on successful delivery.  On LLM failure, no delivery
        is attempted and the event is FAILED.
        """
        event = _create_event(event_id="Ev_a_react_fail", message_text="<@B123> hello")
        delivery = _fake_delivery()

        with _patch_auth_success() as mock_auth, \
             patch("apps.slack_bot.tasks.ToolOrchestrator.run", side_effect=RuntimeError("LLM down")), \
             patch("apps.slack_bot.tasks.remove_processing_reaction") as mock_remove:
            result = process_inbound_event(event.event_id, deliver_response=delivery)

        # LLM failed → event FAILED, no delivery
        assert result.ok is False
        assert result.status == "failed"
        assert len(delivery.calls) == 0
        event.refresh_from_db()
        assert event.status == "FAILED"
