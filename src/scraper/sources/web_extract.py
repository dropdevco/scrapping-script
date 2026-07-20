"""Readable main-content extraction for research documents.

Used to enrich search hits with full article text (respecting robots.txt) when the caller
asks for deeper research. trafilatura does the heavy lifting; we fall back to a plain
BeautifulSoup text dump if it returns nothing.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

from ..core.http import HttpClient
from ..core.models import Document

log = logging.getLogger("scraper.web_extract")

_MAX_CHARS = 12_000


def extract_main_text(html: str) -> Optional[str]:
    """Best-effort readable text from raw HTML."""
    try:
        import trafilatura

        text = trafilatura.extract(html, include_comments=False, include_tables=False)
        if text:
            return text[:_MAX_CHARS]
    except Exception as exc:  # noqa: BLE001
        log.debug("trafilatura failed: %s", exc)

    try:
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(html, "html.parser")
        for tag in soup(["script", "style", "nav", "footer", "header"]):
            tag.decompose()
        text = " ".join(soup.get_text(" ").split())
        return text[:_MAX_CHARS] or None
    except Exception:  # noqa: BLE001
        return None


async def _enrich_one(doc: Document, http: HttpClient) -> Document:
    if doc.text or not doc.url:
        return doc
    try:
        if not await http.can_fetch(doc.url):
            return doc
        html = await http.get_text(doc.url)
        doc.text = extract_main_text(html)
    except Exception as exc:  # noqa: BLE001
        log.debug("enrich %s failed: %s", doc.url, exc)
    return doc


async def enrich_documents(docs: list[Document], http: HttpClient, limit: int) -> list[Document]:
    """Fetch + extract full text for the first ``limit`` documents lacking it."""
    targets = docs[:limit]
    await asyncio.gather(*(_enrich_one(d, http) for d in targets))
    return docs
