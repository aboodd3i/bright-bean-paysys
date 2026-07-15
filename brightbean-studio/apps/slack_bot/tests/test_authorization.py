"""Phase 2 — Authorization resolver tests.

Tests the full resolution chain from :class:`SlackAnalyticsRequest` to
:class:`ToolContext`, covering every fail-closed branch.
"""

from __future__ import annotations

import uuid

import pytest
from django.test import override_settings

from apps.accounts.models import User
from apps.members.models import (
    CustomRole,
    OrgMembership,
    WorkspaceMembership,
)
from apps.organizations.models import Organization
from apps.slack_bot.authorization import resolve_tool_context
from apps.slack_bot.contracts import SlackAnalyticsRequest
from apps.slack_bot.errors import ErrorCode
from apps.slack_bot.exceptions import AuthorizationError
from apps.slack_bot.models import SlackChannelMapping, SlackUserMapping
from apps.social_accounts.models import SocialAccount
from apps.workspaces.models import Workspace

pytestmark = pytest.mark.django_db

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def org():
    return Organization.objects.create(name="Test Org")


@pytest.fixture
def workspace(org):
    return Workspace.objects.create(organization=org, name="Test Workspace")


@pytest.fixture
def user():
    return User.objects.create_user(email="tester@example.com", password="x")


@pytest.fixture
def org_membership(user, org):
    return OrgMembership.objects.create(
        user=user,
        organization=org,
        org_role=OrgMembership.OrgRole.MEMBER,
    )


@pytest.fixture
def ws_membership(user, workspace):
    return WorkspaceMembership.objects.create(
        user=user,
        workspace=workspace,
        workspace_role=WorkspaceMembership.WorkspaceRole.EDITOR,
    )


@pytest.fixture
def channel_mapping(workspace):
    return SlackChannelMapping.objects.create(
        team_id="T0001",
        channel_id="C0001",
        workspace=workspace,
    )


@pytest.fixture
def user_mapping(user):
    return SlackUserMapping.objects.create(
        slack_user_id="U0001",
        team_id="T0001",
        user=user,
    )


@pytest.fixture
def social_accounts(workspace):
    """Create three accounts: connected, token_expiring, disconnected."""
    return {
        "connected": SocialAccount.objects.create(
            workspace=workspace,
            platform="instagram",
            account_platform_id="ig_1",
            account_name="IG Connected",
            connection_status=SocialAccount.ConnectionStatus.CONNECTED,
        ),
        "expiring": SocialAccount.objects.create(
            workspace=workspace,
            platform="facebook",
            account_platform_id="fb_1",
            account_name="FB Expiring",
            connection_status=SocialAccount.ConnectionStatus.TOKEN_EXPIRING,
        ),
        "disconnected": SocialAccount.objects.create(
            workspace=workspace,
            platform="linkedin_company",
            account_platform_id="li_1",
            account_name="LI Disconnected",
            connection_status=SocialAccount.ConnectionStatus.DISCONNECTED,
        ),
    }


def _make_request(
    team_id="T0001",
    channel_id="C0001",
    user_id="U0001",
    text="show me instagram analytics",
):
    return SlackAnalyticsRequest(
        correlation_id="corr-1",
        event_id="evt-1",
        team_id=team_id,
        channel_id=channel_id,
        user_id=user_id,
        thread_ts="1234567890.001",
        text=text,
    )


def _full_setup(
    org, workspace, user, org_membership, ws_membership, channel_mapping, user_mapping
):
    """No-op fixture combiner — just ensures all are created."""
    return None


# ---------------------------------------------------------------------------
# 17.1 — Team allowlist
# ---------------------------------------------------------------------------


class TestTeamAllowlist:
    @override_settings(SLACK_ALLOWED_TEAM_ID="T0001")
    def test_allowed_team_passes(
        self,
        org,
        workspace,
        user,
        org_membership,
        ws_membership,
        channel_mapping,
        user_mapping,
    ):
        ctx = resolve_tool_context(_make_request())
        assert ctx.slack_team_id == "T0001"

    @override_settings(SLACK_ALLOWED_TEAM_ID="T9999")
    def test_wrong_team_rejected(self):
        req = _make_request(team_id="T0001")
        with pytest.raises(AuthorizationError) as exc:
            resolve_tool_context(req)
        assert exc.value.error_code == ErrorCode.UNAUTHORIZED

    @override_settings(SLACK_ALLOWED_TEAM_ID="")
    def test_empty_allowlist_allows_all(
        self,
        org,
        workspace,
        user,
        org_membership,
        ws_membership,
        channel_mapping,
        user_mapping,
    ):
        ctx = resolve_tool_context(_make_request())
        assert ctx.slack_team_id == "T0001"


# ---------------------------------------------------------------------------
# 17.2 — Channel mapping
# ---------------------------------------------------------------------------


