"""Type messages, get replies — the Instagram DM experience in a terminal.

Everything a follower could send goes through the same code the pipeline calls, so
what you see here is what they would receive. Nothing is sent to Meta.

    python3 tools/chat.py                                  # live CBP
    python3 tools/chat.py --from-file tests/fixtures/cbp_feed.json   # offline, fixed data
    python3 tools/chat.py --lang en

Commands start with a slash; anything else is treated as a message from a follower.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tools"))

from border.core import cbp, text  # noqa: E402
from border.core.storage import Storage  # noqa: E402
from border.service import BorderFeed, FeedError  # noqa: E402
from fake_instagram import FakeGraph, FakePipeline  # noqa: E402

COLOR = {"you": "\033[0;36m", "bot": "\033[0;33m", "note": "\033[2m",
         "warn": "\033[0;31m", "off": "\033[0m"}

SUGGESTIONS = [
    ("puentes", "every bridge right now"),
    ("zaragoza", "one bridge — try a typo, it still works"),
    ("guardar zaragoza sentri", "save it, then send \"puentes\" again"),
    ("avísame cuando baje de 30", "set an alert"),
    ("alto", "cancel alerts"),
    ("voy a cruzar paso del norte", "log a real crossing, then \"ya crucé\""),
]
COMMANDS = """  /post      the daily post caption
  /salir     quit (or Ctrl-D)
  /idioma    switch between es and en
  /estado    feed health
  /fuentes   which sites were read, and what they disagree about
  /ayuda     this list"""


def paint(key: str, text: str, on: bool) -> str:
    return f"{COLOR[key]}{text}{COLOR['off']}" if on else text


class Chat:
    def __init__(self, feed: BorderFeed, lang: str = "es", color: bool = True, live: bool = True):
        self.feed, self.lang, self.color, self.live = feed, lang, color, live
        self.graph = FakeGraph()
        self.pipeline = FakePipeline(feed, self.graph, lang)
        self.user = "tu"
        logging.disable(logging.CRITICAL)

    def say(self, text: str) -> None:
        for i, line in enumerate(text.splitlines()):
            prefix = "Chisme →" if i == 0 else "        "
            print(f"  {paint('bot', prefix, self.color)} {line}")

    def note(self, text: str) -> None:
        print(paint("note", f"  {text}", self.color))

    def banner(self) -> None:
        where = "live CBP" if self.live else "saved data"
        print("\n" + paint("bot", "Chisme · puentes Juárez–El Paso", self.color))
        self.note(f"Reading {where}. Type like a follower would; nothing is sent to Instagram.\n")
        for text, why in SUGGESTIONS:
            print(f"  {paint('you', text, self.color):<45} {paint('note', why, self.color)}")
        self.note("\n  /ayuda for commands\n")

    # -- commands ----------------------------------------------------------
    def command(self, raw: str) -> bool:
        """True when handled. Returns False to quit."""
        name = raw[1:].strip().lower()

        if name in ("salir", "quit", "exit"):
            return False
        if name in ("ayuda", "help"):
            print(COMMANDS)
            print()
        elif name in ("post", "publicacion"):
            try:
                data = self.feed.waits(self.lang)
                self.say(data["caption_es"] if self.lang == "es" else data["caption_en"])
            except FeedError as exc:
                self.warn(str(exc))
        elif name in ("idioma", "lang"):
            self.lang = "en" if self.lang == "es" else "es"
            self.pipeline.lang = self.lang
            self.note(f"  → {self.lang}")
        elif name in ("fuentes", "sources"):
            self.sources()
        elif name in ("estado", "health"):
            health = self.feed.health()
            keep = {k: health[k] for k in ("ok", "degraded", "frozen", "last_ok", "storage_enabled")}
            print(f"  {json.dumps(keep, ensure_ascii=False)}")
        else:
            self.note(f"  no such command: /{name}")
        return True

    def sources(self) -> None:
        try:
            snap = self.feed.snapshot()
        except FeedError as exc:
            self.warn(str(exc))
            return
        for source, status in self.feed.health()["sources"].items():
            print(f"  {source:<20}{status}")
        disputed = [(key, d) for key, d in self.feed.decisions.items() if d.disputed]
        if not disputed:
            self.note("  every source agrees right now")
            return
        self.note(f"  {len(disputed)} lane(s) disputed — newest reading shown:")
        for (port_number, lane), decision in disputed:
            port = snap.port(port_number)
            print(f"    {port.name[:22]:<24}{lane:<11}{text.opinions_text(decision.opinions)}")

    def warn(self, message: str) -> None:
        print(f"  {paint('warn', 'CBP unavailable', self.color)} — {message}")
        self.note("  A real pipeline sends nothing here and keeps its last good post.")

    # -- loop --------------------------------------------------------------
    def run(self) -> int:
        self.banner()
        while True:
            try:
                raw = input(paint("you", "  Tú → " if self.lang == "es" else "  You → ", self.color))
            except (EOFError, KeyboardInterrupt):
                print()
                break

            message = raw.strip()
            # Piped input (a saved transcript, a screenshot) does not echo what was
            # typed, which makes the log unreadable. Echo it ourselves in that case.
            if not sys.stdin.isatty():
                print(paint("you", message, self.color))
            if not message:
                continue
            if message.startswith("/"):
                if not self.command(message):
                    break
                continue

            before = len(self.graph.calls)
            try:
                self.pipeline.on_message(self.user, message)
            except FeedError as exc:
                self.warn(str(exc))
                continue

            for call in self.graph.calls[before:]:
                self.say(call["params"]["message"]["text"])
            print()

        print(paint("note", f"\n  {len(self.graph.calls)} Instagram messages composed, 0 sent.\n", self.color))
        return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--from-file", type=Path, help="replay a saved CBP feed instead of going live")
    ap.add_argument("--at", help="pretend it is this local time, e.g. 21:40 (with --from-file)")
    ap.add_argument("--lang", default="es", choices=["es", "en"])
    ap.add_argument("--no-color", action="store_true")
    args = ap.parse_args(argv)

    live = args.from_file is None
    if live:
        feed = BorderFeed(storage=Storage(url="", key=""))
    else:
        payload = json.loads(args.from_file.read_text())
        clock = datetime.now(timezone.utc)
        if args.at:
            hour, _, minute = args.at.partition(":")
            local = datetime.now(cbp.LOCAL_TZ).replace(hour=int(hour), minute=int(minute or 0),
                                                       second=0, microsecond=0)
            clock = local.astimezone(timezone.utc)
        feed = BorderFeed(storage=Storage(url="", key=""), fetcher=lambda: payload,
                          clock=lambda: clock)

    return Chat(feed, args.lang, color=not args.no_color, live=live).run()


if __name__ == "__main__":
    raise SystemExit(main())
