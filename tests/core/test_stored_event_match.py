"""The one definition of "these two stored rows are the same happening".

The cross-venue lane exists because the venue-keyed lane structurally cannot
see the duplicates that were live on 2026-09-18: the same show listed by an
aggregator under its own brand ("El Paso Live") and by the building itself
("Plaza Theatre", "Abraham Chavez Theatre"). Different venue names, different
venue ids, so the venue-bucketed merge never compared them.

Every negative test here is a false-merge guard. The standing bias is that a
false merge silently HIDES a real event, which is worse than leaving a
duplicate, so each gate fails closed.
"""

from __future__ import annotations

import pytest

from scraper.core.dedupe import city_of, is_same_stored_event


def _row(title, venue, location="El Paso, TX", start="2026-09-18T19:30:00-06:00"):
    return {"title": title, "venue": venue, "location": location, "start_time": start}


# ── the three pairs found in production ───────────────────────────────────────

PRODUCTION_PAIRS = [
    pytest.param(
        _row("EPSO: MÁGICO – THE MAGIC OF MEXICO", "El Paso Live", start="2026-09-18T13:30:00-06:00"),
        _row("EPSO: MÁGICO – THE MAGIC OF MEXICO", "Plaza Theatre", "125 W Mills Ave, El Paso, TX 79901"),
        id="epso-magico-aggregator-vs-venue",
    ),
    pytest.param(
        _row("Miss Cosmo USA / Miss Teen Cosmo USA - Preliminaries", "El Paso Live", start="2026-09-18T14:00:00-06:00"),
        _row("Miss Cosmo USA / Miss Teen Cosmo USA - Preliminaries", "Abraham Chavez Theatre",
             "1 Civic Center Plaza, El Paso, TX 79901, US", "2026-09-18T20:00:00-06:00"),
        id="miss-cosmo-aggregator-vs-ticketmaster",
    ),
    pytest.param(
        _row("SIN BANDERA - ESCENAS US TOUR", "El Paso County Coliseum",
             "4100 E Paisano Dr, El Paso, TX 79905", "2026-09-18T20:00:00-06:00"),
        _row("Sin Bandera – ESCENAS US TOUR", "El Paso County Coliseum",
             "El Paso County Coliseum - 4100 E Paisano Dr, El Paso, TX 79905", "2026-09-18T20:00:00-06:00"),
        id="sin-bandera-hyphen-vs-en-dash",
    ),
]


@pytest.mark.parametrize("a, b", PRODUCTION_PAIRS)
def test_the_real_duplicate_pairs_are_recognised_across_venues(a, b):
    assert is_same_stored_event(a, b, allow_cross_venue=True)


@pytest.mark.parametrize("a, b", PRODUCTION_PAIRS)
def test_the_same_pairs_are_invisible_to_the_venue_keyed_lane_on_title_alone(a, b):
    """Not a bug — the venue lane is only ever called on rows already bucketed
    into the same venue_id, so it is correct for it to judge on title alone.
    This pins that the cross-venue lane is genuinely doing extra work."""
    assert is_same_stored_event(a, b, allow_cross_venue=False)


# ── false-merge guards ────────────────────────────────────────────────────────


def test_two_karaoke_nights_at_different_bars_never_cross_venue_merge():
    """"Karaoke Night" reduces to one distinctive word, so it is a genre, not
    an identity. Merging these would delete a real event from a real bar."""
    a = _row("Karaoke Night", "Rosewood Bar")
    b = _row("Karaoke Night", "Wall Street Lounge")
    assert not is_same_stored_event(a, b, allow_cross_venue=True)


def test_a_two_word_yoga_class_never_cross_venue_merge():
    a = _row("Yoga in the Park", "Memorial Park")
    b = _row("Yoga in the Park", "Chihuahuan Desert Gardens")
    assert not is_same_stored_event(a, b, allow_cross_venue=True)


def test_the_same_tour_in_juarez_and_el_paso_is_two_events():
    """A touring act genuinely plays both cities. Same day, identical title,
    and still two events."""
    a = _row("Comic Fest Juárez 7: cultura pop", "Plaza de la Mexicanidad", "Ciudad Juárez, Chihuahua")
    b = _row("Comic Fest Juárez 7: cultura pop", "El Paso Convention Center", "El Paso, TX")
    assert not is_same_stored_event(a, b, allow_cross_venue=True)


def test_an_undeterminable_city_blocks_a_cross_venue_merge():
    a = _row("Some Long Distinctive Event Title", "Venue A", location="")
    b = _row("Some Long Distinctive Event Title", "Venue B", location="")
    assert city_of(a) is None
    assert not is_same_stored_event(a, b, allow_cross_venue=True)


def test_different_events_on_the_same_day_in_one_city_do_not_merge():
    a = _row("El Paso Greek Festival", "St. Nicholas Greek Orthodox Church")
    b = _row("El Paso Symphony Orchestra: Glorious Sounds", "Plaza Theatre")
    assert not is_same_stored_event(a, b, allow_cross_venue=True)


def test_a_different_day_is_never_the_same_event():
    a = _row("EPSO: MÁGICO – THE MAGIC OF MEXICO", "Plaza Theatre")
    b = _row("EPSO: MÁGICO – THE MAGIC OF MEXICO", "Plaza Theatre", start="2026-09-19T19:30:00-06:00")
    assert not is_same_stored_event(a, b, allow_cross_venue=True)


def test_a_missing_start_time_is_never_a_match():
    a = _row("EPSO: MÁGICO – THE MAGIC OF MEXICO", "Plaza Theatre", start=None)
    b = _row("EPSO: MÁGICO – THE MAGIC OF MEXICO", "Plaza Theatre")
    assert not is_same_stored_event(a, b, allow_cross_venue=True)


def test_a_late_evening_show_is_bucketed_by_local_day_not_utc():
    """A 9pm El Paso show is already tomorrow in UTC. Comparing raw .date()
    values files the two copies on different days and defeats the merge."""
    a = _row("EPSO: MÁGICO – THE MAGIC OF MEXICO", "El Paso Live", start="2026-09-18T21:00:00-06:00")
    b = _row("EPSO: MÁGICO – THE MAGIC OF MEXICO", "Plaza Theatre", start="2026-09-19T03:00:00+00:00")
    assert is_same_stored_event(a, b, allow_cross_venue=True)
