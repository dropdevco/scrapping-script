"""The six Instagram content pillars.

Two rules carry the design. SPORTS MEANS SPECTATOR -- a race you enter and a
game you watch are different content, and splitting them was the point of
introducing pillars (core/categorize.py still files "5k" under Sports, and that
taxonomy is left alone because the website depends on it). And an event this
cannot place PLAINLY returns [], never a catch-all, because the empty list is
what tells the council there is a judgement to make.
"""

from __future__ import annotations

import pytest

from scraper.core.content_tags import (
    ARTS,
    FAMILY,
    FITNESS,
    FOOD,
    MUSIC,
    PILLARS,
    SPORTS,
    pillars_for,
)


def test_a_chihuahuas_game_is_spectator_sports():
    assert pillars_for(
        "El Paso Chihuahuas vs. Oklahoma City Comets",
        ["Sports", "Baseball", "Minor League"],
        "Southwest University Park",
    ) == [SPORTS]


def test_a_bare_team_name_is_still_sports():
    """"El Paso Rhinos" carries no descriptive word at all, and there are 24 of
    their games upcoming -- naming the local teams is the highest-yield rule."""
    assert pillars_for("El Paso Rhinos", ["Hockey"], "Events Center") == [SPORTS]


def test_bare_utep_is_not_a_team_name():
    """"utep" used to be in the team list and tagged "Voice Area Alumni - UTEP
    Department of Music" as Sports on a live carousel, alongside its correct
    Live Music tag (from the source's own "Music" category) -- UTEP is the
    whole university, not just its athletics program."""
    assert SPORTS not in pillars_for(
        "Voice Area Alumni- UTEP Department of Music", ["Music"], "Fox Fine Arts Recital Hall"
    )
    assert pillars_for("Rhinoceros- UTEP Theatre & Dance", [], "UTEP") == [ARTS]


@pytest.mark.parametrize(
    "title",
    [
        "UTEP FB vs Oregon State",
        "UTEP FB v Hawaii",              # the abbreviated spelling, no period
        "UTEP Volleyball v. Arizona",    # the abbreviated spelling, with period
        "El Paso Chihuahuas vs. Oklahoma City Comets",
    ],
)
def test_a_versus_marker_is_recognised_in_every_spelling(title):
    assert SPORTS in pillars_for(title)


def test_a_trailing_roman_numeral_is_not_a_versus_marker():
    """A bare "v" is also a Roman numeral, and a versus-marker always names an
    opponent AFTER it -- it is never the last word of a real title, while a
    numeral usually is. Without that distinction this tagged a TV rerelease as
    a sports event."""
    assert pillars_for("Devious Maids Season V") == []


@pytest.mark.parametrize("title", ["Run for the Roses 5K", "Free Yoga Thursdays", "Carrera 10K Juárez"])
def test_things_you_turn_up_and_do_are_fitness_not_sports(title):
    assert pillars_for(title) == [FITNESS]


def test_baby_yoga_is_family_not_fitness():
    """The case that made the rule explicit: a fitness activity aimed at small
    children is a family outing, not a workout."""
    assert pillars_for("Baby Yoga and Sound Bath", ["Early Childhood"], "La Nube") == [FAMILY]


def test_an_event_that_reads_as_both_spectator_and_participatory_is_deferred():
    """Opposites. Rather than pick, hand it upward."""
    assert pillars_for("5K at the Ballpark with the Chihuahuas") == []


def test_an_unplaceable_event_gets_no_pillar_rather_than_a_catch_all():
    assert pillars_for("Ristra de las Flores Workshop") == []
    assert pillars_for("") == []
    assert pillars_for(None) == []


def test_source_classifications_are_used_when_present():
    """Ticketmaster ships real classifications, which beat any title keyword."""
    assert pillars_for("A Beautiful Noise (Touring)", ["Theatre"]) == [ARTS]


def test_sports_and_fitness_as_a_source_category_resolves_neither_way():
    """It is the exact ambiguity the pillars exist to split, so a source using
    that label must not be allowed to settle it."""
    assert pillars_for("Some Ambiguous Program", ["Sports and Fitness"]) == []


def test_an_event_can_hold_more_than_one_pillar():
    tags = pillars_for("Live Music and Taco Festival", [], "A Bar")
    assert MUSIC in tags and FOOD in tags


def test_a_childrens_venue_makes_an_uninformative_title_placeable():
    """La Nube is El Paso's children's museum. "Fab Lab- Code Arcade" and
    "World Day of Play" say nothing about who they are for; the venue does."""
    assert pillars_for("Fab Lab- Code Arcade", [], "La Nube") == [FAMILY]
    assert pillars_for("Fab Lab- Code Arcade", [], "Some Bar") == []


def test_day_of_play_is_not_theatre():
    """"play" means "day of play" or "playdate" far more often than it means a
    stage production, and it tagged a Nickelodeon children's event as Arts &
    Culture before it was removed from the vocabulary."""
    assert ARTS not in pillars_for("Nickelodeon World Day of Play", [], "A Park")
    assert pillars_for("A Stage Play: Our Town", [], "Plaza Theatre") == [ARTS]


@pytest.mark.parametrize(
    "title, expected",
    [
        ("Concierto de Mariachi", MUSIC),
        ("Exposición de Arte Contemporáneo", ARTS),
        ("Cena y Degustación de Vinos", FOOD),
        ("Cuentos Infantiles", FAMILY),
    ],
)
def test_spanish_titles_are_recognised(title, expected):
    assert expected in pillars_for(title)


def test_every_returned_pillar_is_a_known_one():
    assert pillars_for("El Paso Symphony Orchestra", ["Music"]) == [MUSIC]
    for tag in pillars_for("Live Music and Taco Festival"):
        assert tag in PILLARS


def test_output_order_is_stable_regardless_of_match_order():
    """Rendering shows only the FIRST pillar, so the order must not depend on
    which regex happened to fire first."""
    assert pillars_for("Taco and Live Music Festival") == pillars_for("Live Music and Taco Festival")
