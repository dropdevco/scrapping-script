"""Threads API — OWN-ACCOUNT scope only.

Opt-in via ``platforms=["threads"]``. Reads your own recent Threads posts (and, best-effort,
per-post like/reply insights). No global Threads search exists in the API.
"""

from __future__ import annotations

import asyncio
from typing import Any, Optional

from ..core.config import settings
from ..core.http import HttpClient
from ..core.models import Kind, SearchParams, Trend
from . import auth_meta
from .base import Source

THREADS = auth_meta.THREADS


class ThreadsSource(Source):
    name = "social_threads"
    kind = Kind.TRENDS

    def is_configured(self) -> bool:
        return bool(settings.threads_access_token and settings.threads_user_id)

    async def fetch(self, params: SearchParams, http: HttpClient) -> list[Trend]:
        if "threads" not in params.platforms:
            return []
        token = settings.threads_access_token
        data = await http.get_json(
            f"{THREADS}/{settings.threads_user_id}/threads",
            params={
                "fields": "id,text,permalink,timestamp,media_type",
                "limit": min(params.limit, 50),
                "access_token": token,
            },
        )
        posts = (data or {}).get("data", [])
        insights = await asyncio.gather(
            *(self._insight(p.get("id"), http, token) for p in posts)
        )
        return [self._to_trend(p, ins, params) for p, ins in zip(posts, insights)]

    async def _insight(self, media_id: Optional[str], http: HttpClient, token: str) -> dict[str, Any]:
        if not media_id:
            return {}
        try:
            data = await http.get_json(
                f"{THREADS}/{media_id}/insights",
                params={"metric": "likes,replies,reposts,quotes", "access_token": token},
            )
        except Exception:  # noqa: BLE001 - insights are best-effort
            return {}
        out: dict[str, Any] = {}
        for row in (data or {}).get("data", []):
            values = row.get("values") or [{}]
            out[row.get("name")] = values[0].get("value")
        return out

    def _to_trend(self, p: dict[str, Any], insight: dict[str, Any], params: SearchParams) -> Trend:
        text = p.get("text") or ""
        likes = insight.get("likes")
        return Trend(
            source=self.name,
            platform="threads",
            topic=params.query,
            captured_for=params.query,
            title=text[:80] or "own thread",
            summary=text or None,
            url=p.get("permalink"),
            score=float(likes) if likes is not None else None,
            engagement={
                "likes": likes,
                "replies": insight.get("replies"),
                "reposts": insight.get("reposts"),
                "quotes": insight.get("quotes"),
                "timestamp": p.get("timestamp"),
            },
            raw={"post": p, "insight": insight},
        )


SOURCE = ThreadsSource()
