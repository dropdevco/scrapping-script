# HANDOFF — Chisme (scrapping-script)

**Written 2026-10-11 against `main` @ `f5aa3b8`. Live state (database, Actions) checked the same day.**
This is the entry point. It says what the system is, what is broken *right now*, what you need access
to, and where the deep docs are. The deep docs are in [`docs/`](docs/README.md); `PROJECT_HANDOFF.md`
(to 2026-08-06) is a historical change log only. If this file and `docs/` disagree, `docs/` wins; if
`docs/` and the code disagree, the code wins.

---

## 1. What this is

One Supabase database (project `data-scrapping`, ref `vcijttnnopkedkwrcylb`), four surfaces for the
El Paso, TX / Ciudad Juárez, MX region:

| Surface | Code | Runs |
|---|---|---|
| Event scraper (~30 sources) | `src/scraper/core`, `sources` | GitHub Actions `scheduled_scrape.yml`, daily 11:00 UTC |
| Website `www.epchisme.com` (Next.js 16, bilingual) | `web/` | Vercel |
| Instagram carousels `@elpasochisme`, Telegram approval, publish | `src/scraper/social` | `ig_daily.yml` (chained to the scrape + publish sweep + weekly/monthly crons) |
| Knowledge-base Google Sheet → GoHighLevel chatbot | `src/scraper/kb`, `src/scraper/border` | after each scrape; `border_poll.yml` |

Start at [`docs/onboarding.md`](docs/onboarding.md) → [`architecture/overview.md`](docs/architecture/overview.md)
→ [`architecture/invariants.md`](docs/architecture/invariants.md). Backlog: [`docs/known-gaps.md`](docs/known-gaps.md).
Incidents: [`docs/operations/runbooks.md`](docs/operations/runbooks.md).

## 2. Do these first (nothing is on fire, but these are open)

Full detail for each is in `docs/known-gaps.md` → **P0**.

1. **Border wait times are not refreshing every 10 minutes.** GitHub cron fires `border-poll` every
   3–6 h. The fix (migration `0019`, `pg_cron` → `workflow_dispatch`) is merged but **not applied**:
   the live DB has no `pg_cron`/`pg_net`. Needs a GitHub fine-grained token (Actions read/write),
   running `0019` in the Supabase SQL Editor, and `vault.create_secret(... 'github_dispatch_token' ...)`.
   Steps: [`docs/components/border-wait-times.md`](docs/components/border-wait-times.md).
