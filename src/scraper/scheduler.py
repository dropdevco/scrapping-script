"""Scheduled batch runs.

Runs a set of curated jobs (force-refreshed) and writes results to Supabase so the agent's
`query_stored` tool is instant and quota-free. Meant to be invoked by GitHub Actions cron
(see .github/workflows/scheduled_scrape.yml) or any scheduler.

Configure via env:
  SCHEDULE_LOCATION   default event location            (default "El Paso, TX")
  SCHEDULE_TOPICS     comma list of trend topics         (default "AI,technology,business")
  SCHEDULE_DAYS       event look-ahead window in days    (default 7)

Run:  python -m scraper.scheduler            (all jobs)
      python -m scraper.scheduler events     (only events)
      python -m scraper.scheduler trends     (only trends)
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from datetime import date, timedelta

from .core import orchestrator
from .core.models import Kind, SearchParams

log = logging.getLogger("scraper.scheduler")


def _location() -> str:
    return (os.getenv("SCHEDULE_LOCATION") or "El Paso, TX").strip()


def _topics() -> list[str]:
    raw = os.getenv("SCHEDULE_TOPICS") or "AI,technology,business"
    return [t.strip() for t in raw.split(",") if t.strip()]


def _days() -> int:
    try:
        return int(os.getenv("SCHEDULE_DAYS", "7"))
    except ValueError:
        return 7


async def run_events() -> None:
    today = date.today()
    params = SearchParams(
        kind=Kind.EVENTS,
        location=_location(),
        start_date=today,
        end_date=today + timedelta(days=_days()),
        limit=100,
        force_refresh=True,
    )
    result = await orchestrator.run(params)
    log.info("events @ %s: %s stored (sources ok: %s)",
             _location(), result["count"], result.get("sources_ok"))


async def run_trends() -> None:
    for topic in _topics():
        params = SearchParams(
            kind=Kind.TRENDS,
            query=topic,
            timeframe="week",
            limit=50,
            force_refresh=True,
        )
        result = await orchestrator.run(params)
        log.info("trends '%s': %s stored (sources ok: %s)",
                 topic, result["count"], result.get("sources_ok"))


async def run_all(which: str) -> None:
    if which in ("all", "events"):
        await run_events()
    if which in ("all", "trends"):
        await run_trends()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    which = sys.argv[1] if len(sys.argv) > 1 else "all"
    if which not in ("all", "events", "trends"):
        print(f"unknown job '{which}' (use: all | events | trends)")
        raise SystemExit(2)
    asyncio.run(run_all(which))


if __name__ == "__main__":
    main()
