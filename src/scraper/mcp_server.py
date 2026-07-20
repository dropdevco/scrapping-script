"""MCP server: the agent-facing surface.

Thin wrappers over the orchestrator + storage. Every tool returns a JSON-serializable dict
of the shape {count, items, sources, ...} so any MCP client can consume it directly.

Run locally:   python -m scraper.mcp_server        (stdio transport)
"""

from __future__ import annotations

import logging
from datetime import date
from typing import Optional

from mcp.server.fastmcp import FastMCP

from .core import orchestrator
from .core.models import Kind, SearchParams
from .core.storage import Storage
from .sources import registry

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

mcp = FastMCP("scraper-mcp")


def _as_date(value: Optional[str]) -> Optional[date]:
    if not value:
        return None
    try:
        return date.fromisoformat(value.strip())
    except ValueError:
        return None


@mcp.tool()
async def search_events(
    location: str,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    categories: Optional[list[str]] = None,
    query: Optional[str] = None,
    limit: int = 50,
    force_refresh: bool = False,
) -> dict:
    """Find events happening in a place/time window (e.g. "this week in El Paso").

    Aggregates Ticketmaster + open-web event listings, dedupes, and stores them in Supabase.
    Dates are ISO (YYYY-MM-DD). Serves cached results within the freshness window unless
    force_refresh is true.
    """
    params = SearchParams(
        kind=Kind.EVENTS,
        location=location,
        query=query,
        start_date=_as_date(start_date),
        end_date=_as_date(end_date),
        categories=categories or [],
        limit=limit,
        force_refresh=force_refresh,
    )
    return await orchestrator.run(params)


@mcp.tool()
async def find_trends(
    topic: Optional[str] = None,
    platforms: Optional[list[str]] = None,
    timeframe: str = "week",
    limit: int = 50,
    force_refresh: bool = False,
) -> dict:
    """Find trending topics / content ideas for a subject (AI, tech, business…).

    Sources: Reddit, Hacker News, YouTube, Google Trends. Own-account Instagram/Threads are
    included only when explicitly listed in `platforms` (e.g. ["instagram"]). Omit `topic`
    for a general "what's hot right now" pull. timeframe: day | week | month.
    """
    params = SearchParams(
        kind=Kind.TRENDS,
        query=topic,
        platforms=platforms or [],
        timeframe=timeframe,
        limit=limit,
        force_refresh=force_refresh,
    )
    return await orchestrator.run(params)


@mcp.tool()
async def research_topic(query: str, depth: str = "standard", limit: int = 10) -> dict:
    """Research a topic across the web: search providers + main-content extraction.

    depth: "shallow" (snippets only) | "standard" (extract top few) | "deep" (extract more).
    Returns documents with title, url, snippet, and (when extracted) full text.
    """
    return await orchestrator.run_research(query=query, depth=depth, limit=limit)


@mcp.tool()
async def query_stored(
    kind: str,
    location: Optional[str] = None,
    topic: Optional[str] = None,
    platform: Optional[str] = None,
    since: Optional[str] = None,
    until: Optional[str] = None,
    limit: int = 100,
) -> dict:
    """Read previously scraped data back from Supabase — fast and free, no live scraping.

    kind: "events" or "trends". `since`/`until` are ISO datetimes. Use this to retrieve what
    scheduled jobs already collected.
    """
    storage = Storage()
    if not storage.enabled:
        return {"count": 0, "items": [], "note": "Supabase not configured; nothing stored."}
    if kind == "events":
        rows = await storage.query_events(location=location, since=since, until=until, limit=limit)
    elif kind == "trends":
        rows = await storage.query_trends(platform=platform, topic=topic, since=since, limit=limit)
    else:
        return {"count": 0, "items": [], "error": f"unknown kind '{kind}' (use events|trends)"}
    return {"count": len(rows), "items": rows}


@mcp.tool()
async def source_status() -> dict:
    """List all sources and whether each is currently active (configured + allowed)."""
    rows = registry.status()
    return {"count": len(rows), "sources": rows, "active": [r["name"] for r in rows if r["active"]]}


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
