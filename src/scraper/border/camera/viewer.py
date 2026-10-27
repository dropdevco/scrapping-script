"""A local page showing every camera's wait estimate, live.

    python -m scraper.border camera view [--cameras a,b] [--every 120] [--port 8090]

Runs the worker (worker.py) in the background and serves http://127.0.0.1:8090: one card per
bridge, a line per lane, and what each camera is doing right now (learning the road,
calculating, waiting for its next reading, or down), with a countdown to its next result.
Each camera shows its live stream through YouTube's embedded player, or "what it measured":
the last reading's frame with each metre of lane tinted by occupancy. That picture is held in
the worker's memory and served from there, never written to disk. Under each bridge, CBP's own
published waits are shown for comparison only; they never feed the camera estimate.
Only bound to localhost; it is a viewer for the machine running the cameras, not a website.

Below the cards, a "camera estimate" section shows each counting method's camera wait for
Paso del Norte northbound (combine.py), per lane-type group, side by side and never averaged,
with its stretches and flags, and CBP's published wait for that lane type beside it for
comparison only (ADR-0015 decision 2, accepted 2026-10-26: the owner, on this PC). What each
number shows is decided by combine.display alone (`estimate_view`). The section is filled only
when `camera estimate` serves the page; under `camera view` it says the estimate is not running.
Line B's camera (the City's) is read by that command and never shown: the section has no
picture.
"""
from __future__ import annotations

import copy
import json
import threading
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from ..cbp import PORTS
from . import combine
from .estimate import LAG_ALERT_S
from .measure import plain
from .reader import state_dir

ESTIMATE_PAIR = {"port_number": "240202", "direction": "north"}   # the pair `camera estimate`
                                                                  # measures (pairs.json)
NOT_RUNNING = ("The camera estimate is not running: this page was started by `camera view`. "
               "Run `camera estimate` instead to see each counting method's camera wait here.")
NOTHING_YET = ("No camera wait this minute: the counting lines have nothing measured yet (both "
               "streams must be seen first).")
ESTIMATE_NOTE = (
    "Each counting method's wait is shown on its own, never averaged. A method's wait shows "
    "minutes only while the count between the lines is anchored (or given, or continued from "
    "such a count) with its spread known, and every stretch is measured; otherwise the floor "
    "(≥) or a dash, with the reason. Each stretch is shown by the same rule on its own, so a "
    "measured stretch can show minutes beside a floor (asked in Q-26). For you on this PC only: "
    "nothing here votes or is published. Line B's camera is read but never shown.")
STALE = ("Stale: the slower counting line is {n} min behind live (its stream may have stopped "
         "without a refusal), so these waits are {n} min old.")
GROUP_LABELS = {"sentri": "SENTRI", "normal": "Normal lanes (standard and Ready, not told apart)",
                "mixed": "Every lane pooled (lane types not split)"}
METHOD_LABELS = {"loop": "Loop counter", "yolox": "YOLOX counter"}
SEGMENT_LABELS = {"tail_to_a": "Back of the line → line A", "a_to_b": "Line A → line B",
                  "b_to_booth": "Line B → booth"}
CBP_LANES = {"sentri": (("car_sentri", "SENTRI"),),                 # display only, never an input
             "normal": (("car", "standard"), ("car_ready", "Ready")),
             "mixed": (("car", "standard"), ("car_ready", "Ready"), ("car_sentri", "SENTRI"))}
AGREE_TEXT = {True: "The two methods agree (within 3 min or 20 %).",
              False: "The two methods disagree.",
              None: "Agreement: — (both methods need minutes)."}

PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Bridge Wait Times</title>
<style>
:root { --bg:#f3f4f2; --card:#fff; --ink:#1b2120; --soft:#5d6764; --line:#d9ddda;
  --accent:#b8730f; --ok:#2f7a4b; --busy:#9a6408; --down:#b2382c; --idle:#6b7470;
  --mono: ui-monospace, "Cascadia Mono", Consolas, monospace;
  --sans: "Segoe UI", system-ui, -apple-system, sans-serif; }
