"""_suggested_schedule: a best-effort publish time, never in the past.

Deliberately avoids depending on "the current wall-clock hour" for its
future-vs-past split — that would be flaky depending on when the suite runs.
Uses a day fully in the future / fully in the past instead, which pins the
branch unconditionally.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from scraper.social.__main__ import _suggested_schedule

TZ = "America/Denver"


def test_future_day_uses_the_suggested_hour():
    today = datetime.now(timezone.utc).date()
    tomorrow = today + timedelta(days=2)
    result = _suggested_schedule(tomorrow, TZ, 17)
    dt = datetime.fromisoformat(result).astimezone(ZoneInfo(TZ))
    assert dt.date() == tomorrow
    assert (dt.hour, dt.minute) == (17, 0)


def test_past_day_falls_back_to_now():
    today = datetime.now(timezone.utc).date()
    yesterday = today - timedelta(days=2)
    result = _suggested_schedule(yesterday, TZ, 17)
    dt = datetime.fromisoformat(result)
    now = datetime.now(timezone.utc)
    assert abs((now - dt).total_seconds()) < 5


def test_result_is_always_a_valid_utc_iso_string():
    today = datetime.now(timezone.utc).date()
    result = _suggested_schedule(today, TZ, 9)
    dt = datetime.fromisoformat(result)
    assert dt.tzinfo is not None
    assert dt.utcoffset().total_seconds() == 0
