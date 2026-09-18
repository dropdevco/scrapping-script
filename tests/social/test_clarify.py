"""Which titles get a generated one-liner, and which need nothing.

Both examples pinned here are the ones named in the 2026-09-17 feedback on the
published posts: "Yoga Parks" reads as though Yoga were the name of a park, and
a Rhinos matchup needs no help at all. Getting those two right is the whole
brief -- the point is not to describe every event, it is to notice the opaque
ones and fill only that gap.
"""

from __future__ import annotations

import pytest

from scraper.social.clarify import (
    has_mangled_phrasing,
    is_self_explanatory,
    needs_clarifier,
    title_repeats_venue,
)


def _row(title, venue="A Venue"):
    return {"title": title, "venue": venue}


@pytest.mark.parametrize(
    "title",
    [
        "El Paso Rhinos vs. Odessa Jackalopes",
        "El Paso Chihuahuas vs. Oklahoma City Comets",
        "El Paso Rhinos",                       # a known team, no descriptive noun
        "El Paso Greek Festival",
        "Mamma Mia! (Touring) - a musical",
    ],
)
def test_titles_that_already_say_what_they_are(title):
    assert is_self_explanatory(_row(title))
    assert not needs_clarifier(_row(title))


def test_a_borderline_title_errs_toward_asking():
    """"Disney On Ice presents Jump In!" is clear to a human but carries no
    word that says what KIND of thing it is. The gate asks anyway, which is the
    deliberate bias: a wasted call costs cents and usually comes back
    "self_explanatory", while a wrongly-skipped title ships the vague post."""
    assert needs_clarifier(_row("Disney On Ice presents Jump In!"))


@pytest.mark.parametrize(
    "title",
    [
        "Desert Bloomers",                    # named in the feedback
        "Museums on Us",
        "Canvas & Cantaritos",
        "Austin Jimmy Murphy",                # a musician; you cannot tell it is a gig
        "Kermess: Our Lady of Fatima (Van Horn)",
        "Thursday Morning Cruise",
        "Passport to Discovery - Second Stop: S. Korea",
        "EPSO: MÁGICO",                       # an initialism says nothing on its own
    ],
)
def test_opaque_titles_are_sent_for_a_one_liner(title):
    assert needs_clarifier(_row(title))


def test_the_yoga_parks_case_is_not_waved_through_by_a_keyword():
    """The sharpest case. The word "yoga" is right there, so a plain keyword
    test calls this clear -- but the City calendar has glued its own section
    name onto the end and the result reads as a park named Yoga."""
    row = _row("Marty Robbins Recreation Center Yoga Parks", venue="Marty Robbins Recreation Center")
    assert has_mangled_phrasing(row)
    assert not is_self_explanatory(row)
    assert needs_clarifier(row)


def test_a_clean_yoga_title_needs_no_help():
    """The mangled-tail rule must not condemn every yoga class."""
    assert is_self_explanatory(_row("Free Yoga Thursdays"))


def test_a_title_that_is_just_the_venue_name_again_is_opaque():
    row = _row("San Pedro de Jesus Maldonado", venue="San Pedro de Jesus Maldonado")
    assert title_repeats_venue(row)
    assert needs_clarifier(row)


def test_bilingual_descriptive_nouns_are_recognised():
    assert is_self_explanatory(_row("Concierto de Mariachi"))
    assert is_self_explanatory(_row("Taller de Comunicación No Violenta"))
    assert is_self_explanatory(_row("Carrera 5K Juárez"))


def test_an_empty_title_is_not_called_self_explanatory():
    assert not is_self_explanatory(_row(""))
    assert not is_self_explanatory({"title": None, "venue": None})
