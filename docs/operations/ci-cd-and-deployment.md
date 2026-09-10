# CI/CD and Deployment

Two GitHub Actions workflows, one Vercel project, one Supabase project. No PR-validation workflow of
any kind.

> **The most important fact in this document:** nothing in CI runs `pytest`, `ruff check`,
> `npm run lint` or `npm run build`. There are 239 tests and none of them execute in CI — including the
> cron-drift guard that exists precisely to catch a half-year-invisible scheduling regression. **Run
> tests locally; there is no safety net.** Tracked in [known-gaps.md](../known-gaps.md).

---

## `scheduled-scrape` — the data pipeline

| Property | Value |
|---|---|
| Cron | `0 11 * * *` — 11:00 UTC daily = **05:00 MDT / 04:00 MST** El Paso |
| Manual | `workflow_dispatch` with `job` ∈ `all` (default) / `events` / `trends` |
| Runner | `ubuntu-latest`, Python 3.12, **no pip cache** |
| Timeout | 20 minutes |
| Concurrency | **None declared** — two overlapping runs are possible |
| Install | `pip install -e ".[trends,kb]"` |

Two steps:

1. **Run scrape** — `python -m scraper.scheduler <job>`. **Fatal** on failure.
2. **Publish knowledge-base sheet** — `python -m scraper.kb export`. Skipped for trends-only runs (no
   event rows changed), and `continue-on-error: true` because *"a Sheets outage must not fail the
   scrape that already succeeded."*

Consequence of that second flag: **a failed export is invisible in the workflow's status.** You will
only see it in the step log.

---

## `ig-daily` — the Instagram pipeline

Four triggers:

| Trigger | Schedule | Purpose |
|---|---|---|
| `workflow_run` on `scheduled-scrape` completing | — | Builds the daily digest |
| `schedule` | `0,30 13-23,0-2 * * *` — 28 ticks/day | The publish sweep |
| `schedule` | `0 18 * * 4` — Thursdays | Builds the `weekend` post |
| `schedule` | `0 18 1 * *` — the 1st | Builds the `monthly` post |
| `workflow_dispatch` | — | `job` ∈ `both`/`build`/`publish`/`prune`/`rebuild`; `kind`; `post_id` |

Concurrency group `ig-daily`, `cancel-in-progress: false`.

### Cron in local time

| Cron (UTC) | MDT (summer) | MST (winter) |
|---|---|---|
| `0,30 13-23,0-2` | 07:00 – 20:30 | 06:00 – 19:30 |
| `0 18 * * 4` | Thu 12:00 | Thu 11:00 |
| `0 18 1 * *` | 1st 12:00 | 1st 11:00 |

**The sweep window's shape is a bug fix, not an arbitrary range.** A 17:00 Denver deadline is 23:00 UTC
in MDT but **00:00 UTC the next day** in MST, so the old `0,30 14-23` window covered summer and
silently missed every winter post — which then expired unpublished. It must straddle midnight UTC.
Test-enforced by a test that parses this YAML file.

### The four jobs

| Job | Runs when | Timeout |
|---|---|---|
| `preflight` | Every event **except** a dispatch of `prune` or `rebuild` | 10 min |
| `build` | Dispatch (not publish/rebuild), or a **successful** `workflow_run`, or the calendar crons | 20 min |
| `rebuild` | Dispatch only, `job == 'rebuild'` | 15 min |
| `publish` | Schedule **not** matching the calendar crons, or dispatch (not build/rebuild) | 15 min |

All four: `ubuntu-latest`, Python 3.12 with pip cache, `pip install -e ".[social]"`.

### Step-level failure behaviour

This is the operationally important part.

| Job | Step | On failure |
|---|---|---|
| preflight | `check-telegram` | **FATAL — deliberately.** See below |
| preflight | `check-token` | FATAL |
| build | `check-token` | `continue-on-error` — a dead token must not stop today's draft being built for review |
| build | `prune` | FATAL |
| build | `build --kind $KIND` | FATAL |
| rebuild | `apply-edits --post-id` | FATAL |
| publish | `apply-edits` | FATAL |
| publish | `autoapprove` | FATAL |
| publish | `publish` | FATAL |
| publish | `metrics` | `continue-on-error` — a Meta insights outage must not fail a publish that worked |

**Step ordering in `publish` is load-bearing:** `apply-edits` runs *before* `autoapprove`, so a pending
edit is normally applied by the time the deadline is evaluated — and `apply-edits` in the sweep is the
*guarantee* backing the webhook's immediate dispatch, which is *"only latency"*.

