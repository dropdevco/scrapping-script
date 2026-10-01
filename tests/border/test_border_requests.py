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
SNAPSHOT = cbp.build(json.loads(FIXTURE.read_text(encoding="utf-8")), datetime(2026, 9, 22, 19, 40, tzinfo=UTC))


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


# ── every kind of message ────────────────────────────────────────────────────────
# (message, kind, lane, limit, bridges). Written the way people type them: accents or
# none, capitals, "!!", emoji, Spanish and English, the trigger anywhere in the line.
MESSAGES = [
    # alerts
    ("avísame cuando zaragoza baje de 20", "alert", None, 20, ("zaragoza",)),
    ("AVISAME cuando el LIBRE baje de media hora!!", "alert", None, 30, ("libre",)),
    ("me avisas si lerdo baja de veinte?", "alert", None, 20, ("lerdo",)),
    ("cuando zaragoza baje de 20", "alert", None, 20, ("zaragoza",)),
    ("avisame cuando zaragoza o lerdo baje de 15", "alert", None, 15, ("zaragoza", "lerdo")),
    ("porfa avisame cuando sentri en pdn este en menos de 10", "alert", "car_sentri", 10, ("pdn",)),
    ("avisame cuando baje de 1h30", "alert", None, 90, ()),
    ("alert me when santa teresa is under 10", "alert", None, 10, ("santa teresa",)),
    ("let me know when pdn drops below an hour", "alert", None, 60, ("pdn",)),
    ("notify me when walking at bota is under 15 min", "alert", "walk", 15, ("bota",)),
    # cancelling
    ("ya no me avises de zaragoza", "cancel", None, None, ("zaragoza",)),
    ("alto", "cancel", None, None, ()),
    ("Stop", "cancel", None, None, ()),
    ("cancelar alertas", "cancel", None, None, ()),
    ("deja de avisarme", "cancel", None, None, ()),
    # which bridge is fastest
    ("cual puente esta mas rapido?", "best", None, None, ()),
    ("¿Cuál es el puente más rápido a pie?", "best", "walk", None, ()),
    ("por donde cruzo con sentri", "best", "car_sentri", None, ()),
    ("which bridge is fastest right now", "best", None, None, ()),
    # one bridge, or several to compare
    ("zaragoza", "bridge", None, None, ("zaragoza",)),
    ("cuanto esta el libre a pie", "bridge", "walk", None, ("libre",)),
    ("hola, como esta pdn?", "bridge", None, None, ("pdn",)),
    ("zaragoza sentri", "bridge", "car_sentri", None, ("zaragoza",)),
    ("es mejor zaragoza o lerdo", "bridge", None, None, ("zaragoza", "lerdo")),
    ("pdn vs bota", "bridge", None, None, ("pdn", "bota")),
    ("how long is the wait at bota", "bridge", None, None, ("bota",)),
    ("el cruce de zaragoza como esta", "bridge", None, None, ("zaragoza",)),
    # saving
    ("guardar zaragoza sentri", "save", "car_sentri", None, ("zaragoza",)),
    ("guardar zaragoza y lerdo", "save", None, None, ("zaragoza", "lerdo")),
    ("mi puente es santa teresa", "save", None, None, ("santa teresa",)),
    # crossings
    ("voy a cruzar zaragoza", "crossing_start", None, None, ("zaragoza",)),
    ("estoy en la fila de pdn", "crossing_start", None, None, ("pdn",)),
    ("ya crucé!", "crossing_done", None, None, ()),
    ("crossed", "crossing_done", None, None, ()),
    # small talk
    ("hola", "menu", None, None, ()),
    ("Buenos días", "menu", None, None, ()),
    ("puentes", "menu", None, None, ()),
    ("ayuda", "help", None, None, ()),
    ("?", "help", None, None, ()),
    ("gracias!!", "thanks", None, None, ()),
    ("👍", "thanks", None, None, ()),
]


@pytest.mark.parametrize(("message", "kind", "lane", "limit", "bridges"), MESSAGES)
def test_every_kind_of_message_is_read(message, kind, lane, limit, bridges):
    intent = requests.classify(message)
    assert (intent.kind, intent.lane, intent.limit, intent.bridges) == (kind, lane, limit, bridges)


@pytest.mark.parametrize(("message", "kind", "lane", "limit", "bridges"),
                         [m for m in MESSAGES if m[4]])
def test_every_bridge_named_resolves(message, kind, lane, limit, bridges):
    """Each name classify hands back must be one Snapshot.port can actually find."""
    assert all(SNAPSHOT.port(name) is not None for name in bridges)


@pytest.mark.parametrize(("message", "lang"), [
    ("cual puente esta mas rapido", "es"),
    ("which bridge is fastest", "en"),
    ("hola", "es"),
    ("hello", "en"),
    ("gracias", "es"),
    ("thanks", "en"),
    ("zaragoza", "es"),                       # names alone keep the account's language
])
def test_the_language_is_read_from_any_message(message, lang):
    assert requests.classify(message, default_lang="es").lang == lang


@pytest.mark.parametrize(("message", "limit"), [
    ("avisame cuando baje de media hora", 30),
    ("avisame cuando baje de un cuarto de hora", 15),
    ("avisame cuando baje de hora y media", 90),
    ("avisame cuando baje de 2 horas", 120),
    ("avisame cuando baje de <20", 20),
    ("avisame cuando baje de 20min", 20),
    ("alert me when it is under half an hour", 30),
    ("alert me when it is under twenty", 20),
])
def test_limits_are_read_the_way_people_write_them(message, limit):
    assert requests.limit_in(message) == limit


def test_gibberish_is_offered_to_the_bridge_lookup_which_finds_nothing():
    """Answered with "no encuentro ese puente" and the list, never a guessed bridge."""
    intent = requests.classify("asdfgh")
    assert intent.kind == "bridge"
    assert SNAPSHOT.port(intent.bridges[0]) is None
