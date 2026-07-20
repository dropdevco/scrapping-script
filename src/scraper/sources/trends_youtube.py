"""Trending / most-viewed videos from the YouTube Data API v3 (free quota)."""

from __future__ import annotations

from typing import Any

from ..core.config import settings
from ..core.http import HttpClient
from ..core.models import Kind, SearchParams, Trend
from ..core.timeutil import since_iso
from .base import Source

_SEARCH = "https://www.googleapis.com/youtube/v3/search"
_VIDEOS = "https://www.googleapis.com/youtube/v3/videos"


class YouTubeSource(Source):
    name = "trends_youtube"
    kind = Kind.TRENDS

    def is_configured(self) -> bool:
        return bool(settings.youtube_api_key)

    async def fetch(self, params: SearchParams, http: HttpClient) -> list[Trend]:
        if params.platforms and "youtube" not in params.platforms:
            return []
        limit = min(params.limit, 50)

        if params.query:
            search = await http.get_json(
                _SEARCH,
                params={
                    "key": settings.youtube_api_key,
                    "q": params.query,
                    "part": "snippet",
                    "type": "video",
                    "order": "viewCount",
                    "publishedAfter": since_iso(params.timeframe),
                    "maxResults": limit,
                },
            )
            ids = [i["id"]["videoId"] for i in (search or {}).get("items", []) if i.get("id", {}).get("videoId")]
            if not ids:
                return []
            data = await http.get_json(
                _VIDEOS,
                params={
                    "key": settings.youtube_api_key,
                    "id": ",".join(ids),
                    "part": "snippet,statistics",
                },
            )
        else:
            data = await http.get_json(
                _VIDEOS,
                params={
                    "key": settings.youtube_api_key,
                    "chart": "mostPopular",
                    "regionCode": "US",
                    "part": "snippet,statistics",
                    "maxResults": limit,
                },
            )

        out: list[Trend] = []
        for v in (data or {}).get("items", []):
            snip: dict[str, Any] = v.get("snippet", {})
            stats: dict[str, Any] = v.get("statistics", {})
            views = int(stats.get("viewCount", 0) or 0)
            out.append(
                Trend(
                    source=self.name,
                    platform="youtube",
                    topic=params.query,
                    captured_for=params.query,
                    title=snip.get("title") or "(untitled)",
                    summary=(snip.get("description") or "")[:500] or None,
                    url=f"https://www.youtube.com/watch?v={v.get('id')}",
                    score=float(views),
                    engagement={
                        "views": views,
                        "likes": int(stats.get("likeCount", 0) or 0),
                        "comments": int(stats.get("commentCount", 0) or 0),
                        "channel": snip.get("channelTitle"),
                        "published": snip.get("publishedAt"),
                    },
                    raw=v,
                )
            )
        return out


SOURCE = YouTubeSource()
