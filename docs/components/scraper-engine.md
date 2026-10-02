# Component: Scraper Engine

The data-acquisition pipeline. Owns `src/scraper/core/`, `src/scraper/sources/`, `mcp_server.py`,
`scheduler.py`, and the `backfill_*.py` repair scripts. Everything downstream — the website, the
carousel, the knowledge-base sheet — consumes what this writes.

Package version `0.1.0`; distribution name is `scraper-mcp`, not "chisme".

---

## At a glance

| | |
|---|---|
| **Entry points** | `python -m scraper.scheduler [all\|events\|trends]`, `python -m scraper.mcp_server` |
| **Runs on** | GitHub Actions, Python 3.12, daily 11:00 UTC (`scheduled-scrape`) |
| **Writes** | `events`, `venues`, `trends`, `runs` |
| **Hard requirement** | None. Without Supabase it still returns live results; it just persists nothing |
| **Tests** | `tests/sources/` (23) + `tests/core/` (68) — see [testing](../engineering/testing.md) |

---

## Domain models (`core/models.py`)

Pydantic v2. Sources emit these; the orchestrator assigns `content_hash` before persisting, so
**sources never compute their own hash**.

### `Kind`

`EVENTS` | `TRENDS` | `WEB`. Both the request type and the source-selection key —
`registry.sources_for(kind)` filters on `Source.kind` identity.

### `SearchParams`

One flexible request object; each source reads only the fields it cares about.

| Field | Default | Notes |
|---|---|---|
| `kind` | required | which source family runs |
| `query` | `None` | free-text topic/keywords |
| `location` | `None` | e.g. `"El Paso, TX"` |
| `start_date` / `end_date` | `None` | inclusive window |
| `categories` | `[]` | **accepted and logged, but no source reads it.** Treat as a no-op today |
| `platforms` | `[]` | subset filter for trends sources; also the opt-in gate for the Meta sources |
| `timeframe` | `"week"` | `day` / `week` / `month`, source-interpreted |
| `limit` | `50` | final truncation cap |
| `force_refresh` | `False` | bypass the freshness cache |

### `Event`

`source`, `source_id`, `title` (required), `description`, `start_time`, `end_time`, `venue`,
`location`, `lat`, `lng`, `url`, `image_url`, `categories[]`, `ticket_links[]`, `raw`, `content_hash`.

Two fields deserve emphasis:

