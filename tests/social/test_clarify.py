"""Which titles get a generated one-liner, and which need nothing.

Both examples pinned here are the ones named in the 2026-09-17 feedback on the
published posts: "Yoga Parks" reads as though Yoga were the name of a park, and
a Rhinos matchup needs no help at all. Getting those two right is the whole
brief -- the point is not to describe every event, it is to notice the opaque
ones and fill only that gap.
"""

from __future__ import annotations

from unittest import mock

import pytest

from scraper.core.llm import LLMUnavailable
from scraper.social import clarify
from scraper.social.clarify import (
    has_mangled_phrasing,
    is_obviously_clear,
    is_self_explanatory,
    needs_clarifier,
    title_repeats_venue,
)


# Long enough to clear the "is there anything to summarise from?" gate. Real
# descriptions are far longer; this is just past the floor.
_DESC = "A recurring community program held at the venue, open to all ages, with activities throughout."


def _row(title, venue="A Venue", description=_DESC):
    return {"title": title, "venue": venue, "description": description}


@pytest.mark.parametrize(
    "title",
    [
        "El Paso Rhinos vs. Odessa Jackalopes",
        "El Paso Chihuahuas vs. Oklahoma City Comets",
        "El Paso Rhinos",   # a named local team, no descriptive noun needed
    ],
)
def test_a_matchup_or_a_named_team_never_costs_a_call(title):
    """The only free skips. Everything else is the model's judgement."""
    assert is_obviously_clear(_row(title))
    assert not needs_clarifier(_row(title))


@pytest.mark.parametrize(
    "title",
    [
        "El Paso Greek Festival",
        "Ristra de las Flores Workshop",
        "Wraiths of the West Texas Wind : El Paso Ghost Tour",
        "Disney On Ice presents Jump In!",
    ],
)
def test_a_format_word_alone_does_not_count_as_self_explanatory(title):
    """"festival", "workshop" and "tour" name a FORMAT, not a subject -- a
    workshop making what? Treating them as clear suppressed a third of the
    blurbs we could honestly write, so they go to the model, which answers
    self_explanatory when it genuinely is."""
    assert not is_obviously_clear(_row(title))
    assert needs_clarifier(_row(title))


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


def test_an_opaque_title_with_no_description_is_left_alone():
    """The anti-invention guard, and the sharpest lesson from choosing a model.

    Asked to explain an event with an opaque title and no description, a
    candidate model produced "low-cost vaccinations and basic health checks for
    dogs and cats" -- none of which appeared anywhere in the input. With no
    source text there is nothing to summarise, so we say nothing.
    """
    assert needs_clarifier(_row("Canvas & Cantaritos"))          # has a description
    assert not needs_clarifier(_row("Canvas & Cantaritos", description=""))
    assert not needs_clarifier(_row("Canvas & Cantaritos", description=None))
    assert not needs_clarifier(_row("Canvas & Cantaritos", description="Join us!"))


def test_bilingual_descriptive_nouns_are_recognised():
    assert is_self_explanatory(_row("Concierto de Mariachi"))
    assert is_self_explanatory(_row("Taller de Comunicación No Violenta"))
    assert is_self_explanatory(_row("Carrera 5K Juárez"))


def test_an_empty_title_is_not_called_self_explanatory():
    assert not is_self_explanatory(_row(""))
    assert not is_self_explanatory({"title": None, "venue": None})


# ── what we accept back from the model ────────────────────────────────────────


def test_an_overlong_blurb_is_truncated_at_a_word_boundary_not_discarded():
    """Models overrun a stated limit as a matter of course -- measured at 104,
    114, 117, 128, 139 characters against a stated 90. The copy is otherwise
    good, so trim it rather than fall back to the bare title."""
    long = (
        "exhibition exploring the histories of Chinese, Korean, Filipino, Japanese and "
        "Vietnamese communities across the borderland from the 1880s to the 1980s"
    )
    assert len(long) > clarify.MAX_BLURB
    out = clarify._clean_blurb(long, _row("Mountain of Gold"))
    assert out is not None
    assert len(out) <= clarify.MAX_BLURB
    assert out.endswith("…")
    assert not out[:-1].endswith(" ")  # trimmed at a boundary, not mid-word


def test_a_blurb_that_only_echoes_the_title_is_rejected():
    assert clarify._clean_blurb("Desert Bloomers", _row("Desert Bloomers")) is None


def test_a_too_short_blurb_is_rejected():
    assert clarify._clean_blurb("a program", _row("Desert Bloomers")) is None


def test_a_non_string_blurb_is_rejected():
    assert clarify._clean_blurb(None, _row("X")) is None
    assert clarify._clean_blurb({"text": "hi"}, _row("X")) is None


async def test_a_model_outage_yields_no_blurb_rather_than_an_error():
    """The whole degradation contract in one test: if the model is unreachable
    the caption renders exactly what it rendered before blurbs existed."""

    async def boom(*_a, **_kw):
        raise LLMUnavailable("network is down")

    with mock.patch.object(clarify, "complete_json", boom):
        assert await clarify.generate_blurb(None, _row("Canvas & Cantaritos")) is None


