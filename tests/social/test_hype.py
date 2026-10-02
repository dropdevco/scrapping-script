"""The hype auditor: does an event deserve its own standalone spotlight post?

Most events must resolve to "no" with ZERO model calls via the deterministic
pre-pass (no ticket link, not a major venue) — only what passes that gate
costs a call, and even then the model is expected to say no most of the time.
"""

from __future__ import annotations

from unittest import mock

from scraper.social import hype as hype_mod
from scraper.social.hype import hype_from_rules


def _row(id_="e1", ticket_links=None, venue_name=None, checked=None):
    return {
        "id": id_,
        "title": "Some Event",
        "description": "",
        "content_tags": [],
        "ticket_links": ticket_links or [],
        "venues": {"name": venue_name} if venue_name else None,
        "hype_checked_at": checked,
    }


def test_no_tickets_and_no_major_venue_is_not_hype_with_no_model_call():
    assert hype_from_rules(_row()) is False


def test_a_ticket_link_defers_to_the_model():
    assert hype_from_rules(_row(ticket_links=[{"url": "https://x"}])) is None


def test_a_major_venue_defers_to_the_model_even_without_tickets():
    for name in ("El Paso County Coliseum", "Don Haskins Center", "Sun Bowl"):
        assert hype_from_rules(_row(venue_name=name)) is None


def test_case_and_accents_do_not_defeat_the_major_venue_match():
    assert hype_from_rules(_row(venue_name="don haskins center")) is None


def test_an_ordinary_local_venue_is_not_deferred():
    assert hype_from_rules(_row(venue_name="Lowbrow Palace")) is False


async def test_council_off_judges_nothing():
    class _Storage:
        async def cache_event_editorial(self, *_a):
            raise AssertionError("must not write when the council is off")

    rows = [_row(ticket_links=[{"url": "https://x"}])]
    with mock.patch.object(hype_mod.settings, "council_available", False):
        judged = await hype_mod.fill_hype(_Storage(), None, rows)
    assert judged == 0


async def test_a_rule_rejected_event_is_cached_with_no_model_call():
    calls = []

    async def counting(*_a, **_kw):
        calls.append(1)
        return {"judgments": []}

    writes = []

    class _Storage:
        async def cache_event_editorial(self, event_id, patch):
            writes.append((event_id, patch))
            return True

    rows = [_row(id_="e1")]  # no tickets, no major venue
    with mock.patch.object(hype_mod, "complete_json", counting), \
         mock.patch.object(hype_mod.settings, "council_available", True):
        judged = await hype_mod.fill_hype(_Storage(), None, rows)

    assert judged == 1
    assert calls == []  # never reached the model
    event_id, patch = writes[0]
    assert event_id == "e1"
    assert patch["is_hype"] is False
    assert patch["hype_source"] == "rule"


async def test_a_gate_passing_event_goes_to_one_batched_model_call():
    async def batch_judge(*_a, **_kw):
        return {"judgments": [{"i": 0, "is_hype": True, "reason": "headline touring act"}]}

    writes = []

    class _Storage:
        async def cache_event_editorial(self, event_id, patch):
            writes.append((event_id, patch))
            return True

    rows = [_row(id_="e2", ticket_links=[{"url": "https://x"}])]
    with mock.patch.object(hype_mod, "complete_json", batch_judge), \
         mock.patch.object(hype_mod.settings, "council_available", True):
        judged = await hype_mod.fill_hype(_Storage(), None, rows)

    assert judged == 1
    event_id, patch = writes[0]
    assert event_id == "e2"
    assert patch["is_hype"] is True
    assert patch["hype_source"] == "council"


async def test_an_already_judged_event_is_never_re_asked():
    calls = []

    async def counting(*_a, **_kw):
        calls.append(1)
        return {"judgments": []}

    class _Storage:
        async def cache_event_editorial(self, *_a):
            raise AssertionError("an already-judged event must not be written again")

    rows = [_row(id_="e1", ticket_links=[{"url": "https://x"}], checked="2026-09-01T00:00:00Z")]
    with mock.patch.object(hype_mod, "complete_json", counting), \
         mock.patch.object(hype_mod.settings, "council_available", True):
        judged = await hype_mod.fill_hype(_Storage(), None, rows)

    assert judged == 0
    assert calls == []


async def test_a_model_failure_never_raises_and_leaves_events_unjudged():
    """LLMUnavailable must never propagate out of fill_hype — a down model is
    exactly the case the whole council contract exists to survive. Nothing is
    written, so the events stay NULL and are re-asked on the next build: a
    cached "no" would permanently exclude a real headliner after one outage."""
    async def boom(*_a, **_kw):
        raise hype_mod.LLMUnavailable("down")

    writes = []

    class _Storage:
        async def cache_event_editorial(self, event_id, patch):
            writes.append((event_id, patch))
            return True

    rows = [_row(id_="e3", ticket_links=[{"url": "https://x"}])]
    with mock.patch.object(hype_mod, "complete_json", boom), \
         mock.patch.object(hype_mod.settings, "council_available", True):
        judged = await hype_mod.fill_hype(_Storage(), None, rows)

    assert judged == 0
    assert writes == []


def test_clean_judgments_rejects_out_of_range_and_defaults_missing_to_false():
    raw = [
        {"i": 0, "is_hype": True, "reason": "ok"},
        {"i": 99, "is_hype": True, "reason": "out of range"},
    ]
    out = hype_mod._clean_judgments(raw, count=2)
    assert set(out) == {0}
    assert out[0]["is_hype"] is True


def test_the_model_never_gets_to_assert_true_by_rule():
    """hype_from_rules only ever returns False or None -- true is exclusively
    the model's call, since "major enough" is a judgment no rule can make."""
    for name in ("El Paso County Coliseum", "Sun Bowl", "Some Random Bar"):
        assert hype_from_rules(_row(venue_name=name, ticket_links=[{"url": "https://x"}])) in (None,)


async def test_hype_candidates_start_after_the_post_will_have_gone_out():
    """The floor is now + a lead time, not now: the post is scheduled for later that
    day, so an event starting this morning would be announced after it is over."""
    from datetime import datetime, timedelta, timezone

    from scraper.core.storage import Storage

    seen = {}

    class _Q:
        def __getattr__(self, name):
            def call(*args, **kw):
                if name in ("gte", "lt"):
                    seen[name] = args[1]
                return self
            return call

        @property
        def data(self):
            return []

    class _Client:
        def table(self, _):
            return _Q()

    storage = Storage.__new__(Storage)
    storage._client = _Client()
    before = datetime.now(timezone.utc)
    await storage.query_hype_candidates("El Paso", min_lead_hours=24)
    floor = datetime.fromisoformat(seen["gte"])
    assert floor >= before + timedelta(hours=23, minutes=59)
