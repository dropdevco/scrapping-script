"""Reading an alert request: which bridge, which lane, what limit, which language.

The alert handler used to keep only the number. "avísame cuando zaragoza baje de 20"
was filed as Paso del Norte, car lane, answered with "Te aviso cuando Paso del Norte
baje de 20 min", and — because the sweep ignored the stored bridge anyway — the
subscriber would then have been alerted about every bridge that dropped under 20.
"alert me when santa teresa is under 10" got the same, in Spanish.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from scraper.border import cbp, requests

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "cbp_feed.json"
SNAPSHOT = cbp.build(json.loads(FIXTURE.read_text()), datetime(2026, 9, 22, 19, 40, tzinfo=UTC))


@pytest.mark.parametrize(("message", "port"), [
    ("avísame cuando zaragoza baje de 20", "240203"),
    ("avisame cuando el puente libre este en menos de 15 min", "240201"),
    ("alert me when santa teresa is under 10", "240801"),
    ("alerta cuando lerdo baje de 25", "240204"),
    ("avísame cuando sentri en pdn baje de 10", "240202"),
    ("let me know when walking at bota is under 20 min", "240201"),
    ("avisame cuando ready a pie en paso del norte este en 15", "240202"),
    ("alert me when the free bridge drops below 30", "240201"),
    ("avísame cuando sarragoza baje de 20", "240203"),          # typed on a phone
    ("avísame cuando tornilo baje de 20", "240221"),
])
def test_the_bridge_named_is_the_bridge_found(message, port):
    found = SNAPSHOT.port(requests.bridge_text(message))
    assert found is not None and found.port_number == port


@pytest.mark.parametrize("message", ["avísame cuando baje de 30", "alert me when it is under 20 min"])
def test_no_bridge_named_is_empty_not_a_guess(message):
    assert requests.bridge_text(message) == ""


def test_a_word_that_is_no_bridge_finds_none():
    assert SNAPSHOT.port(requests.bridge_text("avísame cuando pizza baje de 20")) is None


@pytest.mark.parametrize(("message", "lane"), [
    ("avísame cuando zaragoza baje de 20", None),
    ("avísame cuando sentri en pdn baje de 10", "car_sentri"),
    ("avisame cuando ready en bota baje de 15", "car_ready"),
    ("let me know when walking at bota is under 20", "walk"),
    ("avisame cuando a pie en pdn baje de 10", "walk"),
    ("avisame cuando ready a pie en paso del norte este en 15", "walk_ready"),
    ("avisame cuando carga en zaragoza baje de 60", "truck"),
])
def test_the_lane_is_read_from_the_message(message, lane):
    assert requests.lane_in(message) == lane


@pytest.mark.parametrize(("message", "limit"), [
    ("avísame cuando zaragoza baje de 20", 20),
    ("avisame cuando el puente libre este en menos de 15 min", 15),
    ("avísame cuando zaragoza baje", requests.DEFAULT_LIMIT),
    ("avísame cuando 240203 baje de 25", 25),                     # a port number is not a limit
])
def test_the_limit_is_the_first_believable_number(message, limit):
    assert requests.limit_in(message) == limit


@pytest.mark.parametrize(("message", "lang"), [
    ("avísame cuando zaragoza baje de 20", "es"),
    ("alert me when santa teresa is under 10", "en"),
    ("alerta cuando lerdo baje de 25", "es"),                     # starts with "alert", is Spanish
    ("let me know when bota is under 20", "en"),
])
def test_the_reply_language_follows_the_request(message, lang):
    assert requests.language_of(message, default="es") == lang


def test_only_alert_requests_are_taken_for_alerts():
    assert requests.is_alert_request("Avísame cuando zaragoza baje de 20")
    assert not requests.is_alert_request("zaragoza")
    assert not requests.is_alert_request("guardar zaragoza sentri")