- **`start_time` / `end_time`** are timezone-aware and event-local after `_localize_times`. This is
  [the invariant](../architecture/invariants.md#event-timestamps-are-timezone-aware-and-expressed-in-the-events-local-zone).
- **`raw`** is the untouched provider payload and is **load-bearing**: every backfill script re-derives
  corrected values from it. Do not trim it.

`TicketLink` is `{source, label, url}` — one real event scraped from several ticketing sites becomes
one row with several ticket links rather than several cards. `label` comes from
`ticket_labels.ticket_label(url)`, which maps by **domain** rather than source name, because a single
source module (`events_web`) fetches Eventbrite, Meetup, Visit El Paso and La Nube alike.

`Trend` and `Document` are the trends and web-research equivalents. Documents are returned to the
caller and **never persisted** — the typed tables only cover events and trends.

---

## The orchestrator (`core/orchestrator.py`)

The one path every caller goes through. **Adding a source never touches this file.**

`run(params) -> dict`:

| Step | Detail |
|---|---|
| 1 | Construct `Storage()` — a no-op if Supabase is unconfigured |
| 2 | Freshness cache check, unless `force_refresh`. A hit returns immediately |
| 3 | Open one shared `HttpClient`, fan out via `_gather` |
| 4 | `sources_for(kind)` is imported lazily — keeps core import cheap |
| 5 | `asyncio.gather` over every eligible source — all at once, bounded only by the shared semaphore |
| 6 | **Failure isolation**: `_fetch_one` catches per source and returns `([], SourceResult(ok=False))` |
| 7 | Events: type filter → `_is_showable` → `_localize_times` → hash + dedupe → chronological sort → truncate → `upsert_events` |
| 8 | Trends: dedupe → sort by score desc → truncate → `upsert_trends` |
| 9 | Web: dedupe by normalised URL → truncate. Not persisted |
| 10 | The whole normalise/persist block is itself wrapped; on exception `items=[]`, `status="error"` |
| 11 | `log_run(...)` → `runs` table |

Returns `{count, cached, items, sources, sources_ok, sources_failed, status, error}`.

### Helpers worth knowing

- **`_is_showable`** drops an event with no date **and** no venue/location — it could not appear on the
  list (no "when"), the map (no "where") or search. These are parsing artifacts, e.g. a directory
  listing's stray link text mistaken for an event.
- **`_event_sort_key`** sorts undated events last and normalises naive/aware datetimes so comparison
  never raises.
- **Sort order is chronological, not source-registration order** — *"otherwise a source that returns
  lots of events crowds out other sources before storage."*
- **`run_research(query, depth, limit)`** is the separate web-research path. `depth` controls how many
  results get full-text extraction: `shallow` 0, `standard` 5, `deep` up to 10.

### The freshness cache is coarser than the request

`_try_cache` passes only `location` + `limit` for events, and `query` + `limit` for trends.
`start_date`, `end_date` and `categories` are **ignored**. `fresh_events` filters only on `last_seen`
and a location `ilike` — it does not filter `status` or restrict to upcoming events. A cache hit also
returns **raw DB dicts**, not `Event` dumps, so the `items` shape differs between a cached and an
uncached response.

**Use `force_refresh=True` whenever the date window matters.** The scheduler always does.

---

## Source contract

```python
class Source(ABC):
    name: str            # unique — the allow/deny-list key and the stored Event.source
    kind: Kind
    def is_configured(self) -> bool: ...          # False if keys/deps missing → skipped
    async def fetch(self, params, http) -> list[...]: ...   # raise on hard failure
```

Registration is a hand-maintained list in `registry._MODULES`. `sources_for(kind)` applies three gates
in order: right kind → `settings.source_allowed(name)` → `is_configured()`. An exception from
`is_configured()` is caught and treated as not-configured.

Full procedure for adding one: [ADR-0001](../architecture/adr/0001-source-connector-pattern.md#adding-a-source).

`status()` backs the `source_status` MCP tool and is the fastest answer to "what is running?":

```bash
python -c "from scraper.sources.registry import status; [print(s) for s in status()]"
```

---

## Source catalogue

13 `Source` instances across 11 modules.

### Events

| Name | Keyless | Keys | What it fetches |
|---|---|---|---|
| `events_ticketmaster` | no | `TICKETMASTER_API_KEY` | Discovery API v2, paginated. **The primary events provider.** |
| `events_web` | yes | — | Eventbrite / Meetup / Ticketmaster public city pages via JSON-LD, a `site:`-scoped DuckDuckGo supplement, plus dedicated Visit El Paso and La Nube crawls |
| `events_directories` | yes | — | 18 first-party El Paso and Juárez calendars |

**`events_ticketmaster`.** `_PAGE_SIZE = 200` (the API maximum), `_MAX_PAGES = 5` (it refuses deep
paging past ~1000 items). Trusts Ticketmaster's own segment/genre classification and only falls back to
`guess_categories`. Two past data-loss bugs now fixed and regression-tested:

- **Single-request fetch was silently dropping the furthest-out events.** Results are date-ascending
  and `size` caps at 200 — measured for El Paso on 2026-09-03, a 200-day window returned 187 on one
  page while a 240-day window returned 215 across two.
- **`images[0]` is not the best image.** The same artwork ships at a dozen sizes in no useful order.
  For one event, `images[0]` was a 305×203 thumbnail while a 2048×1365 original sat further down —
  and the thumbnail then failed the carousel's quality gate, so the event lost its slide for want of a
  photo the provider had all along. `backfill_ticketmaster_images.py` repairs stored rows.

**`events_web`.** Its module docstring is the coverage map. Only platforms *empirically verified* to
serve crawlable JSON-LD to a plain non-JS scraper **and** allow it in `robots.txt` are included;
allevents.in, 10times, bandsintown, seatgeek and dice.fm are intentionally skipped because a keyless
fetch gets nothing from them. Sends a browser User-Agent because several sites return 200 to a browser
UA and block unknown agents — every fetch is still robots-gated.

Two fragile spots the code itself flags: the Visit El Paso and La Nube description selectors are
**Bootstrap utility classes** (`div.mb-5`, `section.event-detail .mt-3`) that match several elements,
so every match is tried in document order and the first containing `<p>` tags wins; and Eventbrite /
Meetup full text is fished out of `__NEXT_DATA__` by key search *"without hardcoding its exact nesting,
which shifts between a site's deploys."* **When descriptions suddenly go short, suspect these first.**

Two more Eventbrite-specific misses, found via a live report of one dropped event (2026-09-20) and now
regression-tested in `tests/sources/test_events_web_time.py`, both of which silently produced a
`start_time`/`end_time` of local midnight rather than an error:

- **`_walk_for_events`'s `@type` match missed schema.org's `Festival` and `Hackathon` subtypes.** The
  old check was a substring test (`"event" in x.lower()`), which matches `MusicEvent` but not
  `Festival` — a real, fully-populated JSON-LD block (correct start/end times) was simply never looked
  at. Fixed by adding `_NON_EVENT_SUFFIXED_TYPES = frozenset({"festival", "hackathon"})` alongside the
  substring check, not instead of it.
- **`_is_detail_url` only recognized Eventbrite's `-tickets-<id>` URL shape.** A free/RSVP listing uses
  `-registration-<id>` instead, so the date-only-time revisit (the mechanism that's supposed to replace
  a bare listing date with the detail page's real hour) never even fetched the page. Fixed by widening
  the regex to `-(?:tickets|registration)-\d+`.

Both bugs hit the same symptom for unrelated reasons, which is itself a lesson: a repeated symptom
across independent code paths is worth checking for more than one root cause before calling it fixed.

**`events_directories`.** Keyless and polite: robots-checked before every fetch, bounded page counts,
and a site with no crawlable markup simply yields nothing rather than blocking the run.

| Region | Directories |
|---|---|
| El Paso (10) | Visit El Paso, El Paso Live, City of El Paso, El Paso County, Southwest University Park, UTEP Special Events, Lowbrow Palace, El Paso County Coliseum, RockHouse, AXS El Paso |
| Juárez (8) | Don Boletón, Boletia, Ticketmaster MX search, Visita Juárez, Juárez municipal, UACJ agenda, Cultura Chihuahua, YOSIVOY |

Parsing is hybrid: generic JSON-LD first, then per-site dispatch for sites with no markup. Region
filtering is the subtlest part — see
[ADR-0005](../architecture/adr/0005-region-restriction-everywhere.md#the-juárez-false-positive--fixed-three-times).

One directory-specific rule: `city_of_el_paso_events` must **not** get the generic detail crawl,
because its regex extraction only matches the listing page's flattened anchor text and re-running it on
each detail page always yields nothing.

### Trends

| Name | Keyless | Keys | Gate |
|---|---|---|---|
| `trends_reddit` | no | `REDDIT_CLIENT_ID`, `REDDIT_CLIENT_SECRET` | opt-out |
| `trends_hackernews` | yes | — | opt-out |
| `trends_google` | yes | — (needs the `trends` extra) | opt-out |
| `trends_youtube` | no | `YOUTUBE_API_KEY` | opt-out |
| `social_instagram` | no | `IG_ACCESS_TOKEN`, `IG_BUSINESS_ACCOUNT_ID` | **opt-in only** |
| `social_threads` | no | `THREADS_ACCESS_TOKEN`, `THREADS_USER_ID` | **opt-in only** |

`trends_google` self-disables when `pytrends` is absent, checked with `importlib.util.find_spec` —
*"cheap check — don't import pandas here."* The endpoint is unofficial and rate-limited, so runtime
errors are isolated by the orchestrator.

The two Meta sources can only read **your own account** — that is an API limitation, not a code one.
No global Instagram or Threads search exists. They are opt-in because own-account data is irrelevant to
an arbitrary topic request.

### Web research

| Name | Keyless | Keys |
|---|---|---|
| `web_ddg` | yes | — |
| `web_tavily` | no | `TAVILY_API_KEY` |
| `web_brave` | no | `BRAVE_API_KEY` |
| `local_news_feeds` | yes | — |

Three separate search sources rather than one with fallbacks, *"so one failing (or being
rate-limited) never sinks the others."*

`local_news_feeds` covers nine borderland outlets (El Paso Times, KVIA, KTSM, KFOX14, El Paso Matters,
El Heraldo de Juárez, El Diario de Juárez, Norte Digital, Puente Libre), each with several candidate
feed URLs tried in order until one yields entries.

---

## Cross-cutting core modules

### `http.py` — the shared client

| Concern | Behaviour |
|---|---|
| Retries | 3 by default, on `{429, 500, 502, 503, 504}` plus transport/timeout errors |
| Backoff | `min(2 ** attempt, 30.0)`; honours `Retry-After` when numeric, clamped to 30s |
| Concurrency | One semaphore, default 8, **shared across the whole fan-out**; released during backoff sleeps |
| robots.txt | Per-origin, memoised, and **fail-open** — unreachable `robots.txt` means allowed |
| Secrets | `redact_secrets()` over credential query params |

`post_json` exists separately because `raise_for_status` hides Meta's actual reason: the error body
carries bad scope / unreachable image / expired container, so it is surfaced rather than swallowed.

The redaction comment is one of the most valuable in the repo — the regex value class is deliberately
**narrow** rather than "anything up to a delimiter", because a greedy class eats the colon and Meta's
error body with it, *"redacting away the very explanation the message exists to carry."*

### `config.py` — settings

`load_dotenv()` at import; real environment variables still win. `Settings` is a **flat snapshot read
once at import time** (`@lru_cache`), so mutating `os.environ` afterwards has no effect — tests
monkeypatch attributes on the object instead.

Empty values are normalised: `_clean` maps `""` → `None`, `_int` and `_bool` fall back to their
defaults on empty or unparsable input. This is what defends against the
[GitHub Actions empty-string trap](../operations/configuration.md#the-empty-string-trap).

`source_allowed(name)`: the denylist (`DISABLED_SOURCES`) wins first; then, **only if the allowlist is
non-empty**, membership in `ENABLED_SOURCES` is required. So an unset allowlist means all sources pass.

### `storage.py` — persistence and the cache

Degrades to a complete no-op when Supabase is unconfigured. The Supabase client is synchronous, so
calls are pushed to threads. **Every public method early-returns when disabled, and write failures are
caught and logged, never raised** — *"never let storage break a run."*

Upsert keys: `events` and `trends` on `content_hash`; `venues` on `address_hash`.

Write order in `upsert_events`: venues first (so event rows can reference them; a venue failure leaves
`venue_id` as `None` rather than breaking the event upsert) → build rows → cross-run merge → upsert.

Read methods, and which to use:

| Method | Filters `status`? | Use for |
|---|---|---|
| `query_events` | **No** | The MCP `query_stored` tool only |
| `query_events_for_day` / `_for_range` | Yes (`approved`) | Anything public-facing |
| `query_upcoming_events` | Yes | The KB export — paginated, uncapped |
| `fresh_events` / `fresh_trends` | No | The freshness cache |

`query_events_for_day`'s docstring is the best single explanation of why these are separate siblings
rather than flags, and it is required reading before you add a read path — see
[invariants §3](../architecture/invariants.md#a-public-facing-read-must-filter-status-itself).

**Cross-run merge — now two lanes.** Only rows with a `start_time` are candidates; the window is built
from **local** midnights, because *"a 7pm show is 01:00Z the next day, so a UTC-midnight window would
drop every evening event on the last day of the range."*

- **Lane 1 (unchanged):** exact `venue_id` match. Fast, and correct whenever both sources resolved to
  the same stored venue row.
- **Lane 2 (`_find_cross_venue_duplicate`, new):** for whatever lane 1 didn't claim, a second pass using
  the shared `is_same_stored_event()` predicate from `dedupe.py` (below) — same local day, folded-title
  similarity ≥ 0.95, ≥ 3 meaningful title tokens, same coarse city, and **exactly one** candidate match
  (ambiguity aborts to a duplicate rather than guessing). This is what catches two sources naming the
  *same building* differently, e.g. "El Paso Live" vs. "Abraham Chavez Theatre" — a case lane 1 can
  never see because the `venue_id`s are genuinely different rows.

Both lanes route through the same `_apply_merge`, which unions ticket links and categories and
backfills only absent `description` / `image_url` / `end_time`, never touching `start_time`, `url`,
`title`, `id` or `content_hash` — which is why the backfill scripts exist. Lane 2 deliberately ignores
which record's `start_time` is picked up as the base row's own — if the two sources disagree on start
time, the loser's value is recorded to `raw["_merged_alt_start_times"]` and logged as a warning rather
than silently discarded, so a real scheduling conflict between sources stays visible.

A kill switch, `DEDUPE_CROSS_VENUE` (default on), disables lane 2 entirely if it ever needs to be turned
off in production without a deploy.

### `dedupe.py` — identity and near-duplicate collapse

`content_hash` = `sha1` of the URL when present, else `title|YYYY-MM-DD|venue`. Two confidence tiers
for in-batch fuzzy matching, both requiring the **same non-null local calendar day** first:

- title similarity ≥ 0.9 alone, or
- title token overlap ≥ 0.8 **and** venue similarity ≥ 0.6.

Stopwords are stripped bilingually, because raw character similarity is actively misleading here:
*"Salsa Night" vs "Bachata Night" scores **higher** (0.67) than genuine same-event pairs like
"Machetes - World Tour 2026" vs "Machetes Live in Concierto" (0.54)*, since short titles sharing filler
words dominate the ratio.

The guiding asymmetry, from the handoff: **a false merge silently hides a real event, which is worse
than an unmerged duplicate card.**

**Stable identity under arrival order.** `_merge_into` used to keep whichever record was field-richer
and never recompute `content_hash` — which meant a cluster's identity depended on *which source
happened to be reachable that run*. If `events_web` failed on Monday, only the directories copy existed
and stored under its hash; if both succeeded Tuesday, `events_web`'s copy was richer, got a *different*
hash, and a second row was inserted for the same real event. One flaky scrape was enough to double a
row. `_merge_into` now sets `content_hash = min(kept.content_hash, dup.content_hash)` — `min()` is
commutative and associative, so a cluster converges on the same hash regardless of arrival order or
which source contributed the richer fields. **This stabilizes the hash across *richness and order*, not
across *which sources are reachable at all* — a genuinely new source joining the cluster for the first
time still computes its own candidate hash and needs the cross-venue merge lane (see `storage.py`
above) to actually catch it as the same event, not `min()` alone.**

**Shared duplicate predicate.** `fold()` (bilingual, accent-stripping, stopword-aware) and
`is_same_stored_event()` are the single implementation of "is this candidate the same real-world event
as that stored row", used identically by `storage._find_cross_venue_duplicate` and
`backfill_merge_duplicates --cross-venue` — previously these were two independently-maintained,
gradually-diverging copies (`storage.py` vs. `backfill_merge_duplicates.py`). One surprising detail:
`fold()` turns an apostrophe into a **space**, not nothing — `"Lowe's"` → `"lowe s"` (two tokens). Code
that folds a name and then does substring/set matching must account for that, or a chain-name match
(see [social-pipeline.md](social-pipeline.md#editorial-council-localnesspy-editorpy-clarifypy-criticpy))
silently fails.

`is_same_stored_event()`'s gates, all required: same local calendar day; folded-title similarity ≥ 0.95
(higher than the in-batch 0.9, since this crosses venues and needs more confidence); **≥ 3 meaningful
title tokens** — the guard that stops "Karaoke Night" or "Yoga in the Park" from ever cross-venue
merging, since a 1–2 token title can't carry enough signal to be sure; same coarse city (`city_of()`);
and **exactly one** candidate match, with ambiguity aborting to "treat as a duplicate row" rather than
guessing which of several is the real match.

### `eventtime.py` + `timeutil.py`

`eventtime` owns the timestamp invariant — `event_tz()`, `to_event_local()`, `local_day()`. Read
[ADR-0002](../architecture/adr/0002-local-timestamp-invariant.md).

`timeutil` is unrelated and trends-only: `day`/`week`/`month` → seconds, plus `since_datetime`,
`since_epoch` (for Hacker News) and `since_iso` (for YouTube).

### `geocode.py`

Nominatim (OpenStreetMap): keyless, requires a real identifying User-Agent and at most 1 req/s — both
honoured, with a process-global 1.1s throttle. **Synchronous**, so it is only ever called from inside
`asyncio.to_thread`.

Two safeguards, because *"a confident wrong pin is worse than no pin"*:

- **A bounding box** covering the El Paso / Juárez / Las Cruces catchment. Anything outside is rejected,
  and every hit is re-checked.
- **Progressive query simplification** — cleaned address, then suffix/suite noise stripped, then the
  numbered half of a `"Landmark / 123 St"` string, then street+city+state, then street+city, and only
  finally the venue name anchored to a city.

Three rejection rules: virtual events never get a pin; a city-only address is refused because it
geocodes to the city centroid and *"would scatter unrelated venues onto one bogus downtown pin"*; and
the name-anchored fallback only injects a city when there is real signal.

The cache key is the **full `(address, venue)` pair**, because a hit can come from the name-anchored
candidate and keying on the address alone could hand one venue's coordinates to another.

### Smaller modules

| Module | Purpose |
|---|---|
| `categorize.py` | Keyword category guessing from the **title only** — descriptions are marketing boilerplate whose incidental words produce false positives. Multi-label on purpose. Default `"Community"`. Writes `events.categories`, the website's taxonomy — **untouched** by, and unrelated to, the Instagram-only `content_tags.py` classifier documented in [social-pipeline.md](social-pipeline.md#content-pillars-corecontent_tagspy) |
| `address.py` | `format_address` — appends a `city, region postal` tail **only if** it is not already inside the street line, because some listings repeat the city in `streetAddress`. Also owns `venue_identity()` — see below |
| `media.py` | `clean_image_url` — rejects relative paths and placeholder markers. Meetup embeds relative fallback-graphic paths in its JSON-LD which would resolve against *our* domain and 404 |
| `ticket_labels.py` | URL host → display label, 22 mappings, longest-first |

---

## Venue identity (`core/address.py`)

The write-side counterpart to `dedupe.py`'s duplicate-detection: `venue_identity()` normalizes a venue's
name + address into the pair `storage._address_hash` upserts on, so two sources describing the same
building converge on one venue row instead of drifting into near-duplicates that then defeat both the
per-venue diversity cap and cross-venue event merging.

`normalize_address()` — NFKD accent-stripping (`café` / `cafe` compare equal), lowercase, drop a
trailing country token (`, US` / `, Mexico`), map leading English number-words to digits (`normalize_venue_name`
handles this too — **the actual "One Civic Center Plaza" vs "1 Civic Center Plaza" case** that was
silently producing two venue rows for the same address), canonicalize street/directional abbreviations
(`St`/`Street`, `Ave`/`Avenue`, `N`/`North`, …), strip a ZIP+4 suffix down to five digits, and remove
punctuation — **except** a suite/unit marker (`ste`, `suite`, `#`), which is deliberately preserved: the
failure mode of over-merging suite tokens is conflating two different tenants in one building onto one
venue page, which is an acceptable annoyance, versus under-merging, which hides an event.

`normalize_venue_name()` does the equivalent folding for the venue's display name alone, used by
`dedupe.py`'s `fold()` and by `social/selection.py`'s name-first `venue_key` (still name-keyed on
purpose — see [social-pipeline.md](social-pipeline.md#selection-selectionpy) for why an aggregator
brand like "El Paso Live" needs name-keying regardless of how good the address normalizer gets).

**Verified, not test-pinned, against the TypeScript port.** `web/src/lib/hash.ts` re-implements the same
normalization line-for-line for the submission form's client-side venue matching. Byte-parity across 12
hand-picked real-world cases (accents, ZIP+4, number-words, suite markers) was verified manually during
this rollout, but **no committed automated test pins Python/TypeScript equivalence going forward** — see
[known-gaps.md](../known-gaps.md).

`backfill_merge_venues.py` (below) is the one-time convergence sweep; `_address_hash` computing the new
identity on every future write is what keeps new rows from drifting again.

---

## MCP tools (`mcp_server.py`)

Thin wrappers over the orchestrator, exposed over stdio for a local agent. Not deployed.

| Tool | Cached? |
|---|---|
| `search_events(location, start_date?, end_date?, categories?, query?, limit?, force_refresh?)` | Yes, `FRESHNESS_HOURS` |
| `find_trends(topic?, platforms?, timeframe?, limit?, force_refresh?)` | Yes |
| `research_topic(query, depth?, limit?)` | **Never** |
| `query_stored(kind, location?, topic?, platform?, since?, until?, limit?)` | Pure DB read, no scrape |
| `source_status()` | n/a |

Note `search_events` parses date strings leniently — an unparsable date becomes `None` **silently**.
And `query_stored` does not filter moderation status; see
[invariants §3](../architecture/invariants.md#a-public-facing-read-must-filter-status-itself).

---

## Scheduler (`scheduler.py`)

The curated recurring jobs. `python -m scraper.scheduler [all|events|trends]`, default `all`.

| Env | Shape | Default |
|---|---|---|
| `SCHEDULE_LOCATIONS` | **semicolon**-separated (commas appear inside a location) | `"El Paso, TX;Ciudad Juarez, Chihuahua, Mexico"` |
| `SCHEDULE_LOCATION` | singular back-compat override; **loses** to the plural | — |
| `SCHEDULE_TOPICS` | comma-separated | `"AI,technology,business"` |
| `SCHEDULE_DAYS` | int look-ahead | `7` in code; **raised to 240 via repo variable** |

`run_events()` makes **one orchestrator pass per location**, `limit=400`, `force_refresh=True`. That
per-city design fixed a real coverage hole: both event sources scope which calendars they hit to the
requested location, so a single "El Paso, TX" run never invoked the Juárez directories at all — they
were reachable but never actually called.

> **Trap:** a leftover `SCHEDULE_LOCATION` repo variable silently collapses the job back to one city
> and kills Juárez coverage. If Juárez goes empty, check that variable first.

None of these appear in `.env.example` — they are documented only in the module docstring and here.

---

## Backfill and maintenance scripts

All are `python -m scraper.<module>`, all support `--dry-run`, and all but `apply_migration` need
`SUPABASE_URL` / `SUPABASE_KEY`.

| Script | Repairs | Idempotent? |
|---|---|---|
| `apply_migration` | Nothing — runs one `.sql` file over raw Postgres. Needs `SUPABASE_DB_URL` | Depends on the SQL; see [migrations](../data/migrations.md) |
| `backfill_categories` | Upgrades single-guess categories to multi-category. Skips Ticketmaster (real data) and refuses to downgrade | Yes |
| `backfill_event_timezones` | The naive-local-stored-as-UTC shift. Re-derives from each row's own `raw` with today's parser — **does not guess an offset** | Yes, and conservative: skips disagreements > 1 day |
| `backfill_full_descriptions` | Re-fetches full descriptions, and for Visit El Paso / La Nube the real outbound link | Yes |
| `backfill_geocode` | Fills `venues.lat/lng` where NULL. `--repair` **nulls out** coordinates today's rules would never produce | Fill pass yes; `--repair` is **deliberately destructive** |
| `backfill_merge_venues` | Converges duplicate venue rows onto `venue_identity()`'s normalized `address_hash`: repoints `events.venue_id`, **then** deletes losers, **then** rewrites the keeper's hash — order is load-bearing since `venue_id` is an FK with no `on delete` | Re-runnable but **destructive — always `--dry-run` first** |
| `backfill_merge_duplicates` | Merges already-stored duplicates and **DELETES the losers**. `--cross-venue` extends this to the lane-2 predicate in `dedupe.py`; also repoints `ig_posts.event_ids` before deleting a loser row, closing a pre-existing gap | Re-runnable but **destructive — always `--dry-run` first** |
| `backfill_missing_event_times` | Date-only rows stored at local midnight: visits the detail page for the real hour. Recovers **both** `start_time` and `end_time` independently — each gated on its *own* field having been date-only in the original `raw` payload, since a real end time can legitimately land after midnight and must not be "fixed" by an hour heuristic | Yes, network-dependent |
| `backfill_ticketmaster_images` | Repoints rows at the largest artwork in `raw.images` | Yes, no-op when already correct |
| `backfill_content_tags` | Runs the rule-based content-pillar baseline (`content_tags.py::pillars_for`) over existing rows, IG-only, never touches `categories` | Yes, narrow `WHERE` so re-runs no-op |

Two of these explain **why they must exist at all**, and both point at the same limitation:
`_apply_merge` only ever backfills a *missing* field, so a truncated description or a midnight start
time **never self-heals** from re-scraping, no matter how often the event is seen.

---

## Gotchas

Ranked by how much time they are likely to cost you.

1. **Do not fix timezones in a connector.** Emit naive wall-clock;
   `orchestrator._localize_times` handles it. [ADR-0002](../architecture/adr/0002-local-timestamp-invariant.md)
2. **`events_web._dt` deliberately throws away an offset that disagrees with the region** — and
   deliberately exempts `+00:00`. The most surprising code in the repo, and correct. Read
   `events_web.py:73-105` before touching it.
3. **Never group stored events by UTC date.** Use `eventtime.local_day`.
4. **`_apply_merge` never overwrites.** Hence the backfills.
5. **`query_events` does not filter `status`, and the client is service-role.** Unmoderated `/submit`
   rows come back.
6. **The freshness cache ignores your date window.** Pass `force_refresh=True` when it matters.
7. **A failed upsert is invisible to the caller.** `run()` can say `status: "ok"` while nothing reached
   Postgres. Check the logs.
8. **`settings` is an import-time snapshot.** Monkeypatch the object in tests, not the environment.
9. **An unset GitHub Actions `vars.*` is an empty string.** The config helpers normalise it back to the
   code default — which means the real risk is silently getting a default that is wrong for production.
   See [configuration](../operations/configuration.md#the-empty-string-trap).
10. **A bare "Juárez" is not Ciudad Juárez**, and an event's **description** is never a regional signal.
11. **Caps are politeness limits, not correctness limits** — and raising one can starve another
    consumer. [ADR-0007](../architecture/adr/0007-horizon-venue-cap.md)
12. **Two CSS selectors and one `__NEXT_DATA__` key search are one site redesign from breaking**, and
    the code knows it. Suspect them when descriptions go short. Two real Eventbrite misses of this
    general shape (type-substring match, URL-shape match) already happened once — see the `events_web`
    section above.
13. **`core/` now has real tests** (`tests/core/`, 68 of them) for venue identity, dedupe stability and
    the cross-venue duplicate predicate — but they're new, and changes to dedupe thresholds or the merge
    paths should still be validated against production data with `--dry-run` backfills before trusting
    them alone.
14. **`min()`-stabilized `content_hash` only survives arrival order and field richness, not a new
    source appearing.** See the "Stable identity under arrival order" note in `dedupe.py` above — a
    genuinely new source for an existing event still needs the cross-venue merge lane to be caught.
15. **Identity-hash changes need a "converge → deploy → scrape → converge again" rollout.** Enabling
    normalized venue identity produced 97 duplicate venue rows on the very first live scrape after
    deploy — stale hashes from rows written between the backfill and the deploy. Expected, not a
    regression: the cross-venue merge lane still caught every resulting event-duplicate attempt (zero
    duplicate posts), but the venue table itself needs a second `backfill_merge_venues` +
    `backfill_merge_duplicates` pass afterward to fully converge.

---

*Verified against commit `7629204` (2026-09-20). Last updated 2026-09-20.*
