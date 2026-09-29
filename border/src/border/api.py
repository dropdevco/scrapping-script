"""HTTP pull surface. Routing is a plain function so tests never bind a port.

    GET /waits                          all bridges + captions
    GET /waits/{port}?lane=car&lang=es  one bridge, one lane, plus a ready reply
    GET /drops?below=30&lane=car        lanes that crossed under the limit; pass
                                        &since=<cursor> from the last answer
    GET /health                         last fetch state
    GET /accuracy?days=30               each source against real crossing times
    POST /crossings                     {"port", "lane", "reporter"}: they joined the line
    POST /crossings/{id}/done           they are past the booth

Run it next to the pipeline:  python -m border.api --port 8088
"""
from __future__ import annotations

import argparse
import json
import logging
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlparse

from .core.cbp import LANE_LABELS
from .crossings import CrossingError
from .service import BorderFeed, FeedError

log = logging.getLogger(__name__)
DEFAULT_LANE = "car"
DEFAULT_LANG = "es"
MAX_BODY = 10_000
ENDPOINTS = ["/waits", "/waits/{port}", "/best", "/mine", "/drops", "/health", "/accuracy",
             "POST /crossings", "POST /crossings/{id}/done"]


class BadRequest(ValueError):
    """A 400, with the message the pipeline sees and anything that helps it retry."""

    def __init__(self, message: str, **extra):
        super().__init__(message)
        self.extra = extra


def _parse_since(raw: str) -> datetime:
    try:
        when = datetime.fromisoformat(raw.replace(" ", "+"))   # a bare "+" in a query decodes as a space
    except ValueError:
        raise BadRequest("since must be the cursor from a previous /drops answer") from None
    return when if when.tzinfo else when.replace(tzinfo=timezone.utc)


def _lanes(raw: str | None, many: bool = False) -> list[str]:
    """Validate the lane parameter; only endpoints that take several lanes pass many."""
    lanes = [l for l in (raw or DEFAULT_LANE).split(",") if l] or [DEFAULT_LANE]
    unknown = [l for l in lanes if l not in LANE_LABELS]
    if unknown:
        raise BadRequest(f"unknown lane: {', '.join(unknown)}", lanes=list(LANE_LABELS))
    if len(lanes) > 1 and not many:
        raise BadRequest("only /waits accepts several lanes")
    return lanes


def _whole(raw: str | None, what: str, low: int | None = None, high: int | None = None) -> int:
    text = (raw or "").strip()
    if not text.lstrip("-").isdigit() or (low is not None and not low <= int(text) <= high):
        raise BadRequest(what)
    return int(text)


def route(feed: BorderFeed, path: str, query: dict[str, list[str]], method: str = "GET",
          body: dict | None = None) -> tuple[int, dict]:
    """(status, body). Never raises: the pipeline gets a shaped error instead."""
    def one(key: str, default: str | None = None) -> str | None:
        return (query.get(key) or [default])[0]

    parts = [unquote(p) for p in path.strip("/").split("/") if p]
    try:
        if method == "POST":
            return _post(feed, parts, body or {})
        lang = one("lang", DEFAULT_LANG)
        if not parts:
            return 200, {"service": "chisme-border", "endpoints": ENDPOINTS}
        if parts == ["health"]:
            return 200, feed.health()
        if parts == ["waits"]:
            return 200, feed.waits(lang, tuple(_lanes(one("lane"), many=True)) if one("lane") else None)
        if len(parts) == 2 and parts[0] == "waits":
            answer = feed.bridge(parts[1], _lanes(one("lane"))[0], lang)
            return (200 if answer.get("found") else 404), answer
        if parts == ["mine"]:
            ports = [p for p in (one("ports") or "").split(",") if p]
            if not ports:
                raise BadRequest("ports is required: a comma-separated list of bridges")
            return 200, feed.my_digest(ports, _lanes(one("lane"))[0], lang)
        if parts == ["best"]:
            lane = _lanes(one("lane"))[0]
            best = feed.best(lane)
            return (200, best) if best else (503, {"error": f"no bridge is reporting {lane} right now"})
        if parts == ["drops"]:
            below = _whole(one("below"), "below is required and must be a whole number of minutes")
            since = _parse_since(one("since")) if one("since") else None
            return 200, feed.drops(below, _lanes(one("lane"))[0], lang, since)
        if parts == ["accuracy"]:
            return 200, feed.accuracy(_whole(one("days", "30"), "days must be a whole number from 1 to 365", 1, 365))
        return 404, {"error": f"no such endpoint: /{'/'.join(parts)}"}
    except BadRequest as exc:
        return 400, {"error": str(exc), **exc.extra}
    except FeedError as exc:
        # 503: CBP is unreachable. The pipeline should keep its last good post.
        return 503, {"error": str(exc), "hint": "CBP feed unavailable — send nothing rather than stale numbers"}
    except Exception as exc:  # a bug here must still answer, not drop the connection
        log.exception("unhandled error for %s", path)
        return 500, {"error": f"{type(exc).__name__}: {exc}"}


def _post(feed: BorderFeed, parts: list[str], body: dict) -> tuple[int, dict]:
    lang = str(body.get("lang") or DEFAULT_LANG)
    try:
        if parts == ["crossings"]:
            if not body.get("port"):
                raise BadRequest("port is required")
            reporter = body.get("reporter")
            return 201, feed.start_crossing(str(body["port"]), _lanes(body.get("lane"))[0], lang,
                                            str(reporter) if reporter else None)
        if len(parts) == 3 and parts[0] == "crossings" and parts[2] == "done":
            return 200, feed.finish_crossing(parts[1], lang)
    except CrossingError as exc:
        return exc.http_status, {"error": str(exc), "code": exc.code}
    return 404, {"error": f"no such endpoint: POST /{'/'.join(parts)}"}


class Handler(BaseHTTPRequestHandler):
    feed: BorderFeed

    def do_GET(self):  # noqa: N802 - stdlib naming
        parsed = urlparse(self.path)
        self._respond("GET", *route(self.feed, parsed.path, parse_qs(parsed.query)))

    def do_POST(self):  # noqa: N802 - stdlib naming
        parsed = urlparse(self.path)
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY:
            return self._respond("POST", 413, {"error": "body too large"})
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except (ValueError, UnicodeDecodeError):
            return self._respond("POST", 400, {"error": "body must be JSON"})
        if not isinstance(body, dict):
            return self._respond("POST", 400, {"error": "body must be a JSON object"})
        self._respond("POST", *route(self.feed, parsed.path, parse_qs(parsed.query), "POST", body))

    def _respond(self, method: str, status: int, body: dict) -> None:
        payload = json.dumps(body, ensure_ascii=False, indent=2).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        # Tell the pipeline how long this answer stays good, so a polite client can
        # skip asking at all until the next reading is due.
        if method == "GET" and status == 200:
            age = self.feed.cache_age_seconds
            remaining = max(0, int(self.feed.cache_seconds - (age or 0)))
            self.send_header("Cache-Control", f"max-age={remaining}")
            if age is not None:
                self.send_header("Age", str(int(age)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, fmt, *args):
        return  # quiet by default; the pipeline's own logs are the record


def serve(port: int = 8088, host: str = "127.0.0.1") -> None:
    Handler.feed = BorderFeed()
    server = ThreadingHTTPServer((host, port), Handler)
    print(f"chisme-border listening on http://{host}:{port}")
    server.serve_forever()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", type=int, default=8088)
    ap.add_argument("--host", default="127.0.0.1")
    args = ap.parse_args(argv)
    serve(args.port, args.host)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
