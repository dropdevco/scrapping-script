"""Getting JSON back out of a model reply.

Asking for JSON does not reliably get you only JSON. Every shape here was an
actual reply observed on 2026-09-20 while wiring the council up through
OpenRouter, and the third one silently disabled the whole curator: the model
emitted a fenced object and then several sentences explaining its reasoning,
which a fence-stripper anchored to the end of the string cannot handle. Every
event came back unplaced and nothing looked broken.
"""

from __future__ import annotations

import json

import pytest

from scraper.core.llm import _json_candidates


def _first_object(text: str):
    for candidate in _json_candidates(text):
        try:
            parsed = json.loads(candidate)
        except ValueError:
            continue
        if isinstance(parsed, dict):
            return parsed
    return None


@pytest.mark.parametrize(
    "reply, expected",
    [
        ('{"pillars": ["Sports"]}', {"pillars": ["Sports"]}),
        ('```json\n{"pillars": ["Sports"]}\n```', {"pillars": ["Sports"]}),
        ('```\n{"pillars": []}\n```', {"pillars": []}),
        ("Here you go:\n{\"pillars\": []}", {"pillars": []}),
        ("  \n {\"blurb\": \"a thing\"}  \n ", {"blurb": "a thing"}),
    ],
)
def test_json_is_recovered_from_every_reply_shape(reply, expected):
    assert _first_object(reply) == expected


def test_a_fenced_object_followed_by_prose_is_recovered():
    """The exact reply that broke the curator. Note the prose even contains a
    brace, so a naive first-{ to last-} span does not save you here -- the
    fenced block has to be preferred."""
    reply = (
        '```json\n{"pillars": ["Fitness & Activities"]}\n```\n\n'
        "This is a hands-on workshop where {attendees} turn up and actively "
        "create their own ristra."
    )
    assert _first_object(reply) == {"pillars": ["Fitness & Activities"]}


def test_a_reply_with_no_object_yields_nothing():
    assert _first_object("I cannot answer that.") is None
    assert _first_object("") is None


def test_a_json_array_is_not_mistaken_for_an_object():
    """Callers index by key; a bare list would blow up at the call site."""
    assert _first_object('["Sports", "Family"]') is None
