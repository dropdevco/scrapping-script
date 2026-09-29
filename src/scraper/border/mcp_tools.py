"""The border feed as MCP tools for the local agent.

Not registered yet — that is a two-line change to mcp_server.py, left for review:

    from .border.mcp_tools import register as register_border_tools
    register_border_tools(mcp)

Same shape as the engine's tools: thin wrappers returning JSON-serialisable dicts. One
feed is shared by every call, so its 5-minute reading serves the whole conversation.
"""

from __future__ import annotations

from typing import Any

from .service import BorderFeed, FeedError

_feed: BorderFeed | None = None


def _shared() -> BorderFeed:
    global _feed
    if _feed is None:
        _feed = BorderFeed()
    return _feed


def register(mcp: Any) -> None:
    @mcp.tool()
    async def border_waits(lane: str = "car", lang: str = "es") -> dict:
        """Live El Paso-Juarez border wait times: every bridge ranked fastest-first for a
        lane (car, car_sentri, car_ready, walk, walk_ready, truck, truck_fast), plus a
        ready-to-post caption in Spanish and English."""
        try:
            return await _shared().waits(lang, (lane,))
        except FeedError as exc:
            return {"error": str(exc)}

    @mcp.tool()
    async def border_bridge(bridge: str, lane: str = "car", lang: str = "es") -> dict:
        """One bridge, by name as people type it ("libre", "zaragoza", "pdn"): minutes,
        movement, when they would get across, and a faster alternative if there is one."""
        try:
            return await _shared().bridge(bridge, lane, lang)
        except FeedError as exc:
            return {"error": str(exc)}

    @mcp.tool()
    async def border_health() -> dict:
        """Whether the border feed is healthy: last read, staleness, source status, and
        whether a CBP mirror disagrees with our parsing."""
        return await _shared().health()
