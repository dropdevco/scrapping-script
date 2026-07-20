"""Trending discussions from Reddit (free app-only OAuth, read-only).

Set up a 'script' app at https://www.reddit.com/prefs/apps to get the client id/secret.
"""

from __future__ import annotations

import base64
from typing import Any

from ..core.config import settings
from ..core.http import HttpClient
from ..core.models import Kind, SearchParams, Trend
from .base import Source

_TOKEN_URL = "https://www.reddit.com/api/v1/access_token"
_API = "https://oauth.reddit.com"
_ALLOWED_T = {"day", "week", "month", "year", "all"}


class RedditSource(Source):
    name = "trends_reddit"
    kind = Kind.TRENDS

    def is_configured(self) -> bool:
        return bool(settings.reddit_client_id and settings.reddit_client_secret)

    async def fetch(self, params: SearchParams, http: HttpClient) -> list[Trend]:
        if params.platforms and "reddit" not in params.platforms:
            return []

        token = await self._token(http)
        headers = {"Authorization": f"Bearer {token}", "User-Agent": settings.reddit_user_agent}
        t = params.timeframe if params.timeframe in _ALLOWED_T else "week"
        limit = min(params.limit, 100)

        if params.query:
            url = f"{_API}/search"
            q = {"q": params.query, "sort": "top", "t": t, "type": "link", "limit": limit}
        else:
            url = f"{_API}/r/all/top"
            q = {"t": t, "limit": limit}

        data = await http.get_json(url, params=q, headers=headers)
        children = (data or {}).get("data", {}).get("children", [])
        out: list[Trend] = []
        for child in children:
            d: dict[str, Any] = child.get("data", {})
            ups = d.get("ups") or d.get("score") or 0
            out.append(
                Trend(
                    source=self.name,
                    platform="reddit",
                    topic=params.query,
                    captured_for=params.query,
                    title=d.get("title") or "(untitled)",
                    summary=(d.get("selftext") or "")[:500] or None,
                    url="https://www.reddit.com" + d.get("permalink", ""),
                    score=float(ups),
                    engagement={
                        "upvotes": ups,
                        "comments": d.get("num_comments"),
                        "subreddit": d.get("subreddit"),
                        "ratio": d.get("upvote_ratio"),
                    },
                    raw=d,
                )
            )
        return out

    async def _token(self, http: HttpClient) -> str:
        creds = f"{settings.reddit_client_id}:{settings.reddit_client_secret}"
        basic = base64.b64encode(creds.encode()).decode()
        resp = await http.request(
            "POST",
            _TOKEN_URL,
            headers={
                "Authorization": f"Basic {basic}",
                "User-Agent": settings.reddit_user_agent,
            },
            data={"grant_type": "client_credentials"},
        )
        resp.raise_for_status()
        return resp.json()["access_token"]


SOURCE = RedditSource()
