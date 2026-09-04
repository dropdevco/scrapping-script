"""Post-publish performance: what Instagram did with what we shipped.

Two things shape this module.

**There is no per-slide attribution.** The Media Insights API reports on a
media object, and a CAROUSEL_ALBUM is one media object — Meta exposes no
per-child insight. So a number here describes a *post*, never an event or a
category. Any future attempt to feed this back into `selection.score_event`
has to reckon with the fact that crediting reach to one of nine slides is a
guess, not a measurement.

**The supported metric set is a moving target.** `impressions` was retired in
favour of `views`; `navigation` is not offered for this media product. Both
were confirmed live against a real CAROUSEL_ALBUM on 2026-09-04, on
graph.instagram.com (the Instagram-Login host — its metric set is narrower
than the Facebook-Login one). Rather than pin that list and break silently the
next time Meta moves, `fetch_media_metrics` drops whatever a 400 names and
retries, then reports the set that actually worked.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Optional

from ..core.config import settings
from ..core.http import HttpClient
from ..sources.auth_meta import GRAPH

log = logging.getLogger("scraper.social.metrics")

# Verified supported for CAROUSEL_ALBUM on graph.instagram.com, 2026-09-04.
# Order is not meaningful; the degrade loop may return a subset.
INSIGHT_METRICS: tuple[str, ...] = (
    "reach",
    "saved",
    "shares",
    "views",
    "total_interactions",
    "likes",
    "comments",
    "profile_visits",
    "follows",
)

# Insight name -> ig_post_metrics column, where they differ.
_COLUMN_FOR = {"saved": "saves"}

# Meta names the offending metric in the error text, e.g.
# "The Media Insights API does not support the impressions metric for this
#  media product type." Pulling it out is what lets one bad name cost one
# retry instead of the whole snapshot.
_UNSUPPORTED_RE = re.compile(
    r"does not support the (\w+) metric|(\w+) metric is not supported", re.IGNORECASE
)

# Snapshots to collect, as (label, hours after publish).
WINDOWS: tuple[tuple[str, int], ...] = (("t24", 24), ("t72", 72))


def _redact(text: str) -> str:
    """Strip access tokens out of anything headed for a log or an alert.

    HttpClient puts the failing URL in the exception text, and every Graph
    call carries ?access_token=... in its query string — so the naive
    `log.error("%s", exc)` publishes a live credential to the CI log and, via
    notify_alert, to Telegram and email as well.
    """
    text = re.sub(r"(access_token=)[^&\s'\"]+", r"\1***", text)
    if settings.ig_access_token:
        text = text.replace(settings.ig_access_token, "***")
    return text


def _error_message(exc: Exception) -> str:
    """Meta's real reason, which HttpClient puts in the exception text."""
    text = _redact(str(exc))
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(0)).get("error", {}).get("message", text)
        except Exception:  # noqa: BLE001
            pass
    return text


def _unsupported_metric(message: str) -> Optional[str]:
    m = _UNSUPPORTED_RE.search(message)
    if not m:
        return None
    return m.group(1) or m.group(2)


async def fetch_media_node(http: HttpClient, media_id: str, token: str) -> dict[str, Any]:
    """like_count / comments_count and identity fields.

    Taken from the media node rather than the insights edge because these two
    are always available even when the insights call is refused outright, and
    a snapshot with likes but no reach beats no snapshot at all.
    """
    data = await http.get_json(
        f"{GRAPH}/{media_id}",
        params={
            "fields": "media_type,like_count,comments_count,permalink,timestamp",
            "access_token": token,
        },
    )
    return data or {}


async def fetch_insights(
    http: HttpClient, media_id: str, token: str, metrics: tuple[str, ...] = INSIGHT_METRICS
) -> tuple[dict[str, int], list[str]]:
    """Return (values, dropped). Retries without any metric Meta rejects.

    Bounded by the number of metrics: each pass drops at most one name, and a
    pass that fails for any other reason raises rather than looping.
    """
    wanted = list(metrics)
    dropped: list[str] = []
    while wanted:
        try:
            data = await http.get_json(
                f"{GRAPH}/{media_id}/insights",
                params={"metric": ",".join(wanted), "access_token": token},
            )
        except Exception as exc:  # noqa: BLE001
            message = _error_message(exc)
            bad = _unsupported_metric(message)
            if bad and bad in wanted:
                log.warning("metric %r is not supported here — retrying without it", bad)
                wanted.remove(bad)
                dropped.append(bad)
                continue
            raise
        values: dict[str, int] = {}
        for entry in (data or {}).get("data") or []:
            name = entry.get("name")
            series = entry.get("values") or []
            if name and series:
                values[name] = int(series[0].get("value") or 0)
        return values, dropped
    return {}, dropped


def to_row(node: dict[str, Any], insights: dict[str, int]) -> dict[str, Any]:
    """Flatten a node + insights pair into an ig_post_metrics row.

    like_count/comments_count from the node win over the `likes`/`comments`
    insights: the node fields are the ones Instagram itself shows on the post,
    so a discrepancy should resolve to what a human would see.
    """
    row: dict[str, Any] = {}
    for name, value in insights.items():
        row[_COLUMN_FOR.get(name, name)] = value
    if node.get("like_count") is not None:
        row["likes"] = int(node["like_count"])
    if node.get("comments_count") is not None:
        row["comments"] = int(node["comments_count"])
    row["raw"] = {"node": node, "insights": insights}
    return row


def format_digest(post_date: str, row: dict[str, Any], baseline: Optional[dict[str, float]]) -> str:
    """One human-readable line per post, with context.

    A bare "412 reach" means nothing to a reader who does not already know
    what normal looks like, so the comparison against the recent average is
    the part that carries the information.
    """
    def n(key: str) -> str:
        v = row.get(key)
        return f"{v:,}" if isinstance(v, int) else "—"

    parts = [
        f"{n('reach')} reach",
        f"{n('likes')} likes",
        f"{n('comments')} comments",
        f"{n('saves')} saves",
        f"{n('shares')} shares",
    ]
    if row.get("follows"):
        parts.append(f"{n('follows')} new follows")
    line = f"{post_date} — " + " · ".join(parts)

    if baseline and baseline.get("reach") and isinstance(row.get("reach"), int):
        avg = baseline["reach"]
        if avg > 0:
            delta = (row["reach"] - avg) / avg * 100
            line += f"\nReach vs the last {int(baseline['n'])} posts: {delta:+.0f}%"
    return line


async def collect(
    http: HttpClient, media_id: str, token: Optional[str] = None
) -> tuple[dict[str, Any], Optional[str]]:
    """Fetch one post's snapshot. Returns (row, error).

    Never raises: a metrics run that dies on one bad post would skip every
    later one, and this is reporting, not the pipeline.
    """
    token = token or settings.ig_access_token or ""
    node: dict[str, Any] = {}
    try:
        node = await fetch_media_node(http, media_id, token)
    except Exception as exc:  # noqa: BLE001
        log.warning("media node fetch failed for %s: %s", media_id, _error_message(exc))
    try:
        insights, dropped = await fetch_insights(http, media_id, token)
        if dropped:
            log.info("collected without unsupported metric(s): %s", ", ".join(dropped))
    except Exception as exc:  # noqa: BLE001
        message = _error_message(exc)
        log.error("insights fetch failed for %s: %s", media_id, message)
        if not node:
            return {}, message[:500]
        return to_row(node, {}), message[:500]
    return to_row(node, insights), None
