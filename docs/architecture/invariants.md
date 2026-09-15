# Invariants

The rules this system depends on. Each one was learned from a real failure, and most are enforced in
code — but enforcement usually lives in *one* place, which is exactly why the rule needs writing
down: the next person to add a source, a query, or a slide layout is the person who can break it.

**Read this before your first change.** Nearly every production incident in this project's history
was a violation of something below.

Each entry gives the rule, the incident behind it, where it is enforced, and how it fails. Note that
failures here are overwhelmingly *silent* — wrong data, not exceptions.

---

## 1. Time

### Event timestamps are timezone-aware and expressed in the event's local zone

Not UTC. Not naive. Both properties at once: aware means the absolute instant is right; local means
`.date()` and `.hour` are the local calendar day and the local wall-clock hour — which is what
dedupe keys on, what the day-bounds query filters by, and what the flyer prints.

**Incident.** Most event listings publish a naive wall clock (`"2026-08-25T18:30"`). Written to a
`timestamptz` column, Postgres read it as UTC, so an 8:00 PM show was stored as 20:00Z and rendered
back six hours early. **343 stored events claimed to start between 1am and 5am.** Meanwhile
Ticketmaster reports a genuine UTC instant, so `.date()` on one of its evening shows returned
*tomorrow* — and the same concert from both kinds of source landed on two different days, defeating
dedupe and putting both copies on one carousel.

**Enforced at** `orchestrator._localize_times` (`orchestrator.py:50-60`) — centrally, on every
event, for exactly this reason: *"Done here rather than in each connector so a new source cannot
reintroduce the naive-local-stored-as-UTC bug."* Backstop at `storage._iso` (`storage.py:28-36`).
Definition and API in `core/eventtime.py`.

**So:** emit **naive wall-clock** datetimes from a connector and let the orchestrator normalise.
Do not "fix" timezones inside a source. Full story in [ADR-0002](adr/0002-local-timestamp-invariant.md).

### Never group or compare stored events by their UTC date

A 7pm Mountain show comes back from Postgres as 01:00 the next day. Grouping on that date files an
evening event and its own duplicate at the same venue under two different days, and they never get
compared.

**Enforced at** `eventtime.local_day()` — use it. Three separate call sites re-derive the local day
and each carries a version of the warning: `storage.py:737-739`, `storage.py:789-791`,
`backfill_merge_duplicates.py:72-78`.

**Breaks as** duplicate event cards that no amount of re-scraping merges.

### A scraped offset that contradicts the region is discarded — except a UTC one

An Eventbrite show on Dyer St in El Paso is published as `2026-08-28T20:00:00-05:00` with
`timezone: America/Chicago`, because the offset follows the *organiser's account setting*, not the
building. Honouring it moves a customer-facing 8:00 PM to 7:00 PM, an hour out of step with the
ticket page we link to.

But `+00:00` is exempt: *"A UTC stamp is a deliberate normalization, not a local-time claim."*
Meetup publishes `"2026-08-30T13:00:00.000Z"` for a 7am El Paso hike — a real instant, which
re-reading as a wall clock would move to 1pm. **Only an offset that asserts a local zone can be
asserting the wrong one.**

**Enforced at** `events_web._dt` (`events_web.py:73-105`) — deliberately *not* in
`to_event_local`, because it must not touch API sources: Ticketmaster returns genuine UTC, and
discarding a non-Mountain offset there would corrupt every one of its timestamps.
Locked by `tests/sources/test_events_web_time.py:29-58`.

### The publish sweep's cron window must straddle midnight UTC

A 17:00 Denver deadline is 23:00 UTC in summer but **00:00 UTC the next day** in winter. A window
ending at 23:59 covers summer and silently misses every winter post — the sweep would not run again
until 14:00 UTC the next day, by which point the staleness guard expires the row unpublished.

**Enforced at** `ig_daily.yml:23` (`0,30 13-23,0-2 * * *`) and, unusually, **by a test that parses
the YAML**: `tests/social/test_scheduling.py:78-90` asserts the hour set equals `{13..23} ∪ {0,1,2}`.
Note nothing in CI runs that test.