async def test_the_model_judging_a_title_clear_yields_no_blurb():
    async def clear(*_a, **_kw):
        return {"self_explanatory": True, "blurb": ""}

    with mock.patch.object(clarify, "complete_json", clear):
        assert await clarify.generate_blurb(None, _row("Canvas & Cantaritos")) is None


async def test_a_dry_run_calls_the_model_but_writes_nothing():
    """`build --dry-run` promises to touch nothing. A preview that quietly
    populated the cache would make the NEXT real build behave differently for
    having been previewed."""

    class _Storage:
        def __init__(self):
            self.writes = []

        async def cache_event_editorial(self, event_id, patch):
            self.writes.append((event_id, patch))
            return True

    async def good(*_a, **_kw):
        return {"self_explanatory": False, "blurb": "paint-and-sip class with cocktails in a studio"}

    row = _row("Canvas & Cantaritos")
    row["id"] = "abc"
    storage = _Storage()

    with mock.patch.object(clarify, "complete_json", good), \
         mock.patch.object(clarify.settings, "council_available", True):
        await clarify.fill_blurbs(storage, None, [row], dry_run=True)

    assert storage.writes == []
    assert row["blurb"] == "paint-and-sip class with cocktails in a studio"  # preview still shows it


async def test_a_real_run_caches_the_result():
    class _Storage:
        def __init__(self):
            self.writes = []

        async def cache_event_editorial(self, event_id, patch):
            self.writes.append((event_id, patch))
            return True

    async def good(*_a, **_kw):
        return {"self_explanatory": False, "blurb": "paint-and-sip class with cocktails in a studio"}

    row = _row("Canvas & Cantaritos")
    row["id"] = "abc"
    storage = _Storage()

    with mock.patch.object(clarify, "complete_json", good), \
         mock.patch.object(clarify.settings, "council_available", True):
        await clarify.fill_blurbs(storage, None, [row], dry_run=False)

    assert len(storage.writes) == 1
    event_id, patch = storage.writes[0]
    assert event_id == "abc"
    assert patch["blurb_source"] == "council"
    assert patch["blurb_checked_at"]


async def test_an_already_judged_event_is_never_re_asked():
    """Including one judged to need no blurb -- blurb_checked_at is the record
    that we looked, so a self-explanatory title costs one call ever, not one
    per build."""
    calls = []

    async def counting(*_a, **_kw):
        calls.append(1)
        return {"self_explanatory": False, "blurb": "some perfectly fine explanation here"}

    cached = _row("Canvas & Cantaritos")
    cached.update(id="a", blurb="already have one")
    checked_blank = _row("Museums on Us")
    checked_blank.update(id="b", blurb=None, blurb_checked_at="2026-09-20T00:00:00Z")

    class _Storage:
        async def cache_event_editorial(self, *_a):
            return True

    with mock.patch.object(clarify, "complete_json", counting), \
         mock.patch.object(clarify.settings, "council_available", True):
        await clarify.fill_blurbs(_Storage(), None, [cached, checked_blank])

    assert calls == []


# ── the curator (pillar placement for what keywords could not place) ──────────


def test_only_the_six_pillars_survive():
    """A model inventing a seventh bucket must not be able to create one
    downstream, where selection weights and slide chips key off the name."""
    assert clarify._clean_pillars(["Sports", "Nightlife", "Family"]) == ["Sports", "Family"]
    assert clarify._clean_pillars(["sports"]) == []          # exact match only
    assert clarify._clean_pillars("Sports") == []            # not a list
    assert clarify._clean_pillars(None) == []


def test_pillar_order_does_not_depend_on_what_the_model_returned_first():
    assert clarify._clean_pillars(["Family", "Live Music"]) == clarify._clean_pillars(
        ["Live Music", "Family"]
    )


async def test_a_curator_outage_leaves_an_event_unplaced_rather_than_misplaced():
    async def boom(*_a, **_kw):
        raise LLMUnavailable("network is down")

    with mock.patch.object(clarify, "complete_json", boom):
        assert await clarify.assign_pillars(None, _row("Ristra de las Flores Workshop")) == []


async def test_the_curator_only_considers_events_the_keywords_left_empty():
    """The keyword pass is confident by construction, so there is nothing for
    the model to second-guess -- and re-asking would cost a call per build."""
    calls = []

    async def counting(*_a, **_kw):
        calls.append(1)
        return {"pillars": ["Family"]}

    already_placed = _row("El Paso Chihuahuas vs. Comets")
    already_placed.update(id="a", content_tags=["Sports"])
    council_said_none = _row("Other Sunday Social on Zoom")
    council_said_none.update(id="b", content_tags=[], content_tags_source="council")

    class _Storage:
        async def cache_event_editorial(self, *_a):
            return True

    with mock.patch.object(clarify, "complete_json", counting), \
         mock.patch.object(clarify.settings, "council_available", True):
        await clarify.fill_pillars(_Storage(), None, [already_placed, council_said_none])

    assert calls == []


async def test_a_usable_blurb_comes_back():
    async def good(*_a, **_kw):
        return {"self_explanatory": False, "blurb": "paint-and-sip class with cocktails in a studio"}

    with mock.patch.object(clarify, "complete_json", good):
        out = await clarify.generate_blurb(None, _row("Canvas & Cantaritos"))
    assert out == "paint-and-sip class with cocktails in a studio"
