"""Shared async HTTP client with retries, a global concurrency cap, polite defaults,
and a robots.txt gate for arbitrary page fetches.

Sources receive a single shared :class:`HttpClient` from the orchestrator so connection
pooling and the concurrency semaphore are shared across the whole fan-out.
"""

from __future__ import annotations

import asyncio
import logging
from urllib.parse import urlsplit
from urllib.robotparser import RobotFileParser

import httpx

from .config import settings

log = logging.getLogger("scraper.http")

_RETRY_STATUS = {429, 500, 502, 503, 504}


class HttpClient:
    def __init__(self, max_concurrency: int | None = None, timeout: int | None = None) -> None:
        self._sem = asyncio.Semaphore(max_concurrency or settings.http_max_concurrency)
        self._client = httpx.AsyncClient(
            timeout=timeout or settings.http_timeout_seconds,
            headers={"User-Agent": settings.user_agent},
            follow_redirects=True,
        )
        self._robots: dict[str, RobotFileParser | None] = {}
        self._robots_lock = asyncio.Lock()

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> "HttpClient":
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def request(
        self,
        method: str,
        url: str,
        *,
        retries: int = 3,
        **kwargs: object,
    ) -> httpx.Response:
        """Request with bounded concurrency + exponential backoff on transient errors."""
        attempt = 0
        while True:
            attempt += 1
            async with self._sem:
                try:
                    resp = await self._client.request(method, url, **kwargs)  # type: ignore[arg-type]
                except (httpx.TransportError, httpx.TimeoutException) as exc:
                    if attempt > retries:
                        raise
                    log.warning("transport error %s (attempt %s) for %s", exc, attempt, url)
                    await asyncio.sleep(_backoff(attempt))
                    continue
            if resp.status_code in _RETRY_STATUS and attempt <= retries:
                log.warning("status %s (attempt %s) for %s", resp.status_code, attempt, url)
                await asyncio.sleep(_backoff(attempt, resp))
                continue
            return resp

    async def get_json(self, url: str, **kwargs: object) -> object:
        resp = await self.request("GET", url, **kwargs)
        resp.raise_for_status()
        return resp.json()

    async def get_text(self, url: str, **kwargs: object) -> str:
        resp = await self.request("GET", url, **kwargs)
        resp.raise_for_status()
        return resp.text

    async def can_fetch(self, url: str) -> bool:
        """Check robots.txt for the URL's host. Fail-open on fetch errors."""
        parts = urlsplit(url)
        if not parts.scheme or not parts.netloc:
            return False
        base = f"{parts.scheme}://{parts.netloc}"
        async with self._robots_lock:
            if base not in self._robots:
                self._robots[base] = await self._load_robots(base)
        rp = self._robots[base]
        if rp is None:
            return True  # no robots.txt reachable -> allowed
        return rp.can_fetch(settings.user_agent, url)

    async def _load_robots(self, base: str) -> RobotFileParser | None:
        try:
            resp = await self._client.get(f"{base}/robots.txt", timeout=10)
            if resp.status_code >= 400:
                return None
            rp = RobotFileParser()
            rp.parse(resp.text.splitlines())
            return rp
        except httpx.HTTPError:
            return None


def _backoff(attempt: int, resp: httpx.Response | None = None) -> float:
    """Exponential backoff, honoring Retry-After when present."""
    if resp is not None:
        ra = resp.headers.get("Retry-After")
        if ra and ra.isdigit():
            return min(float(ra), 30.0)
    return min(2.0 ** attempt, 30.0)
