"""Rebuilding a draft to satisfy a Telegram edit.

The rules being pinned here are the ones whose failure is silent and public:
shipping a carousel that still contains a dropped event, shipping one below
the slide minimum, or overwriting a post that went live mid-rebuild.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from scraper.core.config import settings
from scraper.social import __main__ as social
from scraper.social import selection

TODAY = social._today("America/Denver")  # the code's clock, not the runner's: UTC on CI is a day ahead each evening
POST = "11111111-1111-1111-1111-111111111111"


def _event(n: int) -> dict:
    return {
        "id": f"{n}" * 8 + "-1111-1111-1111-111111111111",
        "title": f"Event {n}",
        "start_time": f"{TODAY.isoformat()}T19:00:00+00:00",
        "venue": f"Venue {n}",
        "image_url": None,
        "categories": ["music"],
    }


EVENTS = [_event(n) for n in range(1, 7)]
WINDOW = (f"{TODAY.isoformat()}T00:00:00+00:00", f"{(TODAY + timedelta(days=1)).isoformat()}T00:00:00+00:00")


class FakeStorage:
    enabled = True
    client = object()

    def __init__(self, post, edits, bench=None):
        self.post = post
        self.edits = edits
        self.marked: list[tuple[list[str], str | None]] = []
        self.final_patch: dict | None = None
        self.cas_succeeds = True
        self.bench = bench or []

    async def pending_edits(self, post_id=None):
        return list(self.edits)

    async def get_ig_post(self, post_id):
        return self.post

    async def events_by_ids(self, ids):
        # Deliberately reversed: events_by_ids makes no order guarantee, and
        # the rebuild must re-impose the post's own stored order.
        pool = EVENTS + self.bench
        return [e for e in reversed(pool) if e["id"] in set(ids)]

    async def mark_edits_applied(self, ids, error=None):
        self.marked.append((list(ids), error))

    async def query_events_for_range(self, city, start_iso, end_iso):
        # A backfill re-queries the post's own window; the fake window covers
        # the whole pool, same as a real digest window would.
        return EVENTS + self.bench

    async def recent_slide_keys(self, since_date, kinds=("digest",)):
        return set()

    async def apply_ig_post_edit_result(self, post_id, patch):
        if not self.cas_succeeds:
            return False
        self.final_patch = patch
        return True

    async def update_ig_post(self, post_id, patch):
        pass


@pytest.fixture
def wired(monkeypatch):
    monkeypatch.setattr(settings, "ig_min_slides", 4)
    monkeypatch.setattr(settings, "ig_timezone", "America/Denver")
    monkeypatch.setattr(settings, "ig_handle", "epchisme.com")

    uploaded: dict = {}
    monkeypatch.setattr(
        social.slides_store, "upload_slides",
        lambda c, b, paths, jpegs: uploaded.update(paths=paths, jpegs=jpegs),
    )
    removed: list = []
    monkeypatch.setattr(social.slides_store, "remove_objects", lambda c, b, p: removed.extend(p))

    async def _no_photo(*a, **k):
        return None

    monkeypatch.setattr(social, "fetch_photo", _no_photo)

    alerts: list[str] = []

    async def _alert(http, text):
        alerts.append(text)

    async def _draft_ready(*a, **k):
        pass

    monkeypatch.setattr(social.notify_mod, "notify_alert", _alert)
    monkeypatch.setattr(social.notify_mod, "notify_draft_ready", _draft_ready)

    def _install(
        *, event_ids, edits, status="draft", caption_is_custom=False, window=None, bench=None
    ):
        post = {
            "id": POST,
            "post_date": TODAY.isoformat(),
            "kind": "digest",
            "status": status,
            "event_ids": event_ids,
            "caption": "original caption",
            "caption_is_custom": caption_is_custom,
            "slide_paths": [f"{TODAY.isoformat()}/{POST}/{i:02d}.jpg" for i in range(len(event_ids) + 1)],
            "photo_overrides": {},
        }
        if window:
            post["window_start"], post["window_end"] = window
        store = FakeStorage(post, edits, bench=bench)
        monkeypatch.setattr(social, "Storage", lambda: store)
        return store, uploaded, removed, alerts

    return _install


def _drop(index):
    return {"id": f"edit-{index}", "post_id": POST, "op": "drop_event", "payload": {"index": index}}


ALL_IDS = [e["id"] for e in EVENTS]


async def test_dropping_one_event_rebuilds_without_it(wired):
    store, uploaded, _, _ = wired(event_ids=list(ALL_IDS), edits=[_drop(2)])
    assert await social.apply_edits() == 0
    kept = store.final_patch["event_ids"]
    assert ALL_IDS[2] not in kept
    assert kept == [ALL_IDS[0], ALL_IDS[1], ALL_IDS[3], ALL_IDS[4], ALL_IDS[5]]


async def test_the_cover_is_rerendered_too_because_the_count_changed(wired):
    """5 events -> 6 slides: one cover plus five. If only the tail were
    re-rendered, the cover would keep advertising the old event count."""
    store, uploaded, _, _ = wired(event_ids=list(ALL_IDS), edits=[_drop(2)])
    await social.apply_edits()
    assert len(uploaded["jpegs"]) == 6
    assert len(store.final_patch["slide_paths"]) == 6


async def test_the_now_orphaned_trailing_slide_is_removed(wired):
    """slide_paths is positional and rewritten wholesale, so the old last
    object is left unreferenced."""
    _, _, removed, _ = wired(event_ids=list(ALL_IDS), edits=[_drop(2)])
    await social.apply_edits()
    assert removed == [f"{TODAY.isoformat()}/{POST}/06.jpg"]


async def test_stored_order_is_reimposed_after_the_reread(wired):
    """events_by_ids gives no order guarantee; the post's order is the one a
    human already reviewed."""
    store, _, _, _ = wired(event_ids=list(ALL_IDS), edits=[_drop(0)])
    await social.apply_edits()
    assert store.final_patch["event_ids"] == ALL_IDS[1:]


async def test_dropping_below_the_minimum_leaves_the_post_untouched(wired):
    """Refusing is the point: a 3-slide digest reads worse than none, and the
    human should be told rather than silently given a short carousel."""
    store, _, _, alerts = wired(event_ids=ALL_IDS[:4], edits=[_drop(0)])
    await social.apply_edits()
    assert store.final_patch is None
    assert store.marked and "below the minimum" in (store.marked[0][1] or "")
    assert alerts and "unchanged" in alerts[0]


async def test_two_drops_apply_in_request_order(wired):
    store, _, _, _ = wired(event_ids=list(ALL_IDS), edits=[_drop(0), _drop(0)])
    await social.apply_edits()
    assert store.final_patch["event_ids"] == ALL_IDS[2:]


async def test_a_post_approved_mid_rebuild_is_abandoned(wired):
    """The final CAS is the guard: past 'draft' the carousel may already be at
    Meta, and overwriting slide_paths under it is worse than losing the edit."""
    store, _, _, alerts = wired(event_ids=list(ALL_IDS), edits=[_drop(2)])
    store.cas_succeeds = False
    assert await social.apply_edits() == 0
    assert store.marked and "approved while the rebuild ran" in (store.marked[0][1] or "")
    assert alerts and "published unchanged" in alerts[0]


async def test_edits_on_an_already_published_post_are_discarded_not_retried(wired):
    """Otherwise they would block the auto-approve sweep forever."""
    store, _, _, _ = wired(event_ids=list(ALL_IDS), edits=[_drop(2)], status="published")
    await social.apply_edits()
    assert store.final_patch is None
    assert store.marked and "published" in (store.marked[0][1] or "")


async def test_caption_is_regenerated_by_default(wired):
    store, _, _, _ = wired(event_ids=list(ALL_IDS), edits=[_drop(2)])
    await social.apply_edits()
    assert store.final_patch["caption"] != "original caption"


async def test_a_hand_written_caption_survives_an_unrelated_rebuild(wired):
    """Dropping an event must not silently overwrite words a human typed."""
    store, _, _, _ = wired(
        event_ids=list(ALL_IDS), edits=[_drop(2)], caption_is_custom=True
    )
    await social.apply_edits()
    assert store.final_patch["caption"] == "original caption"


async def test_dry_run_writes_nothing(wired):
    store, _, _, _ = wired(event_ids=list(ALL_IDS), edits=[_drop(2)])
    await social.apply_edits(dry_run=True)
    assert store.final_patch is None and store.marked == []


async def test_an_out_of_range_index_is_ignored_rather_than_crashing(wired):
    """callback_data is stale the moment another edit lands."""
    store, _, _, _ = wired(event_ids=list(ALL_IDS), edits=[_drop(99)])
    assert await social.apply_edits() == 0
    assert store.final_patch["event_ids"] == ALL_IDS


# ── drop backfill ────────────────────────────────────────────────────────────
async def test_a_drop_is_backfilled_from_the_window_when_the_bench_has_room(wired, monkeypatch):
    """Dropping a duplicate must not quietly ship a shorter carousel than the
    day actually supports — a bench event fills the open slot."""
    monkeypatch.setattr(settings, "ig_max_slides", len(ALL_IDS))
    bench = _event(7)
    store, _, _, _ = wired(
        event_ids=list(ALL_IDS), edits=[_drop(2)], window=WINDOW, bench=[bench]
    )
    await social.apply_edits()
    kept = store.final_patch["event_ids"]
    assert len(kept) == len(ALL_IDS)
    assert ALL_IDS[2] not in kept
    assert bench["id"] in kept


async def test_backfill_never_overshoots_ig_max_slides(wired, monkeypatch):
    """Two bench events are available but only one slot opened up."""
    monkeypatch.setattr(settings, "ig_max_slides", len(ALL_IDS))
    bench = [_event(7), _event(8)]
    store, _, _, _ = wired(
        event_ids=list(ALL_IDS), edits=[_drop(2)], window=WINDOW, bench=bench
    )
    await social.apply_edits()
    assert len(store.final_patch["event_ids"]) == len(ALL_IDS)


async def test_backfill_skips_a_same_venue_duplicate_of_a_kept_event(wired, monkeypatch):
    """The 2026-09-23 shape: a bench row is the same real happening as
    something already kept, just worded differently — it must not be
    readmitted even though it isn't literally the dropped id."""
    monkeypatch.setattr(settings, "ig_max_slides", len(ALL_IDS))
    duplicate_of_event_1 = {
        **_event(8),
        "title": "EVENT 1!",  # near-identical to kept Event 1's title
        "venue": "Venue 1",  # same venue as kept Event 1
        "ticket_links": [{"url": "x"}],  # outscores the real Event 1, so it's
        # THIS row (a different id than kept's Event 1) that survives
        # selection.choose()'s own internal collapse and reaches the pool —
        # exercising _backfill_for_edit's own duplicate guard, not just the
        # plain "already kept" id check.
    }
    store, _, _, _ = wired(
        event_ids=list(ALL_IDS), edits=[_drop(2)], window=WINDOW, bench=[duplicate_of_event_1]
    )
    await social.apply_edits()
    kept = store.final_patch["event_ids"]
    assert duplicate_of_event_1["id"] not in kept
    assert len(kept) == len(ALL_IDS) - 1  # nothing else was on the bench to fill the slot


