# Known Gaps

A triaged backlog of everything known to be broken, missing, unfinished, or unverified — with file
references so any item can be picked up cold.

This list was produced by a full audit of the codebase at commit `9157646`, then updated through `f5aa3b8` (2026-10-11). Items marked **deliberate**
are design decisions, not defects; they are here so nobody "fixes" them by accident.

**Keep this file current.** When you fix something, delete the entry in the same commit. When you find
something, add it.

---

## P0 — act on these first (verified live 2026-10-11)

### Border wait times are NOT refreshing every 10 minutes — migration 0019 was never applied

GitHub Actions cron is best-effort: after the border feed merged, `border-poll` fired roughly once
every 3–6 hours (6 `border_runs` in the last 24 h, against 144 expected). PR #12 moved the clock into
Postgres (`pg_cron` + `pg_net` dispatching `workflow_dispatch`), **but it is inert until manual steps
are done, and none have been**: on 2026-10-11 the live `data-scrapping` database has neither the
`pg_cron` nor `pg_net` extension. The agreed freshness (10 min, agreed 2026-09-29) is not met.

1. Create a GitHub fine-grained token for this repo, **Actions: read and write**; calendar its expiry
   (dispatch stops silently when it lapses).
2. Run `supabase/migrations/0019_border_poll_dispatch.sql` in the Supabase SQL Editor (it was
   dry-run-validated in a rolled-back transaction on production).
3. `select vault.create_secret('<token>', 'github_dispatch_token', 'border-poll dispatch');`
4. Verify `net._http_response` shows `204`s and `border_current_waits.checked_at` advances every ~10 min.

Full steps and check queries: [border-wait-times.md](components/border-wait-times.md).

### Two data bugs in the border feed (found auditing PR #8, still unfixed)

- **Tornillo is mapped to the wrong CBP port.** `src/scraper/border/cbp.py:57` (and
  `sources/borderswaittime.py:26`, `text.py:32`) use port `240221`, a generic "El Paso" entry CBP leaves
  permanently "Update Pending". The real crossing is **`240401`** ("Marcelino Serna / Tornillo"), which
  reports real lane numbers. Effect: Tornillo always reads "no data" and the chatbot says so. Live check
  2026-10-11: zero rows for `240401`.
- **Lanes CBP flips to `N/A` stop updating.** `_lane()` in `cbp.py` returns `None` for `N/A`, so the lane
  drops out of the snapshot and its row is never rewritten. A lane that was "open, 25 min" and then goes
  `N/A` would keep showing 25 min with an old `checked_at`. 4 rows were >1 day stale on 2026-10-11.
  Contradicts the module's "no lane silently drops out" promise.

### Instagram token expires about early December 2026

`IG_ACCESS_TOKEN` was re-minted on 2026-10-07 after the 60-day token lapsed (the daily run failed with
HTTP 400 on `GET /me`; fix = Generate token in the Meta app, then update `.env` and the GitHub secret —
Vercel does not read it). **Run `python -m scraper.social refresh-token` and update both places by late
November**, inside the 60-day window; an expired token cannot be refreshed. The token was pasted into a
chat transcript — regenerate it if that matters. The `ig_exchange_token` "Session key invalid" problem
is unchanged (see below).

### Telegram "Postpone" makes a post expire instead of posting later

Since the digest moved to day-ahead posting (2026-09-16), Postpone moves the post to 5 pm tomorrow but
keeps today's date, so the stale check expires it. Raised 2026-09-17; no fix is committed.

## P1 — reliability and correctness

### CI gates tests only — not web lint/build, repo-wide ruff, or branch protection

