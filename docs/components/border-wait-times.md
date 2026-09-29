# chisme-border

> **Built, not yet wired in.** All of it is new files — `src/scraper/border/`,
> `tests/border/`, migrations `0013`–`0015` and this page — and nothing existing was
> edited. The hooks it still needs (a workflow, an MCP tool, a caption in the social
> pipeline) are listed at the end.

Live CBP border wait times for the six El Paso–Juárez ports, shaped for the Chisme
Instagram pipeline to **pull** from. No app, no UI, no scheduler of our own.

Python 3.12+, standard library only. Nothing to install.

## Run

```bash
pytest tests/border -q                        # 207 tests
python -m scraper.border.devtools.chat        # type messages, get replies
python -m scraper.border.devtools.demo        # five-scene demo, safe to run in a meeting
python -m scraper.border.devtools.fake_instagram   # simulate Instagram against live CBP
python -m scraper.border.api --port 8088      # the pull surface (run next to the pipeline)
python -m scraper.border.selfcheck            # does the live feed still look like the feed we parse?
python -m scraper.border.poll --loop 300      # record a reading every 5 min, prune daily
```

Uses the repo's editable install (`pip install -e ".[dev]"`); no extra setup.

## What the pipeline pulls

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

One CBP read is reused for 5 minutes, so the pipeline can ask as often as it likes.

To import it directly instead of over HTTP:

```python
from border.service import BorderFeed
feed = BorderFeed()
feed.waits()["caption_es"]
feed.bridge("paso-del-norte", "car")["reply"]
feed.drops(below=30)["drops"]
```

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

|                                                 |                               |
| ----------------------------------------------- | ----------------------------- |
| Cold read (CBP + the scraped site, in parallel) | ~700 ms                       |
| `/waits` from cache                             | ~2 ms                         |
| One bridge lookup from cache                    | ~0.02 ms                      |
| CBP payload                                     | 93 KB raw → **9 KB gzipped**  |
| Scraped page                                    | 86 KB raw → **15 KB gzipped** |

What makes that hold:

- **gzip on both sources.** Neither sends `ETag` or `Last-Modified`, so conditional GET
  is not available; compression is the win that is. CBP's own headers say
  `no-cache, no-store`, so caching has to be ours.
- **One reading serves everything.** A post, a reply, a scheduled message and an alert
  sweep inside the same 5 minutes share a single upstream read.
- **Sources are read at the same time.** Total latency is the slowest source, not the
  sum of them.
- **Single-flight.** Several pipeline requests arriving together wait on one fetch
  rather than starting their own.
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
"parser_check": {"compared": 4, "mismatches": []}
```

It is marked `independent=False`, so it can never change an answer — only raise a flag.
Only readings of the **same CBP update** are compared (`At 7:00 pm MDT` on both sides):
the mirror is cached longer than CBP, and right after CBP publishes the two describe
different hours. Those are counted as `skipped_other_update`, not reported as bugs.
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

Deltas and drop alerts read the previous value from memory first, then from
`border_readings`. That database fallback is what makes them survive a restart —
without it, the first sweep after a deploy silently reports nothing.

## Reading the answers

`state` is `open`, `closed`, or `no_data`. **Show `minutes` only when `state` is
`open`** — closed and no-data lanes carry `null`, and rendering that as `0` tells
someone the bridge is clear when we do not know.

Two flags travel with every lane: `stale` (CBP has not refreshed in 90 minutes) and
`stamp_ahead_of_clock` (CBP labelled the coming hour early). Captions never quote a
future stamp as "updated".

## When the feed misbehaves

`fetch` retries on `{429, 500, 502, 503, 504}` and transport errors — three attempts,
exponential backoff capped at 30s, honouring a numeric `Retry-After`. A 404 is not
retried. One flaky moment should not cost a post.

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

| Command                       | What it is for                                                                                                                                                                                                                                                                                                                                    |
| ----------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `python -m scraper.border.selfcheck` | Asks the live feed and fails loudly if a field, a port, a lane group or a status word changed. Unit tests run on saved responses, so they stay green through exactly that kind of drift. Exits 1 on any problem, `--json` for a monitor                                                                                                           |
| `python -m scraper.border.poll` | Records one reading. `--loop 300` keeps going, backing off while the feed is failing, and applies retention at start and once a day; `--prune` does it after a single reading (for cron); `--keep-days` sets how many days of readings stay (60); `--dry-run` writes nothing. A last-good reading served after a failure counts as a failure here |

`selfcheck` also fails when a scraped site parses to nothing, loses a bridge, or a CBP
mirror disagrees with our parsing of the same update. A site that is simply down is
noted, not failed.

Captions are capped at 2,000 characters — below Instagram's 2,200 limit, with room for
the pipeline's own hashtags. If a caption ever ran long, the compact lane lines are
dropped before the headline.

The scraper reads `robots.txt` before its first fetch and disables itself if it is
disallowed; an unreachable `robots.txt` counts as allowed, as crawlers conventionally
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

Additive only, `if not exists` throughout, RLS enabled with zero policies on every table
(deny-all for anon; the pipeline reads with the service-role key).

Nothing in Chisme's own tables is touched — no `events`, `venues`, `trends`, `runs`,
`ig_posts`, `ig_post_metrics` or `ig_post_edits`.

Storage is optional. With `SUPABASE_URL` and `SUPABASE_SERVICE_KEY` unset, `Storage`
disables itself and the feed still answers, the same way `storage.py` degrades in the
scraper engine.

## Chat with it

`python -m scraper.border.devtools.chat` is the DM experience in a terminal: you type what a follower
would type, and the reply comes from the same code the pipeline calls.

```
  Tú → santa teresa
  Chisme → Jerónimo–Santa Teresa · Autos: 40 min — cierra a las 10:00 p. m., no alcanzas
           Mejor Paso del Norte (Santa Fe): 48 min.
