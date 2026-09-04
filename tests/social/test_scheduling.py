"""_suggested_schedule: a best-effort publish time, never in the past.

Deliberately avoids depending on "the current wall-clock hour" for its
future-vs-past split — that would be flaky depending on when the suite runs.
Uses a day fully in the future / fully in the past instead, which pins the
branch unconditionally.
"""

from __future__ import annotations

import pytest
from datetime import date, datetime, timedelta, timezone
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


# ── the publish-sweep window ──────────────────────────────────────────────────
#
# These pin the workflow's cron against _suggested_schedule. The bug they exist
# to prevent: the sweep used to run "0,30 14-23 * * *", which covers a 17:00
# Denver deadline in MDT (23:00 UTC) but NOT in MST, where it lands at 00:00
# UTC the next day. In winter the post was never swept, and _publish_one's
# staleness guard expired it the following morning — a silently missed post for
# half the year, with a quiet `expired` row as the only trace.

import re
from pathlib import Path

# Hours (UTC) the publish sweep runs. Must stay in sync with the schedule in
# .github/workflows/ig_daily.yml — test_cron_matches_the_declared_window below
# reads the workflow and fails if they drift apart.
SWEEP_HOURS = set(range(13, 24)) | {0, 1, 2}

WORKFLOW = Path(__file__).resolve().parents[2] / ".github/workflows/ig_daily.yml"


def _cron_hours(expr: str) -> set[int]:
    """Expand the hour field of a cron expression like '0,30 13-23,0-2 * * *'."""
    hours: set[int] = set()
    for part in expr.split()[1].split(","):
        if "-" in part:
            lo, hi = (int(x) for x in part.split("-"))
            hours.update(range(lo, hi + 1))
        else:
            hours.add(int(part))
    return hours


def test_cron_matches_the_declared_window():
    """If someone edits the workflow's schedule, this test — not a missing
    post in November — is what tells them.

    The sweep is the every-30-minutes entry; the others are the posting
    calendar (weekend, monthly), which run once and are matched by name in
    the workflow's own `if:` guards.
    """
    text = WORKFLOW.read_text(encoding="utf-8")
    crons = re.findall(r'- cron: "([^"]+)"', text)
    sweeps = [c for c in crons if c.startswith("0,30 ")]
    assert len(sweeps) == 1, f"expected exactly one publish sweep cron, got {sweeps}"
    assert _cron_hours(sweeps[0]) == SWEEP_HOURS


def test_calendar_crons_are_excluded_from_the_publish_sweep():
    """Both format crons must be named in the publish job's `if:`, or a
    Thursday weekend build would also trigger a publish sweep — harmless, but
    it would also mean the build job runs on every half-hour sweep."""
    text = WORKFLOW.read_text(encoding="utf-8")
    calendar = [c for c in re.findall(r'- cron: "([^"]+)"', text) if not c.startswith("0,30 ")]
    assert calendar, "expected at least one posting-calendar cron"
    for cron in calendar:
        assert text.count(f"'{cron}'") >= 2, (
            f"{cron} must be matched in both the build and publish job guards"
        )


@pytest.mark.parametrize(
    "day, label",
    [(date(2027, 1, 15), "MST (winter)"), (date(2027, 7, 15), "MDT (summer)")],
)
def test_deadline_falls_inside_the_sweep_window_year_round(day, label):
    iso = _suggested_schedule(day, TZ, 17)
    utc_hour = datetime.fromisoformat(iso).astimezone(timezone.utc).hour
    assert utc_hour in SWEEP_HOURS, (
        f"a 17:00 Denver deadline in {label} lands at {utc_hour:02d}:00 UTC, "
        "which the publish sweep never runs — the post would expire unpublished"
    )


def test_the_two_halves_of_the_year_really_do_differ():
    """Guards the test above from becoming vacuous.

    The dates must stay in the FUTURE: _suggested_schedule clamps a past day
    to "now", so past dates make both branches return the current time and the
    DST assertion proves nothing. That is exactly what this caught when the
    parametrization was first written against 2026 dates."""
    winter = datetime.fromisoformat(_suggested_schedule(date(2027, 1, 15), TZ, 17))
    summer = datetime.fromisoformat(_suggested_schedule(date(2027, 7, 15), TZ, 17))
    assert winter.astimezone(timezone.utc).hour != summer.astimezone(timezone.utc).hour
