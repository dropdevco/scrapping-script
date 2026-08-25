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


# graph.instagram.com refreshes with its own grant and does not accept the
# Facebook-Login one: `fb_exchange_token` against this host is not a real
# endpoint pairing, the same mismatch that made /debug_token 500 forever (see
# social/publish.py). The Instagram-Login refresh is unversioned by
# documentation and takes only the token it is extending — no app id or secret,
# which is why this path works on a repo that has neither set.
_IG_REFRESH = "https://graph.instagram.com/refresh_access_token"


async def refresh_long_lived(http: HttpClient, token: str) -> tuple[str, int] | None:
    """Extend a valid long-lived token's ~60-day window.

    Returns (new_token, seconds_until_expiry), or None if the refresh failed.
    The token must still be valid and at least 24 hours old for Meta to extend
    it — a refresh is not a resurrection, so this cannot rescue a token that has
    already expired.
    """
    try:
        if GRAPH.startswith("https://graph.instagram.com"):
            data = await http.get_json(
                _IG_REFRESH,
                params={"grant_type": "ig_refresh_token", "access_token": token},
            )
        else:
            if not (settings.meta_app_id and settings.meta_app_secret):
                return None
            data = await http.get_json(
                f"{GRAPH}/oauth/access_token",
                params={
                    "grant_type": "fb_exchange_token",
                    "client_id": settings.meta_app_id,
                    "client_secret": settings.meta_app_secret,
                    "fb_exchange_token": token,
                },
            )
        fresh = (data or {}).get("access_token")
        if not fresh:
            return None
        return fresh, int((data or {}).get("expires_in") or 0)
    except Exception as exc:  # noqa: BLE001
        log.warning("token refresh failed: %s", exc)
        return None
