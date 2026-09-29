"""What we know from before this reading: the previous number, today's movement, and
what is normal for this hour.

Pure functions; the state they read lives on BorderFeed.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from .core.cbp import Snapshot

MEANINGFUL_DELTA = 5        # CBP moves in 5-minute steps; smaller is noise, not news
FROZEN_HOURS = 6            # identical numbers for this long means the feed is stuck
TREND_WINDOW_MINUTES = 90   # "is the line growing?" over the last hour and a half
TREND_MIN_SPAN = 20         # two readings closer than this say nothing about direction
TREND_WORTH_SAYING = 10     # minutes of movement before it changes anyone's plan

# "Normal for this hour" comes from our own readings, aggregated in the database. It
# moves slowly, so it is re-read a few times a day rather than on every refresh.
TYPICAL_REFRESH_SECONDS = 6 * 3600
TYPICAL_RETRY_SECONDS = 300
UNUSUAL_MINUTES = 10        # smaller than this is an ordinary day
UNUSUAL_FRACTION = 0.25     # long waits need proportionally more to be unusual

Point = tuple[datetime, int]


def _direction(change: int) -> str:
    return "up" if change > 0 else "down" if change < 0 else "flat"


def fingerprint(snapshot: Snapshot) -> str:
    return "|".join(f"{p.port_number}:{lane_id}:{l.state}:{l.delay_minutes}:{l.cbp_updated_at}"
                    for p in snapshot.ports for lane_id, l in sorted(p.lanes.items()))


def delta(minutes: int, was: int | None) -> dict | None:
    if was is None:
        return None
    change = minutes - was
    return {"previous_minutes": was, "change": change, "direction": _direction(change),
            "meaningful": abs(change) >= MEANINGFUL_DELTA}


def previous_from_rows(rows: list[dict], current_hash: str | None) -> int | None:
    """Newest open reading in border_readings that is not the current one."""
    for row in rows:
        if current_hash and row.get("content_hash") == current_hash:
            continue
        if row.get("state") == "open" and row.get("delay_minutes") is not None:
            return int(row["delay_minutes"])
    return None


def trend_cutoff(now: datetime) -> datetime:
    return now - timedelta(minutes=TREND_WINDOW_MINUTES)


def add_point(recent: dict[tuple[str, str], list[Point]], key: tuple[str, str],
              point: Point, cutoff: datetime) -> None:
    series = recent.setdefault(key, [])
    series.append(point)
    series.sort(key=lambda p: p[0])
    series[:] = [p for p in series if p[0] >= cutoff]


def trend(series: list[Point]) -> dict | None:
    """How a lane has moved over the trend window, if it has been seen long enough."""
    if len(series) < 2:
        return None
    (first_at, first), (last_at, last) = series[0], series[-1]
    minutes_apart = round((last_at - first_at).total_seconds() / 60)
    if minutes_apart < TREND_MIN_SPAN:
        return None
    change = last - first
    return {"change": change, "over_minutes": minutes_apart, "direction": _direction(change),
            "worth_saying": abs(change) >= TREND_WORTH_SAYING}


def typical_key(port_number: str, lane: str, local: datetime) -> tuple[str, str, int, int]:
    return port_number, lane, local.isoweekday() % 7, local.hour    # Postgres dow: 0 = Sunday


def typical(row: dict | None, local: datetime, minutes: int) -> dict | None:
    """The usual wait for this weekday and hour, and how today compares."""
    if row is None:
        return None
    usual = round(row["median_minutes"])
    difference = minutes - usual
    unusual = abs(difference) >= max(UNUSUAL_MINUTES, usual * UNUSUAL_FRACTION)
    return {
        "minutes": usual,
        "p75_minutes": round(row["p75_minutes"]) if row.get("p75_minutes") is not None else None,
        "days": row["days"],
        "weekday": local.isoweekday() % 7,
        "hour": local.hour,
        "difference": difference,
        "compared": ("worse" if difference > 0 else "better") if unusual else "usual",
    }
