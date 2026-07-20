# scraper-mcp

A reusable **MCP server** your agent calls to gather information on demand, then store it in
**Supabase** for later retrieval. Two headline jobs:

- **Events** — "give me all the events happening this week in El Paso"
- **Trends / content ideas** — "what's trending in AI / tech / business right now"

Plus general **web research** and a **query-stored** tool to read back what scheduled jobs already
collected.

Everything runs on **free tiers / keyless sources**. No source is required: whatever keys you
provide light up more sources; the rest self-disable and the run continues.

---

## What it does (and honestly, what it doesn't)

**Backbone (free / keyless):**

| Job | Sources |
|-----|---------|
| Events | Ticketmaster Discovery API (free key) + keyless open-web fallback: direct city pages on Eventbrite, Ticketmaster (public), and Meetup — empirically verified to serve crawlable schema.org/Event JSON-LD — plus a site:-scoped DuckDuckGo supplement |
| Trends | Reddit, Hacker News, YouTube, Google Trends |
| Web research | DuckDuckGo (keyless) + optional Tavily/Brave, with main-content extraction |

**Event addresses:** the `events.location` column holds the fullest address each source actually
publishes — usually a complete street address (`"125 Pioneer Plaza, El Paso, TX 79901, US"`) for
Ticketmaster and most web-sourced venues. When a source only publishes a city/region (or the event
is virtual and has no physical address at all), `location` falls back to whatever is available —
it's never fabricated. See `core/address.py` for the formatting logic.

**Own-account social (opt-in):** Instagram Graph + Threads official APIs. These are free and
ToS-compliant but **only read your own Business/Creator account** — your posts, insights, and IG's
capped *recent* hashtag media. They can't search a platform globally. They run only when you pass
`platforms=["instagram"]` / `["threads"]`.

**Deliberately out of scope:** no scraping of Instagram/Facebook/X/Threads via logged-in browser
sessions (violates their ToS, risks your accounts, breaks constantly). Global X/Twitter search isn't
free; a paid X source can be dropped in later as just another connector.

---

## Architecture

Three thin surfaces over one shared **core**:

```
mcp_server.py         scheduler.py (cron)
        \                   /
         \                 /
      core/ (orchestrator + sources + storage)
                    |
              Supabase (Postgres)
```

- **Source-connector pattern** — every source implements one tiny interface
  (`src/scraper/sources/base.py`). Adding a source = drop a file in `sources/` and list it in
  `sources/registry.py`. The orchestrator never changes.
- **Orchestrator** (`core/orchestrator.py`) — freshness-cache check → concurrent fan-out →
  normalize → dedupe → persist → run log. One dead source never fails the run.
- **Storage** (`core/storage.py`) — Supabase upsert on `content_hash` (idempotent dedupe) +
  freshness cache. If Supabase isn't configured, tools still work; nothing is persisted.

```
src/scraper/
  core/         models, config, http (retries/robots), storage, dedupe, orchestrator, timeutil
  sources/      events_*, trends_*, web_*, social_* (+ base, registry, auth_meta)
  mcp_server.py MCP tools (agent-facing)
  scheduler.py  curated recurring jobs
supabase/migrations/0001_init.sql
.github/workflows/scheduled_scrape.yml
```

---

## Setup

