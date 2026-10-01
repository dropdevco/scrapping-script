"""The rehearsal tools: the fake Instagram and the HTTP client it pulls through.
"""

from __future__ import annotations

import contextlib
import io
import json
import unittest
import urllib.error
from unittest import mock

from border_support import (  # noqa: F401 - setUpModule/tearDownModule are hooks
    MIDDAY,
    feed_for,
    setUpModule,
    tearDownModule,
)

from scraper.border.devtools import fake_instagram


class HttpPull(unittest.IsolatedAsyncioTestCase):
    """The pipeline pulls over HTTP, where a 404 arrives as an exception."""

    async def test_a_404_body_is_returned_not_raised(self):
        payload = json.dumps({"found": False, "reply": "No encuentro ese puente.",
                              "options": ["Zaragoza"]}).encode()
        error = urllib.error.HTTPError("u", 404, "Not Found", {}, io.BytesIO(payload))
        with mock.patch("urllib.request.urlopen", side_effect=error):
            body = await fake_instagram.HttpFeed("http://x").bridge("narnia")
        self.assertFalse(body["found"])
        self.assertIn("options", body)

    async def test_a_503_body_is_returned_not_raised(self):
        payload = json.dumps({"error": "CBP feed unavailable"}).encode()
        error = urllib.error.HTTPError("u", 503, "Unavailable", {}, io.BytesIO(payload))
        with mock.patch("urllib.request.urlopen", side_effect=error):
            body = await fake_instagram.HttpFeed("http://x").waits()
        self.assertIn("CBP feed unavailable", body["error"])

    async def test_spaces_in_a_bridge_name_are_encoded(self):
        seen = {}

        class Fake(io.BytesIO):
            def __enter__(self): return self
            def __exit__(self, *a): return False

        def opener(url, timeout=None):
            seen["url"] = url
            return Fake(b"{}")

        with mock.patch("urllib.request.urlopen", opener):
            await fake_instagram.HttpFeed("http://x").bridge("el puente libre")
        self.assertIn("el%20puente%20libre", seen["url"])
        self.assertNotIn(" ", seen["url"])