class TestChannelMapping:
    @pytest.fixture(autouse=True)
    def _allow_team(self, settings):
        settings.SLACK_ALLOWED_TEAM_ID = "T0001"

    def test_unmapped_channel_rejected(self, org, workspace, user):
        req = _make_request(channel_id="C_UNKNOWN")
        with pytest.raises(AuthorizationError) as exc:
            resolve_tool_context(req)
        assert exc.value.error_code == ErrorCode.CHANNEL_NOT_MAPPED


# ---------------------------------------------------------------------------
# 17.3 — User mapping
# ---------------------------------------------------------------------------


class TestUserMapping:
    @pytest.fixture(autouse=True)
    def _allow_team(self, settings):
        settings.SLACK_ALLOWED_TEAM_ID = "T0001"

    def test_unmapped_user_rejected(
        self,
        org,
        workspace,
        user,
        channel_mapping,
    ):
        req = _make_request(user_id="U_UNKNOWN")
        with pytest.raises(AuthorizationError) as exc:
            resolve_tool_context(req)
        assert exc.value.error_code == ErrorCode.USER_NOT_MAPPED


# ---------------------------------------------------------------------------
# 17.4 — Membership and permission
# ---------------------------------------------------------------------------


class TestMembershipAndPermission:
    @pytest.fixture(autouse=True)
    def _allow_team(self, settings):
        settings.SLACK_ALLOWED_TEAM_ID = "T0001"

    def test_no_org_membership_rejected(
        self,
        org,
        workspace,
        user,
        ws_membership,
        channel_mapping,
        user_mapping,
    ):
        """User has workspace membership but no org membership."""
        req = _make_request()
        with pytest.raises(AuthorizationError) as exc:
            resolve_tool_context(req)
        assert exc.value.error_code == ErrorCode.UNAUTHORIZED

    def test_no_workspace_membership_rejected(
        self,
        org,
        workspace,
        user,
        org_membership,
        channel_mapping,
        user_mapping,
    ):
        req = _make_request()
        with pytest.raises(AuthorizationError) as exc:
            resolve_tool_context(req)
        assert exc.value.error_code == ErrorCode.UNAUTHORIZED

    def test_contributor_role_rejected(
        self,
        org,
        workspace,
        user,
        org_membership,
        channel_mapping,
        user_mapping,
    ):
        """Contributor role lacks view_analytics permission."""
        WorkspaceMembership.objects.create(
            user=user,
            workspace=workspace,
            workspace_role=WorkspaceMembership.WorkspaceRole.CONTRIBUTOR,
        )
        req = _make_request()
        with pytest.raises(AuthorizationError) as exc:
            resolve_tool_context(req)
        assert exc.value.error_code == ErrorCode.UNAUTHORIZED

    def test_custom_role_without_permission_rejected(
        self,
        org,
        workspace,
        user,
        org_membership,
        channel_mapping,
        user_mapping,
    ):
        custom = CustomRole.objects.create(
            organization=org,
            name="No Analytics",
            permissions={"view_analytics": False, "create_posts": True},
        )
        WorkspaceMembership.objects.create(
            user=user,
            workspace=workspace,
            workspace_role=WorkspaceMembership.WorkspaceRole.EDITOR,
            custom_role=custom,
        )
        req = _make_request()
        with pytest.raises(AuthorizationError) as exc:
            resolve_tool_context(req)
        assert exc.value.error_code == ErrorCode.UNAUTHORIZED

    def test_custom_role_with_permission_allowed(
        self,
        org,
        workspace,
        user,
        org_membership,
        channel_mapping,
        user_mapping,
    ):
        custom = CustomRole.objects.create(
            organization=org,
            name="Analytics Viewer",
            permissions={"view_analytics": True, "create_posts": False},
        )
        WorkspaceMembership.objects.create(
            user=user,
            workspace=workspace,
            workspace_role=WorkspaceMembership.WorkspaceRole.CONTRIBUTOR,
            custom_role=custom,
        )
        ctx = resolve_tool_context(_make_request())
        assert ctx.user_id == user.id


# ---------------------------------------------------------------------------
# 17.5 — Account scoping
# ---------------------------------------------------------------------------


class TestAccountScoping:
    @pytest.fixture(autouse=True)
    def _allow_team(self, settings):
        settings.SLACK_ALLOWED_TEAM_ID = "T0001"

    def test_only_connected_and_expiring_accounts_included(
        self,
        org,
        workspace,
        user,
        org_membership,
        ws_membership,
        channel_mapping,
        user_mapping,
        social_accounts,
    ):
        ctx = resolve_tool_context(_make_request())
        assert social_accounts["connected"].id in ctx.allowed_account_ids
        assert social_accounts["expiring"].id in ctx.allowed_account_ids
        assert social_accounts["disconnected"].id not in ctx.allowed_account_ids
        assert len(ctx.allowed_account_ids) == 2

    def test_no_accounts_yields_empty_set(
        self,
        org,
        workspace,
        user,
        org_membership,
        ws_membership,
        channel_mapping,
        user_mapping,
    ):
        ctx = resolve_tool_context(_make_request())
        assert ctx.allowed_account_ids == frozenset()

    def test_can_access_account_method(
        self,
        org,
        workspace,
        user,
        org_membership,
        ws_membership,
        channel_mapping,
        user_mapping,
        social_accounts,
    ):
        ctx = resolve_tool_context(_make_request())
        assert ctx.can_access_account(social_accounts["connected"].id) is True
        assert ctx.can_access_account(social_accounts["disconnected"].id) is False
        assert ctx.can_access_account(uuid.uuid4()) is False