### 1. Install

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate   •   macOS/Linux: source .venv/bin/activate
pip install -e ".[trends]"          # [trends] adds pytrends/pandas for Google Trends
```

### 2. Configure

```bash
cp .env.example .env
```

Fill in what you have — all optional. Quick wins (all free):

- **Ticketmaster** (events): key at <https://developer.ticketmaster.com>
- **Reddit** (trends): create a **script** app at <https://www.reddit.com/prefs/apps> → client id + secret
- **YouTube** (trends): enable *YouTube Data API v3* in Google Cloud → API key
- **Tavily / Brave** (better web research): free-tier keys at tavily.com / brave.com/search/api
- **Hacker News & Google Trends**: no key needed

Check what's active any time via the `source_status` tool.

### 3. Supabase (storage)

1. Create a project at <https://supabase.com> → copy the **Project URL** and a **service_role** key
   into `SUPABASE_URL` / `SUPABASE_KEY`.
2. Run `supabase/migrations/0001_init.sql` in the Supabase **SQL Editor** (creates `events`,
   `trends`, `runs` + dedupe keys and the `last_seen` trigger).

### 4. Connect the MCP server to your agent

Run it over stdio:

```bash
python -m scraper.mcp_server
```

Example Claude Desktop config (`claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "scraper": {
      "command": "python",
      "args": ["-m", "scraper.mcp_server"],
      "env": { "PYTHONPATH": "src" }
    }
  }
}
```

(Or, after `pip install -e .`, use the `scraper-mcp` console command.)

---

## Tools the agent gets

| Tool | Purpose |
|------|---------|
| `search_events(location, start_date?, end_date?, categories?, query?, limit?, force_refresh?)` | Events in a place/time window |
| `find_trends(topic?, platforms?, timeframe?, limit?, force_refresh?)` | Trending topics / content ideas |
| `research_topic(query, depth?, limit?)` | Web research with content extraction (`depth`: shallow/standard/deep) |
| `query_stored(kind, location?, topic?, platform?, since?, until?, limit?)` | Read back stored rows from Supabase (no live scrape) |
| `source_status()` | Which sources are active right now |

Results share the shape `{count, items, sources, cached, ...}`. Within `FRESHNESS_HOURS`, event/trend
calls return cached rows instead of burning quota — pass `force_refresh=true` to override.

---

## Scheduling

`scheduler.py` runs curated, force-refreshed jobs into Supabase so `query_stored` is instant:

```bash
python -m scraper.scheduler          # all jobs
python -m scraper.scheduler events   # events only
python -m scraper.scheduler trends   # trends only
```

Configure with `SCHEDULE_LOCATION`, `SCHEDULE_TOPICS`, `SCHEDULE_DAYS`. The included GitHub Action
(`.github/workflows/scheduled_scrape.yml`) runs it daily — add your keys as repo **Secrets** and the
`SCHEDULE_*`/`REDDIT_USER_AGENT` values as repo **Variables**, or trigger it manually from the Actions
tab.

---

## Instagram + Threads (own-account) setup

These are opt-in and only read *your* account. One-time setup:

1. Create an app at <https://developers.facebook.com/apps> (type: **Business**).
2. **Instagram:** convert your IG account to **Business/Creator** and link it to a Facebook Page.
   Add the *Instagram Graph API* product. Generate a **long-lived** user token with
   `instagram_basic`, `pages_read_engagement`, `pages_show_list`. Put it in `IG_ACCESS_TOKEN`; put
   your IG business account id in `IG_BUSINESS_ACCOUNT_ID`.
3. **Threads:** add the *Threads API* product, authorize your account, generate a long-lived Threads
   token → `THREADS_ACCESS_TOKEN`, and set `THREADS_USER_ID`.
4. Set `META_APP_ID` / `META_APP_SECRET` so `auth_meta.refresh_long_lived` can extend the ~60-day
   tokens for scheduled jobs.

Then call e.g. `find_trends(platforms=["instagram"])` for your own recent media, or
`find_trends(topic="marketing", platforms=["instagram"])` for capped hashtag-recent media.

---

## Extending it

Add a source in ~40 lines: subclass `Source` (`name`, `kind`, `is_configured()`, async `fetch()`),
export `SOURCE`, and add the module name to `sources/registry.py`. Import heavy/optional libs
*inside* `fetch` so the registry stays importable without them. This is exactly how a future paid X
provider (e.g. via Apify) or a new city's event feed would slot in — no orchestrator changes.

Later upgrades reserved in the schema: `pgvector` embedding columns for semantic dedupe/search
(commented at the bottom of the migration).

---

## Notes on being a good web citizen

`core/http.py` sends an identifying User-Agent, honors `robots.txt` for arbitrary page fetches,
retries transient errors with backoff (respecting `Retry-After`), and caps global concurrency.
Keep `USER_AGENT` accurate and stay within each provider's free-tier limits.
