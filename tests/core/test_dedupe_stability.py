"""A merged event cluster must keep one stable content_hash.

Three duplicate pairs were live on 2026-09-17 (EPSO: MÁGICO, Miss Cosmo USA,
SIN BANDERA), each stored twice from two sources. The cause was in _merge_into:
the surviving record kept ITS OWN hash, and which record survived depended on
which copy happened to have more non-null fields. So the day a second source
started carrying one extra field — or the day the first source simply failed
and only one copy was scraped — the cluster's hash changed, missed the
content_hash upsert conflict, and INSERTed a second row for an event already
stored. Nothing ever deleted the first one.
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from scraper.core.dedupe import assign_hashes_events, dedupe_events
from scraper.core.models import Event

TZ = ZoneInfo("America/Denver")


def _event(source: str, url: str, *, description=None, image_url=None, start_hour=19) -> Event:
    return Event(
        source=source,
        title="EPSO: MÁGICO – THE MAGIC OF MEXICO",
        url=url,
        venue="Plaza Theatre",
        location="125 W Mills Ave, El Paso, TX 79901",
        start_time=datetime(2026, 9, 18, start_hour, 30, tzinfo=TZ),
        description=description,
        image_url=image_url,
    )


def _hash_of(events: list[Event]) -> str:
    merged = dedupe_events(assign_hashes_events(events))
    assert len(merged) == 1, "the two copies should have merged into one record"
    return merged[0].content_hash


def test_merged_hash_is_the_same_whichever_record_is_richer():
    lean = _event("events_directories", "https://elpasolive.com/events/epso-magico")
    rich = _event(
        "events_web",
        "https://visitelpaso.com/events/epso-magico",
        description="An evening of Mexican classics.",
        image_url="https://example.com/a.jpg",
    )
    # Same pair, but with the richness reversed.
    lean2 = _event("events_web", "https://visitelpaso.com/events/epso-magico")
    rich2 = _event(
        "events_directories",
        "https://elpasolive.com/events/epso-magico",
        description="An evening of Mexican classics.",
        image_url="https://example.com/a.jpg",
    )
    assert _hash_of([lean, rich]) == _hash_of([lean2, rich2])


def test_merged_hash_is_independent_of_input_order():
    a = _event("events_directories", "https://elpasolive.com/events/epso-magico")
    b = _event("events_web", "https://visitelpaso.com/events/epso-magico", description="x" * 50)
    forward = _hash_of([a, b])

    a2 = _event("events_directories", "https://elpasolive.com/events/epso-magico")
    b2 = _event("events_web", "https://visitelpaso.com/events/epso-magico", description="x" * 50)
    assert _hash_of([b2, a2]) == forward


def test_the_hash_is_stable_across_runs_for_a_fixed_set_of_sources():
    """Two runs that scrape the same two sources must agree, even when the
    copies arrive in a different order and with different fields filled in —
    which is what used to flip the cluster's identity day to day."""
    run_one = _hash_of(
        [
            _event("events_directories", "https://elpasolive.com/events/epso-magico", description="x" * 50),
            _event("events_web", "https://visitelpaso.com/events/epso-magico"),
        ]
    )
    run_two = _hash_of(
        [
            _event("events_web", "https://visitelpaso.com/events/epso-magico", image_url="https://e/x.jpg"),
            _event("events_directories", "https://elpasolive.com/events/epso-magico"),
        ]
    )
    assert run_one == run_two


def test_a_source_joining_the_cluster_still_moves_the_hash():
    """The deliberate boundary of this fix, pinned so nobody assumes otherwise.

    min() cannot be stable when the SET of members changes: a new, lower hash
    joining legitimately moves the minimum. So the day a second source starts
    listing an event we already store, the in-batch hash still changes — and
    the thing that stops a second row being INSERTed is not this function but
    the cross-run merge in storage._merge_with_existing, which finds the stored
    row by venue + day + title and merges into it. That merge is in turn only
    reliable because venue identity is now normalized (see
    tests/core/test_venue_identity.py); all three duplicate pairs found in
    production on 2026-09-17 were rows whose venue_ids had forked.
    """
    together = _hash_of(
        [
            _event("events_directories", "https://elpasolive.com/events/epso-magico"),
            _event("events_web", "https://visitelpaso.com/events/epso-magico", description="x" * 50),
        ]
    )
    # What IS guaranteed: the cluster resolves to one of its own members'
    # hashes, never to a third value invented by the merge.
    member_hashes = {
        e.content_hash
        for e in assign_hashes_events(
            [
                _event("events_directories", "https://elpasolive.com/events/epso-magico"),
                _event("events_web", "https://visitelpaso.com/events/epso-magico"),
            ]
        )
    }
    assert together in member_hashes


def test_a_three_way_cluster_collapses_to_the_same_hash_from_any_arrival_order():
    def build():
        return [
            _event("events_directories", "https://elpasolive.com/events/epso-magico"),
            _event("events_web", "https://visitelpaso.com/events/epso-magico", description="x" * 50),
            _event("events_ticketmaster", "https://ticketmaster.com/epso-magico", image_url="https://e/x.jpg"),
        ]

    a, b, c = build()
    x, y, z = build()
    assert _hash_of([a, b, c]) == _hash_of([z, x, y])


def test_two_different_events_on_the_same_day_are_still_not_merged():
    """Regression guard: stabilizing the hash must not make merging looser."""
    concert = _event("events_web", "https://visitelpaso.com/events/epso-magico")
    unrelated = Event(
        source="events_web",
        title="Beginner's Yoga",
        url="https://example.com/yoga",
        venue="Western Hills Church",
        location="El Paso, TX",
        start_time=datetime(2026, 9, 18, 19, 30, tzinfo=TZ),
    )
    merged = dedupe_events(assign_hashes_events([concert, unrelated]))
    assert len(merged) == 2


def test_merging_records_a_start_time_disagreement_instead_of_picking_one():
    """An aggregator listing 13:30 for a 19:30 show used to silently decide the
    publicly displayed time, by the same richness coin-flip. Now the loser's
    time is kept in raw for later analysis and neither is invented away."""
    aggregator = _event("events_directories", "https://elpasolive.com/events/epso-magico", start_hour=13)
    venue_site = _event(
        "events_web",
        "https://visitelpaso.com/events/epso-magico",
        description="An evening of Mexican classics.",
        start_hour=19,
    )
    merged = dedupe_events(assign_hashes_events([aggregator, venue_site]))
    assert len(merged) == 1
    alts = merged[0].raw.get("_merged_alt_start_times")
    assert alts and alts[0]["start_time"].startswith("2026-09-18T13:30")