### Do not use `%-d`, `%-I`, or `%#I` in any format string

They are platform-specific (glibc vs MSVC) and this code must run identically on CI (Linux) and
local dev (Windows). Every 12-hour clock and ordinal date in the repo is hand-rolled for this
reason: `notify.py:55-62`, `caption.py:234-240`, `render.py:734-744`, `kb/rows.py:72-84`.

---

## 2. Identity and data

### Qualify every Juárez match

A bare `juarez` / `juárez` match is not a Ciudad Juárez match. "Benito Juárez" is a Mexico City
borough and a street, avenue and plaza name across the whole country — including one in the
Chihuahua *state capital*, nowhere near the border.

**This false positive has shipped three times in three different layers:**

| Layer | Fix |
|---|---|
| Geocoding | `geocode.looks_like_ciudad_juarez` + `_OTHER_MX_CITY_RE` veto (`geocode.py:78-100`) |
| Source-side region matching | `events_directories._event_matches_directory_region` requires one of 11 qualified spellings (`events_directories.py:208-249`) |
| Web queries | `CITY_PATTERNS.juarez` lists only qualified forms (`web/src/lib/events.ts:17-20`) |

**So:** any new matching logic must require `ciudad juárez`, `cd. juárez`, `juárez, chih`, or a
Chihuahua qualifier. Never the bare word.

### Never use an event's description as a regional signal

National touring-show listings mention Ciudad Juárez in an "also playing in…" blurb even when the
event on the page is in a different city entirely. A hard negative on the **place fields** always
wins, regardless of what the free-text blurb says (`events_directories.py:170-195`).

### `address_hash` has three implementations that must stay byte-identical

`sha1(lower(trim(address)) + "|" + lower(trim(venue_name)))`, hex-encoded:

| Language | Location |
|---|---|
| Python | `core/storage.py:39-45` |
| SQL | `supabase/migrations/0002_venues.sql:9-12` |
| TypeScript | `web/src/lib/hash.ts:12-15` |

**Breaks as** the browser's submit form creating a duplicate venue row for a venue that already
exists, or the 0002 backfill diverging from every subsequent scraper upsert. There is **no test
pinning their equivalence** — that is a known gap.

### A URL is an event's identity when it has one

`content_hash` is `sha1(url)` when a URL exists, else `sha1(title|date|venue)`
(`core/dedupe.py:59-63`). Two consequences to internalise: two genuinely different events sharing
one listing URL collapse into one row; and a source that changes its URL scheme re-inserts its whole
catalogue as new rows.

### The cross-run merge never overwrites an existing value

`_apply_merge` (`storage.py:806-830`) unions `ticket_links` and `categories`, and backfills only
`description`, `image_url` and `end_time` **when absent**. It never touches `start_time`, `url`,
`title`, `id` or `content_hash`.

**Consequence you will hit:** a row with a truncated description or a midnight start time will
*never* self-heal from re-scraping, no matter how many times the event is seen. That is precisely
why `backfill_full_descriptions.py` and `backfill_missing_event_times.py` exist, and both say so in
their docstrings.

### A coordinate-less source row must not blank out coordinates already stored

The venue upsert writes every column it is given, so a row carrying `lat=None` would erase a good
pin. `_resolve_coords` reads existing coordinates back first (`storage.py:865-925`). **Do not
remove that read-back.**

### Never guess a street suffix to force a geocode match

`Diana Dr` and `Diana St` in El Paso are about 17 km apart. Leaving a venue unresolved is the
correct outcome — the next run retries it. Relatedly, because Nominatim is called with `bounded=1`,
forcing a wrong city still returns *some* in-bbox match: **a confident, wrong pin**, which is worse
than no pin (`geocode.py:11-18`, `:106-117`).

---

## 3. Moderation and access

### A public-facing read must filter `status` itself

The Supabase client used by the scraper, the carousel pipeline, the KB export and the admin actions
is **service role — it bypasses RLS entirely.** RLS will not save you.

