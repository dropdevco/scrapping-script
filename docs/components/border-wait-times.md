# Component: Border Wait Times

Live CBP wait times for the six El Paso–Juárez ports of entry, reconciled with a faster
scraped source, ranked for someone deciding "leave now or wait", and written to Supabase
so everything downstream can read the same numbers. Owns `src/scraper/border/`, the
`border_*` tables, and `.github/workflows/border_poll.yml`.

**Status: built, not yet wired in.** Everything here is new files; nothing existing in
the repo was edited. The four small hooks it still needs are in
[Wiring it in](#wiring-it-in).

---

## At a glance

|                          |                                                                                                                                                                                                                |
| ------------------------ | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **Entry point**          | `python -m scraper.border <poll\|selfcheck\|serve\|chat\|demo\|simulate>`                                                                                                                                      |
| **Runs on** | GitHub Actions (`border-poll`): a reading every 10 min; retention + live selfcheck daily at 10:20 UTC |
| **Reads**                | CBP's public feed (`bwt.cbp.gov/api/waittimes`), `pasosfronterizos.com`, `borderswaittime.com` (a CBP mirror, used only to check our parser)                                                                   |
| **Writes** | `border_current_waits` (the knowledge-base feed), `border_ports`, `border_readings`, `border_runs`, `border_alerts`, `border_crossings` (migrations `0013`–`0016`) |
| **Requires**             | Nothing for live answers. `SUPABASE_URL` + `SUPABASE_KEY` for history, deltas that survive a restart, alerts and "normal for this hour"                                                                        |
| **Uses from the engine** | `core.http.HttpClient` (retries, robots.txt, gzip, `USER_AGENT`), `core.config.settings`, `core.eventtime.event_tz()`, the Supabase client from `core.storage.Storage`, `ENABLED_SOURCES` / `DISABLED_SOURCES` |
| **Tests** | `tests/border/` — 355 tests, one file per module (`pytest tests/border -q`) |

---

## CLI reference

| Command                                      | What it does                                                                                                                         | Where it runs                                 |
| -------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------ | --------------------------------------------- |
| `poll [--prune] [--keep-days N] [--dry-run]` | Takes one reading, upserts `border_current_waits`, and exits. Exit 1: CBP unreadable; 2: Supabase unset; **3: a database write failed** | `border-poll`, every 10 min (`--prune` daily) |
| `selfcheck [--json]`                         | Asks the live feed whether it still parses: fields, ports, status words, scraped pages, and the mirror check. Exits 1 on any problem | `border-poll`, daily                          |
| `serve [--port 8088]`                        | The local HTTP pull surface (below)                                                                                                  | Local only, like `mcp_server.py`              |
| `chat [--from-file F --at 21:40]`            | The DM experience in a terminal                                                                                                      | Local                                         |
| `demo [--live] [--no-color]`                 | Five scripted scenes, identical every run                                                                                            | Local                                         |
| `simulate [--from-file F \| --url U]`        | A fake Instagram that records every Graph call it would make                                                                         | Local                                         |

Each command is also its own module (`python -m scraper.border.poll`, …).

---

## Using it from Python

The primary integration path. Every answer is a coroutine, like the rest of the engine,
and the feed can share a caller's `HttpClient`:

```python
from scraper.core.http import HttpClient
from scraper.border.service import BorderFeed

async with HttpClient() as http:
    feed = BorderFeed(http=http)
    caption = (await feed.waits())["caption_es"]
    reply = (await feed.bridge("paso-del-norte", "car"))["reply"]
    alerts = (await feed.drops(below=30, since=last_cursor))["drops"]
```

One CBP reading is reused for 5 minutes, so a post, a reply and an alert sweep share it.
`ranked_in`, `best_in` and `bridge_in` answer over a snapshot you already hold, with no I/O.

## The local HTTP surface

`python -m scraper.border serve` answers the same questions over HTTP. It is a
development affordance, not production infrastructure — this system's integration
contract is the database — but it is how the fake Instagram tests the pull end to end.

| Request                                          | Answers with                                                                                  | Used for                            |
| ------------------------------------------------ | --------------------------------------------------------------------------------------------- | ----------------------------------- |
| `GET /waits`                                     | All six bridges ranked fastest-first, `best` per lane, plus `caption_es` / `caption_en`       | Posts                               |
| `GET /waits/{port}?lane=car`                     | Minutes, movement, crossing time, a faster alternative, and a ready `reply`                   | Replies                             |
| `GET /mine?ports=240202,240201&lane=car_sentri`  | Only their saved bridges, in their lane                                                       | Their "puentes", scheduled messages |
| `GET /best?lane=car`                             | The fastest open bridge right now                                                             | "¿Cuál puente?"                     |
| `GET /drops?below=30&lane=car&since=…`           | Lanes that crossed under 30 minutes, as events with an `id`, plus a `cursor` for the next ask | Drop alerts                         |
| `GET /health`                                    | Last fetch state, cache age, whether storage is on, `source_problems`                         | Monitoring                          |
| `POST /crossings` `{"port", "lane", "reporter"}` | Starts a real crossing and freezes what every source says                                     | "Voy a cruzar"                      |
| `POST /crossings/{id}/done`                      | Records how long it actually took                                                             | "Ya crucé"                          |
| `GET /accuracy?days=30`                          | Each source's error against real crossings                                                    | Deciding whom to trust              |

`{port}` accepts a port number (`240202`), a slug (`paso-del-norte`), or a loose name
(`zaragoza`). Lanes: `car`, `car_sentri`, `car_ready`, `walk`, `walk_ready`, `truck`,
`truck_fast`. `lang` is `es` (default) or `en`.

## Built for someone who crosses daily

Every open lane carries the three facts that decide "leave now or wait":

| Field                     | Means                                                                                                                                                     |
| ------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `minutes`                 | What CBP says                                                                                                                                             |
| `delta`                   | Change since the previous reading: `{change: -28, direction: "down", meaningful: true}`. Rendered as `↓28`, and only when the change is 5 minutes or more |
| `crosses_at`              | The clock time they would get across — the number people actually plan around                                                                             |
| `alternative`             | A bridge at least 10 minutes faster, or `null`                                                                                                            |
| `closes_at`               | When that bridge shuts, or `null` for the 24-hour ones                                                                                                    |
| `closes_before_you_cross` | True when the bridge shuts before they would reach the booth                                                                                              |

A post leads with the answer rather than a table:

```
Puentes Juárez–El Paso · 1:30 p. m.
Más rápido ahora: Jerónimo–Santa Teresa 20 min · cruzas 1:50 p. m.

Autos
Santa Teresa 20 min
Paso del Norte 38 min
Puente Libre 50 min
Zaragoza cerrado

SENTRI: Zaragoza 1 · Paso del Norte 5 · Lerdo 5
Peatones: Zaragoza 5 · Puente Libre 5 · Santa Teresa 5

Sin datos de CBP: Tornillo
Datos de CBP, actualizados 1:00 p. m.
```

A regular gets two lines instead of thirty, in the lane they actually use:

```
Tus puentes · SENTRI
Paso del Norte: 5 min · cruzas 1:35 p. m.
```

## Cost of an answer

There is no local database of wait times: every answer traces back to a live read, so
the work is spent on doing that rarely and cheaply.

Measured 2026-09-29 against the live feed, on the engine's async `HttpClient`:

|                                                                 |                               |
| --------------------------------------------------------------- | ----------------------------- |
| Cold read (CBP + both scraped sites, concurrently; median of 3) | ~985 ms                       |
| `waits()` from cache                                            | ~0.6 ms                       |
| One `bridge_in()` over a snapshot                               | ~0.007 ms                     |
| CBP payload                                                     | 93 KB raw → **9 KB gzipped**  |
| Scraped page                                                    | 86 KB raw → **15 KB gzipped** |

The cold read includes a fresh `HttpClient` and pasosfronterizos' one robots.txt check
per process; a feed that shares a caller's client skips both.

What makes that hold:

- **gzip on both sources.** Neither sends `ETag` or `Last-Modified`, so conditional GET
  is not available; compression is the win that is. CBP's own headers say
  `no-cache, no-store`, so caching has to be ours.
- **One reading serves everything.** A post, a reply, a scheduled message and an alert
  sweep inside the same 5 minutes share a single upstream read.
- **Sources are read at the same time.** CBP and both scraped sites go out together
  (`asyncio.gather`), so total latency is the slowest of them, not the sum.
- **Single-flight.** Several callers arriving together wait on one refresh (an
  `asyncio.Lock`) rather than starting their own.
- **Derived rows are computed once per reading.** One `/waits` asked for the same lane
  92 times before this; with storage enabled each of those could have been a database
  round trip for the previous value.
- **Answers carry `Cache-Control` and `Age`**, so a polite client can skip asking until
  the next reading is due.
- **Each source has its own window**, because they move at different speeds:

  | Source             | Window | Widens to (quiet) | Widens to (rush) |
  | ------------------ | ------ | ----------------- | ---------------- |
  | `cbp`              | 5 min  | 20 min            | 10 min           |
  | `pasosfronterizos` | 4 min  | 10 min            | 6 min            |

  Rush is 5–9 AM and 3–7 PM local, every day rather than weekdays only — Sunday
  evening northbound is as heavy as any Tuesday morning. A window widened at 2 PM
  tightens the moment the peak starts, because the ceiling is re-checked on read
  rather than trusted from write time.

  A window widens by 5 minutes every time a source repeats itself exactly, and snaps
  back the moment a number moves — so a quiet border costs few calls and a changing one
  is still caught promptly. `/health` reports each window, its age and how many
  unchanged reads it has seen.

- **Unchanged readings are not re-written.** Every row would collide on `content_hash`
  anyway, so the run is logged as `unchanged` and the write skipped.
- **`GET /waits?lane=car`** trims the response from ~31 KB to ~16 KB when the pipeline
  only posts one lane.

### A failed refresh does not mean silence

There is no local history to fall back on, so losing CBP for a minute used to mean the
pipeline had nothing to post. A last-good reading is now served for up to 30 minutes
after a failed refresh, flagged rather than disguised:

```json
{
  "ok": false,
  "serving_stale": true,
  "last_error": "OSError: connection reset"
}
```

Past that grace period, or with nothing cached at all, it fails loudly as before. The
pipeline can decide: post the older number with its timestamp, or say nothing.

## Where the numbers come from

CBP is authoritative for **structure** — the only source covering all six ports, every
lane type, hours and closures. Its weakness is **freshness**: it republishes about once
an hour. Other sites refresh in minutes, and when they disagree with CBP that
disagreement is information rather than noise.

| Source             | Covers                                                                 | Refresh     | Role                                     |
| ------------------ | ---------------------------------------------------------------------- | ----------- | ---------------------------------------- |
| `cbp`              | 6 ports, 7 lane types, hours, closures                                 | ~1 hour     | Structure and closures                   |
| `pasosfronterizos` | 4 bridges (PDN, Stanton, BOTA, Ysleta), car / SENTRI / Ready / walking | ~8 min      | Fresher minutes                          |
| `borderswaittime`  | 4 bridges, car lanes                                                   | mirrors CBP | Checks **our** parsing, never the answer |

### The mirror earns its place by checking us

`borderswaittime` republishes CBP verbatim, which makes it useless as a second opinion
and valuable as a regression test against production: when a mirror of CBP disagrees
with _our_ reading of CBP, the bug is ours. `/health` reports it:

```json
"parser_check": {"compared": 4, "skipped_other_update": 0, "mismatches": []}
```

It is marked `independent=False`, so it can never change an answer — only raise a flag.
Only readings of the **same CBP update** are compared: both sides must carry the stamp
(`At 7:00 pm MDT`) and it must match. The mirror lags CBP — overnight it reads "Update
pending", with no stamp, for lanes CBP already has numbers for — so anything else is
counted as `skipped_other_update`, not reported as a bug. Overnight that is all four.
This is the check that would have caught the ahead-of-clock stamps and the `"At Noon"`
format before either reached a post.

**Counting sources is not the same as corroboration.** Most border sites re-publish the
CBP feed verbatim — borderswaittime.com returns "At 6:00 pm MDT, 43 min delay", the
exact CBP values. Three such sites agreeing is one source agreeing with itself, so a
source carries `independent=False` and never counts toward agreement.

Reconciliation, in `consensus.py`, runs three rules in order:

1. **Closures follow CBP.** It is the operator's own feed. A scraped page showing
   minutes for a lane CBP calls closed is usually stale, not a discovery.
2. **Minutes follow freshness.** Among sources that agree a lane is open, the newest
   reading wins.
3. **Correlated sources do not corroborate.** Only independent sources vote.

Every answer carries `consensus` with the agreement (`single` / `agree` / `conflict` /
`closed`), the chosen source, the spread, and every opinion for audit. `cbp_minutes`
travels alongside the shown number so any difference is visible.

**A disputed lane is ranked on its worst case.** If CBP says 20 and another site says
58, the bridge is ranked as a 58-minute bridge, so an undisputed one wins unless it is
worse than that worst case. Captions then show the range rather than the optimistic
end — `Puente Libre 20–58 min` — because the one mistake worth engineering against is
promising 20 minutes to someone who then sits for 58.

A disputed lane also loses confidence and says so to the reader:

```
Puente Libre (Córdova–Américas) · Autos: 58 min · cruzas 7:50 p. m.
pasosfronterizos, hace 8 min
Las fuentes no coinciden (cbp 20, pasosfronterizos 58); usamos la más reciente.
```

A source that breaks is recorded in `/health` and skipped; the feed still answers from
CBP alone.

### Adding a source

Implement `Source` in `src/scraper/border/sources/`, set `independent` honestly, list the ports a
healthy page always shows in `expected_ports`, and add it to `SOURCES`. A scraper whose
page changed shape parses to nothing rather than raising; `expected_ports` turns that
into `empty` or `partial (… missing 240201)` in `/health` and a failed selfcheck,
instead of a cheerful `ok (0 readings)`. It can only refine minutes for lanes CBP already knows — no source can add
or remove a bridge.

Worth investigating next: **TTI's BCIS** (Bluetooth and RFID sensors at the bridges) is
the only genuinely independent _measurement_ in the region rather than another copy of
CBP. Its public site exposes no data endpoint, so it needs a conversation, not a
scraper.

### How a bridge gets recommended

The smallest number is not the best bet. Two adjustments turn "what CBP published"
into "what to expect on arrival", and both are visible in the response so a person can
argue with them:

| Adjustment     | Rule                                  | Why                                                          |
| -------------- | ------------------------------------- | ------------------------------------------------------------ |
| Age            | +8 min per hour of age, capped at +25 | An hour-old number is a worse bet than a fresh one           |
| Trend, rising  | +0.6 × the increase, capped at +15    | A line that just grew is likely still growing                |
| Trend, falling | −0.3 × the decrease, capped at −15    | Shrinking is trusted half as much: over-promising costs more |

A stamp CBP dated in the future hides the real age, so it counts as 45 minutes old
rather than brand new — otherwise the least trustworthy reading would rank as the
freshest.

Ranking uses the adjusted figure in 5-minute bands, and inside a band the fresher
reading wins. Messages still show CBP's actual number; only the ordering is modelled.
`effective_minutes`, `score` and `confidence` (high / medium / low) ride along on every
open lane.

```
Bridge of the Americas    CBP  37 min  →  expect 43  (age +6, trend +0)  medium
Paso del Norte            CBP  43 min  →  expect 49  (age +6, trend +0)  medium
```

### Alerts fire once per crossing

A wait bouncing 28, 31, 29, 32 around a 30-minute limit would alert on every poll. Once
an alert fires, that lane stays quiet until the wait climbs back to the limit plus 5
minutes, which re-arms it for the next genuine drop.

An alert is an event, not a message waiting to be collected:

- **Asking never consumes it.** Pass the `cursor` from the previous answer as `since`
  and you get only what is newer, including drops that happened while you were away.
  Without `since` you get the alerts that still describe the current reading — the
  same answer every time, so dedupe on `id`. Two pipeline workers, or someone checking
  by hand, can no longer take an alert from each other.
- **It survives a restart.** Fired alerts and their re-arm state live in
  `border_alerts`. The `id` comes from the port, lane, limit and the reading that
  crossed, so writing it twice is a no-op. Before this, a deploy resent the last drop,
  because the previous wait survived in the database and "already sent" did not.

### What else a reply carries

Five things that change what someone actually does, beyond the number itself:

|                                      |                                                                                                                                                                                                                                                                                  |
| ------------------------------------ | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **Movement**                         | "La fila subió 30 min en la última hora" — from readings taken over the last 90 minutes. Today's movement, not history, and only mentioned past 10 minutes of change                                                                                                             |
| **A faster lane at the same bridge** | Paso del Norte was 50 min by car and 1 min by SENTRI. Card lanes are offered as a condition (_"Si tienes SENTRI"_), walking as a fact (_"A pie son 10 min"_), since anyone can park and walk                                                                                     |
| **Hours instead of minutes**         | 75 minutes reads as `1 h 15`. People plan in hours once a wait passes one                                                                                                                                                                                                        |
| **Quiet hours**                      | `/drops` reports `quiet_hour` between 11 PM and 5 AM so the pipeline can hold an alert until morning                                                                                                                                                                             |
| **Normal for this hour**             | "Un lunes a esta hora suele estar en 25 min: hoy 23 min más." The median for that bridge, lane, weekday and hour over 8 weeks of our own readings (`border_typical_waits`). Needs 3 separate days, leaves today out, and is mentioned only when today is 10 minutes (or 25%) off |

Movement survives a restart: a fresh process seeds its 90-minute window from
`border_readings`, so the first reply after a deploy can still say the line is growing.

### Closing times

Santa Teresa runs 6 AM–10 PM and Stanton 6 AM–midnight. At 9:40 PM a 40-minute wait at
Santa Teresa means arriving after it shuts, so it drops out of `best`, sinks in
`ranked`, and its reply says so:

```
Jerónimo–Santa Teresa · Autos: 40 min — cierra a las 10:00 p. m., no alcanzas
Mejor Paso del Norte (Santa Fe): 48 min.
```

That is the one case where a slower bridge is the right answer, so the "must save 10
minutes" rule is dropped for it.

### Names people actually type

`libre`, `el puente libre`, `zaragoza`, `santa fe`, `pdn`, `lerdo` all resolve, and so
do phone typos — `sarragoza`, `tornilo`, `stantn`, `pasodelnorte`, `santa fee`. A word
that fits every bridge (`puente`) and anything too far from a real name (`pizza`)
return nothing, so the pipeline asks instead of sending someone to the wrong bridge.

### Every kind of message

`scraper/border/requests.py` reads a DM the way it is typed — accents or none, capitals,
"!!", emoji, Spanish or English, the key words anywhere in the line — and is meant to
be reused by whatever answers the DMs. `classify(message)` returns what they want,
which bridges, which lane, what limit and which language:

| Message | Kind | Bridges | Lane | Limit |
|---|---|---|---|---|
| `avísame cuando zaragoza baje de 20` | alert | Zaragoza | — | 20 |
| `AVISAME cuando el LIBRE baje de media hora!!` | alert | Puente Libre | — | 30 |
| `avisame cuando zaragoza o lerdo baje de 15` | alert | Zaragoza, Lerdo | — | 15 |
| `alert me when santa teresa is under 10` | alert (en) | Santa Teresa | — | 10 |
| `ya no me avises de zaragoza` / `alto` | cancel | Zaragoza / all | | |
| `¿cuál es el puente más rápido a pie?` | best | | walking | |
| `cuanto esta el libre a pie` / `zaragoza sentri` | bridge | one | walking / SENTRI | |
| `es mejor zaragoza o lerdo` / `pdn vs bota` | bridge (compare) | two | | |
| `guardar zaragoza y lerdo` | save | two | | |
| `voy a cruzar zaragoza` / `ya crucé` | crossing start / done | | | |
| `hola` · `puentes` · `ayuda` · `gracias` · `👍` | menu · menu · help · thanks · thanks | | | |

How it reads them:

- **Kind** — phrases checked in a fixed order, first match wins: cancel before alert
  ("no me avises" contains "avises"), crossing-done before crossing-start, and a named
  bridge before "which is fastest" ("¿es más rápido zaragoza?" is about Zaragoza).
- **Bridges** — intent words, lane words, numbers and filler ("cuando", "baje", "how",
  "puente") are removed, the rest is split on "o" / "y" / "or" / "and" / "vs", and each
  part goes through the same lookup as every other bridge question. Gibberish is offered
  to that lookup and finds nothing, so it gets "no encuentro ese puente" and the list.
- **Limit** — "20", "20min", "<20", "veinte", "media hora", "un cuarto de hora", "1h30",
  "hora y media", "an hour", "half an hour". A figure above 300 is a port number, not a
  limit. Default 30.
- **Language** — words only one language uses are counted; a tie keeps the account's
  language, so a bare "zaragoza" is answered in Spanish.

The fake pipeline shows the rules around it: with no bridge named, an alert uses the
person's first saved bridge (and saved lane unless they named one) and otherwise asks;
saving or crossing with no bridge asks; an unknown bridge, or a lane that bridge lacks
(SENTRI at the Puente Libre), gets the feed's own reply and files nothing; the same
alert asked twice is filed once; each subscription only hears about its own bridge.

Before this, alerts were filed as Paso del Norte whatever was named, "gracias" got "no
encuentro ese puente", "¿cuál puente está más rápido?" was not understood, and a
question about a bridge ignored the lane it named.

## Reading the answers

`state` is `open`, `closed`, or `no_data`. **Show `minutes` only when `state` is
`open`** — closed and no-data lanes carry `null`, and rendering that as `0` tells
someone the bridge is clear when we do not know.

Two flags travel with every lane: `stale` (CBP has not refreshed in 90 minutes) and
`stamp_ahead_of_clock` (CBP labelled the coming hour early). Captions never quote a
future stamp as "updated".

## When the feed misbehaves

Every upstream read goes through the engine's `core/http.py` `HttpClient`: it retries
`{429, 500, 502, 503, 504}` and transport errors with exponential backoff capped at 30s,
honours a numeric `Retry-After`, negotiates gzip, and identifies itself with the
engine's `USER_AGENT`. Both scraped sites were confirmed live to serve their full page
to that honest agent (2026-09-29), so no browser user-agent is sent.

`/health` also catches the quieter failure, CBP answering happily with numbers that
stopped moving:

```json
{
  "ok": false,
  "degraded": true,
  "frozen": true,
  "frozen_hours": 7.0,
  "all_bridges_dark": false,
  "unchanged_since": "2026-09-22T03:40:00+00:00"
}
```

`frozen` means every lane has been identical for 6 hours; `all_bridges_dark` means no
bridge is reporting at all. Either one sets `degraded`, which is the signal to stop
posting rather than keep publishing stale numbers as fresh.

## Keeping it honest in production

| Command                              | What it is for                                                                                                                                                                                                                                                                                                                                        |
| ------------------------------------ | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `python -m scraper.border selfcheck` | Asks the live feed and fails loudly if a field, a port, a lane group or a status word changed. Unit tests run on saved responses, so they stay green through exactly that kind of drift. Exits 1 on any problem, `--json` for a monitor                                                                                                               |
| `python -m scraper.border poll`      | Records one reading and exits. No `--loop`: GitHub Actions is the only scheduler here, so a missed run shows in the Actions UI. `--prune` applies retention after the reading (run it daily); `--keep-days` sets how many days of readings stay (60); `--dry-run` writes nothing. A last-good reading served after a failure counts as a failure here |

`selfcheck` also fails when a scraped site parses to nothing, loses a bridge, or a CBP
mirror disagrees with our parsing of the same update. A site that is simply down is
noted, not failed.

Captions are capped at 2,000 characters — below Instagram's 2,200 limit, with room for
the pipeline's own hashtags. If a caption ever ran long, the compact lane lines are
dropped before the headline.

The scraper asks `robots.txt` through the engine's `HttpClient.can_fetch` before its first
fetch and disables itself if it is disallowed; `DISABLED_SOURCES=pasosfronterizos` switches
it off by name, the same switch the engine's registry honours. An unreachable `robots.txt`
counts as allowed, as crawlers conventionally
treat it.

## Database

`supabase/migrations/0013_border_waits.sql` — apply it like every other migration:
`python -m scraper.apply_migration supabase/migrations/0013_border_waits.sql`.

| Table             | Holds                                                                                                      |
| ----------------- | ---------------------------------------------------------------------------------------------------------- |
| `border_ports`    | The registry CBP omits: names, Spanish names, aliases, pins. Seeded by the migration                       |
| `border_readings` | One row per (port, lane, CBP update). `content_hash` UNIQUE, so polling between CBP updates writes nothing |
| `border_runs`     | Per-port counts per run, so "CBP reported nothing" is distinguishable from "our poll broke"                |

`0014_border_retention.sql` adds `border_prune_readings(keep_days)` (a rolling window —
nothing reads beyond a few days), a partial index for the "last open reading" lookup,
and `border_recent_activity`, an hourly view where an empty row means the poller
stopped.

`0015_border_alerts_crossings_typical.sql` adds:

|                                         |                                                                                                                                                                                                 |
| --------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `border_alerts`                         | Fired drop alerts, keyed by a deterministic id; `rearmed_at` stays NULL while the lane is still inside its band                                                                                 |
| `border_crossings`                      | Real crossing times: joined the line, cleared the booth, and every source's claim at the start. `reporter` is an opaque id — hash it in the pipeline                                            |
| `border_typical_waits(weeks, min_days)` | Median and p75 wait per port, lane, local weekday and hour                                                                                                                                      |
| `border_prune(...)`                     | One retention job: readings 60 days (the typical-wait baseline looks back 8 weeks, so 0002's 30-day default would starve it), runs 14 days, re-armed alerts 30 days. Crossings are never pruned |

`0016_border_current_waits.sql` adds **`border_current_waits`**, the crossing-times table
the knowledge base reads (GitHub Actions → Supabase → Google Sheet → GoHighLevel). One row
per bridge × lane, upserted by `border.poll` on every run, every 10 minutes:

| Column | Holds |
|---|---|
| `bridge_es` / `bridge_en`, `lane_es` / `lane_en` | "Zaragoza–Ysleta" / "Ysleta–Zaragoza", "Peatones" / "Walking" |
| `state` | `open`, `closed` or `no_data` |
| `wait_es` / `wait_en` | "38 min", "1 h 15", "cerrado" / "closed", "sin datos" / "no data" — never empty |
| `summary_es` / `summary_en` | "Paso del Norte (Santa Fe) · Autos: 48 min según CBP (revisado el 22 sep 2026, 1:40 p. m.)." |
| `minutes`, `cbp_minutes`, `source` | What a follower is told (the fresher of CBP and pasosfronterizos), CBP's own figure, and where it came from |
| `lanes_open`, `cbp_updated_at`, `checked_at` | Lanes open, CBP's stamp, and when this row was last written |

Every lane CBP lists gets a row on every run, open or not, so a lane never silently
drops out of the knowledge base. The lanes agreed on 2026-09-29 are all covered:

| Bridge | Lanes |
|---|---|
| Puente Libre (Bridge of the Americas) | all traffic, Ready Lane, walking, trucks (plus walking Ready, truck FAST) |
| Centro (Paso del Norte) | all traffic, SENTRI, Ready Lane, walking (plus walking Ready) |
| Lerdo (Stanton) | SENTRI (the car lane is listed and usually closed) |
| Zaragoza (Ysleta) | all traffic, SENTRI, Ready Lane, walking (plus trucks) |

"Walking SENTRI" was dropped: neither CBP nor pasosfronterizos publishes a walking SENTRI
figure (CBP's pedestrian lanes are standard and Ready only).

A Sheet should read the text columns. The numeric ones may be NULL (a closed lane has no
minutes), and GoHighLevel rejects a row with any blank cell.

Additive only, `if not exists` throughout, RLS enabled with zero policies on every table
(deny-all for anon; the pipeline reads with the service-role key).

Nothing in Chisme's own tables is touched — no `events`, `venues`, `trends`, `runs`,
`ig_posts`, `ig_post_metrics` or `ig_post_edits`.

Storage is optional. With `SUPABASE_URL` and `SUPABASE_KEY` unset (the engine's own
settings), `BorderStore` disables itself and the feed still answers, the same way
`core/storage.py` degrades. It uses the engine's Supabase client, pushed to a thread.

## Chat with it

`python -m scraper.border chat` is the DM experience in a terminal: you type what a follower
would type, and the reply comes from the same code the pipeline calls.

```
  Tú → santa teresa
  Chisme → Jerónimo–Santa Teresa · Autos: 40 min — cierra a las 10:00 p. m., no alcanzas
           Mejor Paso del Norte (Santa Fe): 48 min.
```

Try: `puentes`, a bridge name (misspelled is fine), `guardar zaragoza sentri` then
`puentes` again, `avísame cuando zaragoza baje de 30`, `¿cuál puente está más rápido?`,
`es mejor zaragoza o lerdo`, `alert me when bota is under 20`, `gracias`, `alto`. Commands: `/post`, `/idioma`,
`/estado`, `/ayuda`, `/salir`.

It reads live CBP by default. For a rehearsal that cannot surprise you, replay saved
data and pick the hour:

```bash
python -m scraper.border chat --from-file tests/border/fixtures/cbp_feed.json --at 21:40
```

That is the 9:40 PM case, where the fastest bridge closes before you would reach it.

## Demo

`python -m scraper.border demo` walks five scenes in about twenty seconds: the daily post, a
conversation (misspelled, in Spanish), a bridge that shuts before you would reach it,
a drop alert, and the two ways CBP fails.

It runs on saved responses with a fixed clock, so it is identical every time and does
not depend on what the border is doing during the meeting. `--live` uses the real feed
for the first two scenes; `--no-color` is for slides and screenshots.

## Simulating Instagram

`python -m scraper.border simulate` plays both sides with no Meta credentials and no network to
Meta: `FakeGraph` records the calls it _would_ make, `FakePipeline` drives a daily post,
a conversation, a scheduled message and an alert sweep.

```bash
python -m scraper.border simulate --from-file tests/border/fixtures/cbp_feed.json --out calls.json
python -m scraper.border simulate --url http://127.0.0.1:8088     # test the HTTP surface
```

It prints every recorded call (`POST …/media`, `…/media_publish`, `…/messages`) so the
payloads can be reviewed before anything real is wired up.

### The closest thing to production

Two processes, the pull going over real HTTP:

```bash
python -m scraper.border serve --port 8088                           # terminal 1
python -m scraper.border simulate --url http://127.0.0.1:8088          # terminal 2
```

Everything is real here except Meta: live CBP, real HTTP, a separate client process.
Only the Graph calls are recorded instead of sent.

## What the CBP feed does

Found by running against it, not by reading docs:

- **Tornillo returns no data.** Every lane, every check since Sep 18. It appears with
  `has_live_data: false` and is named in captions under "Sin datos de CBP".
- **Timestamps can be ahead of the clock.** At 9:40 PM, CBP stamped Bridge of the
  Americas "At 10:00 pm".
- **Noon and midnight are words**: `"At Noon MDT"`, not `"At 12:00 pm MDT"`.
- **`delay_minutes` is a string**, and missing values are `""` or `"N/A"`, never null.
- **Status casing is inconsistent**: `"no delay"` but `"Lanes Closed"`.
- **`crossing_name` is empty for port 240221**, which is why the registry exists.
- **Ports close**: Stanton runs 6 AM–midnight, Santa Teresa 6 AM–10 PM.

## Wiring it in

Each is a small change to an existing file, deliberately left for review. Nothing
below has been done.

1. **Apply the migrations**, in order:
   ```bash
   python -m scraper.apply_migration supabase/migrations/0013_border_waits.sql
   python -m scraper.apply_migration supabase/migrations/0014_border_retention.sql
   python -m scraper.apply_migration supabase/migrations/0015_border_alerts_crossings_typical.sql
   python -m scraper.apply_migration supabase/migrations/0016_border_current_waits.sql
   ```
   Or paste each file, in that order, into the Supabase dashboard's **SQL Editor** and run it.
   Do this **before** merging: `border-poll` starts on its schedule once the workflow
   is on `main`, and until the tables exist every run exits 3 and goes red.
2. **Nothing to add for scheduling.** `.github/workflows/border_poll.yml` is new and
   reads the existing `SUPABASE_URL` / `SUPABASE_KEY` secrets. GitHub only runs a
   scheduled workflow from `main`, so it starts once this branch is merged.
3. **Point the Sheet automation at `border_current_waits`**, reading the text columns
   (`summary_es` is written to be quoted as it stands).
4. **Expose it to the local agent** — two lines in `mcp_server.py` add `border_waits`,
   `border_bridge` and `border_health`:
   ```python
   from .border.mcp_tools import register as register_border_tools
   register_border_tools(mcp)
   ```
5. **Decide how wait times reach Instagram.** Every piece exists — a caption capped
   under Instagram's limit, replies, alerts with cursors — but not the choice of
   format: a new `ig_posts.kind` (which means widening `ig_posts_kind_check`, verified
   against the live constraint name as `0010` did), or a line in the existing digest.
   Drop alerts only fire when something calls `drops()`; the poller does not. Parse
   alert DMs with `scraper.border.requests` and keep only each subscriber's bridge, as
   `FakePipeline._subscribe` and `alert_sweep` do.

## Known gaps

- **Southbound waits.** CBP publishes northbound only, and no public feed covers the
  other direction. Half of a daily crosser's trips are invisible to us.
- **`border_subscriptions`.** Who wants which bridge, lane and alert limit is not
  stored here; that belongs with whatever answers the DMs.
- **Holiday and payday effects.** Real, and guessing dates without data would be worse
  than saying nothing.
- **TTI BCIS sensor data.** The only independent _measurement_ rather than another copy
  of CBP. No public endpoint; it needs a conversation.
- **`save_readings` asks for the inserted rows back** to count them. A `count=exact`
  with `return=minimal` would be lighter, but how PostgREST counts ignored duplicates
  has not been observed against the live database, so it was not changed blind.

We can tell which source is fresher, never which is right. Settling the 20-vs-58
disagreements needs ground truth, and it now has somewhere to go: a follower sends
"voy a cruzar paso del norte" when they join the line and "ya crucé" past the booth.
Every source's claim is frozen at the first message, and `/accuracy` reports each
source's mean error, bias (positive = overstates the wait) and share within 10 minutes.
A source is marked `enough_data` after 10 crossings; before that, treat it as anecdote.
Crossings under a minute or over six hours are refused as mistakes.

---

_Verified against commit `8f3107c` (2026-09-29). Last updated 2026-09-29._
