"""One GET for every upstream: CBP and the scraped sites.

All of them accept gzip and none of them sends ETag or Last-Modified, so the only
saving on offer is compression, and it is worth having everywhere (CBP 93 KB -> 9 KB,
pasosfronterizos 86 KB -> 15 KB).
"""
from __future__ import annotations

import gzip
import urllib.request

# The scraped sites serve a stripped page to unknown agents; CBP does not care.
BROWSER_USER_AGENT = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")


def get(url: str, timeout: float, user_agent: str = BROWSER_USER_AGENT) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": user_agent, "Accept-Encoding": "gzip"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = resp.read()
        if resp.headers.get("Content-Encoding") == "gzip":
            body = gzip.decompress(body)
        return body


def get_text(url: str, timeout: float, opener=None) -> str:
    """A page as text. `opener` stands in for the network in tests and replays."""
    if opener:
        return opener()
    return get(url, timeout).decode(errors="replace")
