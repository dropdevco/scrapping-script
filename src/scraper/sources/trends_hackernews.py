"""Trending tech/startup/business stories from Hacker News (Algolia API, keyless)."""

from __future__ import annotations

from typing import Any

from ..core.http import HttpClient
from ..core.models import Kind, SearchParams, Trend
from ..core.timeutil import since_epoch
from .base import Source

_SEARCH = "https://hn.algolia.com/api/v1/search"


class HackerNewsSource(Source):
    name = "trends_hackernews"
    kind = Kind.TRENDS

    def is_configured(self) -> bool:
        return True  # keyless

    async def fetch(self, params: SearchParams, http: HttpClient) -> list[Trend]:
        if "hackernews" not in params.platforms and params.platforms:
            return []
        query: dict[str, Any] = {
            "tags": "story",
            "hitsPerPage": min(params.limit, 50),
            "numericFilters": f"created_at_i>{since_epoch(params.timeframe)}",
        }
        if params.query:
            query["query"] = params.query
        else:
            query["tags"] = "front_page"

        data = await http.get_json(_SEARCH, params=query)
        hits = (data or {}).get("hits", [])
        out: list[Trend] = []
        for h in hits:
            points = h.get("points") or 0
            comments = h.get("num_comments") or 0
            hn_url = f"https://news.ycombinator.com/item?id={h.get('objectID')}"
            out.append(
                Trend(
                    source=self.name,
                    platform="hackernews",
                    topic=params.query,
                    captured_for=params.query,
                    title=h.get("title") or h.get("story_title") or "(untitled)",
                    summary=h.get("story_text"),
                    url=h.get("url") or hn_url,
                    score=float(points),
                    engagement={"points": points, "comments": comments, "author": h.get("author")},
                    raw=h,
                )
            )
        return out


SOURCE = HackerNewsSource()
