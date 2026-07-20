"""Rising search interest from Google Trends (pytrends, keyless).

pytrends is an OPTIONAL dependency (`pip install .[trends]`; it pulls in pandas) and the
endpoint is unofficial/rate-limited, so this source self-disables when the lib is absent
and any runtime error is isolated by the orchestrator.
"""

from __future__ import annotations

import asyncio
import importlib.util
from typing import Any, Optional
from urllib.parse import quote_plus

from ..core.http import HttpClient
from ..core.models import Kind, SearchParams, Trend
from .base import Source

_TF = {"day": "now 1-d", "week": "now 7-d", "month": "today 1-m"}


def _num(value: Any) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None  # e.g. "Breakout"


def _search_url(term: str) -> str:
    return f"https://www.google.com/search?q={quote_plus(term)}"


class GoogleTrendsSource(Source):
    name = "trends_google"
    kind = Kind.TRENDS

    def is_configured(self) -> bool:
        # cheap check — don't import pandas here
        return importlib.util.find_spec("pytrends") is not None

    async def fetch(self, params: SearchParams, http: HttpClient) -> list[Trend]:
        if params.platforms and "google_trends" not in params.platforms:
            return []
        return await asyncio.to_thread(self._blocking_fetch, params)

    def _blocking_fetch(self, params: SearchParams) -> list[Trend]:
        from pytrends.request import TrendReq

        pytrends = TrendReq(hl="en-US", tz=360)
        out: list[Trend] = []

        if params.query:
            pytrends.build_payload(
                [params.query], timeframe=_TF.get(params.timeframe, "now 7-d"), geo="US"
            )
            related = pytrends.related_queries() or {}
            bucket = related.get(params.query) or {}
            for kind_key in ("rising", "top"):
                df = bucket.get(kind_key)
                if df is None or df.empty:
                    continue
                for _, row in df.iterrows():
                    term = str(row.get("query", "")).strip()
                    if not term:
                        continue
                    out.append(
                        Trend(
                            source=self.name,
                            platform="google_trends",
                            topic=params.query,
                            captured_for=params.query,
                            title=term,
                            summary=f"{kind_key} related query for '{params.query}'",
                            url=_search_url(term),
                            score=_num(row.get("value")),
                            engagement={"kind": kind_key, "value": row.get("value")},
                            raw={"query": term, "value": row.get("value"), "kind": kind_key},
                        )
                    )
        else:
            df = pytrends.trending_searches(pn="united_states")
            for rank, term in enumerate(df[0].tolist() if not df.empty else []):
                out.append(
                    Trend(
                        source=self.name,
                        platform="google_trends",
                        title=str(term),
                        summary="daily trending search (US)",
                        url=_search_url(str(term)),
                        score=float(len(df) - rank),  # higher = more trending
                        engagement={"rank": rank + 1},
                        raw={"term": term, "rank": rank + 1},
                    )
                )

        return out[: params.limit]


SOURCE = GoogleTrendsSource()