# ---------------------------------------------------------------------------
# 17.6 — ToolContext immutability and fields
# ---------------------------------------------------------------------------


class TestToolContext:
    @pytest.fixture(autouse=True)
    def _allow_team(self, settings):
        settings.SLACK_ALLOWED_TEAM_ID = "T0001"

    def test_context_is_frozen(
        self,
        org,
        workspace,
        user,
        org_membership,
        ws_membership,
        channel_mapping,
        user_mapping,
    ):
        ctx = resolve_tool_context(_make_request())
        with pytest.raises(AttributeError):
            ctx.workspace_id = uuid.uuid4()

    def test_context_fields(
        self,
        org,
        workspace,
        user,
        org_membership,
        ws_membership,
        channel_mapping,
        user_mapping,
    ):
        ctx = resolve_tool_context(_make_request())
        assert ctx.workspace_id == workspace.id
        assert ctx.user_id == user.id
        assert ctx.organization_id == org.id
        assert ctx.slack_team_id == "T0001"
        assert ctx.slack_channel_id == "C0001"
        assert isinstance(ctx.allowed_account_ids, frozenset)

    def test_allowed_account_ids_is_frozenset(
        self,
        org,
        workspace,
        user,
        org_membership,
        ws_membership,
        channel_mapping,
        user_mapping,
        social_accounts,
    ):
        ctx = resolve_tool_context(_make_request())
        assert isinstance(ctx.allowed_account_ids, frozenset)


# ---------------------------------------------------------------------------
# 17.7 — Workspace state
# ---------------------------------------------------------------------------


class TestWorkspaceState:
    @pytest.fixture(autouse=True)
    def _allow_team(self, settings):
        settings.SLACK_ALLOWED_TEAM_ID = "T0001"

    def test_archived_workspace_rejected(
        self,
        org,
        workspace,
        user,
        org_membership,
        ws_membership,
        channel_mapping,
        user_mapping,
    ):
        workspace.is_archived = True
        workspace.save()
        req = _make_request()
        with pytest.raises(AuthorizationError) as exc:
            resolve_tool_context(req)
        assert exc.value.error_code == ErrorCode.WORKSPACE_UNAVAILABLE

    def test_inactive_user_rejected(
        self,
        org,
        workspace,
        user,
        org_membership,
        ws_membership,
        channel_mapping,
        user_mapping,
    ):
        user.is_active = False
        user.save()
        req = _make_request()
        with pytest.raises(AuthorizationError) as exc:
            resolve_tool_context(req)
        assert exc.value.error_code == ErrorCode.UNAUTHORIZED


# ---------------------------------------------------------------------------
# 17.8 — Security: fail-closed
# ---------------------------------------------------------------------------


class TestFailClosed:
    @pytest.fixture(autouse=True)
    def _allow_team(self, settings):
        settings.SLACK_ALLOWED_TEAM_ID = "T0001"

    def test_no_mappings_at_all_rejected(self):
        """No channel or user mappings exist in the DB."""
        req = _make_request()
        with pytest.raises(AuthorizationError) as exc:
            resolve_tool_context(req)
        # Channel mapping is checked first
        assert exc.value.error_code == ErrorCode.CHANNEL_NOT_MAPPED

    def test_error_code_is_always_fatal(
        self,
        org,
        workspace,
        user,
        org_membership,
        ws_membership,
        channel_mapping,
        user_mapping,
    ):
        """All authorization errors must be fatal (not warnings)."""
        # Create a request with an unmapped user to trigger an error
        req = _make_request(user_id="U_EVIL")
        with pytest.raises(AuthorizationError) as exc:
            resolve_tool_context(req)
        assert ErrorCode.is_fatal(exc.value.error_code)
        assert not ErrorCode.is_warning(exc.value.error_code)

    def test_account_ids_never_come_from_request(
        self,
        org,
        workspace,
        user,
        org_membership,
        ws_membership,
        channel_mapping,
        user_mapping,
        social_accounts,
    ):
        """ToolContext.allowed_account_ids must come from DB, not request."""
        ctx = resolve_tool_context(_make_request())
        # The request text mentions "instagram" but all connected accounts
        # are included — the text does not filter the allowed set.
        assert social_accounts["connected"].id in ctx.allowed_account_ids
        assert social_accounts["expiring"].id in ctx.allowed_account_ids
