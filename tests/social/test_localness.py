"""The localness auditor: local independent business vs. chain, judged once
per venue and cached forever.

The boss's own example (Flix Brewhouse) must be resolvable with ZERO model
calls, since a deterministic rule is free and a chain's identity does not
need a model's judgment. Everything else goes through exactly one batched
call per build, never one call per venue.
"""

from __future__ import annotations

from unittest import mock

from scraper.social import localness as localness_mod
from scraper.social.localness import chain_scope_from_rules


def test_the_named_example_is_recognised_with_no_model_call():
    """The boss's own example, and the reason a deterministic pre-pass exists
    at all: this must never cost a call."""
    assert chain_scope_from_rules("Flix Brewhouse") == "national"


def test_real_chains_from_the_live_dataset_are_recognised():
    """Confirmed present in production on 2026-09-20."""
    for name in ("Topgolf El Paso", "Starbucks", "Lowe's Home Improvement"):
        assert chain_scope_from_rules(name) == "national"


def test_an_independent_or_civic_venue_is_not_a_chain():
    assert chain_scope_from_rules("Plaza Theatre") is None
    assert chain_scope_from_rules("El Paso Museum of History") is None
    assert chain_scope_from_rules("La Nube") is None


def test_case_and_accents_do_not_defeat_the_match():
    assert chain_scope_from_rules("STARBUCKS") == "national"
    assert chain_scope_from_rules("starbucks ") == "national"


def test_an_empty_name_is_never_a_chain():
    assert chain_scope_from_rules(None) is None
    assert chain_scope_from_rules("") is None


def _row_at(venue_id, name, checked=None):
    return {"venues": {"id": venue_id, "name": name, "address": "1 Main St", "localness_checked_at": checked}}


async def test_council_off_judges_nothing():
    class _Storage:
        async def cache_venue_editorial(self, *_a):
            raise AssertionError("must not write when the council is off")

    rows = [_row_at("v1", "Flix Brewhouse")]
    with mock.patch.object(localness_mod.settings, "council_available", False):
        judged = await localness_mod.fill_localness(_Storage(), None, rows)
    assert judged == 0


async def test_a_known_chain_is_cached_by_rule_with_no_model_call():
    calls = []
    writes = []

    async def counting(*_a, **_kw):
        calls.append(1)
        return {"judgments": []}

    class _Storage:
        async def cache_venue_editorial(self, venue_id, patch):
            writes.append((venue_id, patch))
            return True

    rows = [_row_at("v1", "Flix Brewhouse")]
    with mock.patch.object(localness_mod, "complete_json", counting), \
         mock.patch.object(localness_mod.settings, "council_available", True):
        judged = await localness_mod.fill_localness(_Storage(), None, rows)

    assert judged == 1
    assert calls == []  # never reached the model
    venue_id, patch = writes[0]
    assert venue_id == "v1"
    assert patch["chain_scope"] == "national"
    assert patch["is_local"] is False
    assert patch["localness_source"] == "rule"


async def test_an_unknown_venue_goes_to_one_batched_model_call():
    async def batch_judge(*_a, **_kw):
        return {"judgments": [{"i": 0, "chain_scope": "local", "reason": "independent bar"}]}

    writes = []

    class _Storage:
        async def cache_venue_editorial(self, venue_id, patch):
            writes.append((venue_id, patch))
            return True

    rows = [_row_at("v2", "Some Local Bar")]
    with mock.patch.object(localness_mod, "complete_json", batch_judge), \
         mock.patch.object(localness_mod.settings, "council_available", True):
        judged = await localness_mod.fill_localness(_Storage(), None, rows)

    assert judged == 1
    venue_id, patch = writes[0]
    assert venue_id == "v2"
    assert patch["chain_scope"] == "local"
    assert patch["is_local"] is True
    assert patch["localness_source"] == "council"


async def test_a_venue_already_judged_is_never_re_asked():
    calls = []

    async def counting(*_a, **_kw):
        calls.append(1)
        return {"judgments": []}

    class _Storage:
        async def cache_venue_editorial(self, *_a):
            raise AssertionError("an already-judged venue must not be written again")

    rows = [_row_at("v1", "Flix Brewhouse", checked="2026-09-01T00:00:00Z")]
    with mock.patch.object(localness_mod, "complete_json", counting), \
         mock.patch.object(localness_mod.settings, "council_available", True):
        judged = await localness_mod.fill_localness(_Storage(), None, rows)

    assert judged == 0
    assert calls == []


async def test_an_unresolvable_venue_is_cached_as_unknown_rather_than_reasked_forever():
    """A model that returns nothing usable for an index still results in the
    venue being marked checked, with is_local left NULL (unjudged posture) —
    the alternative is asking about the same unresolvable venue every build."""
    async def empty(*_a, **_kw):
        return {"judgments": []}

    writes = []

    class _Storage:
        async def cache_venue_editorial(self, venue_id, patch):
            writes.append((venue_id, patch))
            return True

    rows = [_row_at("v3", "Ambiguous LLC")]
    with mock.patch.object(localness_mod, "complete_json", empty), \
         mock.patch.object(localness_mod.settings, "council_available", True):
        judged = await localness_mod.fill_localness(_Storage(), None, rows)

    assert judged == 1
    _, patch = writes[0]
    assert patch["chain_scope"] == "unknown"
    assert patch["is_local"] is None
    assert patch["localness_checked_at"]


def test_clean_judgments_rejects_out_of_range_and_invalid_scopes():
    raw = [
        {"i": 0, "chain_scope": "local", "reason": "ok"},
        {"i": 99, "chain_scope": "national", "reason": "out of range"},
        {"i": 1, "chain_scope": "not-a-real-scope", "reason": "invalid"},
    ]
    out = localness_mod._clean_judgments(raw, count=2)
    assert set(out) == {0, 1}
    assert out[0]["chain_scope"] == "local"
    assert out[1]["chain_scope"] == "unknown"  # invalid scope falls back, not dropped


def test_duplicate_venues_across_rows_are_judged_once():
    rows = [_row_at("v1", "Flix Brewhouse"), _row_at("v1", "Flix Brewhouse")]
    unjudged = localness_mod._unjudged_venues(rows)
    assert len(unjudged) == 1