@media (prefers-color-scheme: dark) { :root { --bg:#131816; --card:#1c2220; --ink:#e6ebe8;
  --soft:#9ba6a1; --line:#2e3633; --accent:#e6a33e; --ok:#6cc28c; --busy:#e2b04f;
  --down:#ec7d70; --idle:#95a09b; color-scheme: dark; } }
* { box-sizing: border-box }
body { margin:0; background:var(--bg); color:var(--ink); font:15px/1.5 var(--sans) }
main { max-width:68rem; margin:0 auto; padding:24px 16px 48px; display:grid; gap:18px }
header h1 { margin:0; font-size:1.7rem; letter-spacing:.01em }
header p { margin:4px 0 0; color:var(--soft); font-size:14px }
.grid { display:grid; gap:14px; grid-template-columns: repeat(auto-fill, minmax(min(100%, 22rem), 1fr)) }
.bridge { background:var(--card); border:1px solid var(--line); border-radius:8px; padding:14px 16px;
  display:grid; gap:12px; align-content:start }
.bridge h2 { margin:0; font-size:1.2rem }
.cam { display:grid; gap:8px; border-top:1px dashed var(--line); padding-top:10px }
.cam:first-of-type { border-top:0; padding-top:0 }
.camhead { display:flex; flex-wrap:wrap; gap:6px 10px; align-items:baseline; justify-content:space-between }
.view { color:var(--soft); font-size:13px }
.pill { font:500 11px/1 var(--mono); text-transform:uppercase; letter-spacing:.06em; padding:5px 8px;
  border-radius:999px; border:1px solid currentColor; white-space:nowrap }
.p-ok { color:var(--ok) } .p-reading { color:var(--busy) } .p-learning { color:var(--accent) }
.p-down, .p-error { color:var(--down) } .p-waiting { color:var(--idle) }
.status { font-size:13px; color:var(--soft) }
.status b { color:var(--ink); font-variant-numeric:tabular-nums }
.lane { display:grid; grid-template-columns: 1fr auto; gap:2px 12px; align-items:baseline }
.lname { font-size:14px }
.wait { font-size:1.5rem; font-weight:650; font-variant-numeric:tabular-nums; text-align:right }
.wait small { font-size:.8rem; font-weight:400; color:var(--soft) }
.detail { grid-column:1 / -1; font-size:12.5px; color:var(--soft) }
.stale .wait, .stale .lname { opacity:.55 }
.flag { font-size:12px; color:var(--busy) }
footer { font-size:12.5px; color:var(--soft) }
.empty { color:var(--soft); padding:20px; border:1px dashed var(--line); border-radius:8px }
.media { display:grid; gap:6px }
.tabs { display:flex; gap:6px }
.tabs button { font:500 12px var(--sans); padding:4px 10px; border-radius:999px; cursor:pointer;
  border:1px solid var(--line); background:transparent; color:var(--soft) }
.tabs button[aria-pressed="true"] { border-color:var(--accent); color:var(--ink) }
.tabs button:focus-visible { outline:2px solid var(--accent); outline-offset:2px }
.frame { position:relative; width:100%; aspect-ratio:16/9; background:#000; border-radius:6px; overflow:hidden }
.frame iframe, .frame img { position:absolute; inset:0; width:100%; height:100%; border:0; object-fit:contain }
.frame .note { position:absolute; inset:0; display:grid; place-items:center; color:#c9d0cd;
  font-size:13px; padding:12px; text-align:center }
.legend { font-size:12px; color:var(--soft) }
.cbp { font-size:12.5px; color:var(--soft); margin-top:-6px }
.estimate .egroup { display:grid; gap:8px; border-top:1px dashed var(--line); padding-top:10px }
.estimate h3 { margin:0; font-size:1.05rem }
.methods { display:grid; gap:10px; grid-template-columns: repeat(auto-fit, minmax(min(100%, 18rem), 1fr)) }
.method { border:1px solid var(--line); border-radius:6px; padding:10px 12px; display:grid; gap:4px;
  align-content:start }
.method .wait { text-align:left }
.mname { font-size:13px; color:var(--soft) }
.seg { display:grid; grid-template-columns: 1fr auto; gap:0 10px; font-size:13px }
.seg .detail { font-size:12px }
.estimate .cbp { margin-top:0 }
</style></head>
<body><main>
<header>
  <h1>Bridge Wait Times</h1>
  <p>Measured from the bridge cameras: how long each lane's line is, and how fast the whole
  line moves (averaged over every car in it, stopped ones included). Wait = distance to the
  booth ÷ that speed. <span id="clock"></span></p>
</header>
<div class="grid" id="bridges"><div class="empty">Waiting for the worker's first status…</div></div>
<section class="bridge estimate" id="estimate" aria-live="polite"></section>
<footer id="foot"></footer>
</main>
<script>
const TZ = "America/Denver";
let status = null, fetchedAt = 0;
const el = (t, c, x) => { const n = document.createElement(t); if (c) n.className = c; if (x != null) n.textContent = x; return n; };
const clock = d => d.toLocaleTimeString("en-US", { timeZone: TZ, hour: "numeric", minute: "2-digit" });
function left(iso) {
  if (!iso) return null;
  const s = Math.round((new Date(iso) - Date.now()) / 1000);
  if (s <= 0) return "any moment";
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
}
const LANE_NAMES = { car: "Cars", car_sentri: "SENTRI", car_ready: "Ready Lane", walk: "Walking",
  walk_ready: "Walking Ready", truck: "Trucks", truck_fast: "FAST" };
function laneRow(l, stale) {
  // The worker decides what a lane's number means (worker.lane_summary); the page only prints it.
  const row = el("div", "lane" + (stale ? " stale" : ""));
  const name = l.cbp_lane ? LANE_NAMES[l.cbp_lane] || l.cbp_lane : l.lane;
  row.append(el("span", "lname", name + (l.cbp_lane ? "" : " (lane type unconfirmed)")));
  const w = el("span", "wait", l.display_value || "—");
  if (l.minutes != null) w.append(el("small", null, " min"));
  row.append(w);
  row.append(el("span", "detail", l.display_detail || ""));
  return row;
}
function camInfo(c) {
  const box = el("div", "info");
  const head = el("div", "camhead");
  const dir = c.direction === "north" ? "northbound" : "southbound";
  head.append(el("span", "view", `${c.view} · ${dir}${c.direction_verified ? "" : " (direction unconfirmed)"}`));
  const label = { ok: "live", reading: "calculating", learning: "learning", down: "down", error: "error", waiting: "starting" }[c.phase] || c.phase;
  head.append(el("span", "pill p-" + c.phase, label));
  box.append(head);
  const st = el("div", "status");
  if (c.phase === "reading") {
    st.append("Calculating… result in ", el("b", null, left(c.eta_at) || "—"));
  } else if (c.phase === "learning" && c.learning) {
    const ready = c.learning.ready_at ? new Date(c.learning.ready_at) : null;
    st.append(`${c.message ? c.message[0].toUpperCase() + c.message.slice(1) : "Learning the empty road"}. First estimate ≈ `,
      el("b", null, ready ? `${clock(ready)} (in ${left(c.learning.ready_at)})` : "soon"));
  } else if (c.phase === "down") {
    st.append(`Camera down: ${c.message}. Retrying in `, el("b", null, left(c.next_at) || "—"));
  } else if (c.phase === "error") {
    st.append(`Reading failed: ${c.message}. Retrying in `, el("b", null, left(c.next_at) || "—"));
  } else if (c.phase === "waiting") {
    st.append("Starting…");
  } else {
    st.append(c.updated_at ? `Updated ${clock(new Date(c.updated_at))}. ` : "", "Next update in ",
      el("b", null, left(c.next_at) || "—"));
  }
  box.append(st);
  const stale = c.phase === "reading" || c.phase === "down" || c.phase === "error";
  for (const l of c.lanes || []) box.append(laneRow(l, stale));
  return box;
}

// One persistent card per camera: { root, info, frame, mode, videoId, overlayStamp }.
const cams = new Map();
const bridges = new Map();
function makeCam(c) {
  const root = el("div", "cam");
  const info = el("div");
  const media = el("div", "media");
  const tabs = el("div", "tabs");
  const frame = el("div", "frame");
  const legend = el("div", "legend");
  const entry = { root, info, frame, legend, mode: "video", videoId: null, overlayStamp: null };
  for (const [mode, text] of [["video", "Live video"], ["measured", "What it measured"]]) {
    const btn = el("button", null, text);
    btn.type = "button";
    btn.setAttribute("aria-pressed", String(mode === entry.mode));
    btn.addEventListener("click", () => {
      entry.mode = mode;
      tabs.querySelectorAll("button").forEach(b => b.setAttribute("aria-pressed", String(b === btn)));
      entry.videoId = null; entry.overlayStamp = null;   // force the panel to rebuild once
      updateMedia(entry, status.cameras[c.id]);
    });
    tabs.append(btn);
  }
  media.append(tabs, frame, legend);
  root.append(info, media);
  cams.set(c.id, entry);
  return entry;
}
function updateMedia(entry, c) {
  if (entry.mode === "video") {
    entry.legend.textContent = "";
    if (!c.video_id) {
      if (entry.videoId !== "none") {
        entry.frame.replaceChildren(el("div", "note", "Finding this camera's stream…"));
        entry.videoId = "none";
      }
      return;
    }
    if (entry.videoId === c.video_id) return;            // already playing: never reload it
    const f = document.createElement("iframe");
    f.src = `https://www.youtube.com/embed/${encodeURIComponent(c.video_id)}?autoplay=1&mute=1&playsinline=1`;
    f.title = `${c.view} live video`;
    f.allow = "autoplay; encrypted-media; picture-in-picture";
    f.referrerPolicy = "strict-origin-when-cross-origin";
    f.loading = "lazy";
    entry.frame.replaceChildren(f);
    entry.videoId = c.video_id;
  } else {
    entry.legend.textContent = "The last reading's final frame. Each metre of lane is tinted by how much of the minute it was occupied (green clear, red full); the cyan line is where the queue ends.";
    const stamp = c.updated_at || "";
    if (entry.overlayStamp === stamp) return;
    if (!c.updated_at) {
      entry.frame.replaceChildren(el("div", "note", "No reading yet: the first one appears after the camera finishes learning."));
    } else {
      const img = document.createElement("img");
      img.alt = `What the reader measured on ${c.view}`;
      img.src = `overlay/${encodeURIComponent(c.id)}.jpg?t=${encodeURIComponent(stamp)}`;
      img.onerror = () => entry.frame.replaceChildren(el("div", "note", "Picture not available yet."));
      entry.frame.replaceChildren(img);
    }
    entry.overlayStamp = stamp;
  }
}
function render() {
  if (!status) return;
  const host = document.getElementById("bridges");
  const placeholder = host.querySelector(".empty");
  if (placeholder && Object.keys(status.cameras).length) placeholder.remove();
  for (const c of Object.values(status.cameras)) {
    let card = bridges.get(c.bridge);
    if (!card) {
      card = el("section", "bridge");
      card.append(el("h2", null, c.bridge));
      const cbpLine = el("div", "cbp");
      cbpLine.dataset.port = c.port_number;
      card.append(cbpLine);
      host.append(card);
      bridges.set(c.bridge, card);
    }
    const entry = cams.get(c.id) || makeCam(c);
    if (!entry.root.parentNode) card.append(entry.root);
    entry.info.replaceChildren(camInfo(c));
    updateMedia(entry, c);
  }
  for (const line of document.querySelectorAll(".cbp")) {
    const lanes = (status.cbp || {})[line.dataset.port];
    const names = { car: "Cars", car_ready: "Ready", car_sentri: "SENTRI" };
    const parts = lanes ? Object.entries(lanes).filter(([, v]) => v.state === "open" && v.minutes != null)
      .map(([k, v]) => `${names[k] || k} ${v.minutes} min`) : [];
    const label = lanes ? Object.values(lanes).map(v => v.label).find(Boolean) : null;
    line.textContent = parts.length
      ? `For comparison, CBP publishes (northbound): ${parts.join(" · ")}${label ? ` — ${label.toLowerCase()}` : ""}`
      : (status.cbp_error ? "CBP's published waits unavailable right now" : "");
  }
  document.getElementById("clock").textContent = `El Paso time ${clock(new Date())}.`;
  const age = Math.round((Date.now() - fetchedAt) / 1000);
  document.getElementById("foot").textContent =
    `A reading takes ${status.reading_s} s of video; each camera is read every ${status.every_s} s. ` +
    "Live video is YouTube's own embedded player. " +
    (age > 10 ? `Status not refreshed for ${age} s — is the worker still running?` : "");
}
// The camera estimate (estimate.json): every value, sign and reason is decided in Python by
// combine.display (viewer.estimate_view); the page only prints them. It has no picture.
let estimate = null;
function renderEstimate() {
  const box = document.getElementById("estimate");
  if (!estimate) return;
  const parts = [el("h2", null, estimate.title)];
  if (!estimate.running) {
    parts.push(el("div", "status", estimate.message));
    box.replaceChildren(...parts);
    return;
  }
  if (estimate.at) parts.push(el("div", "status", `As of ${clock(new Date(estimate.at))} (the slower counting line's time).`));
  if (estimate.message) parts.push(el("div", estimate.stopped || estimate.stale ? "flag" : "status", estimate.message));
  for (const g of estimate.groups) {
    const sec = el("div", "egroup");
    sec.append(el("h3", null, g.label), el("div", "detail", g.lane_text));
    const row = el("div", "methods");
    for (const m of g.methods) {
      const card = el("div", "method");
      card.append(el("div", "mname", m.label));
      const w = el("div", "wait", m.value);
      if (m.unit) w.append(el("small", null, m.unit));
      card.append(w, el("div", "detail", m.detail));
      for (const s of m.segments) {
        const r = el("div", "seg");
        r.append(el("span", null, s.label), el("span", null, s.value + s.unit));
        if (s.detail) r.append(el("span", "detail", s.detail));
        card.append(r);
      }
      if (m.count_text) card.append(el("div", "detail", m.count_text));
      if (m.transient.length) card.append(el("div", "flag", m.transient.join(" · ")));
      if (m.info.length) card.append(el("div", "detail", m.info.join(" · ")));
      row.append(card);
    }
    sec.append(row, el("div", "cbp", g.cbp_text), el("div", "detail", g.agree_text),
      el("div", "detail", g.vote_text));
    parts.push(sec);
  }
  parts.push(el("div", "legend", estimate.note));
  box.replaceChildren(...parts);
}
async function poll() {
  try {
    const r = await fetch("status.json", { cache: "no-store" });
    if (r.ok) { status = await r.json(); fetchedAt = Date.now(); }
  } catch (e) { /* keep showing the last status; the footer says how old it is */ }
  try {
    const r = await fetch("estimate.json", { cache: "no-store" });
    if (r.ok) estimate = await r.json();
  } catch (e) { /* keep the last one */ }
  render();
  renderEstimate();
}
poll(); setInterval(poll, 2000); setInterval(render, 1000);
</script></body></html>
"""


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path in ("/", "/index.html"):
            body, kind = PAGE.encode("utf-8"), "text/html; charset=utf-8"
        elif self.path.startswith("/overlay/"):
            name = self.path.split("?")[0][len("/overlay/"):].removesuffix(".jpg")
            worker = getattr(self.server, "worker", None)
            body = worker.overlays.get(name) if worker else None
            if body is None:
                self.send_error(404)
                return
            kind = "image/jpeg"
        elif self.path.split("?")[0] == "/status.json":
            # This page's own worker, from memory: never a file another worker may be writing.
            worker = getattr(self.server, "worker", None)
            if worker is not None:
                with worker._lock:
                    body = json.dumps(plain(worker.status), ensure_ascii=False).encode("utf-8")
            else:
                path = state_dir() / "status.json"
                body = path.read_bytes() if path.exists() else b'{"cameras": {}}'
            kind = "application/json"
        elif self.path.split("?")[0] == "/estimate.json":
            body = json.dumps(_estimate(self.server), ensure_ascii=False).encode("utf-8")
            kind = "application/json"
        else:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", kind)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):     # the worker's output matters; request lines do not
        pass


def _estimate(server) -> dict:
    """The page's camera-estimate section from the `camera estimate` running this server
    (`server.estimate`, an estimate.Live), with CBP's published waits from its Worker; or, with
    none (`camera view`), that it is not running."""
    live = getattr(server, "estimate", None)
    if live is None:
        return estimate_view(None)
    worker = getattr(server, "worker", None)
    cbp = cbp_error = None
    if worker is not None:
        with worker._lock:
            cbp = copy.deepcopy(worker.status.get("cbp"))
            cbp_error = worker.status.get("cbp_error")
    return estimate_view(live.snapshot(), cbp, cbp_error)


def estimate_view(snapshot: dict | None, cbp: dict | None = None,
                  cbp_error: str | None = None) -> dict:
    """What the camera-estimate section prints, from the latest camera record (combine.combine)
    in `snapshot` (estimate.Live.snapshot). Every value and reason comes from combine.display,
    so the page shows exactly what the rule allows: per group and method, the total or its
    floor, and each stretch. `cbp` (worker status "cbp": port -> lane -> state and minutes) is
    shown beside each group for comparison only; it never reaches a camera number.

    A record whose `at` lies more than LAG_ALERT_S behind the tick that made it (`updated_at`)
    is flagged stale: a stream that stops without a refusal leaves each tick re-measuring the
    same old minute, for up to SIGNATURE_TTL_S."""
    pair = (snapshot or {}).get("pair") or ESTIMATE_PAIR
    port = pair.get("port_number")
    title = (f"{PORTS.get(port, {}).get('name', port)} {pair.get('direction')}bound - "
             "camera estimate")
    if snapshot is None:
        return {"running": False, "title": title, "message": NOT_RUNNING}
    show = bool(pair.get("show_minutes"))
    record = snapshot.get("record")
    behind = _behind_s(record, snapshot.get("updated_at"))
    stale = behind > LAG_ALERT_S
    out = {"running": True, "title": title, "at": record.get("at") if record else None,
           "stopped": snapshot.get("stopped"), "stale": stale,
           "message": (snapshot.get("stopped") or ("" if record else NOTHING_YET)
                       or (STALE.format(n=round(behind / 60)) if stale else "")),
           "groups": [], "note": ESTIMATE_NOTE}
    lanes = (cbp or {}).get(port)
    for group, g in ((record or {}).get("groups") or {}).items():
        reasons = g.get("would_vote_reasons") or []
        out["groups"].append({
            "group": group, "label": GROUP_LABELS.get(group, group),
            "lane_text": (f"CBP lane {g['lane']}" if g.get("lane")
                          else "No CBP lane id: this group never votes."),
            "methods": [_method_view(m, w, show) for m, w in g["methods"].items()],
            "agree_text": _agree_text(g),
            "vote_text": ("Votes in the hidden estimate: yes." if g.get("would_vote") else
                          "Votes in the hidden estimate: no" + (f" ({'; '.join(reasons)})."
                                                                 if reasons else ".")),
            "cbp_text": _cbp_text(group, lanes, cbp_error)})
    return out


def _agree_text(g: dict) -> str:
    """AGREE_TEXT, except that a group with a method the runner does not measure (combine's
    not_measured: at PDN the loop's SENTRI) cannot agree until that changes, so the page names
    that method rather than waiting on its minutes."""
    skipped = [m for m, w in g["methods"].items() if w.get("not_measured")]
    if g.get("agree") is None and skipped:
        return f"Agreement: — (not possible: {' and '.join(skipped)} not measured here)."
    return AGREE_TEXT[g.get("agree")]


def _method_view(method: str, wait: dict, show: bool) -> dict:
    shown = combine.display(wait, show)
    count = wait.get("count") or {}
    sd = count.get("offset_sd")
    return {"method": method, "label": METHOD_LABELS.get(method, method),
            "value": shown["value"], "unit": _unit(shown["value"]), "detail": shown["detail"],
            "minutes_shown": shown["minutes_shown"],
            "segments": [_segment_view(s, count, show) for s in wait.get("segments") or []],
            "count_text": ("" if wait.get("not_measured") else
                           f"Cars between the lines: count {count.get('source') or 'none'}, "
                           f"sd {'—' if sd is None else f'{sd} cars'}"
                           + ("" if count.get("sure") else ", not sure")),
            "transient": list(wait.get("transient") or []), "info": list(wait.get("info") or [])}


def _segment_view(seg: dict, count: dict, show: bool) -> dict:
    """One stretch through combine.display, as a wait of that stretch alone. Line A to B rests
    on the count between the lines, so it carries that count and is shown only as the count
    allows; the tail (the queue reader) and B to the booth (K_eff over the counted exit rate)
    do not rest on that count. Each stretch is gated on its own, so a measured stretch can show
    minutes beside the method's floor total (asked in Q-26 (ii); ESTIMATE_NOTE says so)."""
    one = {"count": count if seg["name"] == "a_to_b" else {"sure": True},
           "floor": not seg["known"], "minutes": seg["minutes"], "low": seg["low"],
           "high": seg["high"], "segments": [seg], "transient": []}
    shown = combine.display(one, show)
    return {"name": seg["name"], "label": SEGMENT_LABELS.get(seg["name"], seg["name"]),
            "value": shown["value"], "unit": _unit(shown["value"]), "detail": shown["detail"]}


def _behind_s(record: dict | None, updated_at: str | None) -> float:
    """Seconds from the record's `at` (the slower line's time) to the tick that made it; both
    are cut to the minute. 0 when either is missing."""
    if not record or not record.get("at") or not updated_at:
        return 0.0
    return (datetime.fromisoformat(updated_at)
            - datetime.fromisoformat(record["at"])).total_seconds()


def _unit(value: str) -> str:
    return "" if value in ("", "—") else " min"


def _cbp_text(group: str, lanes: dict | None, error: str | None) -> str:
    if not lanes:
        return ("CBP's published waits unavailable right now." if error
                else "CBP's published waits not fetched yet.")
    parts = []
    for lane_id, label in CBP_LANES.get(group, ()):
        lane = lanes.get(lane_id) or {}
        if lane.get("state") == "open" and lane.get("minutes") is not None:
            parts.append(f"{label} {lane['minutes']} min")
        else:
            parts.append(f"{label} {str(lane.get('state') or 'not published').replace('_', ' ')}")
    return f"For comparison only, CBP publishes: {' · '.join(parts)}."


def serve(port: int, host: str = "127.0.0.1") -> ThreadingHTTPServer:
    return ThreadingHTTPServer((host, port), Handler)


def cmd_view(args, cameras) -> int:
    from .worker import AlreadyRunning, Worker

    chosen = ([cameras[c.strip()] for c in args.cameras.split(",") if c.strip()]
              if args.cameras else list(cameras.values()))
    # Cameras marked display: false (the City of El Paso feeds, ADR-0014) are never shown here,
    # even when asked for; cameras with no lanes have no queue to show.
    chosen = [c for c in chosen if c.display and c.lanes]
    try:
        worker = Worker(chosen, seconds=args.seconds, every=args.every,
                        emit=(lambda sig: print(json.dumps(sig, ensure_ascii=False), flush=True))
                        if args.json else None, compare_cbp=True)
    except AlreadyRunning as exc:
        print(exc)
        return 1
    stop = threading.Event()
    threading.Thread(target=worker.forever, args=(stop,), daemon=True).start()
    server = serve(args.port)
    server.worker = worker
    print(f"Bridge wait times: http://127.0.0.1:{args.port}  (Ctrl+C to stop)", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        server.server_close()
    return 0
