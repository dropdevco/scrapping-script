"""Turning "what CBP published" into "what to expect on arrival", and ranking on it.

A recommendation is a bet about the wait when they ARRIVE, not the wait CBP last
published, so the raw number is adjusted for two things we know about it. Every
adjustment travels with the answer so a person can argue with it.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from .cbp import DAY_MINUTES, UNKNOWN_AGE, Port
from .history import MEANINGFUL_DELTA

WORTH_SWITCHING = 10     # minutes saved before suggesting another bridge
CLOSING_MARGIN = 10      # minutes of slack before we call a crossing too tight
BIG_LANE_SAVING = 15     # minutes before suggesting a different lane at the same bridge
STALENESS_PER_HOUR = 8   # minutes added per hour of age: old readings deserve less trust
STALENESS_CAP = 25       # but age alone never disqualifies a bridge outright
RISING_WEIGHT = 0.6      # a line that grew 20 min since the last reading is probably still growing
FALLING_WEIGHT = 0.3     # shrinking is trusted less than growing — being wrong costs more
TREND_CAP = 15           # neither direction may swamp the actual number
CONFIDENT_AGE = 35       # a reading fresher than this is "high" confidence
USABLE_AGE = 95          # older than this is "low"
BIG_JUMP = 15            # a change this large since the last reading costs confidence

# Lanes worth pointing out from each lane. Anyone can park and walk; card lanes are
# offered only from the general car line, where they may already hold the card.
FASTER_LANES = {"car": ("walk", "car_ready", "car_sentri"),
                "car_ready": ("walk",), "car_sentri": ("walk",)}


def score(minutes: int | None, age: int | None, delta: dict | None,
          stamp_ahead: bool = False) -> dict | None:
    """Two adjustments, both explainable to a person:
      - age: an hour-old number is a worse bet than a fresh one
      - trend: a line that just grew is likely to keep growing, and growth is
        weighted more heavily than shrinkage because over-promising costs more
    """
    if minutes is None:
        return None
    # A stamp ahead of the clock reads as age 0, which would make the least
    # trustworthy reading rank as the freshest. Treat that age as unknown instead.
    effective_age = UNKNOWN_AGE if stamp_ahead else (age or 0)
    stale_penalty = min(round(effective_age / 60 * STALENESS_PER_HOUR), STALENESS_CAP)
    trend_penalty = 0
    if delta and delta["change"]:
        weight = RISING_WEIGHT if delta["change"] > 0 else FALLING_WEIGHT
        trend_penalty = max(-TREND_CAP, min(TREND_CAP, round(delta["change"] * weight)))
    return {
        "effective_minutes": max(0, minutes + stale_penalty + trend_penalty),
        "staleness_penalty": stale_penalty,
        "trend_penalty": trend_penalty,
        "age_used": effective_age,
    }


def confidence(age: int | None, delta: dict | None, stamp_ahead: bool = False) -> str:
    """How much the number deserves to be trusted, in one word."""
    if age is None or age > USABLE_AGE:
        return "low"
    if stamp_ahead or age > CONFIDENT_AGE or (delta and abs(delta["change"]) >= BIG_JUMP):
        return "medium"
    return "high"


def rank_key(row: dict) -> tuple:
    """Expected wait, then freshness when two bridges are within noise of each other.

    Rounding to a 5-minute band first stops a 1-minute modelled difference from
    deciding it — at that distance the fresher reading is the better bet.
    """
    band = round(row["effective_minutes"] / MEANINGFUL_DELTA)
    age = row["score"]["age_used"] if row.get("score") else 999
    return band, age, row["minutes"]


def closes_at(local: datetime, port: Port) -> datetime | None:
    """Today's closing time in local terms, or None when the bridge runs 24 hours."""
    window = port.hours_window
    if not window:
        return None
    midnight = local.replace(hour=0, minute=0, second=0, microsecond=0)
    closing = midnight + timedelta(minutes=window[1])
    if closing <= local:                      # already past today's close
        closing += timedelta(minutes=DAY_MINUTES)
    return closing


def faster_lane_here(port: Port, lane_id: str) -> dict | None:
    """A different lane at the same bridge that is much quicker.

    Walking across at Paso del Norte is routinely an hour faster than driving, and
    a Ready Lane holder queueing in the general line is giving away the benefit.
    """
    current = port.lanes.get(lane_id)
    if not current or not current.has_minutes:
        return None
    worth_offering = FASTER_LANES.get(lane_id, ())
    options = []
    for other_id, other in port.lanes.items():     # feed order, so ties break as before
        if other_id not in worth_offering or not other.has_minutes:
            continue
        saved = current.delay_minutes - other.delay_minutes
        if saved >= BIG_LANE_SAVING:
            options.append({"lane": other_id, "minutes": other.delay_minutes, "saves_minutes": saved})
    return max(options, key=lambda o: o["saves_minutes"]) if options else None
