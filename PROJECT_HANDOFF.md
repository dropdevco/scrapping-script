# Project Handoff

Last updated: 2026-07-29 (later)

This file is the durable context document for this repo, meant to replace re-exploration. Read this file FIRST, before grepping the codebase, when starting a new session/harness on this repo — it should answer "what is this, how is it built, what's the current state" without needing to re-derive it from source. Update it every time you make a meaningful change, especially when changing architecture, data contracts, setup steps, UI style, source behavior, migrations, or testing expectations.

**Protocol for every session (any harness — Claude Code, Codex, other agents):**
1. Read this file before exploring the repo.
2. Do the requested work.
3. Before finishing, update the relevant section(s) above the Change Log AND append one Change Log entry (see format at the bottom). Bump "Last updated". Keep edits surgical — don't rewrite unrelated sections.
4. If a change is a trivial typo/style tweak, a one-line Change Log entry is enough; do not touch other sections.

Keep entries concise and factual so a fresh context window or automation harness can understand the project quickly without re-reading source files.

## Project Summary

This repository contains two connected products:

- `scraper-mcp`: a Python 3.11 MCP server and scheduler that gathers events, trends, and web research, normalizes results, dedupes them, and optionally stores them in Supabase.
- `web`: a Next.js 16 Chisme web app for bilingual event discovery across El Paso and Juarez. It reads approved event and venue data from Supabase and provides event listings, detail pages, a map view, auth, and user event submission.

Data flow:

```text
source connectors -> Python orchestrator -> normalization/dedupe -> Supabase -> Next.js web app
```

## Current Repository Shape

```text
src/scraper/
  mcp_server.py            Agent-facing MCP tools.
  scheduler.py             Curated recurring scrape jobs — one pass per SCHEDULE_LOCATIONS entry.
  apply_migration.py       CLI: run a .sql migration file via direct Postgres (DDL; SUPABASE_KEY/PostgREST can't).
  backfill_geocode.py      CLI to repair/fill venue coordinates.
  backfill_merge_duplicates.py  CLI: one-off sweep to merge pre-existing duplicate event rows.
  backfill_categories.py  CLI: upgrade old single-guess categories to the multi-category classifier.
  core/
    models.py              Shared Pydantic models and request params. TicketLink model lives here.
    orchestrator.py        Cache check, source fan-out, dedupe, persistence, run summary.
    storage.py             Supabase upserts, queries, run logging, and cross-run duplicate-event merge.
    config.py              Env-driven optional configuration.
    http.py                Async HTTP client, robots/backoff/concurrency handling.
    dedupe.py              Content hashes, in-batch same-event merge, ticket_link/category union.
    ticket_labels.py        URL domain -> human ticket-source label ("Ticketmaster", "Eventbrite", ...).
    address.py             Address formatting helpers.
    categorize.py          Multi-category keyword classifier (guess_categories).
    geocode.py             Venue address -> lat/lng (Nominatim) + safety guards.
  sources/
    base.py                Source interface.
    registry.py            Source module registry and active-source filtering.
    events_*.py            Event connectors.
    trends_*.py            Trend connectors.
    web_*.py               Web search and extraction.
    social_*.py            Own-account Meta sources.

supabase/migrations/
  0001_init.sql            Base events, trends, runs tables.
  0002_venues.sql          Venue-centric schema and event venue links.
  0003_ticket_links.sql    events.ticket_links jsonb — multiple ticketing links per event.

web/
  src/app/                 Next.js App Router pages and route handlers.
  src/components/          UI components for listings, hero, map, auth, filters, form.
  src/lib/                 Supabase queries, i18n, shared types, hashing.
  src/proxy.ts             Next 16 proxy for Supabase session refresh.
  AGENTS.md                Important Next.js 16 note for coding agents.
  SETUP.md                 Local setup, deployment, schema, and troubleshooting.
```

## Python Scraper Architecture

The scraper uses a thin source-connector pattern.

- Every connector subclasses `Source` from `src/scraper/sources/base.py`.
- A connector declares `name`, `kind`, `is_configured()`, and async `fetch(params, http)`.
- Sources emit normalized `Event`, `Trend`, or `Document` objects from `core/models.py`.
- Add a source by creating a module in `src/scraper/sources/`, exporting `SOURCE` or `SOURCES`, and adding the module name to `_MODULES` in `sources/registry.py`.
- Keep optional and heavy dependencies imported inside `fetch()` or guarded so the registry remains importable when extras are missing.
- The orchestrator should not need edits for a new source unless the shared data contract changes.

Important behavior:

- `orchestrator.run()` first checks the freshness cache unless `force_refresh=True`.
- Source failures are isolated into per-source summaries; one dead source should not fail the whole run.
- Events are hash-assigned, deduped, sorted chronologically, truncated to `limit`, and upserted.
- Trends are hash-assigned, deduped, sorted by score descending, truncated to `limit`, and upserted.
- Web research documents are deduped and returned but not persisted to typed tables.
- Supabase is optional. If `SUPABASE_URL` and `SUPABASE_KEY` are missing, live tools should still return results where possible, but persistence is skipped.

MCP tools exposed in `src/scraper/mcp_server.py`:

- `search_events(location, start_date?, end_date?, categories?, query?, limit?, force_refresh?)`
- `find_trends(topic?, platforms?, timeframe?, limit?, force_refresh?)`
- `research_topic(query, depth?, limit?)`
- `query_stored(kind, location?, topic?, platform?, since?, until?, limit?)`
- `source_status()`

