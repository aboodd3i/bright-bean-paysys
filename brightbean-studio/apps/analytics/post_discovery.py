"""Post discovery: import externally-published posts into Brightbean's database.

When posts are published directly on Facebook/Instagram/LinkedIn (not through
Brightbean), no ``PlatformPost`` record exists — so the analytics sync has
nothing to fetch per-post metrics for. This module fetches each platform's
recent feed and creates ``PlatformPost`` records for posts that aren't yet
tracked, making them visible to the existing analytics sync.
"""

from __future__ import annotations

import logging
from typing import Any

from django.db import transaction
from django.utils import timezone

from apps.composer.models import Post, PlatformPost
from apps.social_accounts.models import AnalyticsPlatformConfig, SocialAccount

logger = logging.getLogger(__name__)

# Platforms that support post discovery via get_recent_posts.
DISCOVERY_PLATFORMS = {"facebook", "instagram", "instagram_login", "linkedin_company", "linkedin_personal"}

# How many recent posts to fetch per account per sync cycle.
DISCOVERY_LIMIT = 25


def _resolve_provider(account: SocialAccount):
    """Resolve the provider for an account (mirrors analytics/tasks.py)."""
    from apps.credentials.models import resolve_platform_credentials
    from providers import get_provider

    credentials = resolve_platform_credentials(account.platform, account.workspace.organization_id)

    if account.platform == "mastodon" and account.instance_url:
        from apps.social_accounts.models import MastodonAppRegistration

        try:
            reg = MastodonAppRegistration.objects.get(instance_url=account.instance_url)
            credentials = {
                **credentials,
                "instance_url": account.instance_url,
                "client_id": reg.client_id,
                "client_secret": reg.client_secret,
            }
        except MastodonAppRegistration.DoesNotExist:
            pass
    elif account.platform == "facebook":
        credentials = {**credentials, "page_id": account.account_platform_id}
    elif account.platform == "instagram":
        credentials = {**credentials, "ig_user_id": account.account_platform_id}
    return get_provider(account.platform, credentials)


def _connected_accounts_for_discovery() -> list[SocialAccount]:
    """All connected accounts on platforms that support post discovery."""
    enabled = set(AnalyticsPlatformConfig.enabled_platforms())
    return list(
        SocialAccount.objects.filter(
            connection_status=SocialAccount.ConnectionStatus.CONNECTED,
            platform__in=DISCOVERY_PLATFORMS & (enabled | {"facebook", "instagram", "instagram_login"}),
        ).order_by("platform", "account_name")
    )


def discover_posts_for_account(account: SocialAccount, limit: int = DISCOVERY_LIMIT) -> int:
    """Fetch recent posts from the platform and import new ones.

    Returns the count of newly imported posts.
    """
    if account.platform not in DISCOVERY_PLATFORMS:
        return 0

    try:
        provider = _resolve_provider(account)
    except Exception:
        logger.exception("Failed to resolve provider for account %s", account.id)
        return 0

    try:
        recent_posts = provider.get_recent_posts(account.oauth_access_token, limit=limit)
    except NotImplementedError:
        logger.debug("Provider for %s does not support get_recent_posts", account.platform)
        return 0
    except Exception:
        logger.exception("Failed to fetch recent posts for account %s (%s)", account.id, account.platform)
        return 0

    if not recent_posts:
        return 0

    # Get existing platform_post_ids to avoid duplicates.
    existing_ids = set(
        PlatformPost.objects.filter(
            social_account=account,
            platform_post_id__in=[p["platform_post_id"] for p in recent_posts if p["platform_post_id"]],
        ).values_list("platform_post_id", flat=True)
    )

    imported = 0
    for post_data in recent_posts:
        platform_post_id = post_data.get("platform_post_id", "")
        if not platform_post_id or platform_post_id in existing_ids:
            continue

        published_at = post_data.get("published_at")
        caption = post_data.get("caption", "")

        try:
            with transaction.atomic():
                # Create a base Post in the workspace.
                base_post = Post.objects.create(
                    workspace=account.workspace,
                    caption=caption,
                    published_at=published_at,
                )

                # Create the PlatformPost linked to the account.
                PlatformPost.objects.create(
                    post=base_post,
                    social_account=account,
                    status=PlatformPost.Status.PUBLISHED,
                    platform_post_id=platform_post_id,
                    platform_specific_caption=caption,
                    published_at=published_at or timezone.now(),
                )

            imported += 1
            existing_ids.add(platform_post_id)
            logger.info(
                "Discovered and imported post %s for account %s (%s)",
                platform_post_id,
                account.account_name,
                account.platform,
            )
        except Exception:
            logger.exception("Failed to import post %s for account %s", platform_post_id, account.id)

    return imported


def discover_all_posts(limit: int = DISCOVERY_LIMIT) -> dict[str, int]:
    """Run post discovery for all eligible connected accounts.

    Returns a dict mapping account names to imported counts.
    """
    accounts = _connected_accounts_for_discovery()
    results: dict[str, int] = {}

    for account in accounts:
        count = discover_posts_for_account(account, limit=limit)
        key = f"{account.account_name} ({account.platform})"
        results[key] = count
        if count:
            logger.info("Discovered %d new posts for %s", count, key)

    return results