```

Try: `puentes`, a bridge name (misspelled is fine), `guardar zaragoza sentri` then
`puentes` again, `avísame cuando baje de 30`, `alto`. Commands: `/post`, `/idioma`,
`/estado`, `/ayuda`, `/salir`.

It reads live CBP by default. For a rehearsal that cannot surprise you, replay saved
data and pick the hour:

```bash
python -m scraper.border.devtools.chat --from-file tests/border/fixtures/cbp_feed.json --at 21:40
```

That is the 9:40 PM case, where the fastest bridge closes before you would reach it.

## Demo

`python -m scraper.border.devtools.demo` walks five scenes in about twenty seconds: the daily post, a
conversation (misspelled, in Spanish), a bridge that shuts before you would reach it,
a drop alert, and the two ways CBP fails.

It runs on saved responses with a fixed clock, so it is identical every time and does
not depend on what the border is doing during the meeting. `--live` uses the real feed
for the first two scenes; `--no-color` is for slides and screenshots.

## Simulating Instagram

`scraper.border.devtools.fake_instagram` plays both sides with no Meta credentials and no network to
Meta: `FakeGraph` records the calls it _would_ make, `FakePipeline` drives a daily post,
a conversation, a scheduled message and an alert sweep.

```bash
python -m scraper.border.devtools.fake_instagram --from-file tests/border/fixtures/cbp_feed.json --out calls.json
python -m scraper.border.devtools.fake_instagram --url http://127.0.0.1:8088     # test the HTTP surface
```

It prints every recorded call (`POST …/media`, `…/media_publish`, `…/messages`) so the
payloads can be reviewed before anything real is wired up.

### The closest thing to production

Two processes, the pull going over HTTP exactly as Carlos' pipeline will do it:

```bash
python -m scraper.border.api --port 8088                           # terminal 1
python -m scraper.border.devtools.fake_instagram --url http://127.0.0.1:8088   # terminal 2
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

## What is deliberately not here

- **Southbound waits.** CBP publishes northbound only, and no public feed covers the
  other direction. Half of a daily crosser's trips are invisible to us. This is a
  research question — the Chihuahua bridge trust has cameras but no numbers.
- **`border_subscriptions`.** Whether we store each person's bridge, lane, time and
  limit depends on whether Carlos' pipeline already does.
- **Holiday and payday effects.** Real, and guessing dates without data would be worse
  than saying nothing.
- **TTI BCIS sensor data.** The only independent _measurement_ rather than another copy
  of CBP. No public endpoint; needs a conversation.

We can tell which source is fresher, never which is right. Settling the 20-vs-58
disagreements needs ground truth, and it now has somewhere to go: a follower sends
"voy a cruzar paso del norte" when they join the line and "ya crucé" past the booth.
Every source's claim is frozen at the first message, and `/accuracy` reports each
source's mean error, bias (positive = overstates the wait) and share within 10 minutes.
A source is marked `enough_data` after 10 crossings; before that, treat it as anecdote.
Crossings under a minute or over six hours are refused as mistakes.

## Open questions for Carlos

- HTTP or a direct Python import?
- Does the pipeline store each person's bridge, lane, time and limit, or should we?
- What exact fields does a post need beyond the caption?
- Where does the pipeline run, so this can run next to it?

## Wiring it in

Nothing below is done yet; each is a small change to an existing file, left for review.

- Apply `0013`–`0015` with `python -m scraper.apply_migration`.
- Schedule the poller from `.github/workflows/` — GitHub Actions is the only scheduler here.
- Expose the feed to the local agent as an MCP tool in `mcp_server.py`.
- Decide how a wait-time caption reaches Instagram (a new `ig_posts.kind`, or a line in the digest).