## Web App Architecture

The `web` app is a Next.js 16 App Router application using React 19, Tailwind CSS 4, Supabase SSR, Leaflet, and `motion/react`.

Important conventions:

- Read `web/AGENTS.md` before framework-specific edits. It warns that this is Next.js 16 and local Next docs in `node_modules/next/dist/docs/` should be checked when changing APIs or conventions.
- Prefer server components and server-side Supabase reads for data-heavy pages.
- Use client components only for browser-only behavior, interactivity, animation, auth UI, Leaflet, forms, and language context.
- `web/src/lib/events.ts` is the central place for event listing, detail, category, and map queries. `applyEventFilters()` there is shared by `fetchEvents` (list) and `fetchMappableEvents` (map) so both surfaces interpret the same URL params identically — add new filters there, not in one call site.
- `/` and `/map` both render the same `<Filters>` component and read the same `q` / `city` / `when` / `categories` search params. Filter state lives entirely in the URL, so the two views stay consistent and links are shareable.
- Crawler-facing event extraction depends on `web/src/lib/event-schema.ts`, `web/src/lib/site.ts`, `/events/[id]`, `/crawler/events`, `/sitemap.xml`, and `/robots.txt`.
- Event detail pages must keep full visible event facts and schema.org Event JSON-LD in sync. Do not add fields to one without considering the other.
- Shared row types live in `web/src/lib/types.ts`.
- Bilingual copy lives in `web/src/lib/i18n.ts`; do not hard-code user-facing copy in components when it belongs in the dictionary.
- Supabase browser/server clients live in `web/src/lib/supabase/`. `web/src/lib/supabase/admin.ts` is a THIRD client — server-only, service-role, RLS-bypassing — used exclusively by `/admin`. Never import it from a `"use client"` file.

Current main routes:

- `/`: homepage with scratch-off hero, filters, and event grid.
- `/events/[id]`: event detail page, with schema.org Event JSON-LD and expanded metadata (see `web/src/lib/event-schema.ts`).
- `/map`: interactive venue/event map.
- `/submit`: Google-authenticated event submission.
- `/auth/callback`: OAuth callback route.
- `/admin`: moderation queue for pending submissions (approve/reject). NOT linked from nav — reach by typing the URL. Email-allowlist gated via `ADMIN_EMAILS`; see "Event moderation" below.
- `/crawler/events`: plain public index of upcoming approved event links for knowledge-base crawlers.
- `/sitemap.xml` and `/robots.txt`: generated metadata routes for search/crawler discovery (`web/src/app/sitemap.ts`, `web/src/app/robots.ts`).

### Google OAuth (sign-in / event submission)

Fully implemented in code (`auth-button.tsx`, `submit-form.tsx`, `/auth/callback/route.ts`) — enabling it is pure dashboard configuration, no code changes needed. The flow: Google → Supabase's own `/auth/v1/callback` (NOT the app's) → Supabase redirects to the app's `/auth/callback` (registered separately in Supabase's own Redirect URLs allowlist, distinct from what's registered in Google Cloud Console). Live in production as of 2026-07-29 on `epchisme.com`. If reconfiguring: Google Cloud Console now uses a restructured "Google Auth Platform" UI (Clients / Audience / Branding tabs replacing the old single "Credentials" + "OAuth consent screen" pages) — check "Audience" → Publishing status is "In production", not "Testing" (Testing mode silently blocks every non-allowlisted Google account from signing in).

### Event moderation (`/admin`)

Submissions land as `status='pending'` and are invisible until approved. **No RLS policy grants UPDATE/DELETE on `events` to any authenticated role** — not even for a user to edit their own pending submission — so moderation cannot go through the normal anon-key client. `/admin` uses `supabaseAdmin()` (service-role key, bypasses RLS) inside Server Actions (`web/src/app/admin/actions.ts`) that independently re-verify `isAdminEmail()` before every write — never trust the page component's own gate alone, Server Actions are directly callable. Approve sets `status='approved'`; Reject sets `status='rejected'` (kept, not deleted — audit trail, and `events_select_approved`'s filter already keeps it non-public forever).

Env (server-only, no `NEXT_PUBLIC_` prefix, never sent to the browser): `SUPABASE_SERVICE_ROLE_KEY`, `ADMIN_EMAILS` (comma-separated). Must be set on the hosting platform for production, not just `web/.env.local`. See `web/SETUP.md` "Admin / Moderation" for full setup steps.

App name is still "Chisme" (`web/src/app/layout.tsx` title: "Chisme — El Paso + Juárez events"). The 2026-07-28 commit titled "rebrand" did NOT rename the app — it bundled the crawler/SEO feature work plus filter and hero-scratch refinements; treat that commit message as inaccurate/misleading, not as evidence of a brand change.

## UI And Style Direction

Chisme currently uses a light-first pop-art and magazine-collage identity, not the older dark desert theme still described in parts of `web/SETUP.md`.

Active design tokens live in `web/src/app/globals.css`:

- Warm paper base: `--color-paper`
- Raised card surface: `--color-card`
- Near-black ink: `--color-ink`
- Secondary ink: `--color-ink-soft`
- Signature magenta: `--color-cosmo`
- Pop accents: red, yellow, blue
- Fonts: Fraunces display, Archivo sans, Oswald condensed

Style cues to preserve:

- Editorial collage feel: torn photo edges, halftone textures, pasted-card shadows, bold ink outlines.
- Event cards use rounded paper surfaces, real event images, compact metadata pills, and subtle motion.
- The homepage events section is intentionally wide on large displays (`max-w-[96rem]`) with a wider desktop filter rail and four event-card columns at `2xl`.
- The hero has NO subtitle/tagline paragraph under the title — just the cover-line and the two CTA buttons. Do not reintroduce descriptive copy there.
- The hero scratch reveal uses a large irregular canvas brush, not a circular eraser. Preserve the rough scratch feel when changing `HeroScratch`.

### Landmark collage (`web/src/components/landmark-backdrop.tsx`)

`LandmarkBackdrop` in the root layout is the SINGLE source of landmark imagery site-wide. Do not add section-scoped collages — a second layer sharing the same photos is guaranteed to overlap the fixed one.

It is a lane-based layout with a hard non-overlap guarantee. Two invariants make collisions impossible at any scroll position:

1. **Different lanes never overlap horizontally.** Pieces are centred on `lanes` evenly spaced columns across the full viewport width. Each piece's *rotated* bounding width (`w·cosθ + h·sinθ`, θ ≤ `MAX_ROTATE`) is kept well inside its `100/lanes` vw lane.
2. **Same-lane pieces share one parallax `factor`,** so their vertical spacing is frozen forever. `STRIDE` (vh between pieces in a lane) exceeds the tallest rotated piece.

Because lanes are horizontally disjoint, each lane can parallax at its own rate safely. Breakpoint layouts: mobile 2 lanes, tablet 3, desktop 4.

If you change `MOBILE`/`TABLET`/`DESKTOP` widths, `MAX_ROTATE`, `STRIDE`, or add taller aspect ratios, **re-verify the non-overlap math** — widening a piece without widening its lane breaks invariant 1. The component returns `null` until mounted (breakpoint is measured via `matchMedia`) to avoid hydration mismatch, and stays gated behind `#hero-block` so nothing renders over the hero.
- Motion uses `motion/react` with restrained entry animations and cubic-bezier easing.
- Leaflet is styled to match the light paper theme.
- Keep responsive layouts practical and scan-friendly; avoid changing the visual language to generic SaaS, generic dark mode, or template-like gradients.
- Some files contain mojibake in comments/text from prior encoding issues. Do not spread it. Use UTF-8 for human-facing Spanish text and ASCII for purely technical comments unless the file already requires accents.

## Data Contracts

Python `Event` fields that feed Supabase and the web app include:

- `source`, `source_id`, `title`, `description`
- `start_time`, `end_time`
- `venue`, `location`, `lat`, `lng`
- `url`, `image_url`
- `categories` (list — an event can be more than one bucket), `ticket_links` (list of `{source, label, url}`), `raw`, `content_hash`

Web `EventRow` expects:

- Event columns including `id`, `source`, `title`, `description`, `start_time`, `end_time`, `venue`, `location`, `url`, `image_url`, `categories`, `ticket_links`, `status`, `venue_id`
- Joined `venues` row with `id`, `name`, `address`, `city`, `region`, `postal`, `country`, `lat`, `lng`

Approved events are visible publicly. User-submitted events are inserted as `pending` and need moderation before normal public visibility.

### Duplicate events across ticketing sites are merged, not shown twice (important)

