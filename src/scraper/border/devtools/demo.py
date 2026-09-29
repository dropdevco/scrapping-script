"""A five-scene demo of the border feed. Safe to run in front of people.

Uses saved CBP responses and a fixed clock by default, so every run is identical and
nothing depends on what the border happens to be doing. --live reads real CBP instead.

    python -m scraper.border.devtools.demo              # scripted, reproducible
    python -m scraper.border.devtools.demo --live       # scenes 1-2 against the real feed
    python -m scraper.border.devtools.demo --no-color   # for slides and screenshots

Nothing is ever sent to Meta: the Graph client records calls and drops them.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path

from scraper.border.devtools.fake_instagram import FakeGraph, FakePipeline
from scraper.border.service import BorderFeed, FeedError
from scraper.border.storage import BorderStore

# The saved CBP responses the tests use: identical every run, whatever the border is doing.
FIXTURES = Path(__file__).resolve().parents[4] / "tests" / "border" / "fixtures"
MIDDAY = datetime(2026, 9, 22, 19, 40, tzinfo=UTC)   # 1:40 pm MDT
NIGHT = datetime(2026, 9, 22, 3, 40, tzinfo=UTC)     # 9:40 pm MDT

COLOR = {"head": "\033[1;36m", "note": "\033[2m", "msg": "\033[0;33m",
         "good": "\033[0;32m", "bad": "\033[0;31m", "off": "\033[0m"}


def paint(key: str, text: str, enabled: bool) -> str:
    return f"{COLOR[key]}{text}{COLOR['off']}" if enabled else text


class Demo:
    def __init__(self, color: bool = True, live: bool = False):
        self.color, self.live = color, live
        self.graph = FakeGraph()
        # Scene 5 provokes a fetch failure on purpose; its warning would otherwise
        # land at the top of the demo, since stderr is not buffered with stdout.
        logging.disable(logging.CRITICAL)

    # -- presentation ------------------------------------------------------
    def scene(self, number: int, title: str, why: str) -> None:
        print("\n" + paint("head", f"── {number}. {title} ", self.color) + paint("head", "─" * max(0, 58 - len(title)), self.color))
        print(paint("note", f"   {why}\n", self.color))

    def post(self, caption: str) -> None:
        for line in caption.splitlines():
            print(f"   {line}")

    def dm(self, who: str, text: str) -> None:
        print(f"   {paint('note', who + ' →', self.color)} {paint('msg', text, self.color)}")

    def bot(self, text: str) -> None:
        for i, line in enumerate(text.splitlines()):
            print(f"   {'Chisme →' if i == 0 else '        '} {line}")

    # -- feeds -------------------------------------------------------------
    def feed(self, *fixtures: str, now: datetime = MIDDAY) -> BorderFeed:
        if self.live:
            return BorderFeed(storage=BorderStore(client=None))
        payloads = [json.loads((FIXTURES / f).read_text()) for f in fixtures]
        state = {"n": 0}

        async def fetcher(_http):
            payload = payloads[min(state["n"], len(payloads) - 1)]
            state["n"] += 1
            return payload

        return BorderFeed(storage=BorderStore(client=None), fetcher=fetcher,
                          clock=lambda: now, cache_seconds=0)

    # -- scenes ------------------------------------------------------------
    async def scene_post(self) -> None:
        self.scene(1, "The daily post", "Leads with an answer, not a table of thirty numbers.")
        feed = self.feed("cbp_feed_midday.json")
        pipeline = FakePipeline(feed, self.graph)
        self.post(await pipeline.daily_post())

    async def scene_conversation(self) -> None:
        self.scene(2, "Someone asks about their bridge",
                   "Typed on a phone, in Spanish, misspelled — and still answered.")
        feed = self.feed("cbp_feed_midday.json")
        pipeline = FakePipeline(feed, self.graph)

        for message in ["el puente libre", "sarragoza"]:
            self.dm("Ana", message)
            await pipeline.on_message("ana", message)
            self.bot(self.graph.calls[-1]["params"]["message"]["text"])
            if message == "sarragoza":
                print(paint("note", "   Misspelled, and its car lanes are shut — said plainly, not as 0 min.",
                            self.color))
            print()

        self.dm("Ana", "guardar zaragoza sentri")
        await pipeline.on_message("ana", "guardar zaragoza sentri")
        self.bot(self.graph.calls[-1]["params"]["message"]["text"])
        print()
        self.dm("Ana", "puentes")
        await pipeline.on_message("ana", "puentes")
        self.bot(self.graph.calls[-1]["params"]["message"]["text"])
        print(paint("note", "\n   Her lane, her bridge. Two lines instead of thirty.", self.color))

    async def scene_closing(self) -> None:
        self.scene(3, "9:40 PM — the fastest bridge is about to shut",
                   "Santa Teresa closes at 10. A 40-minute line means arriving too late.")
        feed = self.feed("cbp_feed.json", now=NIGHT)
        pipeline = FakePipeline(feed, self.graph)
        self.dm("Beto", "santa teresa")
        await pipeline.on_message("beto", "santa teresa")
        self.bot(self.graph.calls[-1]["params"]["message"]["text"])
        best = await feed.best("car")
        print(paint("note",
                    f"\n   Santa Teresa is faster (40 min) but drops out of the recommendation."
                    f"\n   Fastest they can actually use: {best['name']} at {best['minutes']} min.",
                    self.color))

    async def scene_alert(self) -> None:
        self.scene(4, "The line drops", "One alert when it crosses their limit — not every poll.")
        feed = self.feed("cbp_feed_midday.json", "cbp_feed_midday_after_drop.json")
        pipeline = FakePipeline(feed, self.graph)

        self.dm("Ana", "avísame cuando baje de 30 min")
        await pipeline.on_message("ana", "avísame cuando baje de 30 min")
        self.bot(self.graph.calls[-1]["params"]["message"]["text"])

        await feed.snapshot()                      # first reading: 48 min
        print(paint("note", "\n   [CBP updates: Paso del Norte 48 → 20 min]\n", self.color))
        sent = await pipeline.alert_sweep()
        if sent:
            self.bot(self.graph.calls[-1]["params"]["message"]["text"])
        again = await pipeline.alert_sweep()
        print(paint("note", f"\n   Next sweep sends {again} — already below the limit is not new news.",
                    self.color))

    async def scene_failures(self) -> None:
        self.scene(5, "When CBP misbehaves", "The two ways this breaks, and what we do about them.")

        async def boom(_http):
            raise OSError("connection reset by peer")

        broken = BorderFeed(storage=BorderStore(client=None), fetcher=boom, clock=lambda: MIDDAY)
        try:
            await broken.waits()
        except FeedError as exc:
            print(f"   CBP unreachable → {paint('bad', type(exc).__name__, self.color)}: {exc}")
        print(paint("note", "   The pipeline posts nothing and keeps its last good post.\n", self.color))

        payload = json.loads((FIXTURES / "cbp_feed_midday.json").read_text())
        clock = {"n": 0}
        times = [MIDDAY, MIDDAY + timedelta(hours=7)]

        def tick():
            now = times[min(clock["n"], len(times) - 1)]
            clock["n"] += 1
            return now

        async def same(_http):
            return payload

        stuck = BorderFeed(storage=BorderStore(client=None), fetcher=same,
                           clock=tick, cache_seconds=0)
        await stuck.snapshot()
        await stuck.snapshot()
        health = await stuck.health()
        print(f"   CBP stuck on the same numbers for {health['frozen_hours']:.0f} h → "
              f"{paint('bad', 'degraded', self.color)}: frozen={health['frozen']}, ok={health['ok']}")
        print(paint("note", "   Stale numbers posted as fresh is the failure people would notice.",
                    self.color))

    async def run(self) -> None:
        title = "Chisme border feed — live CBP wait times" + (" (LIVE)" if self.live else "")
        print("\n" + paint("head", title, self.color))
        print(paint("note", "El Paso–Juárez · no app to download · nothing sent to Meta in this demo",
                    self.color))

        await self.scene_post()
        await self.scene_conversation()
        if not self.live:
            await self.scene_closing()
            await self.scene_alert()
            await self.scene_failures()

        print("\n" + paint("head", "── Summary ", self.color) + paint("head", "─" * 48, self.color))
        print(f"   {paint('good', str(len(self.graph.calls)), self.color)} Instagram calls recorded, "
              f"{paint('good', '0', self.color)} sent")
        print("   Data: CBP public feed · 6 bridges · every lane type")
        print("   Part of the scraper package: python -m scraper.border, Supabase, GitHub Actions\n")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--live", action="store_true", help="use the real CBP feed for scenes 1-2")
    ap.add_argument("--no-color", action="store_true")
    args = ap.parse_args(argv)
    asyncio.run(Demo(color=not args.no_color, live=args.live).run())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