async def test_no_backfill_without_a_stored_window(wired, monkeypatch):
    """An older post (or any caller that never set window_start/window_end)
    gets no backfill rather than an error — best-effort, not a hard promise."""
    monkeypatch.setattr(settings, "ig_max_slides", len(ALL_IDS))
    store, _, _, _ = wired(event_ids=list(ALL_IDS), edits=[_drop(2)], bench=[_event(7)])
    await social.apply_edits()
    assert store.final_patch["event_ids"] == [ALL_IDS[0], ALL_IDS[1], ALL_IDS[3], ALL_IDS[4], ALL_IDS[5]]


async def test_a_short_post_can_be_rescued_above_the_minimum_by_backfill(wired, monkeypatch):
    """Below-minimum after a drop is a reason to backfill first, not to
    refuse outright — refusing should only happen if the bench can't help.
    The original post only used 4 of the 6 window events (say, an earlier
    build with a tighter cap); backfill draws on the other 2 plus the bench,
    not just enough to clear the minimum."""
    monkeypatch.setattr(settings, "ig_max_slides", 9)
    store, _, _, alerts = wired(
        event_ids=ALL_IDS[:4], edits=[_drop(0)], window=WINDOW, bench=[_event(7)]
    )
    await social.apply_edits()
    assert store.final_patch is not None
    kept = store.final_patch["event_ids"]
    assert len(kept) == 6  # 3 kept + the 2 unused window events + the bench event
    assert ALL_IDS[0] not in kept  # the dropped event is not handed right back
    assert not alerts