2. **Two border data bugs, unfixed:** Tornillo is mapped to port `240221` (permanently "Update
   Pending") instead of `240401` (`border/cbp.py:57`, `sources/borderswaittime.py:26`, `text.py:32`);
   and lanes CBP marks `N/A` are dropped from the snapshot so their DB rows go stale
   (`cbp.py::_lane`).
3. **Instagram token expires ≈ early December.** Re-minted 2026-10-07. Run
   `python -m scraper.social refresh-token` and update `.env` + the GitHub secret `IG_ACCESS_TOKEN`
   by late November. (Vercel doesn't use it.)
4. **Telegram "Postpone" expires the post** instead of delaying it (day-ahead posting kept today's date).
5. **Watch the first weekly (Sunday 13:07 UTC) and hype runs** — merged 2026-10-01, never observed.
6. Make `ci` a **required** check in GitHub branch protection (it exists but only advises).
7. `.diagram-design/` is an untracked folder nobody could identify. Left alone.

## 3. Access you need (values are never in the repo)

- **GitHub** `dropdevco/scrapping-script` — Actions secrets present: `SUPABASE_URL/KEY`,
  `IG_ACCESS_TOKEN`, `IG_BUSINESS_ACCOUNT_ID`, `IG_NOTIFY_SECRET`, `META_APP_ID/SECRET`,
  `TELEGRAM_BOT_TOKEN/CHAT_ID`, `OPENROUTER_API_KEY`, `TICKETMASTER_API_KEY`, `KB_SHEET_ID`,
  `GOOGLE_SERVICE_ACCOUNT_JSON`. Variables set: `IG_AUTO_APPROVE=true`, `IG_AUTO_APPROVE_HOUR=17`,
  `IG_TIMEZONE=America/Denver`, `SCHEDULE_DAYS=240`, `SITE_BASE_URL=https://www.epchisme.com`,
  `COUNCIL_ENABLED=true`, `COUNCIL_MODEL=anthropic/claude-haiku-4.5`. **Unset on purpose:**
  `BORDER_KB_TAB`, `IG_AUTOPOST`.
- **Supabase** project above (SQL Editor for migrations; `SUPABASE_DB_URL` is in local `.env`).
- **Vercel** (web app; env vars in [`docs/operations/configuration.md`](docs/operations/configuration.md)).
- **Meta developer app + the Instagram account**, **Telegram bot**, **OpenRouter**, **Google service
  account** for the sheet. Setup playbooks: [`meta-instagram-onboarding.md`](docs/meta-instagram-onboarding.md),
  [`social-automation-onboarding.md`](docs/social-automation-onboarding.md).
- Local: copy `.env.example` → `.env`; **use the project venv** (`.venv\Scripts\python.exe`) — global
  Python lacks `psycopg`. On this Windows machine `python`, never `python3`.

## 4. How things are done here (hard-won)

- **Every event edit in Supabase must be followed by `python -m scraper.kb export`** or the chatbot sheet
  goes stale.
- **Unset `vars.*` arrive in CI as empty strings**, not code defaults (the engine's `_clean()` treats
  `""` as unset; your own code may not).
- **`SITE_BASE_URL` must be the `www` host** — the bare apex 308s and Telegram won't follow it.
- **PostgREST caps selects at 1000 rows silently** — paginate with `.range()` in any whole-table sweep.
- **Content pillars are Instagram-only** (`events.content_tags`); they never replace `categories`.
- **Changing a natural key forks every stale row on the next scrape** — converge identity hashes first,
  twice.
- Never put a "tear me off" affordance on flyer graphics.
- Migrations are applied **by hand**; there is no tracking table, and production has drifted from the
  repo before (0018 formalised schema applied directly). Diff live schema against the files before
  related work. Don't apply via the Supabase connector — use `python -m scraper.apply_migration`.
- Tests: `.venv\Scripts\python.exe -m pytest -q` (819 collected). CI runs them with no `SUPABASE_*`.
  One border test, `test_sources_are_read_at_the_same_time_not_one_after_another`, is timing-sensitive
  and failed once locally under load (limit 0.7 s); re-run before assuming a regression.
  CI tests must use the Denver clock, not `date.today()` (UTC on runners) — that flake bit twice.
- Commit bodies are unusually detailed and are the record for decisions; ADRs distil them.
- After code changes in this repo: `graphify update .` (a graph exists in `graphify-out/`).

## 5. How we got here (by date)

- **Aug 2026** — scraper, site, IG carousel pipeline; Telegram approval; `PROJECT_HANDOFF.md` kept to Aug 6.
- **Sep 4–15** — opt-out posting with deadline sweep, post kinds (weekend/monthly/horizon), Google Maps
  swap, region restriction everywhere, KB sheet export, `docs/` set written (Sep 6–10).
- **Sep 15–17** — daily digest posts a **day ahead** (pushed Sep 17; first one covered Sep 18);
  migration `0011` (RLS on `runs`, `ig_post_edits`) applied and verified.
- **Sep 20** — dedupe hardening (address normalizer, cross-venue merge), Instagram content pillars,
  editorial council (localness auditor, editor, critic), on-slide blurbs, Eventbrite time fixes.
- **Oct 1** — Enrique/Kuike's border wait-times feature (PR #8, migrations 0013–0017) merged; follow-ups
  (#9: utf-8 fixtures, `mcp<2` pin, tab-collision guard); CI test gate (#10); weekly pillar posts and hype
  spotlights (#11, migration 0018) after a review that fixed an outage-poisons-hype bug, slot/cron
  timing, a day-ahead lead floor and hype twin-dedupe; `pg_cron` poll clock (#12, migration 0019, **unapplied**).
- **Oct 7** — IG token had expired (HTTP 400 on `/me`); re-minted, secret updated, `ig-daily` run green.
  Audit of the border feed against live CBP data found the Tornillo and `N/A` bugs and measured the
  real poll cadence (~16 polls in 3 days).

## 6. Related work in other repos (not this project)

The same session list also holds `medora-marketplace`, `northwell`, `premier-real-state-site` and
`dropdev-skills` sessions. They are separate codebases and out of scope here.

---

*Verified against commit `f5aa3b8`; live DB and Actions state checked 2026-10-11.*
