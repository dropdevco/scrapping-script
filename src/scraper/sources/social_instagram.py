"""Instagram Graph API — OWN-ACCOUNT scope only.

Two modes, both opt-in via ``platforms=["instagram"]`` so they never pollute a generic
trends request:
  • no query -> your own recent media + engagement (content performance signal)
  • query    -> limited *recent* media for a hashtag (IG's capped hashtag search)

It cannot search Instagram globally or read arbitrary users — that's an API limitation,
not a code one.
"""

from __future__ import annotations

from typing import Any

from ..core.config import settings
from ..core.http import HttpClient
from ..core.models import Kind, SearchParams, Trend
from . import auth_meta
from .base import Source

GRAPH = auth_meta.GRAPH


class InstagramSource(Source):
    name = "social_instagram"
    kind = Kind.TRENDS

    def is_configured(self) -> bool:
        return bool(settings.ig_access_token and settings.ig_business_account_id)

    async def fetch(self, params: SearchParams, http: HttpClient) -> list[Trend]:
        # Opt-in only: own-account data is irrelevant to arbitrary topic requests.
        if "instagram" not in params.platforms:
            return []
        token = settings.ig_access_token
        if params.query:
            return await self._hashtag_media(params, http, token)
        return await self._own_media(params, http, token)

    async def _own_media(self, params: SearchParams, http: HttpClient, token: str) -> list[Trend]:
        data = await http.get_json(
            f"{GRAPH}/{settings.ig_business_account_id}/media",
            params={
                "fields": "id,caption,media_type,permalink,timestamp,like_count,comments_count,media_url",
                "limit": min(params.limit, 50),
                "access_token": token,
            },
        )
        return [self._to_trend(m, params, own=True) for m in (data or {}).get("data", [])]

    async def _hashtag_media(self, params: SearchParams, http: HttpClient, token: str) -> list[Trend]:
        tag = params.query.lstrip("#")
        search = await http.get_json(
            f"{GRAPH}/ig_hashtag_search",
            params={"user_id": settings.ig_business_account_id, "q": tag, "access_token": token},
        )
        hits = (search or {}).get("data", [])
        if not hits:
            return []
        hashtag_id = hits[0]["id"]
        data = await http.get_json(
            f"{GRAPH}/{hashtag_id}/recent_media",
            params={
                "user_id": settings.ig_business_account_id,
                "fields": "id,caption,media_type,permalink,timestamp,like_count,comments_count",
                "limit": min(params.limit, 50),
                "access_token": token,
            },
        )
        return [self._to_trend(m, params, own=False) for m in (data or {}).get("data", [])]

    def _to_trend(self, m: dict[str, Any], params: SearchParams, own: bool) -> Trend:
        caption = m.get("caption") or ""
        likes = m.get("like_count")
        return Trend(
            source=self.name,
            platform="instagram",
            topic=params.query,
            captured_for=params.query,
            title=(caption[:80] or ("own post" if own else "hashtag post")),
            summary=caption or None,
            url=m.get("permalink"),
            score=float(likes) if likes is not None else None,
            engagement={
                "likes": likes,
                "comments": m.get("comments_count"),
                "media_type": m.get("media_type"),
                "own_account": own,
                "timestamp": m.get("timestamp"),
            },
            raw=m,
        )


SOURCE = InstagramSource()