async def test_swap_photo_alone_never_triggers_a_backfill_query(wired, monkeypatch):
    """A photo swap doesn't change the slide count, so there's nothing to
    refill — and no reason to pay for a window re-query."""
    monkeypatch.setattr(settings, "ig_max_slides", len(ALL_IDS))
    store, _, _, _ = wired(
        event_ids=ALL_IDS[:4],
        edits=[{"id": "e1", "post_id": POST, "op": "swap_photo",
                "payload": {"event_id": ALL_IDS[0], "file_id": "f1"}}],
        window=WINDOW, bench=[_event(7)],
    )

    async def _boom(*a, **k):
        raise AssertionError("swap_photo alone must not query the window")

    async def _no_swap(*a, **k):
        return None

    monkeypatch.setattr(store, "query_events_for_range", _boom)
    monkeypatch.setattr(social, "_store_swapped_photo", _no_swap)
    await social.apply_edits()
    assert store.final_patch["event_ids"] == ALL_IDS[:4]


# ── candidates_from_rows ──────────────────────────────────────────────────────
def test_candidates_from_rows_preserves_order_and_does_not_rank():
    cands = selection.candidates_from_rows(EVENTS, "America/Denver")
    assert [c.row["id"] for c in cands] == ALL_IDS
    assert all(c.score == 0.0 for c in cands)
