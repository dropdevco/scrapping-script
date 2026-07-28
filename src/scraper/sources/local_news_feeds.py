"""Borderland local news feeds for web research.

News is represented as ``Document`` items because the current storage schema has
typed tables for events/trends, while web research results are returned live.
The source tries common RSS/Atom endpoints for the requested outlets and skips
anything unavailable.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Optional

from ..core.http import HttpClient
from ..core.models import Document, Kind, SearchParams
from .base import Source

log = logging.getLogger("scraper.local_news_feeds")


@dataclass(frozen=True)
class FeedSite:
    name: str
    homepage: str
    candidates: tuple[str, ...]
    region: str


FEEDS: tuple[FeedSite, ...] = (
    FeedSite(
        "el_paso_times",
        "https://www.elpasotimes.com/",
        (
            "https://www.elpasotimes.com/rss/",
            "https://www.elpasotimes.com/news/rss/",
        ),
        "el paso",
    ),
    FeedSite(
        "kvia",
        "https://kvia.com/",
        (
            "https://kvia.com/feed/",
            "https://kvia.com/news/feed/",
        ),
        "el paso",
    ),
    FeedSite(
        "ktsm",
        "https://www.ktsm.com/",
        (
            "https://www.ktsm.com/feed/",
            "https://www.ktsm.com/news/feed/",
        ),
        "el paso",
    ),
    FeedSite(
        "kfox14",
        "https://kfoxtv.com/",
        (
            "https://kfoxtv.com/news/local/rss",
            "https://kfoxtv.com/rss",
        ),
        "el paso",
    ),
    FeedSite(
        "el_paso_matters",
        "https://elpasomatters.org/",
        (
            "https://elpasomatters.org/feed/",
            "https://elpasomatters.org/category/news/feed/",
        ),
        "el paso",
    ),
    FeedSite(
        "el_heraldo_de_juarez",
        "https://www.elheraldodejuarez.com.mx/",
        (
            "https://www.elheraldodejuarez.com.mx/rss.xml",
            "https://www.elheraldodejuarez.com.mx/local/rss.xml",
            "https://www.elheraldodejuarez.com.mx/feed/",
        ),
        "juarez",
    ),
    FeedSite(
        "el_diario_de_juarez",
        "https://diario.mx/",
        (
            "https://diario.mx/rss/",
            "https://diario.mx/feed/",
        ),
        "juarez",
    ),
    FeedSite(
        "norte_digital",
        "https://nortedigital.mx/",
        (
            "https://nortedigital.mx/feed/",
            "https://nortedigital.mx/rss/",
        ),
        "juarez",
    ),
    FeedSite(
        "puente_libre",
        "https://puentelibre.mx/",
        (
            "https://puentelibre.mx/rss.xml",
            "https://puentelibre.mx/rss",
            "https://puentelibre.mx/feed/",
        ),
        "juarez",
    ),
)


def _wanted_for_location(site: FeedSite, location: str | None) -> bool:
    loc = (location or "").lower()
    if not loc:
        return True
    if "juarez" in loc or "juárez" in loc or "chihuahua" in loc:
        return site.region == "juarez"
    if "el paso" in loc:
        return site.region == "el paso"
    return True


def _published(entry: object) -> Optional[str]:
    if not isinstance(entry, dict):
        return None
    for key in ("published", "updated", "created"):
        value = entry.get(key)
        if not isinstance(value, str) or not value.strip():
            continue
        try:
            return parsedate_to_datetime(value).isoformat()
        except (TypeError, ValueError):
            return value
    return None


def _dt(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        try:
            return parsedate_to_datetime(value)
        except (TypeError, ValueError):
            return None


def _sort_ts(value: Optional[str]) -> float:
    parsed = _dt(value)
    if parsed is None:
        return 0.0
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


class LocalNewsFeedsSource(Source):
    name = "local_news_feeds"
    kind = Kind.WEB

    def is_configured(self) -> bool:
        return True

    async def fetch(self, params: SearchParams, http: HttpClient) -> list[Document]:
        sites = [site for site in FEEDS if _wanted_for_location(site, params.location)]
        results = await asyncio.gather(*(self._site_docs(site, params, http) for site in sites))
        docs = [doc for site_docs in results for doc in site_docs]
        docs.sort(key=lambda doc: _sort_ts(doc.published), reverse=True)
        return docs[: params.limit]

    async def _site_docs(
        self, site: FeedSite, params: SearchParams, http: HttpClient
    ) -> list[Document]:
        for feed_url in site.candidates:
            docs = await self._feed_docs(site, feed_url, params, http)
            if docs:
                return docs
        return []

    async def _feed_docs(
        self, site: FeedSite, feed_url: str, params: SearchParams, http: HttpClient
    ) -> list[Document]:
        try:
            if not await http.can_fetch(feed_url):
                return []
            text = await http.get_text(feed_url)
        except Exception as exc:  # noqa: BLE001
            log.debug("feed %s failed: %s", feed_url, exc)
            return []

        try:
            import feedparser
        except Exception as exc:  # noqa: BLE001
            log.warning("feedparser unavailable: %s", exc)
            return []

        parsed = feedparser.parse(text)
        query = (params.query or "").strip().lower()
        docs: list[Document] = []
        for entry in parsed.entries[: max(params.limit, 20)]:
            title = (entry.get("title") or "").strip()
            link = (entry.get("link") or "").strip()
            summary = (entry.get("summary") or entry.get("description") or "").strip()
            haystack = f"{title} {summary}".lower()
            if query and query not in haystack:
                continue
            if not link:
                continue
            docs.append(
                Document(
                    source=self.name,
                    title=title or site.name,
                    url=link,
                    snippet=summary or None,
                    published=_published(entry),
                )
            )
        return docs


SOURCE = LocalNewsFeedsSource()
