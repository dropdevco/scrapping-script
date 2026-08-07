"""Meta (Instagram Graph + Threads) token helpers.

For own-account use you paste a long-lived token into ``.env`` (see README for how to mint
one). Long-lived tokens last ~60 days; ``refresh_long_lived`` extends them so a scheduled
job can keep itself alive. There is no global-search capability here — these tokens only
read the account they belong to.
"""

from __future__ import annotations

import logging

from ..core.config import settings
from ..core.http import HttpClient

log = logging.getLogger("scraper.auth_meta")

# Confirmed empirically against a real account (2026-08-06): this app went
# through Meta's newer "Instagram API with Instagram Login" setup (a
# standalone Instagram Business Login, no Facebook Page in the loop) rather
# than the older Facebook-Login-based flow. That flow's tokens and IDs
# (IGAA-prefixed token, a distinct numeric Instagram user id) are only valid
# against graph.instagram.com — graph.facebook.com rejects them outright.
# If this project ever adds a second account that went through the classic
# Facebook-Login flow instead, that one would need graph.facebook.com; this
# constant is shared because every current use (publishing, own-media reads)
# is the same account through the same Instagram Login flow.
GRAPH = "https://graph.instagram.com/v21.0"
THREADS = "https://graph.threads.net/v1.0"


def ig_token() -> str | None:
    return settings.ig_access_token


def threads_token() -> str | None:
    return settings.threads_access_token


async def refresh_long_lived(http: HttpClient, token: str) -> str | None:
    """Exchange a valid long-lived FB token for a fresh one (extends the ~60-day window)."""
    if not (settings.meta_app_id and settings.meta_app_secret):
        return None
    try:
        data = await http.get_json(
            f"{GRAPH}/oauth/access_token",
            params={
                "grant_type": "fb_exchange_token",
                "client_id": settings.meta_app_id,
                "client_secret": settings.meta_app_secret,
                "fb_exchange_token": token,
            },
        )
        return (data or {}).get("access_token")
    except Exception as exc:  # noqa: BLE001
        log.warning("token refresh failed: %s", exc)
        return None
