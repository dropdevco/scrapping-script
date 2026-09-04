"""Shared async HTTP client with retries, a global concurrency cap, polite defaults,
and a robots.txt gate for arbitrary page fetches.

Sources receive a single shared :class:`HttpClient` from the orchestrator so connection
pooling and the concurrency semaphore are shared across the whole fan-out.
"""

from __future__ import annotations

import asyncio
import logging
import re
from urllib.parse import urlsplit
from urllib.robotparser import RobotFileParser

import httpx

from .config import settings

log = logging.getLogger("scraper.http")

_RETRY_STATUS = {429, 500, 502, 503, 504}

# Query-string credentials that must never reach a log line, a database
# `error` column, or an alert. The Graph API takes its token as a URL
# parameter, and httpx puts the full URL in every error it raises — so the
# obvious `log.error("%s", exc)` publishes a live credential. Redacting at the
# point the message is built is the only place that catches all of them.
_SECRET_PARAMS = ("access_token", "secret_token", "api_key", "apikey", "key", "token")
# The value class is deliberately narrow -- the characters that actually appear
# in these tokens -- rather than "anything up to a delimiter". A Graph error
# reads `...?access_token=ABC: {"error": {...}}`, and a greedy class eats the
# colon and the Meta error body with it, redacting away the very explanation
# the message exists to carry.
_SECRET_RE = re.compile(
    r"\b(" + "|".join(_SECRET_PARAMS) + r")=([A-Za-z0-9._~+/-]+=*)",
    re.IGNORECASE,
)


def redact_secrets(text: str) -> str:
    """Replace credential query-string values with ***, keeping the param name.

    The name is the useful half: `access_token=***` says which credential the
    failing call used, where a bare `***` does not.
    """
    return _SECRET_RE.sub(r"\1=***", text)


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

    async def get_bytes(self, url: str, **kwargs: object) -> bytes:
        resp = await self.request("GET", url, **kwargs)
        resp.raise_for_status()
        return resp.content

    async def post_json(self, url: str, **kwargs: object) -> object:
        """POST returning parsed JSON. Graph API publishing is POST-only, and
        the error body carries the actual reason (bad scope, unreachable image
        URL, expired container), so it's surfaced rather than swallowed by
        raise_for_status' generic message."""
        resp = await self.request("POST", url, **kwargs)
        if resp.status_code >= 400:
            raise httpx.HTTPStatusError(
                redact_secrets(f"{resp.status_code} from {url}: {resp.text[:500]}"),
                request=resp.request,
                response=resp,
            )
        return resp.json()

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