`Storage.query_events` (the MCP `query_stored` tool's contract) deliberately does *not* filter
status, and that is documented as the reason a separate family of methods exists
(`storage.py:271-284`): *"reusing it would put unmoderated /submit rows straight onto the public
Instagram feed."*

**So:** anything public-facing uses `query_events_for_day` / `query_events_for_range` /
`query_upcoming_events`, which all pin `status = 'approved'`. Also note `query_events` uses `lte` on
its upper bound, so an event at exactly 00:00 tomorrow lands in "today".

### Every Server Action re-checks authorisation itself

Server Actions are directly callable — not only reachable through the page's own UI. The page
component's gate is friendly copy, **not security**.

**Enforced at** `requireAdmin()` (`web/src/app/admin/actions.ts:18-26`), called as the first
statement of all six admin actions; and `verifyReviewToken` on the token-gated path
(`web/src/app/admin/ig/review/actions.ts:12-14`), which re-verifies rather than trusting a resolved
id.

### There is no UPDATE or DELETE policy on `events` for any role

Not even for a submitter to edit their own pending row. Moderation happens through the service-role
key by design. Rejected events are **kept, not deleted**, to preserve an audit trail and keep the
submitter's own view consistent (`admin/actions.ts:74-77`).

### Admin is an email allowlist, not a role table

`ADMIN_EMAILS`, comma-separated (`web/src/lib/admin.ts:1-16`). An unset value yields an empty set →
**nobody is an admin and every admin action throws.** Fine for a one-or-two-person moderation team;
the code says to revisit if that grows.

---

## 4. Publishing safety

### Slides and caption are frozen at build time

All third-party I/O happens during `build`. By publish time every slide is a JPEG we own, at exactly
the right dimensions, on infrastructure we control — so the publisher can only fail Meta-side.

**The publisher must never regenerate either**, or the human would approve something different from
what actually ships (`social/__init__.py:16-25`).

### `ig_creation_id` is written before `media_publish`, and is a hard stop on retry

The container id is persisted *before* the publish call precisely so recovery knows the carousel
reached Meta (`publish.py:157-158`). Once it exists, the row is **never auto-retried**: we cannot
know whether it actually posted, and **a duplicate public post costs far more than a missed one**.

`retryable = attempts < MAX_PUBLISH_ATTEMPTS and not reached_meta` (`social/__main__.py:777-778`).

### A transient publish failure returns the row to `approved`, not `failed`

`failed` is terminal — `approved_ready_to_publish` only selects `approved` — so writing it on the
first error made the `attempts < 3` cap dead code. One transient Meta refusal then permanently lost
the day, and it **took out 3 of 25 days in one sample month** (`social/__main__.py:762-776`).

### Every `ig_posts` transition is a compare-and-swap, and losing must be clean

`UPDATE … WHERE id = ? AND status = '<expected>'`, returning affected rows. Zero rows means somebody
else got there first.

Two reasons, both quoted in `storage.py`: a slow run *will* eventually overlap the next one, and
without the claim both workers would build from the same row and double-post (`:402-405`); and **a
human tapping Cancel at 16:59:59 must beat the 17:00 sweep** — whichever loses has to lose cleanly
rather than overwrite the other (`:451-453`).

The accepted status sets differ per operation, on purpose:

| Operation | Accepts |
|---|---|
| approve / reject / publish-now / reschedule | `draft` only |
| cancel / postpone / update caption | `draft` **or** `approved` |

Cancel extends past approval because with opt-out posting the sweep flips the row by itself — if
Cancel still required a draft, the Telegram button would start refusing at exactly the moment
someone most wants to stop it (`web/src/lib/ig/moderate.ts:66-73`).

### Postponing must move both `scheduled_for` and `auto_approve_at`

Moving only `scheduled_for` leaves tonight's deadline armed: the sweep auto-approves today and
publishes tomorrow, with the staleness guard expiring the row in between. Described in code as
*"the sharpest edge in the opt-out design, which is why it is one function rather than a documented
two-step"* (`moderate.ts:86-92`).

### `publish` verifies `post_date == today` before any network call

A post approved late that only succeeds the next morning would otherwise publish "TOMORROW IN EL PASO —
Aug 6" (built Aug 5 for Aug 6's events) on Aug 6 itself, with the whole point of the day's advance
notice gone (`social/__main__.py:716-723`).

### A pending edit fails closed

If unapplied `ig_post_edits` rows exist, `autoapprove` holds the post rather than shipping it.
Publishing a carousel that still contains the event someone explicitly asked to remove is worse than
publishing late (`social/__main__.py:638-658`).

### `slide_paths` is positional and rewritten wholesale

A rebuild that shortens a carousel must delete the now-orphaned trailing objects
(`social/__main__.py:449-453`). And **every** slide is re-rendered on a drop, not just the tail,
because `_variant_for` and `_accent_for` are functions of slide *position* — removing slide 3 changes
the layout and accent of every slide after it.

Relatedly, `photo_overrides` is keyed by **event uuid, not slide index**, so a later drop cannot
silently reattach someone's photo to a different event.

### Error text is redacted before it is written or sent

Every Graph call carries `?access_token=…` in its query string, and httpx puts the full URL in every
exception it raises. This pipeline writes exception text into `ig_posts.error` **and** broadcasts it
to Telegram and email — so a missing redaction publishes a live credential to three places at once.
This actually happened and was fixed in `51e5f24`.

**Enforced at** `http.redact_secrets` (`http.py:41-47`) and `metrics._redact`
(`metrics.py:63-74`). Note the regex value class is deliberately narrow: a greedy one eats the colon
and Meta's error body with it, redacting away the very explanation the message exists to carry.

---

## 5. Rendering

### Every slide is exactly 1080×1350

Instagram crops all carousel items to the **first** item's aspect ratio, so identical dimensions make
that rule a no-op. A hard assertion at the end of each render is what keeps it true
(`render.py:7-9`, `:646`).

### Nothing is cropped, nothing is blurred, nothing is ellipsised

Three separate rules with three separate incidents behind them:

- **Crop ceiling.** Above 12% crop, `_fit_photo` mounts the whole image on a paper board rather than
  cropping (`render.py:420-430`). A wide flyer centre-cropped into the band once lost 24% of its
  width and sliced straight through "VIDA SANA MIDDAY MEDITATION" on a live slide.
- **Mount, not blur.** The first version used a blurred, darkened copy of the photo — the Instagram
  Stories idiom — and *"it read as exactly what it was: a smear"* (`render.py:432-443`).
- **Height-budget text fitting, not line capping.** `fit_block` finds the largest size whose wrap
  fits a pixel height. Capping lines shipped real slides reading `"…Cultures in El Paso del…"` and
  `"Hueco Tanks 10,000 b…"` — *"worse than useless: the reader cannot tell what the event is, and
  the information was there all along"* (`render.py:286-294`).

Pinned by `tests/social/test_slide_layout.py` and `test_fit_photo.py`.

### Fonts must be static instances, never variable fonts

`ImageFont.truetype()` on a variable font **silently renders the default (Regular) instance**, so a
"Black" request comes out wrong with no error anywhere. Google Fonts serves most of these families
as variable by default — hand it a `[wght].ttf` and the slides just quietly look wrong
(`render.py:70-74`, `assets/fonts/README.md`).

Related: a *missing* font file does not raise either. `font()` logs a warning once per role and falls
back to Pillow's bitmap font (`render.py:128-151`), so the slides still publish — off-brand.

### Cover branding stays inside rows 135–1215

Instagram's **profile grid** centre-crops slide 1 to a square. Branding outside that band is
invisible exactly where discovery happens (`render.py:44-46`). Pinned by a test in
`test_caption_render.py`.

### The palette is closed

Four brand colours plus neutral tints (`render.py:48-61`). No outside hue, ever, even for a single
accent. Stated as a deliberate constraint, not a limitation.

### No fake affordances in graphics

A cover once labelled itself with a torn-tape "TEAR ME OFF" ticket, implying a physical affordance a
static Instagram image does not have (`c592173`). This is also a standing product rule.

---

## 6. Contracts with external consumers

### The knowledge-base sheet: no blank cell, ever

GoHighLevel's importer rejects a row with **any** empty cell (`"Required field cannot be null or
empty"`), so one blank breaks the entire knowledge base rather than degrading one field. Every cell
falls back to the literal string `"Not listed"`, which reads as an honest answer if the bot quotes it
(`kb/rows.py:57-62`).

### The knowledge-base sheet: no relative dates, in any language

"This weekend" is true at export time and false by the time anyone asks. Rows carry absolute weekday
+ date + time plus a `data_current_as_of` column so the bot can date itself (`kb/rows.py:3-13`).
Pinned by `tests/kb/test_rows.py:40-43`, which asserts the absence of `tonight`, `this weekend`,
`tomorrow`, `hoy`, `mañana`.

### The knowledge-base sheet: never publish an empty one

An export that finds nothing is a *scrape* problem, not a signal to wipe the knowledge base —
otherwise the bot tells customers nothing is happening in El Paso. Exits 1 before any write
(`kb/__main__.py:62-68`).

### The knowledge-base sheet: grow, write, then shrink — never clear first

A clear leaves the sheet empty for the length of a round trip, and a GHL sync landing in that window
wipes the knowledge base. Worst case under the current order is a few stale trailing rows for one
round trip (`kb/sheets.py:81-88`).

### The sheet's column order is append-only

GHL's importer maps by **position** for an existing import, so reordering silently rewrites every
field (`kb/rows.py:24-25`). Add columns at the end. Tests index headers by name precisely so this
stays safe (`tests/kb/test_rows.py:27-28`).

### A time before 6am is omitted, not printed

Public events essentially never start before 6am, so a time in that window is a parse we cannot
vouch for. An omitted time is unremarkable; a wrong one gets repeated back to a customer. The same
call is made by the carousel and the sheet (`selection.py:61-76`).

### The review-token format is a cross-language contract

Python signs, TypeScript verifies: `base64url(post_id.expiry) + "." + base64url(HMAC-SHA256)`, with
padding stripped. `notify.py:69-82` ↔ `web/src/lib/ig/reviewToken.ts:13-41`. A mismatch breaks every
emailed review link silently. Pinned by `tests/social/test_notify.py`.

---

## 7. The website

### The region filter is applied to every public query, unconditionally

Not only when a city tab is selected. `fetchEvents`, `fetchMappableEvents`, `fetchEvent` **and**
`fetchCrawlerEvents` all apply it (`web/src/lib/events.ts`). The site never shows anything outside
El Paso / Juárez regardless of what a source scraped. See
[ADR-0005](adr/0005-region-restriction-everywhere.md).

Note the consequence: an approved event whose `location`/`venue` do not match a regional pattern
**404s on its own detail page** even with a valid id. Surprising when debugging a "missing" event.

### All date formatting is pinned to El Paso — never the viewer's zone, never the server's

Two bugs in one rule. Server-rendered pages format on a Vercel box whose clock is UTC, so an 8pm
show printed as "2:00 AM" in the HTML and then silently changed after hydration. And a visitor
reading from another timezone got the show translated into *their* local time, which is meaningless
for deciding whether to drive to a venue in El Paso (`web/src/lib/datetime.ts:1-12`).

The same applies in reverse for form input: `new Date(naive)` reads a `datetime-local` value in the
*browser's* zone, so a submitter on a laptop set to another city filed an 8pm show at 8pm their time.
`eventLocalToIso` does a two-pass offset sample to handle DST-transition days correctly
(`datetime.ts:52-87`).

### Never add `line-clamp` or any visual truncation to `/crawler/events`

`-webkit-line-clamp` uses `overflow: hidden`, and headless scrapers that read *rendered* text
(`innerText`, not `textContent`) stop exactly at the visual cutoff. That page exists to be
machine-read in full (`crawler/events/page.tsx:157-161`). It also paginates by **content size**
(~25k chars), not event count, because a 500KB single URL was silently dropping its tail.

### User-facing copy lives in the dictionary, in both languages

Add the key to **both** `en` and `es` in `web/src/lib/i18n.ts` and read it via `t.<key>`. The
`Dict` type means TypeScript errors if `es` is missing a key `en` has. Admin and crawler surfaces are
a deliberate English-only exception.

### `CutoutText` must never receive accented text

The four ransom-note display fonts are loaded with `subsets: ["latin"]` only, so accented glyphs are
not in the subset (`layout.tsx:44-45`). The current Spanish hero accent happens to be accent-free —
that is a constraint, not a coincidence.

### The page background lives on `html`, not `body`

`html` is the true canvas paint layer and always sits beneath all content regardless of z-index.
Moving that declaration to `body` buries the entire `position: fixed` negative-z-index landmark
backdrop under the page background (`globals.css:34-42`).

---

## 8. Operations

### A source must self-disable cleanly when its keys or dependencies are missing

`is_configured()` returns `False`; heavy or optional third-party imports go **inside** `fetch()` so
the registry stays importable without them (`sources/base.py:1-6`). This is what makes "every API
key is optional" (`.env.example:2-3`) true rather than aspirational, and it is the invariant that
lets the system run on whatever subset of credentials you happen to have.

### Migrations are additive only

No drops, no deletes, no alterations of existing columns, safe to run against a live database with
data (`0002_venues.sql:1-2`, restated in 0003–0006). Every new column gets a default or is nullable
so pre-existing rows keep behaving exactly as before. The one drop in ten migrations is of an
*index*, and it was behaviour-preserving by construction.

If you must touch a named constraint, **verify its real name against the live database first** —
a collision makes it `..._check1`, and `drop … if exists` on the wrong name silently drops nothing
(`0010_ig_post_kinds.sql:7-11`).

### `fetch` does not reject on a non-2xx response — check the status

Hit twice, in two languages. An expired `GH_DISPATCH_TOKEN` returned 401 and the call looked
successful (`githubDispatch.ts:33-38`). Separately, **Telegram answers HTTP 200 with
`{"ok": false}`** for "chat not found", "bot was blocked", "bot was kicked" — every one of which
reads as success to a status-only check (`telegram.py:5-9`).

### Make swallowed failures loud

A failure the code caught and handled correctly is still invisible to whoever is waiting on
Telegram: the bot went quiet, and *"went quiet" and "is fine" look identical unless something says
otherwise.* This principle has its own ADR — [ADR-0011](adr/0011-make-swallowed-failures-loud.md) —
because the same bug class was fixed in five places.

The structural expression: the `preflight` job is deliberately **not** a dependency of `build` (a
broken notification channel must not stop today's draft being made) yet **fatal on failure**, so
GitHub's own email becomes the one alert path that does not depend on Telegram working
(`ig_daily.yml:89-94`).

### Any change to stored event data is followed by a knowledge-base export

`python -m scraper.kb export`, so the bot and the site never disagree about what is on this week.
Wired into the scrape workflow (`scheduled_scrape.yml:53-66`) and a standing rule for manual edits.

---

## Quick self-check before you commit

| You changed… | Re-read |
|---|---|
| A source connector | §1 (naive times), §2 (Juárez, identity), §8 (self-disable) |
| Selection or scoring | [social-pipeline.md](../components/social-pipeline.md), §4 |
| A slide layout | §5 in full, then run `build --dry-run --out` and look at the images |
| Anything touching `ig_posts` | §4 (CAS, both timestamps, creation id) |
| A web query | §3 (status filtering), §7 (region filter) |
| The KB export | §6 in full |
| A migration | §8 (additive only), [migrations.md](../data/migrations.md) |
| A cron or workflow | §1 (midnight UTC), §8 (loud failures) |

---

*Verified against commit `9157646` (2026-09-06). Last updated 2026-09-10.*
