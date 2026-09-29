"""How long each upstream read is trusted, and the local hours that change it.

CBP republishes about hourly, so re-reading it every 5 minutes is mostly wasted; the
scraped site moves every few minutes and deserves shorter windows. Every window widens
by BACKOFF_STEP each time a source repeats itself exactly and snaps back the moment a
number moves, so a quiet border costs few calls and a changing one is caught promptly.
"""
from __future__ import annotations

import time
from datetime import datetime

from .core.cbp import LOCAL_TZ

SOURCE_WINDOWS = {"cbp": (300, 1200), "pasosfronterizos": (240, 600),
                  "borderswaittime": (600, 1800)}   # a mirror; read it rarely
DEFAULT_WINDOW = (300, 900)
BACKOFF_STEP = 300

# When people are actually crossing, a wait can double in the time a widened window
# would have slept through. Ceilings tighten during the morning and evening peaks.
# Applied every day, not only weekdays: Sunday evening northbound is as heavy as any
# Tuesday morning, and polling a little more often costs less than missing a spike.
RUSH_HOURS = ((5, 9), (15, 19))          # local time, half-open [start, end)
RUSH_CEILINGS = {"cbp": 600, "pasosfronterizos": 360}
DEFAULT_RUSH_CEILING = 600
QUIET_HOURS = (23, 5)                    # no alerts between these local hours unless asked for


def is_rush_hour(now: datetime) -> bool:
    hour = now.astimezone(LOCAL_TZ).hour
    return any(start <= hour < end for start, end in RUSH_HOURS)


def is_quiet_hour(now: datetime) -> bool:
    """Nobody wants a wait-time alert at 3 am."""
    hour = now.astimezone(LOCAL_TZ).hour
    start, end = QUIET_HOURS
    return hour >= start or hour < end


def window(name: str, now: datetime) -> tuple[int, int]:
    """(base, ceiling) in seconds, tightened during the commute peaks."""
    base, ceiling = SOURCE_WINDOWS.get(name, DEFAULT_WINDOW)
    if is_rush_hour(now):
        ceiling = min(ceiling, RUSH_CEILINGS.get(name, DEFAULT_RUSH_CEILING))
    return base, max(base, ceiling)      # a ceiling below the base would be nonsense


def ttl(name: str, entry: dict, now: datetime) -> int:
    """Worked out on read, not stored at write time: a window widened at 2 pm must
    tighten the moment rush hour starts at 3."""
    base, ceiling = window(name, now)
    return min(base + entry["repeats"] * BACKOFF_STEP, ceiling)


def held_for(entry: dict) -> float:
    return time.monotonic() - entry["at"]


def next_entry(previous: dict | None, value, signature) -> dict:
    repeats = previous["repeats"] + 1 if previous and previous["signature"] == signature else 0
    return {"readings": value, "at": time.monotonic(), "signature": signature, "repeats": repeats}
