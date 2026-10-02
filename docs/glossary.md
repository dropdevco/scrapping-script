# Glossary

Terms that mean something specific in this codebase. Where a term maps to code, the file is named.

---

## Product and domain

**Chisme** — the product. Mexican Spanish for gossip / the buzz. Renamed from "Frontera" on
2026-07-20 (`e59ef9a`). The Instagram account is `@elpasochisme`; the site is `www.epchisme.com`.

**The border region** — El Paso, Texas and Ciudad Juárez, Chihuahua, treated as one metro area.
Both observe Mountain time and the same DST transitions (Mexico dropped nationwide DST in 2022 and
Juárez was explicitly exempted to stay aligned with El Paso), which is why one timezone covers the
whole product (`core/eventtime.py:39-46`).

**Benito Juárez** — a Mexican president, and therefore a street, avenue, plaza and Mexico City
borough name all over Mexico. A bare `juarez` match is *not* a Ciudad Juárez match. This false
positive has been fixed three separate times in three different places. See
[invariants](architecture/invariants.md#qualify-every-juárez-match).

---

## Data model

**Source** — one connector: a class implementing `name`, `kind`, `is_configured()`, and async
`fetch()` (`sources/base.py:16-28`). Registered by adding its module name to `sources/registry.py`.
Thirteen exist today across events, trends and web research.

**Kind** — in the *scraper*, one of `EVENTS`, `TRENDS`, `WEB` (`core/models.py:17-22`). It selects
which sources run. Not to be confused with post kind, below.

**content_hash** — the identity of a stored event or trend, and the Supabase upsert key. For an
event it is `sha1(url)` when a URL exists, otherwise `sha1(title|date|venue)`
(`core/dedupe.py:59-63`). **A URL is the identity when present** — two different events sharing one
listing URL collapse into one row.

**address_hash** — the identity of a venue:
`sha1(lower(trim(address)) + "|" + lower(trim(venue_name)))`, computed over
**normalized** address/name (`core/address.py::venue_identity`) — see below. Implemented in Python
(`core/storage.py` delegates to `core/address.py`), SQL (`0002_venues.sql:9-12`, the raw
pre-normalization formula), and TypeScript (`web/src/lib/hash.ts`, ported line-for-line). The Python
and TypeScript sides must stay byte-identical; verified manually, not yet by a committed test.

**venue_identity / address normalizer** (`core/address.py`) — folds a venue's raw name and address
onto a canonical form before hashing: accent-stripping, dropping a trailing country token, mapping
number-words to digits ("One Civic Center Plaza" → "1 civic center plaza"), canonicalizing street
abbreviations, and stripping ZIP+4. What makes two sources describing the same building converge on one
`venues` row instead of drifting into near-duplicate rows.

**Cross-run merge** — the logic that recognises an event scraped today as the same event stored
yesterday from a different ticketing site, and unions their ticket links rather than creating a
second card (`core/storage.py`). Two lanes: exact `venue_id` match, and a **cross-venue** lane for the
same event stored under two different venue rows (gated by `DEDUPE_CROSS_VENUE`). Its counterpart, the
*in-batch* merge, does the same within a single scrape (`core/dedupe.py`).

**Content pillar** — one of six Instagram-only editorial labels (Arts & Culture, Live Music, Sports,
Fitness & Activities, Family, Food & Drink) stored in `events.content_tags`. Drives slide chips,
hashtags, score weighting and the carousel diversity cap. **Not** a replacement for the website's
`categories` taxonomy — see [social-pipeline.md](components/social-pipeline.md#content-pillars-corecontent_tagspy).

**The editorial council** — five model-backed roles (localness auditor, curator, clarifier, editor,
critic) that add judgment on top of `selection.py`'s deterministic scoring — assigning a pillar,
writing a blurb, judging whether a venue is a local business or a national chain, reordering/excluding
candidates, and reviewing the finished post. Governed by `COUNCIL_ENABLED`; **never blocks a post** —
every failure mode leaves the pipeline behaving as if the council didn't exist. Full description in
[social-pipeline.md](components/social-pipeline.md#editorial-council-localnesspy-editorpy-clarifypy-criticpy).

**chain_scope** — a venue's `local` / `regional` / `national` / `unknown` classification, judged once
by the localness auditor (or a zero-cost known-chain rule pass) and cached forever on `venues`. Feeds
`ScoreProfile.chain_venue_penalty`, applied narrowly — only for a national chain in the Food & Drink or
Fitness & Activities pillar.

**blurb** — a generated, ≤90-char plain-language line explaining a title that doesn't explain itself
(e.g. "Libre para Volar"), written by the clarifier role only for titles that fail a deterministic
self-explanatory check, and rendered both on the carousel slide and in the caption. Cached on
`events.blurb`, so a recurring series is paid for once.

**last_seen** — refreshed by a database trigger on every write to an event row
(`0001_init.sql:62-76`). It means "this event was still listed at the source as of T", which is
what the freshness cache reads. `first_seen` is protected by the same trigger and can never be
overwritten by an upsert.

**Freshness cache** — within `FRESHNESS_HOURS` (default 24), an events or trends request returns
stored rows instead of re-scraping. Bypassed with `force_refresh=true`, which the scheduler always
sets. Note it keys only on location + limit, so it is coarser than the request that consults it.

**Status** (on `events`) — `approved` (scraped rows land here, and it is the only value the public
site will render), `pending` (user submissions await moderation), `rejected`. Free text, no CHECK
constraint.

---

## The Instagram pipeline

**Post kind** — the *format* of a carousel. Seven exist (`selection.py`):

| Kind | Window | Voice |
|---|---|---|
| `digest` | one local day, the day *after* build day | "TOMORROW IN EL PASO" — the daily post, shipped the evening before |
| `weekend` | Fri 00:00 → Mon 00:00 | "THIS WEEKEND" — built Thursdays |
| `monthly` | the calendar month | "THIS MONTH IN EL PASO" — built on the 1st |
| `horizon` | a 60-day span starting ~6 months out | "SAVE THE DATE" — ticketed events only |
| `breaking` | one local day | "JUST IN" — **implemented but never scheduled**; reachable only from the CLI |
| `weekly` | Mon 00:00 → the following Mon 00:00 | "THIS WEEK IN {PILLAR}" — six posts built every Sunday, one per content pillar |
| `hype` | not a window — the soonest not-yet-spotlighted hype-worthy event | "DON'T MISS THIS" — cover + exactly one event slide, checked once per scrape |

**Slot** — when more than one digest runs in a day, the slot names it (`morning`, `evening`),
configured by `IG_DIGEST_SLOTS`. `NULL` means the single unnamed digest, which is the default. A
`weekly` post reuses the same mechanism to fire six drafts from one build, but names the pillar in the
dedicated `ig_posts.pillar` column rather than `slot` (which stays `NULL` for `weekly` rows) — the live
unique index keys on `pillar`, not `slot`, for every kind but `digest`.

**Hype post** — a standalone spotlight carousel for one event judged to deserve its own post rather
than a roundup slot: a headline touring artist, a major convention, a championship-level local game.
Judged by `social/hype.py` (`events.is_hype`, cached once per event, tri-state like `is_local`) — a
free deterministic pre-pass (no ticket link and not a major venue ⇒ definite no) gates which events even
reach one batched model call. Checked once per scrape; most days posts nothing.

**period_key** — the bucket a non-daily post belongs to: `YYYY-Www` for a weekend, `YYYY-MM` for a
month or horizon target. It exists as a stored column rather than a derived expression because
Postgres's `date_trunc(text, timestamptz)` is `STABLE`, not `IMMUTABLE`, and cannot appear in an
index predicate (`0010_ig_post_kinds.sql:21-26`).

**slide_key** — a content-derived key for one event on one slide: normalised title with date/
occurrence tails stripped, plus the venue key (`selection.py:288-296`). It is what makes "did we
post this last week?" answerable, because a recurring event is stored as a **new row with a fresh
uuid for every date** — so event ids cannot answer that question.

**Draft** — an `ig_posts` row at `status='draft'`: slides rendered and uploaded, caption written,
human notified, nothing published.

**Opt-out posting** — the current model. A draft carries an `auto_approve_at` deadline; if the
deadline passes with the row still `draft`, silence is read as consent and it publishes. Governed
by `IG_AUTO_APPROVE`. Introduced because opt-in did not survive contact with a busy month — 17 of
25 drafts were built, notified, and never touched (`config.py:127-139`).

**IG_AUTOPOST vs IG_AUTO_APPROVE** — the most confusable pair in the system. `IG_AUTOPOST`
publishes at **build** time and skips the Telegram notification entirely: there is no review window
at all. `IG_AUTO_APPROVE` keeps the notification and the all-day window, and only removes the
requirement that a human tap Approve.

**CAS (compare-and-swap)** — the concurrency idiom used on every `ig_posts` transition: an
`UPDATE … WHERE id = ? AND status = '<expected>'` that returns the affected rows. Zero rows means
somebody else got there first, and losing must be handled cleanly rather than overwritten. A human
tapping Cancel at 16:59:59 must beat the 17:00 sweep (`core/storage.py:449-467`).

**The sweep** — the `publish` job, which runs every 30 minutes across El Paso daytime. It applies
pending edits, auto-approves anything past its deadline, and publishes anything approved and due.
It is the *guarantee*; the immediate `workflow_dispatch` fired by a Telegram tap is only latency
(`social/__main__.py:330-336`).

**Edit intent** — a row in `ig_post_edits` recording that a human asked to drop an event or swap a
photo. The Telegram webhook is a stateless serverless handler that cannot run Pillow, so it records
the intent and a Python job (`apply-edits`) performs the rebuild. See
[ADR-0009](architecture/adr/0009-edit-intents-as-rows.md).

**Container** — Meta's term for an unpublished media object. Publishing a carousel is three calls:
create a child container per slide, create a carousel container listing them, then publish the
carousel. Containers expire after 24h if never published.

**ig_creation_id** — the carousel container id, written to the database **before** the publish
call. Its presence means "this reached Meta — do not blindly retry", because a duplicate public
post costs far more than a missed one (`publish.py:116-120`).

---

## The website

**Canonical categories** — the 14 labels in `web/src/lib/categories.ts:104` that drive the filter
rail, the submit form and the card badges. Raw source strings are mapped onto them by alias table
first, then by regex fallback. Note the scraper has its own, smaller taxonomy in
`core/categorize.py` — the web layer's alias expansion is what reconciles them.

**Region filter** — `regionFilter()` in `web/src/lib/events.ts:30-34`, applied to **every** public
query unconditionally, not only when a city tab is selected. See
[ADR-0005](architecture/adr/0005-region-restriction-everywhere.md).

**The crawler page** — `/crawler/events`, an unstyled, untruncated, paginated-by-content-size index
that exists to be machine-read by search and AI crawlers. Never add visual truncation to it:
headless scrapers read rendered text and stop at a CSS line-clamp.

**Review link** — a token-gated URL (`/admin/ig/review/<token>`) that lets someone approve a
carousel with no session. The token is `base64url(post_id.expiry) + "." + base64url(HMAC-SHA256)`,
signed in Python (`notify.py:69-82`) and verified in TypeScript
(`web/src/lib/ig/reviewToken.ts:13-41`). Telegram needs no token because the chat is itself the auth
boundary.

---

## Infrastructure

**The sweep window** — cron `0,30 13-23,0-2 * * *`. It deliberately straddles midnight UTC: a 17:00
Denver deadline is 23:00 UTC in summer but **00:00 UTC the next day** in winter, so a window ending
at 23:59 silently misses every winter post (`ig_daily.yml:16-22`). Test-enforced in
`tests/social/test_scheduling.py:78-90`.

**Secrets vs variables** — GitHub Actions distinguishes them, and the distinction matters here: an
unset repository *variable* expands to an **empty string**, not to nothing. The Python config layer
normalises `""` back to the code default, so the real risk is not an empty value — it is
**silently getting the code default when the code default is wrong for production**. See
[configuration](operations/configuration.md#the-empty-string-trap).

**Service role vs anon** — the `anon` Supabase key is subject to row-level security and can read
only approved events. The `service_role` key bypasses RLS entirely and is what the scraper, the
carousel pipeline, the KB export and the admin server actions all use. Anything reading events for
a *public* surface must filter `status` itself, because RLS will not do it for a service-role
client.

**GHL** — GoHighLevel, the CRM that runs an Instagram comment-to-DM automation. It syncs from the
knowledge-base Google Sheet, and the sheet is the only grounded source of event facts the bot is
permitted to speak from.

**graphify** — the knowledge graph at `graphify-out/`. `graphify query "<question>"` returns a
scoped subgraph; use it before grepping, and run `graphify update .` after code changes.

---

*Verified against commit `7629204` (2026-09-20). Last updated 2026-09-22.*