class InstagramSimulation(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.graph = fake_instagram.FakeGraph()
        self.feed = feed_for("cbp_feed.json", "cbp_feed_after_drop.json")
        self.pipeline = fake_instagram.FakePipeline(self.feed, self.graph)

    async def test_daily_post_publishes_a_caption_and_sends_nothing(self):
        caption = await self.pipeline.daily_post()
        self.assertIn("Puentes Juárez–El Paso", caption)
        self.assertEqual([c["endpoint"].rsplit("/", 1)[-1] for c in self.graph.calls],
                         ["media", "media_publish"])

    async def test_bridge_question_is_answered_with_live_minutes(self):
        await self.pipeline.on_message("user-1", "paso del norte")
        self.assertIn("48 min", self.graph.calls[-1]["params"]["message"]["text"])

    async def test_menu_offers_quick_replies(self):
        await self.pipeline.on_message("user-1", "puentes")
        self.assertTrue(self.graph.calls[-1]["params"]["message"]["quick_replies"])

    async def test_alert_subscription_then_drop_sends_one_message(self):
        await self.pipeline.on_message("user-2", "avísame cuando paso del norte baje de 30 min")
        (await self.feed.snapshot())                       # first reading: 48 min
        self.assertEqual((await self.pipeline.alert_sweep()), 1)
        self.assertIn("20 min", self.graph.calls[-1]["params"]["message"]["text"])

    async def test_saving_a_bridge_then_asking_returns_only_that_bridge(self):
        await self.pipeline.on_message("user-1", "guardar paso del norte sentri")
        await self.pipeline.on_message("user-1", "puentes")
        sent = self.graph.calls[-1]["params"]["message"]["text"]
        self.assertIn("Tus puentes", sent)
        self.assertIn("SENTRI", sent)
        self.assertNotIn("Santa Teresa", sent)

    async def test_scheduled_message_uses_their_saved_bridges(self):
        await self.pipeline.on_message("user-1", "guardar zaragoza")
        await self.pipeline.scheduled_message("user-1")
        self.assertIn("Tus puentes", self.graph.calls[-1]["params"]["message"]["text"])

    async def test_stop_cancels_subscriptions(self):
        await self.pipeline.on_message("user-2", "avísame cuando zaragoza baje de 30")
        await self.pipeline.on_message("user-2", "alto")
        self.assertEqual(self.pipeline.subscriptions, [])

    async def test_simulation_run_records_calls_without_network(self):
        with contextlib.redirect_stdout(io.StringIO()):
            graph = await fake_instagram.run(feed_for("cbp_feed.json", "cbp_feed_after_drop.json"))
        self.assertTrue(graph.calls)
        self.assertTrue(all(c["endpoint"].startswith("https://graph.facebook.com") for c in graph.calls))


class EveryKindOfMessage(unittest.IsolatedAsyncioTestCase):
    """The whole conversation, reply by reply. Midday feed: Santa Teresa is the fastest
    car lane at 40, Paso del Norte 48, Zaragoza's car lane is closed, walking is quick."""

    async def asyncSetUp(self):
        self.feed = feed_for("cbp_feed_midday.json", now=MIDDAY)
        self.graph = fake_instagram.FakeGraph()
        self.pipeline = fake_instagram.FakePipeline(self.feed, self.graph)

    async def say(self, message: str, user: str = "u") -> str:
        await self.pipeline.on_message(user, message)
        return self.graph.calls[-1]["params"]["message"]["text"]

    async def test_a_greeting_with_a_question_answers_the_question(self):
        reply = await self.say("hola, como esta pdn?")
        self.assertTrue(reply.startswith("Paso del Norte (Santa Fe) · Autos: 48 min"))

    async def test_the_lane_in_a_question_is_the_lane_answered(self):
        self.assertIn("· Peatones:", await self.say("cuanto esta el libre a pie"))
        self.assertIn("· SENTRI:", await self.say("zaragoza sentri"))

    async def test_an_english_question_gets_an_english_answer(self):
        reply = await self.say("how long is the wait at bota")
        self.assertTrue(reply.startswith("Bridge of the Americas · Cars:"))

    async def test_which_bridge_is_fastest(self):
        reply = await self.say("cual puente esta mas rapido?")
        self.assertTrue(reply.startswith("Más rápido ahora (Autos):\nJerónimo–Santa Teresa"))
        walking = await self.say("which bridge is fastest walking")
        self.assertTrue(walking.startswith("Fastest right now (Walking):"))

    async def test_two_bridges_are_compared_with_a_verdict(self):
        reply = await self.say("es mejor santa teresa o pdn")
        self.assertEqual(reply.splitlines()[0], "Autos:")
        self.assertIn("Mejor ahora: Santa Teresa.", reply)

    async def test_a_multi_bridge_alert_in_half_an_hour(self):
        reply = await self.say("avisame cuando pdn o santa teresa baje de media hora")
        self.assertIn("Paso del Norte (Santa Fe) y Jerónimo–Santa Teresa (Autos) bajen de 30 min", reply)
        self.assertEqual({s["port"] for s in self.pipeline.subscriptions}, {"240202", "240801"})

    async def test_asking_twice_does_not_file_twice(self):
        await self.say("avisame cuando pdn baje de 30")
        await self.say("avísame cuando PDN baje de 30!!")
        self.assertEqual(len(self.pipeline.subscriptions), 1)

    async def test_cancelling_one_bridge_keeps_the_others(self):
        await self.say("avisame cuando pdn o santa teresa baje de 30")
        reply = await self.say("ya no me avises de pdn")
        self.assertEqual(reply, "Listo, ya no te aviso de Paso del Norte (Santa Fe).")
        self.assertEqual([s["port"] for s in self.pipeline.subscriptions], ["240801"])

    async def test_cancelling_everything_and_cancelling_nothing(self):
        await self.say("avisame cuando pdn baje de 30")
        self.assertEqual(await self.say("alto"), "Listo, cancelé tus avisos.")
        self.assertEqual(await self.say("stop"), "You had no alerts set.")

    async def test_saving_two_bridges_then_the_menu_shows_just_those(self):
        self.assertIn("Guardado: Paso del Norte (Santa Fe) y Jerónimo–Santa Teresa",
                      await self.say("guardar pdn y santa teresa"))
        menu = await self.say("puentes")
        self.assertIn("Tus puentes", menu)
        self.assertNotIn("Zaragoza", menu)

    async def test_saving_or_crossing_with_no_bridge_asks_which(self):
        self.assertIn("¿Qué puente?", await self.say("guardar"))
        self.assertIn("¿Qué puente?", await self.say("voy a cruzar"))

    async def test_crossed_before_starting_explains(self):
        self.assertIn('Primero escribe "voy a cruzar"', await self.say("ya crucé"))

    async def test_thanks_help_and_emoji(self):
        self.assertTrue((await self.say("gracias!!")).startswith("¡De nada!"))
        self.assertTrue((await self.say("thanks")).startswith("You're welcome!"))
        self.assertTrue((await self.say("👍")).startswith("¡De nada!"))
        self.assertIn("avísame cuando zaragoza baje de 20", await self.say("ayuda"))
        self.assertIn("alert me when zaragoza is under 20", await self.say("help"))

    async def test_gibberish_gets_the_bridge_list_not_a_guess(self):
        reply = await self.say("asdfgh")
        self.assertIn("No encuentro ese puente", reply)
        self.assertTrue(self.graph.calls[-1]["params"]["message"]["quick_replies"])
