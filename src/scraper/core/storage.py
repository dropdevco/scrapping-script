"""Supabase persistence + freshness cache.

If Supabase isn't configured the whole layer degrades to a no-op: tools still return
live results, they just aren't persisted and nothing is served from cache. This keeps
the server fully usable for local testing without a database.

Supabase's Python client is synchronous, so calls are pushed to a thread to avoid
blocking the event loop.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from .config import settings
from .models import Event, Trend

log = logging.getLogger("scraper.storage")


def _iso(dt: Optional[datetime]) -> Optional[str]:
    return dt.isoformat() if dt else None


def _event_row(e: Event) -> dict[str, Any]:
    # first_seen/last_seen are managed by DB default + trigger (see migration).
    return {
        "source": e.source,
        "source_id": e.source_id,
        "title": e.title,
        "description": e.description,
        "start_time": _iso(e.start_time),
        "end_time": _iso(e.end_time),
        "venue": e.venue,
        "location": e.location,
        "url": e.url,
        "image_url": e.image_url,
        "categories": e.categories,
        "raw": e.raw,
        "content_hash": e.content_hash,
    }


def _trend_row(t: Trend) -> dict[str, Any]:
    return {
        "source": t.source,
        "platform": t.platform,
        "topic": t.topic,
        "title": t.title,
        "summary": t.summary,
        "url": t.url,
        "score": t.score,
        "engagement": t.engagement,
        "captured_for": t.captured_for,
        "raw": t.raw,
        "content_hash": t.content_hash,
    }


class Storage:
    def __init__(self) -> None:
        self._client = None
        if settings.storage_enabled:
            try:
                from supabase import create_client

                self._client = create_client(settings.supabase_url, settings.supabase_key)
            except Exception as exc:  # noqa: BLE001 - never let storage break a run
                log.error("Supabase init failed, running without storage: %s", exc)
                self._client = None

    @property
    def enabled(self) -> bool:
        return self._client is not None

    # ── writes ────────────────────────────────────────────────────────────────
    async def upsert_events(self, events: list[Event]) -> None:
        if not self.enabled or not events:
            return
        rows = [_event_row(e) for e in events]
        await asyncio.to_thread(self._upsert, "events", rows, "content_hash")

    async def upsert_trends(self, trends: list[Trend]) -> None:
        if not self.enabled or not trends:
            return
        rows = [_trend_row(t) for t in trends]
        await asyncio.to_thread(self._upsert, "trends", rows, "content_hash")

    async def log_run(
        self,
        tool: str,
        params: dict[str, Any],
        source_counts: dict[str, Any],
        status: str,
        error: Optional[str] = None,
    ) -> None:
        if not self.enabled:
            return
        row = {
            "tool": tool,
            "params": params,
            "source_counts": source_counts,
            "status": status,
            "error": error,
        }
        try:
            await asyncio.to_thread(lambda: self._client.table("runs").insert(row).execute())
        except Exception as exc:  # noqa: BLE001
            log.error("run log failed: %s", exc)

    # ── reads (query_stored + freshness) ───────────────────────────────────────
    async def query_events(
        self,
        location: Optional[str] = None,
        since: Optional[str] = None,
        until: Optional[str] = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        if not self.enabled:
            return []

        def _q() -> list[dict[str, Any]]:
            q = self._client.table("events").select("*")
            if location:
                q = q.ilike("location", f"%{location}%")
            if since:
                q = q.gte("start_time", since)
            if until:
                q = q.lte("start_time", until)
            return q.order("start_time", desc=False).limit(limit).execute().data or []

        return await asyncio.to_thread(_q)

    async def query_trends(
        self,
        platform: Optional[str] = None,
        topic: Optional[str] = None,
        since: Optional[str] = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        if not self.enabled:
            return []

        def _q() -> list[dict[str, Any]]:
            q = self._client.table("trends").select("*")
            if platform:
                q = q.eq("platform", platform)
            if topic:
                q = q.ilike("captured_for", f"%{topic}%")
            if since:
                q = q.gte("captured_at", since)
            return q.order("score", desc=True).limit(limit).execute().data or []

        return await asyncio.to_thread(_q)

    async def fresh_events(self, location: Optional[str], limit: int) -> Optional[list[dict[str, Any]]]:
        """Return recently-captured events for this location, or None if cache is cold."""
        if not self.enabled:
            return None
        cutoff = self._cutoff()

        def _q() -> list[dict[str, Any]]:
            q = self._client.table("events").select("*").gte("last_seen", cutoff)
            if location:
                q = q.ilike("location", f"%{location}%")
            return q.limit(limit).execute().data or []

        rows = await asyncio.to_thread(_q)
        return rows or None

    async def fresh_trends(self, topic: Optional[str], limit: int) -> Optional[list[dict[str, Any]]]:
        if not self.enabled:
            return None
        cutoff = self._cutoff()

        def _q() -> list[dict[str, Any]]:
            q = self._client.table("trends").select("*").gte("captured_at", cutoff)
            if topic:
                q = q.ilike("captured_for", f"%{topic}%")
            return q.order("score", desc=True).limit(limit).execute().data or []

        rows = await asyncio.to_thread(_q)
        return rows or None

    # ── internals ───────────────────────────────────────────────────────────────
    def _upsert(self, table: str, rows: list[dict[str, Any]], on_conflict: str) -> None:
        try:
            self._client.table(table).upsert(rows, on_conflict=on_conflict).execute()
        except Exception as exc:  # noqa: BLE001
            log.error("upsert into %s failed: %s", table, exc)

    @staticmethod
    def _cutoff() -> str:
        return (datetime.now(timezone.utc) - timedelta(hours=settings.freshness_hours)).isoformat()
