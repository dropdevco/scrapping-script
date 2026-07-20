"""Web search providers for general research.

DuckDuckGo is keyless and always on. Tavily and Brave are optional (free tiers) and add
quality/coverage when their keys are present. Each provider is its own Source so one
failing (or being rate-limited) never sinks the others.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from ..core.config import settings
from ..core.http import HttpClient
from ..core.models import Document, Kind, SearchParams
from .base import Source

log = logging.getLogger("scraper.web_search")


class DuckDuckGoSource(Source):
    name = "web_ddg"
    kind = Kind.WEB

    def is_configured(self) -> bool:
        return True  # keyless

    async def fetch(self, params: SearchParams, http: HttpClient) -> list[Document]:
        if not params.query:
            return []
        return await asyncio.to_thread(self._blocking, params.query, min(params.limit, 25))

    def _blocking(self, query: str, n: int) -> list[Document]:
        try:
            from ddgs import DDGS

            with DDGS() as ddg:
                results = ddg.text(query, max_results=n)
        except Exception as exc:  # noqa: BLE001
            log.warning("ddg failed: %s", exc)
            return []
        return [
            Document(source=self.name, title=r.get("title"), url=r.get("href", ""), snippet=r.get("body"))
            for r in results
            if r.get("href")
        ]


class TavilySource(Source):
    name = "web_tavily"
    kind = Kind.WEB

    def is_configured(self) -> bool:
        return bool(settings.tavily_api_key)

    async def fetch(self, params: SearchParams, http: HttpClient) -> list[Document]:
        if not params.query:
            return []
        payload: dict[str, Any] = {
            "api_key": settings.tavily_api_key,
            "query": params.query,
            "max_results": min(params.limit, 20),
            "search_depth": "advanced",
            "include_raw_content": True,
        }
        resp = await http.request("POST", "https://api.tavily.com/search", json=payload)
        resp.raise_for_status()
        data = resp.json()
        out: list[Document] = []
        for r in (data or {}).get("results", []):
            out.append(
                Document(
                    source=self.name,
                    title=r.get("title"),
                    url=r.get("url", ""),
                    snippet=r.get("content"),
                    text=r.get("raw_content"),
                )
            )
        return out


class BraveSource(Source):
    name = "web_brave"
    kind = Kind.WEB

    def is_configured(self) -> bool:
        return bool(settings.brave_api_key)

    async def fetch(self, params: SearchParams, http: HttpClient) -> list[Document]:
        if not params.query:
            return []
        data = await http.get_json(
            "https://api.search.brave.com/res/v1/web/search",
            params={"q": params.query, "count": min(params.limit, 20)},
            headers={
                "X-Subscription-Token": settings.brave_api_key,
                "Accept": "application/json",
            },
        )
        results = (data or {}).get("web", {}).get("results", [])
        return [
            Document(
                source=self.name,
                title=r.get("title"),
                url=r.get("url", ""),
                snippet=r.get("description"),
                published=r.get("age"),
            )
            for r in results
            if r.get("url")
        ]


SOURCES = [DuckDuckGoSource(), TavilySource(), BraveSource()]
