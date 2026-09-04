"""The opt-out sweep: silence is consent, but only for today's untouched drafts.

Every case here is one that would publish something wrong if it regressed —
a month-old backlog going out at once, a cancelled post going out anyway, or
the same post going out twice.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from scraper.core.config import settings
from scraper.social import __main__ as social


class FakeStorage:
    enabled = True

    def __init__(self, rows):
        self.rows = rows
        self.approved: list[str] = []
        self.updates: list[tuple[str, dict]] = []
        # Rows a concurrent human already moved out of 'draft'; the CAS loses.
        self.taken: set[str] = set()

    async def drafts_past_deadline(self, now_iso=None):
        return list(self.rows)

    async def auto_approve_ig_post(self, post_id):
        if post_id in self.taken:
            return False
        self.approved.append(post_id)
        return True

    async def update_ig_post(self, post_id, patch):
        self.updates.append((post_id, patch))


TODAY = date.today()


def _row(post_id, day):
    return {"id": post_id, "post_date": day.isoformat()}


@pytest.fixture
def wired(monkeypatch):
    """Opt-out on, IG_AUTOPOST off, Storage stubbed."""
    monkeypatch.setattr(settings, "ig_auto_approve", True)
    monkeypatch.setattr(settings, "ig_autopost", False)
    monkeypatch.setattr(settings, "ig_timezone", "America/Denver")

    def _install(rows):
        store = FakeStorage(rows)
        monkeypatch.setattr(social, "Storage", lambda: store)
        return store

    return _install


async def test_todays_untouched_draft_is_approved(wired):
    store = wired([_row("p1", TODAY)])
    assert await social.autoapprove() == 0
    assert store.approved == ["p1"]


async def test_stale_draft_is_expired_not_approved(wired):
    """A draft from three days ago must never be resurrected: _publish_one
    would expire it a moment later anyway, and 'TODAY IN EL PASO' dated last
    week is worse than posting nothing."""
    store = wired([_row("old", TODAY - timedelta(days=3))])
    await social.autoapprove()
    assert store.approved == []
    assert store.updates[0][0] == "old"
    assert store.updates[0][1]["status"] == "expired"


async def test_a_backlog_of_stale_drafts_does_not_all_publish_at_once(wired):
    """The failure this guards against is the first run after enabling
    opt-out posting dumping a month of unapproved drafts onto the feed."""
    store = wired([_row(f"old{i}", TODAY - timedelta(days=i)) for i in range(1, 15)])
    await social.autoapprove()
    assert store.approved == []
    assert all(p["status"] == "expired" for _, p in store.updates)


async def test_future_dated_draft_is_left_alone(wired):
    store = wired([_row("tomorrow", TODAY + timedelta(days=1))])
    await social.autoapprove()
    assert store.approved == [] and store.updates == []


async def test_a_human_who_acts_first_wins_the_race(wired):
    """The CAS is the arbiter: cancelling at 16:59:59 must beat the sweep."""
    store = wired([_row("p1", TODAY)])
    store.taken.add("p1")
    assert await social.autoapprove() == 0
    assert store.approved == []


async def test_running_twice_approves_nothing_the_second_time(wired):
    store = wired([_row("p1", TODAY)])
    await social.autoapprove()
    store.taken.add("p1")  # it is no longer a draft
    await social.autoapprove()
    assert store.approved == ["p1"]


async def test_disabled_is_a_total_no_op(monkeypatch):
    monkeypatch.setattr(settings, "ig_auto_approve", False)

    def _boom():
        raise AssertionError("must not touch storage when IG_AUTO_APPROVE is off")

    monkeypatch.setattr(social, "Storage", _boom)
    assert await social.autoapprove() == 0


async def test_inert_when_autopost_already_publishes_at_build_time(monkeypatch):
    """IG_AUTOPOST posts during the build, so nothing ever reaches 'draft'.
    Overlapping the two switches is a config mistake worth naming, not a crash."""
    monkeypatch.setattr(settings, "ig_auto_approve", True)
    monkeypatch.setattr(settings, "ig_autopost", True)

    def _boom():
        raise AssertionError("must not touch storage when IG_AUTOPOST is on")

    monkeypatch.setattr(social, "Storage", _boom)
    assert await social.autoapprove() == 0


async def test_dry_run_reports_without_writing(wired):
    store = wired([_row("p1", TODAY)])
    await social.autoapprove(dry_run=True)
    assert store.approved == [] and store.updates == []


async def test_date_filter_scopes_to_one_day(wired):
    other = TODAY + timedelta(days=1)
    store = wired([_row("p1", TODAY), _row("p2", other)])
    await social.autoapprove(day=TODAY)
    assert store.approved == ["p1"]
