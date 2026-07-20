"""Orchestration: cache check -> fan-out -> normalize/dedupe -> persist -> summarize.

This is the one place callers (MCP tools, scheduler) go through. Adding a source never
touches this file — it just appears in the registry for its ``kind``.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import timezone
from typing import Any

from .dedupe import (
    assign_hashes_events,
    assign_hashes_trends,
    dedupe_documents,
    dedupe_events,
    dedupe_trends,
)
from .http import HttpClient
from .models import Document, Event, Kind, SearchParams, SourceResult, Trend
from .storage import Storage

log = logging.getLogger("scraper.orchestrator")


def _event_sort_key(e: Event) -> tuple[int, float]:
    """Sort undated events last; normalize naive/aware datetimes so comparison never raises."""
    dt = e.start_time
    if dt is None:
        return (1, 0.0)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return (0, dt.timestamp())


async def _fetch_one(source, params: SearchParams, http: HttpClient):
    """Run a single source, isolating failures into a SourceResult."""
    try:
        items = await source.fetch(params, http)
        return items, SourceResult(source=source.name, ok=True, count=len(items))
    except Exception as exc:  # noqa: BLE001 - one dead source must not fail the run
        log.warning("source %s failed: %s", source.name, exc)
        return [], SourceResult(source=source.name, ok=False, error=str(exc))


async def _gather(params: SearchParams, http: HttpClient):
    from ..sources.registry import sources_for  # lazy: keeps core import cheap

    sources = sources_for(params.kind)
    if not sources:
        return [], []
    results = await asyncio.gather(*(_fetch_one(s, params, http) for s in sources))
    items: list[Any] = []
    summaries: list[SourceResult] = []
    for got, summary in results:
        items.extend(got)
        summaries.append(summary)
    return items, summaries


def _summ(summaries: list[SourceResult]) -> dict[str, Any]:
    return {
        "sources": [s.model_dump() for s in summaries],
        "sources_ok": [s.source for s in summaries if s.ok],
        "sources_failed": [s.source for s in summaries if not s.ok],
    }


async def run(params: SearchParams) -> dict[str, Any]:
    """Execute a request end-to-end and return a JSON-serializable result."""
    storage = Storage()

    # 1) Freshness cache — serve stored rows and skip the network unless forced.
    if not params.force_refresh:
        cached = await _try_cache(params, storage)
        if cached is not None:
            return {"count": len(cached), "cached": True, "items": cached, "sources": []}

    # 2) Fan out.
    async with HttpClient() as http:
        raw_items, summaries = await _gather(params, http)

    # 3) Normalize + dedupe + persist by kind.
    tool = f"run_{params.kind.value}"
    try:
        if params.kind is Kind.EVENTS:
            events: list[Event] = [i for i in raw_items if isinstance(i, Event)]
            events = dedupe_events(assign_hashes_events(events))
            # Chronological, not by source registration order — otherwise a source
            # that returns lots of events crowds out other sources before storage.
            events.sort(key=_event_sort_key)
            events = events[: params.limit]
            await storage.upsert_events(events)
            items = [e.model_dump(mode="json") for e in events]
        elif params.kind is Kind.TRENDS:
            trends: list[Trend] = [i for i in raw_items if isinstance(i, Trend)]
            trends = dedupe_trends(assign_hashes_trends(trends))
            trends.sort(key=lambda t: (t.score or 0), reverse=True)
            trends = trends[: params.limit]
            await storage.upsert_trends(trends)
            items = [t.model_dump(mode="json") for t in trends]
        else:  # WEB — research results are returned, not persisted to typed tables
            docs: list[Document] = [i for i in raw_items if isinstance(i, Document)]
            docs = dedupe_documents(docs)[: params.limit]
            items = [d.model_dump(mode="json") for d in docs]
        status = "ok"
        error = None
    except Exception as exc:  # noqa: BLE001
        log.exception("normalize/persist failed")
        items, status, error = [], "error", str(exc)

    await storage.log_run(tool, params.model_dump(mode="json"), _summ(summaries), status, error)

    return {
        "count": len(items),
        "cached": False,
        "items": items,
        **_summ(summaries),
        "status": status,
        "error": error,
    }


async def _try_cache(params: SearchParams, storage: Storage):
    if params.kind is Kind.EVENTS:
        return await storage.fresh_events(params.location, params.limit)
    if params.kind is Kind.TRENDS:
        return await storage.fresh_trends(params.query, params.limit)
    return None  # web research is never cached


async def run_research(query: str, depth: str = "standard", limit: int = 10) -> dict[str, Any]:
    """Web research: fan out search providers, then (optionally) fetch + extract full text.

    depth: "shallow" (snippets only) | "standard" (extract top few) | "deep" (extract more).
    """
    from ..sources.web_extract import enrich_documents

    params = SearchParams(kind=Kind.WEB, query=query, limit=limit)
    storage = Storage()

    async with HttpClient() as http:
        raw_items, summaries = await _gather(params, http)
        docs: list[Document] = dedupe_documents([i for i in raw_items if isinstance(i, Document)])
        enrich_n = {"shallow": 0, "standard": 5, "deep": min(limit, 10)}.get(depth, 5)
        if enrich_n:
            docs = await enrich_documents(docs, http, enrich_n)

    docs = docs[:limit]
    await storage.log_run(
        "research_topic",
        {"query": query, "depth": depth, "limit": limit},
        _summ(summaries),
        "ok",
    )
    return {
        "count": len(docs),
        "query": query,
        "depth": depth,
        "items": [d.model_dump(mode="json") for d in docs],
        **_summ(summaries),
    }