The same real event is routinely scraped from more than one site (Ticketmaster + Eventbrite, or a venue's own calendar + Visit El Paso). Rather than one card per site, the scraper detects the same real event and merges duplicates into ONE row carrying every source's link in `events.ticket_links` (jsonb array of `{source, label, url}`, added in `0003_ticket_links.sql`). The frontend renders one button per `ticket_links` entry (`web/src/app/events/[id]/page.tsx`), falling back to the single legacy `url` field for older/un-migrated rows or user submissions (which don't set `ticket_links` at all).

Two merge passes, matched by **same venue + same calendar day + near-identical title**:

- **In-batch** (`core/dedupe.py::dedupe_events`) — catches duplicates arriving in the SAME scrape (e.g. Ticketmaster and Eventbrite both returning tonight's concert in one orchestrator run). Merges `ticket_links` and `categories` by union, never by picking a "winner" and discarding the loser's link.
- **Cross-run** (`core/storage.py::Storage._merge_with_existing`, called from `upsert_events`) — catches the same real event showing up in a LATER scrape via a source that didn't have it before. Queries existing `approved` events at the same `venue_id` within the incoming batch's day range, and folds a title match into the existing row via a targeted UPDATE instead of a new insert. This is why a new event scraped tomorrow that turns out to be something already on the site gets an extra ticket link instead of a duplicate card.

**Title matching is NOT plain string similarity.** `SequenceMatcher` ratio on raw title text is unreliable for short event titles — verified empirically that "Salsa Night" vs "Bachata Night" scores *higher* (0.67) than genuine same-event pairs like "Machetes - World Tour 2026" vs "Machetes Live in Concierto" (0.54), because short titles sharing generic filler words dominate the ratio. The actual signal used:

1. `title_sim >= 0.9` (near-identical full strings) → merge on title alone, no venue check needed.
2. Otherwise, strip stopwords/filler (`_TITLE_STOPWORDS` in `dedupe.py` — "live", "tour", "night", "concierto", EN+ES) from both titles and compute containment: what fraction of the SHORTER title's remaining distinctive words also appear in the other. `>= 0.8` (near-total overlap) + venue similarity `>= 0.6` (in-batch) or same `venue_id` (cross-run, already an exact match) → merge.

If you touch these thresholds, re-verify against adversarial pairs (two genuinely different recurring events at the same venue/day, e.g. differently-themed weekly nights) — a false merge silently hides a real event, which is worse than an unmerged duplicate card. `ticket_labels.py` derives the human label from the URL's domain (Ticketmaster, Eventbrite, AXS, Boletia, Don Boletón, ...), not the internal scraper source module — one module (`events_web`) fetches several different real platforms.

One-off backfills for data that predates this feature:

    python -m scraper.backfill_merge_duplicates --dry-run   # merge existing duplicate rows already stored
    python -m scraper.backfill_categories --dry-run         # upgrade old single-guess categories to multi-category

### Multi-category events

`core/categorize.py::guess_categories()` returns EVERY matching bucket for a title, not just the first ("Beer & Live Music Festival" → Food & Drink + Music + Festivals). `guess_category()` (singular) still exists as a thin `guess_categories()[0]` wrapper for anywhere that only wants one label. The `categories` column has always been a Postgres array and the frontend (`event-card.tsx`'s "+N" badge, `filters.tsx`'s `.overlaps()` multi-select, `web/src/lib/categories.ts`'s canonicalization) already fully supported multiple categories — the only gap was the classifier only ever assigning one. The submission form (`submit-form.tsx`) now uses multi-select toggle pills instead of a single `<select>`, matching what scraped events already do.

Ticketmaster-sourced events keep using the provider's own segment/genre/subGenre classification (richer than the keyword guesser) and only fall back to `guess_categories()` when TM gives nothing.

If you add a keyword rule to `_RULES`, keep it specific — a Tech-bucket regression was found and fixed here: bare "conferencia"/"universidad" (conference/university) matched almost anything and mistagged unrelated events as Tech.

### Scheduler covers multiple cities now (important)

`src/scraper/scheduler.py::run_events()` runs one events pass **per location** in `_locations()`, not a single hardcoded location. This matters because both `events_directories.py` and `events_web.py` scope which calendars they hit to the request's `location` string — a single "El Paso, TX" run (the old default, and the only thing the scheduler ever ran) never invoked the 7 Ciudad Juarez directories in `events_directories.DIRECTORIES` at all, even though they were fully implemented. That was the actual root cause of Juarez being under-represented, not a scraping/parsing failure.

Configure via `SCHEDULE_LOCATIONS` (semicolon-separated — a plain comma is already used inside one location, e.g. "City, State"). Default: `"El Paso, TX;Ciudad Juarez, Chihuahua, Mexico"`. `SCHEDULE_LOCATION` (singular) still works as a back-compat single-location override if `SCHEDULE_LOCATIONS` is unset — if the GitHub Actions repo has a `SCHEDULE_LOCATION` variable already set from before, it will silently limit the job back to one city; check `vars.SCHEDULE_LOCATION` in repo settings if Juarez coverage regresses.

**Region-filter precision, not just coverage.** `events_directories._event_matches_directory_region()` decides whether a scraped item is really Ciudad Juarez before accepting it. Two classes of false positive were found and fixed live (via `python -m scraper.scheduler events` against production Supabase) after wiring Juarez in:

- National touring-show / ticketing sites (`ticketmaster_mx_juarez`, `boletia_juarez`, `don_boleton_juarez`) list shows in many cities; a page can mention "Ciudad Juarez" in a "now playing in: ..." blurb for an event whose actual venue is CDMX, Guadalajara, Los Cabos, etc. Region matching now checks only `venue`/`location` (not `description`, which is where those incidental mentions live), and a hard negative list (`_OTHER_CITY_MARKERS`: cdmx, ciudad de méxico, monterrey, guadalajara, the Chihuahua **state capital** — a different city from Cd. Juarez despite the same state name) rejects a match regardless of any positive token found elsewhere.
- The "trusted, always-in-region" domains (`visita_juarez`, `juarez_municipal_events`, `uacj_agenda`) are no longer exempt from that negative check — a state/university portal can still syndicate an out-of-region item.
- Separately, `geocode.py`'s last-resort venue-name-only fallback used to *unconditionally* inject a city ("Ciudad Juárez" or else "El Paso, TX") into the query. Because Nominatim is called with `bounded=1`, forcing a wrong city into the query can still return *some* in-region match — a confident, wrong, in-bbox pin — for a venue that has nothing to do with either city (e.g. a Mexico City address containing "Benito Juárez", the CDMX borough name, which the old bare `juarez` regex misread as Ciudad Juárez). `_fallback_anchor_city()` now only injects a city when there's real signal for it (an unambiguous Ciudad Juarez marker via `looks_like_ciudad_juarez()`, an explicit "El Paso" mention, or no address text at all — this site's dominant-city default); anything else yields no candidate rather than a fabricated one.

If you add a new Juarez-side directory or touch region matching again, re-run a live pass and eyeball `location` on the output for stray other-city addresses — the failure mode here doesn't throw, it just quietly geocodes into the wrong city.

### Junk events never get stored (orchestrator)

`orchestrator._is_showable()` drops any scraped `Event` with no `start_time` AND no `venue`/`location` before it's ever upserted — such rows are unrenderable everywhere (no date for the list, no place for the map) and were previously scraper parsing artifacts (e.g. a directory listing's stray link text mistaken for an event title) silently accumulating as `status='approved'`. Keep this guard when touching orchestrator normalization.

### Venue coordinates and the map (important)

`events` has NO lat/lng columns — map position comes solely from the joined `venues.lat/lng`. A venue without coordinates can never appear on `/map`, which is why the map used to show far fewer events than the list.

`src/scraper/core/geocode.py` geocodes venue addresses via Nominatim (keyless; ~1 req/s, enforced). It is called from `SupabaseStore._resolve_coords()` during venue upsert and by `python -m scraper.backfill_geocode`. Two guards exist because both failure modes were observed in production data and a confident wrong pin is worse than no pin:

- `is_virtual()` — online events ("Virtual via Zoom, El Paso, TX") carry a nominal city. They must never get a pin. Returns no candidates at all.
- `is_city_only()` — an address like `"El Paso, TX"` geocodes *successfully* to the city centroid, which silently collapsed 7 unrelated venues onto one bogus downtown pin. City-only addresses are rejected as addresses; the venue **name** is used instead, or nothing.

Results are also bounds-checked against a border-region bbox, since sources carry occasional out-of-area listings ("Boston Career Fair").

`_resolve_coords()` additionally **preserves** existing coordinates: the venue upsert writes every column it is given, so a source row with `lat=None` would otherwise blank out coordinates already in the DB. Do not remove that read-back.

Never guess a street suffix to force a match — `Diana Dr` and `Diana St` in El Paso are ~17 km apart. Leaving a venue unresolved is the correct outcome; the next run retries it.

Env: `GEOCODE_VENUES` (default true), `GEOCODE_MAX_PER_RUN` (default 25, keeps scrapes fast; leftovers picked up next run).

    python -m scraper.backfill_geocode --dry-run     # report only
    python -m scraper.backfill_geocode --repair      # clear untrustworthy coords, then refill

## Setup And Commands

Python:

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -e ".[trends,dev]"
python -m scraper.mcp_server
python -m scraper.scheduler
pytest
ruff check .
```

Web:

```bash
cd web
npm install
npm run dev
npm run lint
npm run build
```

Environment:

- Root scraper env uses optional keys such as `SUPABASE_URL`, `SUPABASE_KEY`, `TICKETMASTER_API_KEY`, `TAVILY_API_KEY`, `BRAVE_API_KEY`, `REDDIT_CLIENT_ID`, `REDDIT_CLIENT_SECRET`, `YOUTUBE_API_KEY`, and Meta own-account tokens.
- Web env uses `NEXT_PUBLIC_SUPABASE_URL` and `NEXT_PUBLIC_SUPABASE_ANON_KEY`.
- Set `NEXT_PUBLIC_SITE_URL` in production so canonical URLs, Open Graph URLs, JSON-LD `url`, robots, and sitemap entries use the deployed domain. If unset, Vercel's `VERCEL_URL` is used when available; local development falls back to `http://localhost:3000`.
- Source allow/deny behavior is controlled by `ENABLED_SOURCES` and `DISABLED_SOURCES`.
- Venue geocoding is controlled by `GEOCODE_VENUES` (default true) and `GEOCODE_MAX_PER_RUN` (default 25).

### Running schema migrations (DDL) against Supabase

`SUPABASE_KEY` (used everywhere else) only grants PostgREST access — it can insert/update/delete rows but CANNOT run `ALTER TABLE`/DDL; the Supabase MCP tool available in this environment is also read-only, including its `apply_migration` action. Confirmed by hitting both walls directly in session.

The working path: a **direct Postgres connection** via `SUPABASE_DB_URL` (optional env var, `admin` extra: `pip install -e ".[admin]"` for `psycopg`) + `python -m scraper.apply_migration <path-to-sql>`. Get the connection string from Supabase Dashboard → **Connect** button (top of the project page, NOT under Settings) → **Direct** tab → **Session pooler** variant specifically — the plain "Direct connection" hostname (`db.<ref>.supabase.co`) did not resolve from this environment (IPv6-only), while the pooler hostname (`aws-<n>-<region>.pooler.supabase.com`) worked. The password in that URI is a literal `[YOUR-PASSWORD]` placeholder — Supabase never shows the real one; get it from the user or use "Reset database password" in the same dashboard area. URL-encode special characters in the password before building the URI (`urllib.parse.quote(pw, safe="")`).

New migration files go in `supabase/migrations/NNNN_name.sql` (numbered, matches existing `0001`/`0002`/`0003`) and get applied the same way. `SUPABASE_DB_URL` is admin-only — the scraper's normal runtime never touches it.

## Testing And Verification Expectations

For scraper changes:

- Run focused tests if present, otherwise at least import affected modules.
- Check `source_status()` behavior when touching source configuration or registry logic.
- For a new source, verify it self-disables cleanly without required keys/deps.
- Avoid requiring Supabase for basic live-source functionality.

For web changes:

- Run `npm run lint` after TypeScript/React/CSS changes.
- Run `npm run build` for routing, server component, Supabase query, or Next config changes.
- Start `npm run dev` when the user needs to try UI changes locally.
- For visual changes, inspect responsive desktop and mobile states when possible.

## Editing Rules For Future Agents

- Preserve user changes in the worktree. Do not reset or revert unrelated files.
- Keep edits scoped to the requested behavior.
- Use existing source, model, query, i18n, and component patterns before adding new abstractions.
- Update Supabase migrations when the stored data contract changes; do not silently rely on app-only schema assumptions.
- Update this file when your change alters how the project should be understood or modified.
- If a change only fixes a typo or makes a tiny local style tweak, add a short line to the change log instead of rewriting sections.

## Known Notes And Risks

- Repo root has an untracked, unrelated 560KB `allevents2.html` file (looks like a raw scrape/debug capture, not a repo asset). Not referenced by any code. Leave it alone unless the user asks about it — do not assume it's safe to delete without asking.
- `web/SETUP.md` still contains some outdated design-system wording from an older dark theme. Prefer `web/src/app/globals.css` and this handoff file for current UI style.
- Next.js 16 APIs may differ from older Next versions. Check local docs (`web/node_modules/next/dist/docs/`) before editing framework-sensitive behavior. Also read `web/AGENTS.md` / `web/CLAUDE.md` before framework-specific edits.
- Supabase RLS and migrations are central to production behavior. Any change to insert/select/update assumptions should be checked against migrations and policies.
- `LangToggle` in `web/src/components/lang-context.tsx` sets the `lang` cookie and calls `router.refresh()` from a `useEffect` keyed on `pendingLang` state (not directly in the click handler) — a deliberate fix for a prior issue, preserve this pattern if touching language switching.

## Change Log

Newest entry first. One entry per session/meaningful change; trivial fixes get one line.

### 2026-07-29 (later)

- Wired up Google OAuth end to end with the user (Google Cloud Console's new "Google Auth Platform" UI, Supabase provider config, redirect URL allowlists) — pure dashboard config, no code changes; the app already fully implemented the sign-in flow. Live on `epchisme.com`.
- Discovered doing this that event moderation had no actual mechanism — submissions land as `status='pending'` with no RLS UPDATE policy for anyone, not even the submitter, to change that. Built `/admin`: an email-allowlisted moderation queue using a new service-role Supabase client (`web/src/lib/supabase/admin.ts`) inside Server Actions that independently re-check admin auth. See "Event moderation" above for the full design and why a service-role bypass was the right call over adding RLS policies.
- Added `SUPABASE_SERVICE_ROLE_KEY` and `ADMIN_EMAILS` (server-only) to `web/.env.local`; created `web/.env.example` (didn't exist before) documenting all web env vars.
- `status` can now also be `'rejected'` (previously only `'approved'`/`'pending'`) — no migration needed since the column was always free text, but worth knowing if you write a query assuming only two status values.

### 2026-07-29

- Built cross-source duplicate-event merging: the same real event scraped from multiple ticketing sites (Ticketmaster + Eventbrite, a venue's own calendar + Visit El Paso, etc.) now shows as ONE card with multiple "buy tickets" buttons instead of one card per site. New `events.ticket_links` jsonb column (migration `0003_ticket_links.sql`); matching logic in `core/dedupe.py` (same scrape) and `core/storage.py::_merge_with_existing` (across scrapes, by venue_id + day + title).
- Had to build a real title-similarity metric for this — plain `SequenceMatcher` on raw strings was proven unreliable ("Salsa Night" vs "Bachata Night" scored higher than genuine duplicate pairs). Replaced with stopword-stripped token-containment for the venue-assisted tier; see the Duplicate events section above for the exact thresholds and why.
- Built multi-category support: `categorize.guess_categories()` returns every matching bucket instead of the first. The DB column and frontend (badges, "+N" indicator, multi-select filter) already supported arrays — only the classifier needed the fix. Submission form's category `<select>` replaced with multi-select toggle pills to match.
- Along the way, fixed a pre-existing Tech-bucket false positive: bare "conferencia"/"universidad" matched almost any event.
- Backfilled existing data: `backfill_merge_duplicates` found and merged 1 pre-existing duplicate pair (142 rows checked, most cross-source dupes were already being caught by the in-batch merge, which fires every scheduled run since all sources for a location run together); `backfill_categories` upgraded 2 rows whose classification would change under the new fixed rules (459 checked) — conservative by design, only touches rows that still look like the old single-guess output.
- Hit a real capability wall applying the schema migration: neither `SUPABASE_KEY` (PostgREST, no DDL) nor the Supabase MCP tool (read-only, including its own `apply_migration` action) can run `ALTER TABLE`. Solved by asking the user for a direct Postgres connection string (the pooler variant — the plain direct-connection hostname doesn't resolve from this environment) and adding `SUPABASE_DB_URL` + `python -m scraper.apply_migration` as a permanent admin path for future migrations. See "Running schema migrations" above — don't attempt DDL via the normal `SUPABASE_KEY`/MCP path again, it will not work.
- Verified live end-to-end against production: ran the full two-city scheduler after all changes (El Paso 100 stored, Ciudad Juarez 41 stored, merges logged and spot-checked correct), confirmed rendering in-browser (multi ticket-link buttons, multi-category badges, single-link fallback still says "Get tickets" not a domain name).

### 2026-07-28 (session 7)

- Added `src/scraper/sources/events_directories.py`, a keyless source for the requested El Paso/Juarez primary calendars, venue pages, and ticketing portals: Visit El Paso, El Paso Live, City of El Paso events, El Paso County, Southwest University Park, UTEP Special Events, Lowbrow Palace, El Paso County Coliseum, RockHouse, best-effort AXS El Paso, Don Boleton, Boletia Juarez, guarded Ticketmaster Mexico Juarez search, Visita Juarez, Chihuahua culture/CCPN, Juarez municipal pages, and UACJ agenda.
- Added `src/scraper/sources/local_news_feeds.py`, a keyless RSS/Atom web-research source for El Paso Times, KVIA, KTSM, KFOX14/KDBC, El Paso Matters, El Heraldo de Juarez, El Diario de Juarez, Norte Digital, and Puente Libre. News remains live `Document` output because the current database schema persists events/trends, not web documents.
- Expanded `src/scraper/core/categorize.py` with Spanish event/category terms so Juarez sources normalize into the same internal category taxonomy as El Paso sources.
- Registered both new sources in `src/scraper/sources/registry.py`. Focused live checks returned El Paso events from Visit El Paso/City of El Paso, Juarez events from Visita Juarez plus a Ciudad Juarez Ticketmaster venue, and El Paso news from local RSS feeds. AXS direct fetches returned an access-protection page during verification, so AXS is also included in the indexed `events_web` site-search supplement. `www.cultura.chihuahua.gob.mx` and `rockhousebarandgrill.com` had DNS failures during verification but were isolated by source-level error handling.

### 2026-07-28 (session 6)

- Added `web/src/lib/categories.ts` as the canonical category taxonomy. Filters and the submit form now use stable main buckets instead of exposing raw source-specific category strings.
- Category filtering now expands a selected canonical bucket into known raw aliases before the Supabase `overlaps("categories", ...)` query, so list and map filters still match scraped events carrying source-specific labels.
- Event cards now display canonical category badges. When category filters are active, cards prefer showing the selected matching category; the `+` chip still exposes the detailed raw category list.

### 2026-07-28 (session 5)

- Changed `LandmarkBackdrop` to gate on the measured document-space top of `#events` and return `null` until that measurement exists, preventing parallax landmark images from briefly rendering over the hero or divider before the events section.
- Added a matching magenta `+` chip to event cards when an event has multiple categories; the visible primary category remains unchanged and the chip exposes the full category list via title text.

### 2026-07-28 (session 4)

- Removed the hard 8-category cap from `fetchCategories()` so every upcoming approved event category appears in the shared homepage/map filter rail, ordered by frequency and then name.
- Added a hero loading veil that stays up until `/background.webp` is loaded/decoded and the scratch canvas cover has been painted, preventing the revealed image from flashing before hydration.
- Added scratch completion detection: when the remaining cover is effectively gone, the canvas clears and a short fireworks animation plays over the hero.
- Reworked `LangToggle` to keep the existing cookie + `router.refresh()` effect pattern while adding an immediate sliding-pill active state and avoiding redundant same-language refreshes.

### 2026-07-29 (session 4)

- Reviewed whether all DB events reach the frontend: they do, for the default view. `web/src/lib/events.ts::fetchEvents()` with no filters resolves to "everything upcoming" (`range()` returns `{from: now}`, no `to`), and the current volume (~150 future approved rows) is well under the 500-row `limit`, so nothing is silently truncated today. Past events (hundreds of rows) are correctly excluded by design — there's no "past events" browse mode, which is worth knowing if that's ever wanted, but wasn't asked for here.
- Found and fixed the actual reason Ciudad Juarez coverage was thin: the scheduler (`src/scraper/scheduler.py`) only ever ran ONE events pass, hardcoded to "El Paso, TX". The 7 Ciudad Juarez calendars in `events_directories.DIRECTORIES` were fully implemented but never invoked, because both `events_directories.py` and `events_web.py` scope their fetch to the request's `location`. Added `SCHEDULE_LOCATIONS` (semicolon-separated) so the scheduler runs one pass per city; default is now `"El Paso, TX;Ciudad Juarez, Chihuahua, Mexico"`. Wired the same var into `.github/workflows/scheduled_scrape.yml`.
- While live-testing the new Juarez pass against production Supabase, found and fixed two classes of false positive in `events_directories._event_matches_directory_region()`: national touring-show pages mentioning "Ciudad Juarez" in a multi-city description for an event actually in CDMX/Guadalajara/Los Cabos/etc, and three "trusted" Juarez-domain directories that skipped region validation entirely. See the Region-filter precision note above for the fix.
- Fixed the same root problem in `geocode.py`'s last-resort venue-only fallback: it used to always inject "Ciudad Juárez" or "El Paso, TX" into the query, and `bounded=1` meant a wrong injected city could still return *some* in-region match (e.g. a real Mexico City address containing "Benito Juárez" — a CDMX borough — misread as the border city by a bare `juarez` regex). Now only injects a city when there's real signal for it; added `looks_like_ciudad_juarez()` / `_fallback_anchor_city()`.
- Added `orchestrator._is_showable()`: drops events with no start_time AND no venue/location before storage — these are unrenderable parsing artifacts, not real events short one field.
- Cleaned up production data found bad during this review: 2 pre-existing junk rows (no date, no place), 1 mis-geocoded venue ("Indiana 46" — a Mexico City address that had been pinned inside the border bbox), and 13 CDMX/Guadalajara/Los Cabos/León event rows that had slipped in from `ticketmaster_mx_juarez` over time (found via a full re-validation of every `events_directories` row against the fixed filter, not just the ones surfaced by today's test runs).
- Net result verified against production: Ciudad Juarez went from 6 to 5 *clean* upcoming events (several loose false positives removed, a handful of genuine new ones added — the 7 Juarez directories are real but thin: `cultura.chihuahua.gob.mx` doesn't resolve/DNS-fails, and most of the rest either have no crawlable JSON-LD or few upcoming events at all). El Paso held steady at ~117. Coverage is real now but modest — most Juarez sites need bespoke HTML parsers (like El Paso's `el_paso_live`/`axs_el_paso`/`city_of_el_paso_events`) to do meaningfully better, since generic JSON-LD scraping is what's actually running for all 7 of them today.
- Verified: Python modules import cleanly; scheduler runs both cities end-to-end (`python -m scraper.scheduler events` — El Paso 100 stored, Ciudad Juarez 42 stored, both `sources_failed: []`).

### 2026-07-28 (session 3)

- Fixed the map showing only ~40 of 117 events. Root cause was missing data, not a bad query: `events` has no coordinates of its own, and 80 of 153 venues had null `lat/lng`, so their events could never be mapped.
- Added `src/scraper/core/geocode.py` (Nominatim, rate-limited, region bounds-checked) plus `python -m scraper.backfill_geocode` to repair/fill venue coordinates.
- Wired geocoding into `SupabaseStore._resolve_coords()` so newly scraped venues get coordinates automatically, capped by `GEOCODE_MAX_PER_RUN`.
- Fixed a latent data-loss bug found while doing this: the venue upsert sends every column, so a source row with `lat=None` silently ERASED coordinates already stored for that venue. `_resolve_coords()` now reads existing coords back and preserves them.
- Added two guards after a first backfill pass produced bad pins: online/virtual venues were being geocoded to their nominal city, and city-only addresses ("El Paso, TX") geocoded to the city centroid, collapsing 7 unrelated venues onto one downtown pin. Both are now rejected, and `--repair` clears any such coordinates so they can be re-derived by venue name.
- `event-map.tsx` now groups pins by rounded coordinate instead of `venue.id`, because the same physical place often has several venue rows (differing address spellings hash to different venues) and was stacking duplicate pins.
- Result: **117 list events -> 101 on the map**, 15 online/no-location correctly excluded, 1 unresolved (Western Technical College; `Diana St` is not in Nominatim and `Diana Dr` is a different street ~17 km away, so guessing was rejected). 0 online events pinned.
- Verified: `npm run lint` + `npm run build` clean; Python modules import cleanly (note: `ruff`/`pytest` are not installed in `.venv` — `pip install -e ".[dev]"` to run them).

### 2026-07-28 (session 2)

- Removed the hero subtitle paragraph ("Concerts, ballgames, markets, meetups…") from `web/src/app/page.tsx` and deleted the now-unused `heroSub` EN/ES keys from `web/src/lib/i18n.ts`. CTA row spacing bumped `mt-8` → `mt-10` to absorb the gap.
- Added the full filter rail to `/map`: extracted `applyEventFilters()` in `web/src/lib/events.ts`, gave `fetchMappableEvents` the same `EventFilters` contract as `fetchEvents`, and rebuilt `web/src/app/map/page.tsx` with the same `<Filters>` aside + grid as the homepage, a live filtered count, an empty state, and a search-param-keyed Suspense boundary.
- Rewrote `LandmarkBackdrop` as a lane-based layout with a provable non-overlap guarantee (see the Landmark collage section above) and spread pieces across the full viewport width instead of pinning them to the left/right corners.
- Deleted the section-scoped `EVENT_COLLAGE` / `EventSectionBackdrop` from `web/src/app/page.tsx`. It was the main cause of visible overlap: it duplicated the same 8 photos as the fixed backdrop, so the two layers always collided. The widened lane layout now covers the events area.
- Verified: `npm run lint` and `npm run build` both clean; non-overlap checked numerically across 12 viewport sizes (incl. short and ultrawide) over the full scroll range — ~986k pair checks, 0 overlaps, tightest clearance 25.7px.
- Added `"autoPort": true` to `.claude/launch.json` because port 3000 was occupied. If the Google OAuth dev callback requires exactly `localhost:3000`, flip this back to `false` and free the port.

### 2026-07-28 (session 1)

- Reviewed repo state and rewrote this handoff file's process section so it's read first by any harness (Claude Code, Codex, others) and updated every session, reducing repeated re-exploration/token spend.
- Noted: commit `5ce8306` "rebrand" is a misleading message — it actually bundled the crawler/SEO feature set (JSON-LD, sitemap, robots, `/crawler/events`) with filter UI refactor (extracted `SearchField`) and a lang-toggle debounce/effect fix. No actual brand/name change happened; app is still "Chisme".
- No functional code changes made this session (documentation-only).

### 2026-07-22

- Added crawler-focused event extraction support: schema.org Event JSON-LD, semantic event detail HTML, dynamic per-event metadata/Open Graph fields, meaningful event image alt text, a public `/crawler/events` index, generated sitemap and robots files, and initial-HTML metadata for all bots via `htmlLimitedBots: /.*/`.
- Widened the homepage event browsing surface for large screens: `#events` now uses a `96rem` max width, a wider filter rail, and a fourth event-card column at `2xl`.
- Added a section-scoped landmark photo collage behind the event filters/cards so clippings appear across the event area, not just at the viewport edges.
- Changed the hero scratch reveal from a 46px circular eraser to a 61px irregular brush made from jittered ellipses and scratch strokes.
- Raised default event listing and mappable event query limits from 60/200 to 500 so the UI can show all current approved events instead of truncating around 60.
- Added this project handoff file after scanning the Python scraper, Supabase migrations, and Next.js web app structure.
- Captured the current architecture, data contracts, active UI style, setup commands, and future update expectations.
