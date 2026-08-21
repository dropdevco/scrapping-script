# HANDOFF

Onboarding doc for a fresh AI agent (any harness) picking up this repo cold. See also
`PROJECT_HANDOFF.md` in this same directory — a much longer, session-by-session log maintained by
prior agent sessions (last updated 2026-08-06) with deep detail on the Instagram automation
subsystem, data contracts, and change history. This file is the fast-orientation entry point; that
file is the deep reference.

## 1. Purpose

This repo is **not** a simple scraper — it's two connected products sharing one Supabase database.
`scraper-mcp` (Python) is an MCP server + scheduler that pulls **events** (concerts, markets,
meetups in El Paso, TX and Ciudad Juárez, MX), **trends** (Reddit/HN/YouTube/Google Trends), and
general **web research** from free/keyless APIs and public event-listing sites, normalizes and
dedupes the results, and stores them in Supabase. **Chisme** (`web/`) is a bilingual Next.js site
that reads the approved events out of that same Supabase database and presents them as a public
event-discovery app (listings, map, event detail pages, a crowd-sourced submission form). A third
piece (`src/scraper/social/`) renders the day's events into an Instagram carousel image set and
publishes it via the Graph API, gated behind human approval in a `/admin/ig` review page.

## 2. Status

**Active.** Last commit `45125a99` on `Tue Aug 18 2026` (branch `main`, clean working tree at time
of writing). `PROJECT_HANDOFF.md`'s change log shows near-daily sessions through early August 2026.
One subsystem is stalled, not the whole repo: the Instagram auto-posting pipeline is fully built and
tested manually but not yet running unattended — see Known Issues (#1).

## 3. Stack

From `pyproject.toml` (Python, `requires-python = ">=3.11"`; CI installs `python-version: "3.12"`):

- `mcp>=1.2.0` — MCP server SDK (FastMCP)
- `pydantic>=2.6`, `httpx>=0.27`, `python-dotenv>=1.0`, `supabase>=2.4`
- `trafilatura>=1.8`, `beautifulsoup4>=4.12` — content extraction / HTML parsing
- `ddgs>=6.0` — DuckDuckGo search, `feedparser>=6.0` — RSS/Atom
- Optional extras: `pytrends>=4.9` (`[trends]`), `pillow>=10.0` (`[social]`, carousel rendering),
  `psycopg[binary]>=3.1` (`[admin]`, direct-Postgres DDL), `ruff>=0.5`/`pytest>=8.0`/
  `pytest-asyncio>=0.23` (`[dev]`)

From `web/package.json` (Node; no `.nvmrc`/`engines` field found — Node version UNKNOWN, checked
`web/package.json` and repo root for both):

- `next@16.2.10`, `react@19.2.4`, `react-dom@19.2.4`
- `@supabase/ssr@^0.12.3`, `@supabase/supabase-js@^2.110.7`
- `leaflet@^1.9.4` + `react-leaflet@^5.0.0` (map), `motion@^12.42.2` (animation), `geist@^1.7.2` (font)
- Dev: `typescript@^5`, `tailwindcss@^4`, `eslint@^9`, `eslint-config-next@16.2.10`

Next.js 16 is recent enough that `web/AGENTS.md` explicitly warns it differs from an agent's
training data — read `web/node_modules/next/dist/docs/` before framework-sensitive edits.

## 4. Setup & Commands

**Python scraper (repo root):**
```bash
python -m venv .venv
.venv\Scripts\activate            # Windows; source .venv/bin/activate on macOS/Linux
pip install -e ".[trends,dev]"    # add ",social" for Instagram rendering, ",admin" for DDL migrations
cp .env.example .env              # fill in whatever keys you have — all are optional
python -m scraper.mcp_server      # run the MCP server over stdio
python -m scraper.scheduler       # run curated scrape jobs once (all sources)
python -m scraper.scheduler events   # events only
python -m scraper.scheduler trends   # trends only
pytest                            # tests/social only — see Conventions & Gotchas
ruff check .
```

**Instagram carousel (needs `[social]` extra):**
```bash
python -m scraper.social build --dry-run --out ./_preview   # render without touching Supabase
python -m scraper.social build
python -m scraper.social publish
python -m scraper.social check-token
```

**Web app:**
```bash
cd web
npm install
npm run dev      # dev server, port 3000 (autoPort in .claude/launch.json if occupied)
npm run lint
npm run build
```
No test script is defined in `web/package.json` (`dev`, `build`, `start`, `lint` only). No test
script exists at the repo root either — `pytest` is invoked directly (not via a `[project.scripts]`
entry), and only covers `tests/social/`.

## 5. Architecture Map

**Scraper source (Python, `src/scraper/`):**
- `mcp_server.py` — MCP tools exposed to an agent (`search_events`, `find_trends`,
  `research_topic`, `query_stored`, `source_status`)
- `scheduler.py` — curated recurring jobs, one pass per entry in `SCHEDULE_LOCATIONS`
- `core/` — shared engine: `models.py` (Pydantic `Event`/`Trend`/`Document`/`SearchParams`),
  `orchestrator.py` (fan-out + dedupe + persist), `storage.py` (all Supabase reads/writes),
  `http.py` (async client with robots.txt/backoff/concurrency cap), `dedupe.py`, `config.py`,
  `address.py`, `categorize.py`, `geocode.py`, `ticket_labels.py`
- `sources/` — one connector per file (`events_ticketmaster.py`, `events_web.py`,
  `events_directories.py`, `local_news_feeds.py`, `trends_reddit.py`, `trends_youtube.py`,
  `trends_hackernews.py`, `trends_google.py`, `web_search.py`, `web_extract.py`,
  `social_instagram.py`, `social_threads.py`), plus `base.py` (interface) and `registry.py`
  (active-source list)
- `social/` — Instagram carousel pipeline: `__main__.py` (CLI), `selection.py`, `imaging.py`,
  `render.py` (Pillow), `caption.py`, `slides_store.py`, `publish.py`
- `backfill_*.py`, `apply_migration.py` — one-off/admin CLI scripts at the top of `src/scraper/`

**Scraped/generated data (NOT source code):**
- Nothing is checked in as bulk scraped output — live data lives only in Supabase (external, not in
  this repo). The one exception is `allevents2.html` (560KB, repo root) — a single raw HTML page
  capture (contains a New Relic RUM loader, looks like a saved/downloaded page snapshot). It **is**
  tracked by git (`git ls-files` confirms it) but is not imported or referenced by any `.py`/`.ts`/
  `.tsx` file (`grep` across `src/`, `web/`, `tests/` returns nothing) — treat it as inert, not as
  part of the pipeline. This single large file is very likely why GitHub's language detector reports
  HTML as the primary language despite the codebase being ~53 Python files vs. 1 HTML file.
- `graphify-out/` — a pre-built code-knowledge graph for this repo (see section 10), not app data.

**Web app (`web/`, Next.js 16 App Router):**
- `src/app/` — routes: event listing (home), `/map`, event detail pages, `/submit`, `/admin`
  (moderation), `/admin/ig` (Instagram draft approval), `/crawler/events` (bot-facing index),
  sitemap/robots generation
- `src/components/` — event grid/cards, filters, map (`react-leaflet`), auth, submission form
- `src/lib/` — Supabase client(s), event/venue queries, i18n (EN/ES), `lib/ig/githubDispatch.ts`
  (triggers the `ig_daily.yml` workflow from the admin UI)
- `SETUP.md` — web-specific local dev + deployment + schema notes; `AGENTS.md` — Next.js 16 warning

**Database:**
- `supabase/migrations/` — **six** tracked migrations, apply ALL in order:
  `0001_init.sql`, `0002_venues.sql`, `0003_ticket_links.sql`, `0004_ig_posts.sql`,
  `0005_ig_scheduling.sql`, `0006_ig_kind_slot.sql` — events/trends/runs, venue-centric.
  (0005/0006 add the IG scheduling tables the Instagram pipeline depends on — stopping
  at 0004 leaves the DB two migrations behind.)
  schema, `ticket_links` jsonb, and the `ig_posts` state machine, applied in order

**CI:**
- `.github/workflows/scheduled_scrape.yml` — daily scrape at `0 11 * * *` UTC (Python 3.12)
- `.github/workflows/ig_daily.yml` — chained `build`/`publish` for the Instagram carousel (exists,
  correct, but not yet actually running — see Known Issues #1)

## 6. Entry Points — Read These First

1. `README.md` — the project's own architecture/setup overview; most accurate single doc for "what
   is this and how do I run it."
2. `src/scraper/core/models.py` — the shared `Event`/`Trend`/`Document`/`SearchParams` types every
   source and consumer speaks; understand these before touching any source or the web queries.
3. `src/scraper/core/orchestrator.py` — the actual control flow: cache check → concurrent source
   fan-out → normalize → dedupe → persist → run log.
4. `src/scraper/sources/base.py` + `registry.py` — the connector interface and how a source gets
   wired in; needed before adding/modifying any source.
5. `src/scraper/core/storage.py` — every Supabase read/write in the scraper lives here (largest,
   most load-bearing file in `core/`).
6. `web/src/lib/` (start with the events query module) — how the web app reads what the scraper
   wrote; the seam between the two halves of the repo.
7. `PROJECT_HANDOFF.md` — once oriented, this has the detailed "why" behind non-obvious decisions
   (especially the Instagram automation section) that this file only summarizes.

## 7. Conventions & Gotchas

- **Test coverage is narrow.** `tests/` only contains `tests/social/` (carousel rendering, caption,
  scheduling, selection, publish). There are no dedicated tests for `core/` or `sources/`; verify
  changes there by importing the module and/or running `python -m scraper.scheduler` against a real
  or dry environment.
- **Source connectors must self-disable, never crash the run.** Every `Source.is_configured()` must
  return `False` cleanly when required keys/deps are missing; the orchestrator treats one dead
  source as a partial result, not a failure.
- **`SUPABASE_KEY` cannot run DDL.** It's PostgREST-only (insert/update/select/delete). Schema
  migrations require `SUPABASE_DB_URL` (direct Postgres, `[admin]` extra) + `python -m
  scraper.apply_migration <file>` — the Supabase MCP tool's own `apply_migration` action is also
  read-only in this environment per prior investigation in `PROJECT_HANDOFF.md`.
- **Timezone bug class already fixed once, watch for regressions:** event/day-boundary logic must
  use `America/Denver` local time, not UTC — a UTC-day split silently misfiles evening events onto
  the wrong date.
- **Venue coordinates can be silently erased on upsert** if a source sends `lat=None` for an
  existing venue; `storage.py`'s `_resolve_coords()` guards this by reading existing coords back
  first — preserve that pattern if touching venue upsert logic.
- **Instagram carousel fonts must be static instances, not variable fonts** — `PIL.ImageFont` on a
  variable font silently renders the wrong weight with no error. Vendored fonts live in
  `assets/fonts/`.
- The `TODO`/`Todos` pattern check from the task brief did not surface anything here — no
  stray `TODO`/`FIXME` markers found masquerading as or mixed up with legitimate Spanish "todos"
  (= "all") copy during this review; none identified either way.

## 8. External Dependencies & Environment

**Target sites/APIs the scraper talks to:** Ticketmaster Discovery API, Eventbrite/Meetup public
event pages, Visit El Paso + La Nube calendar widgets, El Paso Live, City of El Paso events, El Paso
County, Southwest University Park, UTEP Special Events, Lowbrow Palace, El Paso County Coliseum,
RockHouse, AXS El Paso, Don Boleton, Boletia Juárez, Ticketmaster Mexico (Juárez), Visita Juárez,
Chihuahua culture/CCPN, Juárez municipal pages, UACJ agenda, local news RSS feeds (El Paso Times,
KVIA, KTSM, KFOX14/KDBC, El Paso Matters, El Heraldo de Juárez, El Diario de Juárez, Norte Digital,
Puente Libre), Reddit, Hacker News, YouTube Data API v3, Google Trends, DuckDuckGo, optional
Tavily/Brave search, Nominatim (geocoding), Meta Graph API (`graph.instagram.com` — Instagram Login
flow, not the Facebook-Page-linked flow).

**Env var names** (values live in `.env`, never in this file — see `.env.example` for the full,
already-documented list):
- Storage: `SUPABASE_URL`, `SUPABASE_KEY`, `SUPABASE_DB_URL`
- Behavior: `FRESHNESS_HOURS`, `HTTP_MAX_CONCURRENCY`, `HTTP_TIMEOUT_SECONDS`, `USER_AGENT`,
  `ENABLED_SOURCES`, `DISABLED_SOURCES`, `SCHEDULE_LOCATIONS`, `GEOCODE_VENUES`,
  `GEOCODE_MAX_PER_RUN`
- Events: `TICKETMASTER_API_KEY`
- Web research: `TAVILY_API_KEY`, `BRAVE_API_KEY`
- Trends: `REDDIT_CLIENT_ID`, `REDDIT_CLIENT_SECRET`, `REDDIT_USER_AGENT`, `YOUTUBE_API_KEY`
- Meta / Instagram / Threads: `META_APP_ID`, `META_APP_SECRET`, `IG_ACCESS_TOKEN`,
  `IG_BUSINESS_ACCOUNT_ID`, `THREADS_ACCESS_TOKEN`, `THREADS_USER_ID`
- Instagram automation (workflow-level, per `ig_daily.yml`): `IG_AUTOPOST`, `IG_SLIDES_BUCKET`,
  `IG_MIN_SLIDES`, `IG_MAX_SLIDES`, `IG_HANDLE`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`,
  `RESEND_API_KEY`, `NOTIFY_ADMIN_EMAIL`, `IG_NOTIFY_SECRET`, `NOTIFY_EMAIL_FROM`,
  `IG_NOTIFY_TTL_HOURS`, `SITE_BASE_URL`
- Web app (`web/`): `NEXT_PUBLIC_SUPABASE_URL`, `NEXT_PUBLIC_SUPABASE_ANON_KEY`,
  `SUPABASE_SERVICE_ROLE_KEY` (server-action only, no `NEXT_PUBLIC_` prefix), `NEXT_PUBLIC_SITE_URL`

## 9. Known Issues & TODOs

1. **Instagram auto-posting is built and manually verified end-to-end but not yet live.** Per
   `PROJECT_HANDOFF.md`, the blocker is a Meta long-lived-token exchange (`ig_exchange_token`)
   returning "session key invalid" against `graph.instagram.com`, despite a confirmed-correct token
   and secret (both bad-secret and bad-token causes have been ruled out with direct evidence). The
   leading, unconfirmed theory is that the token was generated via the developer console's quick-test
   button rather than a real OAuth redirect flow. Two real posts were published manually with a
   short-lived token to prove the render/select/publish pipeline works; the automation itself is
   what's stalled. Do not re-diagnose from scratch — read the "Publishing" subsection of
   `PROJECT_HANDOFF.md`'s Instagram Automation section first.
2. **`NEXT_PUBLIC_SITE_URL` was unset in Vercel Production as of 2026-08-06**, per
   `PROJECT_HANDOFF.md`, which caused sitemap/OG/canonical/JSON-LD URLs to point at the internal
   `*.vercel.app` hostname instead of `www.epchisme.com`. UNKNOWN whether this has since been fixed —
   this repo has no way to inspect live Vercel env vars; verify by checking `www.epchisme.com/robots.txt`
   before assuming either way.
3. `PROJECT_HANDOFF.md` describes `allevents2.html` as "untracked"; that is stale/incorrect as of
   this review — `git ls-files --error-unmatch allevents2.html` confirms it **is** tracked. It is
   still unreferenced by any code path, so functionally inert either way, but don't repeat the
   "untracked" claim without re-checking.
4. `web/SETUP.md` reportedly still has some outdated dark-theme wording per `PROJECT_HANDOFF.md`
   (not independently re-verified here); prefer `web/src/app/globals.css` and `PROJECT_HANDOFF.md`
   for current UI style if a discrepancy is found.

## 10. Fast Orientation for a New Agent

1. Orient with the graph first — cheap, no repo reading required:
   ```bash
   export PATH="$HOME/.local/bin:$PATH"
   graphify god-nodes --top 15
   cat graphify-out/GRAPH_REPORT.md
   ```
   Graph stats: 992 nodes, 2156 edges, 51 communities, built from commit `45125a99` — run
   `git rev-parse HEAD` and compare; run `graphify update .` if the graph is stale.
2. **Single best first question to ask the graph for this repo:**
   `graphify query "how does an event flow from a source connector through the orchestrator into Storage, and how does the web app read it back"`
   — this repo's core complexity is the source → orchestrator → `Storage` → Supabase → Next.js
   pipeline (`Storage` and `HttpClient` are the top two god-nodes at 41 and 91 edges), so tracing
   that path answers most "where do I make this change" questions in one query.
3. Then read, in order: `README.md` → `src/scraper/core/models.py` →
   `src/scraper/core/orchestrator.py` → `src/scraper/sources/base.py` → `src/scraper/core/storage.py`.
4. For anything touching the Instagram pipeline specifically, read `PROJECT_HANDOFF.md`'s
   "Instagram Automation" section before writing code — the credential/token investigation there is
   long and you do not want to redo it.
5. Confirm current git state before assuming anything above is still accurate: `git log -1`,
   `git status`, and re-check `graphify-out/GRAPH_REPORT.md`'s freshness note.
