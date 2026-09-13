# Known Gaps

A triaged backlog of everything known to be broken, missing, unfinished, or unverified — with file
references so any item can be picked up cold.

This list was produced by a full audit of the codebase at commit `9157646`. Items marked **deliberate**
are design decisions, not defects; they are here so nobody "fixes" them by accident.

**Keep this file current.** When you fix something, delete the entry in the same commit. When you find
something, add it.

---

## P0 — migration written, needs applying and verifying

### Row-level security was never enabled on `runs` or `ig_post_edits`

No migration through 0010 contains an `enable row level security` statement for either table. Every
other `public` table has one. On Supabase, a `public` table without RLS is reachable through PostgREST
**by the anon key**, which is published in the browser bundle.

`ig_post_edits` is the serious one: it drives what gets dropped from a carousel that publishes
publicly.

- **Fix written:** `supabase/migrations/0011_internal_table_rls.sql` enables RLS with zero policies (a
  deny-all) on both, matching what `0004` and `0008` do for the other internal tables.
- **Not yet applied to the live database.** Needs `SUPABASE_DB_URL` (not available in every
  environment) and `python -m scraper.apply_migration supabase/migrations/0011_internal_table_rls.sql`
  — see [migrations.md](data/migrations.md#how-they-are-applied). The Supabase MCP tool cannot run this
  DDL even if it's connected; do not attempt it that way.
- **Verify after applying:** query `relrowsecurity` on both tables, and confirm an anon-key read on
  each now returns nothing.
- **Refs:** [schema.md](data/schema.md#two-tables-whose-rls-fix-is-written-but-not-yet-applied), all
  eleven files in `supabase/migrations/`.

---

## P1 — reliability and correctness

### Nothing in CI runs tests, lint, or the web build

239 tests exist and none execute in CI. Neither does `ruff check`, `npm run lint`, or `npm run build`.
A commit that breaks an import reaches the 11:00 UTC scrape directly.

Most pointedly: `tests/social/test_scheduling.py` parses `ig_daily.yml` to catch a scheduling regression
that would otherwise be invisible for half a year — and it never runs.

- **Fix:** a PR-validation workflow running `pytest`, `ruff check .`, and `cd web && npm run lint &&
  npm run build`.
- **Refs:** [ci-cd-and-deployment.md](operations/ci-cd-and-deployment.md), `.github/workflows/`.

### The social pipeline and the KB export are El Paso-only

`social/__main__.py:37` hardcodes `CITY = "El Paso"`, passed to a `location ILIKE '%El Paso%'` filter.
`KB_LOCATION` defaults to `"El Paso"` with the same effect. So **a Ciudad Juárez event whose `location`
lacks "El Paso" can never reach a carousel or the chatbot's knowledge base** — even though the scraper,
the website and the map all now cover Juárez.

Nothing in the code justifies the restriction; it looks like a leftover from before Juárez coverage
landed.

- **Decide first:** is a single bilingual carousel wanted, or separate per-city posts? That changes the
  fix substantially (the cover kickers, the caption and the period/slot uniqueness indexes all encode
  "EL PASO").
- **Refs:** `src/scraper/social/__main__.py:37,130`, `src/scraper/core/storage.py:293`,
  [ADR-0005](architecture/adr/0005-region-restriction-everywhere.md#consequences).

### `EVENT_TIMEZONE` is not passed to the scrape step

Compare the env blocks of `Run scrape` and `Publish knowledge-base sheet` in `scheduled_scrape.yml`:
`EVENT_TIMEZONE` appears only in the latter. The scrape therefore runs on the hardcoded
`America/Denver` default, and **setting the repo variable would have no effect on it.**

Harmless today because the default is correct — but it means the timestamp invariant's zone is not
actually configurable where it matters most.

- **Refs:** `.github/workflows/scheduled_scrape.yml:33-46` vs `:56-65`, `src/scraper/core/config.py:62-66`.

### There is no way to disable a misbehaving source in CI

`ENABLED_SOURCES` / `DISABLED_SOURCES` exist and work, but are **not plumbed into either workflow**. So
turning off a source that has started returning garbage requires a commit and a push.

- **Fix:** add both to the scrape step's env block as `${{ vars.* }}`.
- **Refs:** `src/scraper/core/config.py:73-74`, `.env.example:21-22`.

### `SITE_BASE_URL`'s code default is a value the repo documents as broken

The default is the bare apex `https://epchisme.com`, which **308-redirects**. The repo's own docs say
the value must be the final `www` host, because Telegram does not follow redirects when delivering
updates. If the repo variable is unset, CI silently falls back to the broken value.

- **Fix:** change the code default to the `www` form, and/or set the repo variable explicitly.
- **Refs:** `src/scraper/core/config.py:193`, `.env.example:98-102`,
  [configuration.md](operations/configuration.md#the-empty-string-trap).

### Nothing pins the three-way `address_hash` parity

`sha1(lower(trim(address)) + "|" + lower(trim(venue_name)))` is implemented in Python, SQL **and**
TypeScript, and all three must stay byte-identical or the browser's submit form creates duplicate venue
rows. All three files warn about it. **No test checks it.**

- **Fix:** a parity test. Cheap, high value — a good first task.
- **Refs:** `src/scraper/core/storage.py:39-45`, `supabase/migrations/0002_venues.sql:9-12`,
  `web/src/lib/hash.ts:12-15`.

---

## P2 — maintainability, hygiene, small fixes

### `IG_SLIDES_BUCKET` uses `??` where its siblings use `||`

`admin/ig/page.tsx:10` and `review/[token]/page.tsx:15` use `?? "ig-slides"`, so an **empty-string**
value — which is what an unset variable becomes on some platform paths — yields bucket `""` and every
`createSignedUrls` call silently returns nothing, rendering cards with no slides. `IG_MIN_SLIDES`,
`IG_TIMEZONE` and `IG_AUTO_APPROVE_HOUR` all use `||` and are immune.

**Fix:** change both to `||`. Two characters, twice. Good first task.

### Two `.env.example` files are incomplete

- Root: missing `SCHEDULE_LOCATIONS`, `SCHEDULE_LOCATION`, `SCHEDULE_TOPICS`, `SCHEDULE_DAYS`,
  `EVENT_TIMEZONE`, `GEOCODE_VENUES`, `GEOCODE_MAX_PER_RUN`.
- `web/`: missing nine server-only vars the app reads — `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`,
  `TELEGRAM_WEBHOOK_SECRET`, `GH_DISPATCH_TOKEN`, `IG_NOTIFY_SECRET`, `IG_SLIDES_BUCKET`,
  `IG_MIN_SLIDES`, `IG_TIMEZONE`, `IG_AUTO_APPROVE_HOUR`.

`GH_DISPATCH_TOKEN` appears in **no** `.env.example` and **no** workflow. Good first task; the complete
inventory is in [configuration.md](operations/configuration.md).

### No migration-tracking table

Which migrations have been applied to the live project is tracked by humans and by filename prefix only.
Adding a `schema_migrations` table and backfilling it with 0001–0010 would be small and high-value.

### Storage buckets are not in version control

`ig-slides` exists only because someone called `create_bucket()` once. A fresh environment has no bucket
and no migration will create one. Worth either a provisioning script or an explicit onboarding step.

### No rollback procedure is documented

For any surface. Vercel can promote a previous deployment and Python has no deploy to roll back, but
none of this is written down. A short runbook section would do.

### `scheduled_scrape.yml` hygiene

No `cache: pip` (every daily run reinstalls pandas and gspread cold), and no `concurrency` group, so two
overlapping runs are possible.

### `preflight` produces ~28 failure emails a day when unhealthy

It fires on every publish-sweep tick. Loud is the point ([ADR-0011](architecture/adr/0011-make-swallowed-failures-loud.md)),
but once a day would be loud enough. Consider gating it on the first tick of the day.

### `GH_DISPATCH_TOKEN` expiry has no alerting

Fine-grained tokens require an expiration, so "Publish now" will silently stop being immediate on a
schedule. The failure is detected and alerted *when a dispatch is attempted*, not proactively.

### Untested, load-bearing code

`core/orchestrator.py`, `core/dedupe.py`, `core/storage.py`, `core/geocode.py`, `sources/registry.py`,
every directory parser, `slides_store.py`, and `publish_carousel`'s child-skip / `min_children` logic.
See [testing.md](engineering/testing.md#coverage-gaps) for a ranked list of where to start.

### Duplicate venue rows

The same building holds two or three `venue_id`s when sources punctuate its address differently. There
are **two independent read-side defences** — `selection.venue_key` keys on the venue *name*, and the map
groups pins by *coordinate*. The comment naming the real fix is explicit: *"the duplicate venue rows
themselves are a storage-side problem and want a real address normalizer; this is the read-side
defense."*

### Hardcoded constants that will surprise someone

| Constant | Where | Risk |
|---|---|---|
| Owner / repo / workflow filename | `web/src/lib/ig/githubDispatch.ts:3-5` | A repo rename or fork silently breaks every "now" action |
| `"America/Denver"` ×3 | `lib/datetime.ts:13`, `admin/ig/PostCard.tsx:9`, `review/RescheduleForm.tsx:6` | Changing the zone means touching four places, one of which is Python |

### Stale comments and dead weight

- `imaging.fetch_photo`'s docstring says an unusable photo makes the event lose its slot. It no longer
  does — there is a text-only layout.
- `metrics.py`'s module docstring names a function `fetch_media_metrics` that does not exist; the real
  name is `fetch_insights`.
- `geist` is declared in `web/package.json` and imported nowhere.
- `web/README.md` is untouched `create-next-app` boilerplate.
- `allevents2.html` (560 KB at the repo root) is referenced by no code path and is probably why GitHub
  reports HTML as the primary language. **Do not delete without asking** — provenance is unclear.
- Some comments and Spanish strings carry mojibake from a prior encoding issue. Do not spread it.

### `venues_insert_authenticated` is `with check (true)`

Any signed-in user can insert an arbitrary venue row from the browser — which is exactly what the submit
form does. Dedupe relies on `address_hash` uniqueness, not validation. Worth knowing as an abuse surface;
a constraint or a moderation pass would close it.

---

## Unfinished, and deliberate

These are recorded decisions. Do not "fix" them without reading the reasoning.

| Item | Status |
|---|---|
| **Metrics do not feed into selection scoring** | **Deliberate.** No per-slide attribution exists, and reach is 6–10 per post — too little signal to tune weights on without fitting noise. `category_bias` is a declared seam with a revisit condition: ≥60 published posts with t24 metrics |
| **No "past events" browse mode** | **Deliberate.** Every query sets a lower time bound |
| **`breaking` post kind** | Implemented and CLI-reachable, but no cron fires it and the workflow does not offer it. Scope was explicitly limited to what the schema needed |
| **`horizon` is not on the cron calendar** | Deferred because its data blocker (`SCHEDULE_DAYS`) was unresolved at the time. That has since been raised to 240, so this **may now be ready to schedule** — unverified whether the deferral is still intentional |
| **`THREADS_ACCESS_TOKEN` / `THREADS_USER_ID`** | Read by config, exposed by a helper, used by nothing. Scaffolding for a surface that does not exist |
| **pgvector semantic search** | Scoped in `0001_init.sql:78-81`, left commented out |
| **Juárez coverage is "real but modest"** | Most Juárez sites need bespoke HTML parsers rather than generic JSON-LD. Two were added 2026-09-06 |

### The one genuinely open external problem

**The long-lived Meta token exchange (`ig_exchange_token`) fails with "Session key invalid"** for
console-issued tokens. Wrong secret, invalid/expired token and unaccepted tester invite have all been
explicitly ruled out with evidence. The leading unconfirmed theory is that console "Generate token"
tokens may not be exchange-eligible by design, and that a genuine OAuth authorization-code redirect flow
is required.

**The documented instruction is not to re-derive this.** Start from
`docs/meta-instagram-onboarding.md:290-312`. `refresh-token` plus a manual secret paste is the working
rotation path in the meantime.

---

## Unverified — needs someone with console access

None of these are inspectable from the repository. Each is worth five minutes from whoever has access.

| Question | How to check |
|---|---|
| Has `0011_internal_table_rls.sql` been applied yet? | Query `relrowsecurity` on `runs` / `ig_post_edits`. **This is the P0 above** |
| Is `NEXT_PUBLIC_SITE_URL` set in Vercel Production? | `curl https://www.epchisme.com/robots.txt` and look at the `Sitemap:` host |
| Which Actions secrets and variables are populated, and to what? | Repo settings. Note `SITE_BASE_URL`, `SCHEDULE_DAYS`, `SCHEDULE_LOCATIONS` and `SCHEDULE_LOCATION` in particular |
| Is `IG_AUTO_APPROVE` / `IG_AUTOPOST` on? | Repo variables. Both default `false`; the docs are written as though opt-out is the intended steady state |
| Is the Instagram pipeline currently posting daily? | The account, and the `ig_posts` table |
| Which migrations have been applied to the live project? | Compare the live schema against `supabase/migrations/`. There is no tracking table |
| Are PRs / branch protection / code review used in practice? | GitHub settings. No PR template, CODEOWNERS or protection is visible from the repo |
| What Node version does the web app target? | No `engines` field and no `.nvmrc`; `web/SETUP.md` says 20.9+ |
| Is `IG_HANDLE=epchisme.com` vs the `@elpasochisme` account intentional? | It is display-only branding, so possibly yes |

---

## Good first tasks

Small, well-bounded, and each improves something real:

1. **Change `??` to `||`** for `IG_SLIDES_BUCKET` in two files.
2. **Complete both `.env.example` files** from [configuration.md](operations/configuration.md).
3. **Write the `address_hash` parity test** (P1 above).
4. **Fix the two stale docstrings** in `imaging.py` and `metrics.py`.
5. **Remove the unused `geist` dependency** and replace `web/README.md` with something true.
6. **Add the CI validation workflow** — bigger, but the highest-leverage item on this page.

---

*Verified against commit `9157646` (2026-09-06). Last updated 2026-09-10.*