`ci.yml` (added 2026-10-01, PR #10) runs the ~819 pytest tests and `ruff check` on `src/scraper/border`
and `tests/border` only. Still ungated: `npm run lint` / `npm run build` for `web/`, repo-wide
`ruff check` (363 findings at the time), and — most importantly — **`ci` is not a required status
check**, so it advises rather than blocks. Whether branch protection is on is unverified.

- **Fix:** make `ci` required (Settings → Branches); add a web lint+build job; burn down the ruff
  backlog and widen the scope.
- **Refs:** [ci-cd-and-deployment.md](operations/ci-cd-and-deployment.md#ci--the-test-gate).

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

### Nothing pins the Python/TypeScript `address_hash` parity

`sha1(lower(trim(address)) + "|" + lower(trim(venue_name)))`, computed over the output of the address
normalizer (`core/address.py::venue_identity`, ported line-for-line to `web/src/lib/hash.ts`), is
implemented in both Python and TypeScript, and both must stay byte-identical or the browser's submit
form creates duplicate venue rows. Parity across 12 hand-picked real-world cases (accents, ZIP+4,
number-words, suite markers) **was** manually verified during the 2026-09-20 normalizer rollout, which
narrows this gap — but **no committed automated test pins the two implementations going forward**, so a
future edit to either side can silently drift without anything catching it.

- **Fix:** a parity test, ideally property-based or fixture-driven from a shared JSON case list both
  runtimes read. Cheap, high value — a good first task.
- **Refs:** `src/scraper/core/address.py`, `web/src/lib/hash.ts`,
  [scraper-engine.md](components/scraper-engine.md#venue-identity-coreaddresspy).

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
Adding a `schema_migrations` table and backfilling it with 0001–0012 would be small and high-value.

**This is not hypothetical — it already happened once.** Before migration `0018_weekly_and_hype_posts`
existed, `ig_posts.kind`'s CHECK constraint, an `ig_posts.pillar` column, and a live unique index were
all already applied directly against the production database, with zero rows using any of it and zero
application code anywhere in the repo referencing it. Caught only by manually diffing the live schema
against every committed migration file before starting related work, and formalized into 0018 rather
than left silent. A `schema_migrations` table with a CI check would have caught this automatically.

### `ig_post_reviews` is a live table with no migration and no code

Discovered during the same 0018 drift audit: `ig_post_reviews` (`id`, `post_id`, `kind`, `pillar`,
`metrics` jsonb, `review` jsonb, `created_at`) exists on the live database, empty, with no migration
file and no reference anywhere in `src/` or `web/src/`. Shape suggests a planned post-performance
review feature (metrics + a review verdict, keyed by kind/pillar), but its purpose hasn't been
confirmed with anyone, so nothing here builds on or assumes it. Left alone rather than guessed at.

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

`core/orchestrator.py`, `core/geocode.py`, `sources/registry.py`, every directory parser,
`slides_store.py`, and `publish_carousel`'s child-skip / `min_children` logic. `core/dedupe.py` and
`core/storage.py` gained targeted tests (`tests/core/`) for venue identity, hash stability and the
cross-venue duplicate predicate during the 2026-09-20 dedupe hardening, but that is coverage of the new
surfaces specifically, not full coverage of either module. See
[testing.md](engineering/testing.md#coverage-gaps) for a ranked list of where to start.

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
| **A hype post's cover/caption copy is static, not event-specific** | **Deliberate, v1.** `("DON'T MISS THIS", "EL PASO")` and a fixed opener list, the same shape every other kind's cover/caption already uses. Printing the actual artist/event name on the cover itself is a natural fast-follow — it would need `render_cover`'s signature to accept event-specific text, which no other kind needs today |
| **Hype judgments are not build-to-build reproducible** | **Deliberate**, same posture as the editor/critic council roles: `hype_from_rules` only ever returns `False` or defers to the model — "major enough to deserve its own post" is a judgment call, not a rule, so two judging passes over a borderline event could disagree. Determinism lives only in the deterministic pre-pass (no tickets + not a major venue ⇒ definite no), never in the model's yes |
| **Editorial council verdicts are not build-to-build reproducible** | **Deliberate.** Editor/critic are model judgment, not a pure function of input — `temperature` is unavailable on current models, so determinism is enforced only at the referee's bounds (exclude caps, floors, discard-on-mass-exclusion, clamped score deltas), never at the content. Two builds of the same pool can legitimately disagree on which events the editor excludes |
| **Weekly + hype first runs not yet observed** | Merged 2026-10-01 (PR #11). The first Sunday build (13:07 UTC) should produce six pillar drafts that approve at 9/11/12/14/15/17 local and publish at 10/12/13/15/16/18; hype runs after each scrape and should yield few drafts. Confirm in Telegram and `ig_posts`. Cancelled hype drafts keep their event spent (deliberate). Review items left open: manual hype override not protected from re-judging; skipped weekly rows write the pillar into `slot`; one event can land in two same-day weekly posts; `--dry-run` still spends model calls; hype step has no `continue-on-error`; 0018's header says "additive only" but drops an index |
| **`BORDER_KB_TAB` is unset** | Deliberate: the crossing-times tab is not published to the GoHighLevel sheet until someone is ready. Must differ from `KB_SHEET_TAB` (the code refuses equal names) |
| **A "Deals" content pillar** | Explicitly low-priority, deferred as a content-strategy decision (promote chain venues to build reach, then de-emphasize and monetize local) rather than a code change. `venues.chain_scope` is the data groundwork it would need; the "drop chains once reach grows" lever, `ScoreProfile.chain_venue_penalty`, is already tunable without a deploy |
| **Junk values in `events.categories`** (`accessible_forward`, `paid`, a stray `Friendly`) | Deferred — website-facing cleanup, out of scope for the pillar/council work. `categories` is union-only on write (never shrinks), so these persist until a dedicated backfill targets them directly |

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
| Is `NEXT_PUBLIC_SITE_URL` set in Vercel Production? | `curl https://www.epchisme.com/robots.txt` and look at the `Sitemap:` host |
| Which Actions secrets and variables are populated, and to what? | Repo settings. Note `SITE_BASE_URL`, `SCHEDULE_DAYS`, `SCHEDULE_LOCATIONS` and `SCHEDULE_LOCATION` in particular |
| Is `IG_AUTO_APPROVE` / `IG_AUTOPOST` on? | `IG_AUTO_APPROVE=true` (hour 17, Denver) is set as a repo variable as of 2026-10-11; `IG_AUTOPOST` is not set |
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

*Verified against commit `f5aa3b8` (2026-10-01); live state checked 2026-10-11. Last updated 2026-10-11.*
