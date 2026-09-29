"""Simulate Instagram so the pull can be tested end to end, with no Meta credentials.

Two halves:

  FakeGraph     stands in for the Meta Graph API. Records every call it would make —
                publish a carousel, send a direct message — and sends nothing.
  FakePipeline  plays Carlos' side: pulls from our feed and drives a conversation,
                a scheduled message and a drop alert.

    python3 tools/fake_instagram.py                     # live CBP
    python3 tools/fake_instagram.py --from-file tests/fixtures/cbp_feed.json
    python3 tools/fake_instagram.py --url http://127.0.0.1:8088   # over HTTP

Exit code 0 means every simulated call carried a sane payload.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from scraper.border import cbp, requests, text
from scraper.border.crossings import CrossingError
from scraper.border.service import BorderFeed
from scraper.border.storage import BorderStore

GRAPH = "https://graph.facebook.com/v21.0"


# ---------------------------------------------------------------- fake Meta
@dataclass
class FakeGraph:
    """Every Meta call the pipeline would make, recorded instead of sent."""
    calls: list[dict] = field(default_factory=list)

    def publish_carousel(self, caption: str, image_count: int = 1) -> str:
        self.calls.append({"call": "POST", "endpoint": f"{GRAPH}/{{ig-user-id}}/media",
                           "params": {"caption": caption, "media_type": "CAROUSEL", "children": image_count}})
        creation_id = f"fake-creation-{len(self.calls)}"
        self.calls.append({"call": "POST", "endpoint": f"{GRAPH}/{{ig-user-id}}/media_publish",
                           "params": {"creation_id": creation_id}})
        return creation_id

    def send_message(self, recipient_id: str, message: str, quick_replies: list[str] | None = None) -> None:
        params: dict = {"recipient": {"id": recipient_id}, "message": {"text": message}}
        if quick_replies:
            params["message"]["quick_replies"] = [
                {"content_type": "text", "title": q, "payload": q.upper()} for q in quick_replies]
        self.calls.append({"call": "POST", "endpoint": f"{GRAPH}/{{ig-user-id}}/messages", "params": params})


# ------------------------------------------------------------- feed clients
class HttpFeed:
    """Pulls over HTTP, for testing a running `python -m scraper.border.api`.

    Same coroutine surface as BorderFeed, so FakePipeline cannot tell them apart."""

    def __init__(self, base: str):
        self.base = base.rstrip("/")

    def _send(self, target) -> tuple[int, dict]:
        """(status, body). A 404 or 400 is an answer, not a crash.

        "I don't know that bridge" arrives as 404 with the options in the body, and
        "CBP is unreachable" as 503. The pipeline has to read those, so urllib's
        habit of raising on them is unwrapped here.
        """
        try:
            with urllib.request.urlopen(target, timeout=30) as resp:
                return resp.status if hasattr(resp, "status") else 200, json.load(resp)
        except urllib.error.HTTPError as exc:
            with exc:
                raw = exc.read()
            try:
                return exc.code, json.loads(raw)
            except json.JSONDecodeError:
                return exc.code, {"error": f"HTTP {exc.code}", "body": raw[:200].decode(errors="replace")}

    async def _get(self, path: str, **params) -> dict:
        query = urllib.parse.urlencode(params)
        target = f"{self.base}{path}?{query}" if query else f"{self.base}{path}"
        return (await asyncio.to_thread(self._send, target))[1]

    async def waits(self, lang="es"):
        return await self._get("/waits", lang=lang)

    async def my_digest(self, ports, lane="car", lang="es"):
        return await self._get("/mine", ports=",".join(ports), lane=lane, lang=lang)

    async def bridge(self, key, lane="car", lang="es"):
        # People type "el puente libre"; a raw space is not a legal request target.
        return await self._get(f"/waits/{urllib.parse.quote(key, safe='')}", lane=lane, lang=lang)

    async def drops(self, below, lane="car", lang="es", since=None):
        params = {"below": below, "lane": lane, "lang": lang}
        if since is not None:
            params["since"] = since.isoformat()
        return await self._get("/drops", **params)

    async def _post(self, path: str, body: dict) -> dict:
        request = urllib.request.Request(self.base + path, data=json.dumps(body).encode(),
                                         headers={"Content-Type": "application/json"}, method="POST")
        status, answer = await asyncio.to_thread(self._send, request)
        if status >= 400:
            raise CrossingError(answer.get("error", f"HTTP {status}"), answer.get("code", "unknown"))
        return answer

    async def start_crossing(self, port, lane="car", lang="es", reporter=None):
        return await self._post("/crossings", {"port": port, "lane": lane, "lang": lang, "reporter": reporter})

    async def finish_crossing(self, crossing_id, lang="es"):
        return await self._post(f"/crossings/{crossing_id}/done", {"lang": lang})


# ---------------------------------------------------------------- pipeline
class FakePipeline:
    def __init__(self, feed, graph: FakeGraph, lang: str = "es"):
        self.feed, self.graph, self.lang = feed, graph, lang
        self.subscriptions: list[dict] = []   # what Chisme would store per person
        self.saved: dict[str, dict] = {}      # user -> {"ports": [...], "lane": "car_sentri"}
        self.crossing: dict[str, str] = {}    # user -> the crossing they are in the middle of

    async def daily_post(self) -> str:
        data = await self.feed.waits(self.lang)
        caption = data["caption_es"] if self.lang == "es" else data["caption_en"]
        slides = sum(1 for p in data["ports"] if p["has_live_data"]) or 1
        self.graph.publish_carousel(caption, slides)
        return caption

    async def on_message(self, user_id: str, message: str) -> None:
        """The small part of the conversation that needs our data."""
        msg = message.strip().lower()

        if msg in ("puentes", "bridges", "hola", "hi"):
            saved = self.saved.get(user_id)
            if saved:
                # A regular crosser gets their two bridges in their lane, not all thirty lines.
                digest = await self.feed.my_digest(saved["ports"], saved["lane"], self.lang)
                self.graph.send_message(user_id, digest["message"],
                                        quick_replies=["Todos", "Cambiar carril", "Alto"])
                return
            data = await self.feed.waits(self.lang)
            names = [p["name_es"] if self.lang == "es" else p["name"] for p in data["ports"]]
            head = data["caption_es"] if self.lang == "es" else data["caption_en"]
            self.graph.send_message(user_id, head, quick_replies=names[:4])
            return

        if msg.startswith(("guardar", "save", "mi puente")):
            target = msg.split(maxsplit=1)[1] if " " in msg else ""
            lane = requests.lane_in(msg) or "car"
            answer = await self.feed.bridge(requests.bridge_text(target) or target, lane, self.lang)
            if not answer.get("found"):
                self.graph.send_message(user_id, answer["reply"], quick_replies=answer.get("options"))
                return
            entry = self.saved.setdefault(user_id, {"ports": [], "lane": lane})
            entry["lane"] = lane
            if answer["port_number"] not in entry["ports"]:
                entry["ports"].append(answer["port_number"])
            name = answer["name_es"] if self.lang == "es" else answer["name"]
            self.graph.send_message(user_id, f"Guardado: {name}. Escribe \"puentes\" para verlo primero."
                                    if self.lang == "es" else
                                    f"Saved: {name}. Send \"bridges\" to see it first.")
            return

        if requests.is_alert_request(msg):
            await self._subscribe(user_id, msg)
            return

        if msg.startswith(("voy a cruzar", "cruzando", "crossing")):
            target = msg.split("cruzar", 1)[-1] if "cruzar" in msg else msg.split(maxsplit=1)[-1]
            saved = self.saved.get(user_id)
            lane = saved["lane"] if saved else "car"
            port = target.strip() or (saved["ports"][0] if saved else "")
            try:
                crossing = await self.feed.start_crossing(port, lane, self.lang, reporter=user_id)
            except CrossingError as exc:
                self.graph.send_message(user_id, text.crossing_error(exc.code, self.lang))
                return
            self.crossing[user_id] = crossing["id"]
            self.graph.send_message(user_id, crossing["reply"])
            return

        if msg in ("ya crucé", "ya cruce", "crucé", "cruce", "crossed"):
            crossing_id = self.crossing.pop(user_id, None)
            if not crossing_id:
                self.graph.send_message(user_id, "Primero escribe \"voy a cruzar\" y el puente."
                                        if self.lang == "es" else "Send \"crossing\" and the bridge first.")
                return
            try:
                reply = (await self.feed.finish_crossing(crossing_id, self.lang))["reply"]
            except CrossingError as exc:
                reply = text.crossing_error(exc.code, self.lang)
            self.graph.send_message(user_id, reply)
            return

        if msg in ("alto", "stop"):
            self.subscriptions = [s for s in self.subscriptions if s["user_id"] != user_id]
            self.graph.send_message(user_id, "Listo, cancelé tus avisos." if self.lang == "es"
                                    else "Done, your alerts are cancelled.")
            return

        # anything else is read as a bridge name
        answer = await self.feed.bridge(msg, "car", self.lang)
        self.graph.send_message(user_id, answer["reply"],
                                quick_replies=None if answer.get("found") else answer.get("options"))

    async def _subscribe(self, user_id: str, msg: str) -> None:
        """File an alert for the bridge and lane actually named, in their language.

        A bridge not named falls back to their first saved bridge (and their saved lane
        unless they named one); with nothing saved either, we ask rather than guess.
        """
        lang = requests.language_of(msg, self.lang)
        below = requests.limit_in(msg)
        saved = self.saved.get(user_id)
        lane = requests.lane_in(msg) or (saved["lane"] if saved else "car")
        target = requests.bridge_text(msg) or (saved["ports"][0] if saved else "")
        if not target:
            options = (await self.feed.bridge("", lane, lang)).get("options")
            self.graph.send_message(user_id, text.alert_which_bridge(below, lang), quick_replies=options)
            return
        answer = await self.feed.bridge(target, lane, lang)
        if not answer.get("found") or answer.get("state") == "absent":
            # Unknown bridge, or a lane this bridge does not have (SENTRI at Tornillo):
            # the feed's own reply already says which, and lists the bridges.
            self.graph.send_message(user_id, answer["reply"], quick_replies=answer.get("options"))
            return
        self.subscriptions.append({"user_id": user_id, "port": answer["port_number"],
                                   "lane": lane, "below": below, "lang": lang})
        self.graph.send_message(user_id, text.alert_confirmed(answer, lane, below, lang))

    async def scheduled_message(self, user_id: str, port_key: str | None = None, lane: str = "car") -> None:
        """Their saved bridges when they have some, otherwise the one asked for."""
        saved = self.saved.get(user_id)
        if saved and not port_key:
            self.graph.send_message(user_id, (await self.feed.my_digest(saved["ports"], saved["lane"], self.lang))["message"])
            return
        self.graph.send_message(user_id, (await self.feed.bridge(port_key, lane, self.lang))["reply"])

    async def alert_sweep(self) -> int:
        """Each subscription keeps its own cursor, so no subscriber's alert is taken by
        another asking for the same limit first."""
        sent = 0
        for sub in self.subscriptions:
            answer = await self.feed.drops(sub["below"], sub["lane"], sub["lang"], since=sub.get("cursor"))
            # drops() reports every bridge under the limit; a subscriber asked about one.
            for drop in answer["drops"]:
                if drop["port_number"] != sub["port"]:
                    continue
                self.graph.send_message(sub["user_id"], drop["message"])
                sent += 1
            if answer.get("cursor"):
                sub["cursor"] = datetime.fromisoformat(answer["cursor"])
        return sent


# -------------------------------------------------------------------- main
async def run(feed, lang: str = "es") -> FakeGraph:
    graph = FakeGraph()
    pipeline = FakePipeline(feed, graph, lang)

    print("== daily post ==")
    print(await pipeline.daily_post(), "\n")

    print("== messages ==")
    for user, msg in [("user-1", "puentes"), ("user-1", "el puente libre"),
                      ("user-1", "guardar paso del norte sentri"), ("user-1", "puentes"),
                      ("user-2", "zaragoza"), ("user-2", "avísame cuando paso del norte baje de 30 min"),
                      ("user-3", "puente que no existe")]:
        await pipeline.on_message(user, msg)
        print(f"  {user} sent {msg!r}")
        for line in graph.calls[-1]["params"]["message"]["text"].splitlines():
            print(f"    -> {line}")

    print("\n== scheduled 6:30 message (their saved bridges) ==")
    await pipeline.scheduled_message("user-1")
    for line in graph.calls[-1]["params"]["message"]["text"].splitlines():
        print(f"  -> {line}")

    print("\n== alert sweep ==")
    print(f"  {await pipeline.alert_sweep()} alert(s) sent "
          f"({len(pipeline.subscriptions)} subscription(s) checked)")

    return graph


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--from-file", type=Path, help="replay a saved CBP feed")
    ap.add_argument("--url", help="pull over HTTP from a running scraper.border.api instead")
    ap.add_argument("--lang", default="es", choices=["es", "en"])
    ap.add_argument("--out", type=Path, help="write the recorded Meta calls here")
    args = ap.parse_args(argv)

    if args.url:
        feed = HttpFeed(args.url)
    else:
        async def from_file(_http):
            return json.loads(args.from_file.read_text())

        feed = BorderFeed(storage=BorderStore(client=None),
                          fetcher=from_file if args.from_file else cbp.fetch)

    graph = asyncio.run(run(feed, args.lang))

    print(f"\n== recorded {len(graph.calls)} Meta call(s), none sent ==")
    for call in graph.calls:
        print(f"  {call['call']} {call['endpoint']}")
    if args.out:
        args.out.write_text(json.dumps(graph.calls, indent=2, ensure_ascii=False))
        print(f"\nwrote {args.out}")

    empty = [c for c in graph.calls if not c["params"]]
    return 1 if empty else 0


if __name__ == "__main__":
    raise SystemExit(main())
