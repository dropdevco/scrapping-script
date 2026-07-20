"""Small timeframe helpers shared by trend sources ("day"/"week"/"month")."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

_SECONDS = {"day": 86_400, "week": 604_800, "month": 2_592_000}


def timeframe_seconds(timeframe: str) -> int:
    return _SECONDS.get(timeframe, _SECONDS["week"])


def since_datetime(timeframe: str) -> datetime:
    return datetime.now(timezone.utc) - timedelta(seconds=timeframe_seconds(timeframe))


def since_epoch(timeframe: str) -> int:
    return int(since_datetime(timeframe).timestamp())


def since_iso(timeframe: str) -> str:
    # RFC3339 with trailing Z, as several APIs expect.
    return since_datetime(timeframe).strftime("%Y-%m-%dT%H:%M:%SZ")