**Why `preflight` is fatal but not a dependency.** It is deliberately **not** in `build`'s `needs`
chain — *"a broken notification channel must not stop today's draft being made"* — yet it exits
non-zero so **GitHub's own failed-job email becomes the one alert path that does not depend on Telegram
working.** With opt-out posting, a broken channel means posts ship with nobody able to stop them, which
is worse than a noisy CI failure. See
[ADR-0011](../architecture/adr/0011-make-swallowed-failures-loud.md).

Cost of that design: `preflight` fires on all 28 sweep ticks per day, so a genuinely unhealthy webhook
produces roughly **28 failed-job emails a day**, not one.

### Two meta-decisions embedded in the YAML

- **Chaining instead of an offset cron.** `build` hangs off `workflow_run` rather than its own schedule,
  because *"Actions cron routinely drifts 10-20 minutes and the scrape itself can take up to 20"*, so a
  fixed offset *"would only be a hope"*.
- **The posting calendar lives in cron, not Python.** *"Actions is already the only scheduler in this
  system, and a Python-side calendar would be a second place to get DST and 'which weekday is it in
  which timezone' wrong — while being invisible from the Actions UI, where a build that never fired
  looks exactly like one that fired and skipped."*

---

## Deployment topology

| Surface | Host |
|---|---|
| Python scraper, social, KB export | GitHub Actions, `ubuntu-latest`, Python 3.12 |
| Web app | Vercel, project `scrapping-script`, **root directory `web`** |
| Database, auth, object storage | Supabase — Postgres + RLS + the private `ig-slides` bucket |
| Telegram webhook receiver | The Vercel app, `POST /api/telegram/webhook` |
| MCP server | **Not deployed** — local stdio only |

There is **no `vercel.json`, no Dockerfile, no Terraform.** All Vercel configuration is in the
dashboard.

### Production URLs

| URL | Role |
|---|---|
| `https://www.epchisme.com` | Canonical site. **`www` is mandatory** |
| `https://epchisme.com` | Bare apex — **308-redirects**, and is the code default for `SITE_BASE_URL` |
| `https://www.epchisme.com/api/telegram/webhook` | Telegram webhook target |
| `@elpasochisme` | The Instagram account |

Note `IG_HANDLE` is configured as `epchisme.com` while the account posted to is `@elpasochisme`. The
handle is display-only branding on the slides, so they need not match — but it looks like a
discrepancy.

---

## How a change reaches production

| Surface | Path |
|---|---|
| **Python** | Merge to `main` → **nothing deploys.** The next workflow run checks out `main` and `pip install -e .` from it. So the change lands on the **next scheduled or dispatched run**, with no build artifact, no version pin and **no CI gate** — a commit that breaks an import reaches the 11:00 UTC scrape directly |
| **Web app** | Push to GitHub → Vercel auto-deploys |
| **Database** | **Not automated.** `python -m scraper.apply_migration <file>` by hand — see [migrations](../data/migrations.md) |
| **Actions env var** | Takes effect on the next run |
| **Vercel env var** | `NEXT_PUBLIC_*` are inlined at build time — a **redeploy** is required. New vars only reach deployments created *after* the var was added |

---

## The on-demand trigger path (web → CI)

The web app can start a workflow run, which is what makes "Publish now" and immediate rebuilds
possible.

`web/src/lib/ig/githubDispatch.ts` POSTs to the `ig_daily.yml` **workflow dispatch** REST endpoint with
`{ref: "main", inputs: {job, …}}` and a `Bearer GH_DISPATCH_TOKEN`. Wrappers:
`triggerImmediatePublish()` → `job=publish`, `triggerBuild()` → `job=build`, `triggerRebuild(postId)` →
`job=rebuild`.

Three things to know:

- **Best-effort by design.** A failure alerts Telegram and logs; it never throws. The sweep is the
  guarantee, so the cost of a failed dispatch is latency, not correctness.
- **`fetch` does not reject on non-2xx**, so the status is checked explicitly — an expired token
  returned 401 and the call looked successful until that was fixed.
- **Owner, repo and workflow filename are hardcoded constants**, so a repo rename or a fork silently
  breaks every "now" action.

`repository_dispatch` is not used.

---

## Local verification (what CI would run, if it ran anything)

```bash
# Python
pytest
ruff check .

# Web
cd web && npm run lint && npm run build
```

Do this before pushing. See the [verification contract](../engineering/conventions.md#the-verification-contract).

---

## Gaps in this area

- No CI runs tests, lint, or the web build.
- `scheduled_scrape.yml` sets no `cache: pip` and declares no `concurrency` group.
- **No rollback procedure is documented** for any surface.
- Fine-grained `GH_DISPATCH_TOKEN` expiry will break "now" actions on a schedule with no alerting until
  someone notices.

All tracked in [known-gaps.md](../known-gaps.md).

---

*Verified against commit `9157646` (2026-09-06). Last updated 2026-09-10.*
