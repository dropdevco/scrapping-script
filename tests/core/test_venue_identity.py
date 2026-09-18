"""Venue identity: one building must resolve to one address_hash.

Measured on the live database 2026-09-17: 439 venue rows held only 412 distinct
buildings, because address_hash was sha1 over the raw strings and every
difference in punctuation, spacing or country suffix forked a new venue. That
fragmentation defeated the cross-run event merge, which matches on exact
venue_id, and shipped duplicate events to the carousel — the El Paso County
Coliseum held two ids differing by a single double-space.
"""

from __future__ import annotations

import pytest

from scraper.core.address import normalize_address, normalize_venue_name, venue_identity


def test_one_civic_center_plaza_and_1_civic_center_plaza_are_the_same_venue():
    """The real Abraham Chavez Theatre incident: three ids for one theatre."""
    spellings = [
        "1 Civic Center Plaza, El Paso, TX 79901, US",
        "One Civic Center Plaza, El Paso, TX 79901",
        "1 Civic Center Plaza, El Paso, TX 79901",
    ]
    hashes = {venue_identity(a, "Abraham Chavez Theatre") for a in spellings}
    assert len(hashes) == 1


@pytest.mark.parametrize(
    "a, b",
    [
        # Trailing country token — Ticketmaster appends it, venue sites don't.
        ("3200 Durazno Ave, El Paso, TX 79905, US", "3200 Durazno Ave, El Paso, TX 79905"),
        # Country spelled out.
        ("1 Civic Center Plaza, El Paso, TX 79901, United States", "1 Civic Center Plaza, El Paso, TX 79901"),
        # ZIP+4 against plain ZIP.
        ("2829 Montana Ave, El Paso, TX 79903-1234", "2829 Montana Ave, El Paso, TX 79903"),
        # Street type spelled out vs abbreviated vs abbreviated-with-period.
        ("7315 Canutillo-La Union Road, Canutillo, TX", "7315 Canutillo-La Union Rd., Canutillo, TX"),
        # Collapsed whitespace — the live El Paso County Coliseum fork.
        ("El Paso County Coliseum  - 4100 E Paisano Dr", "El Paso County Coliseum - 4100 E Paisano Dr"),
        # Accents.
        ("Plaza de la Mexicanidad, Ciudad Juárez", "Plaza de la Mexicanidad, Ciudad Juarez"),
        # Suite marker spelled out vs the # sigil.
        ("2829 Montana Ave Ste 200, El Paso, TX", "2829 Montana Ave #200, El Paso, TX"),
    ],
)
def test_cosmetic_address_differences_do_not_fork_a_venue(a, b):
    assert venue_identity(a, "Some Venue") == venue_identity(b, "Some Venue")


def test_two_suites_in_one_building_stay_separate_venues():
    """The over-merge guard. Suite markers are canonicalized, never dropped —
    two tenants at one street address are two venues, and collapsing them
    would silently merge unrelated businesses."""
    a = venue_identity("100 Main St Ste 200, El Paso, TX", "Tenant A")
    b = venue_identity("100 Main St Ste 400, El Paso, TX", "Tenant B")
    assert a != b


def test_different_buildings_stay_separate():
    a = venue_identity("125 W Mills Ave, El Paso, TX 79901", "Plaza Theatre")
    b = venue_identity("4100 E Paisano Dr, El Paso, TX 79905", "El Paso County Coliseum")
    assert a != b


def test_a_leading_article_does_not_fork_a_venue_name():
    assert normalize_venue_name("The Plaza Theatre") == normalize_venue_name("Plaza Theatre")


def test_mid_address_number_words_are_left_alone():
    """Only a LEADING number word is a street number. "Two Bridges Road" is a
    street name and must not become "2 Bridges Road"."""
    assert normalize_address("100 Two Bridges Road") == "100 two bridges rd"


def test_an_empty_address_does_not_raise():
    assert normalize_address(None) == ""
    assert normalize_venue_name(None) == ""
    assert venue_identity(None, None)  # a hash of "|", but a stable one
