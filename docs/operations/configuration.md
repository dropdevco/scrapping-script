# Configuration

Every environment variable in the system: who reads it, what it defaults to, where the value comes
from, and what breaks without it.

**The project's convention:** *"EVERY key is optional: a source with no key simply disables itself and
the run continues without it"* (`.env.example:2-3`). That is true for the scraper's sources. It is
**not** true for Supabase, the Instagram credentials, or the web app's own keys.

Two files seed local setup — `.env.example` (repo root) and `web/.env.example` — and both are
incomplete. The tables here are the authoritative inventory.

---

## The empty-string trap

This deserves reading before anything else in this document, because the usual statement of it is
subtly wrong.

GitHub Actions expands an **unset repository variable to an empty string**, not to nothing. So
`FOO: ${{ vars.FOO }}` with no variable set puts `FOO=""` into the environment.

But the Python config layer normalises that away: `_clean` maps `""` → `None`, and `_int` / `_bool`
fall back to their defaults on empty input. The scheduler helpers and the TypeScript side (`|| default`)
do the same.

**So the precise danger is not "you get an empty string". It is:**

> You silently get the **code default**, and the code default may be wrong for production.

There is a live instance. `SITE_BASE_URL` is a repo *variable*, and its code default is
`https://epchisme.com` — **the bare apex, which 308-redirects.** The repo's own documentation says the
value must be the final `www` host, because *"Telegram does not follow redirects when delivering
updates, so a bare apex that 308s to www yields a registration that succeeds and then delivers
nothing."* If the variable is unset, CI falls back to a value the repo itself documents as broken.

`telegram-webhook --set` refuses to register a redirecting URL, which turns that into a loud failure —
but only at registration time.

**Two exceptions to the normalisation, worth knowing:**

- `web/src/app/admin/ig/page.tsx` and the review page read `IG_SLIDES_BUCKET` with `??` rather than
  `||`, so an **empty-string** value yields bucket `""` and every signed-URL call silently returns
  nothing. Their siblings use `||` and are immune.
- Anything reading `os.getenv` directly, without going through `config.py`, gets no protection.

---

## Legend

| | |
|---|---|
| **S** | Wired into a workflow as a GitHub Actions **secret** |
| **V** | Wired into a workflow as a GitHub Actions **variable** |
| **—** | Not wired into any workflow — local/dev only, or set in Vercel |
| **Vercel** | Must be set in the Vercel project |

---

## Supabase

Provider: Supabase Dashboard → Settings → API.

| Var | Consumer | Req | CI | Notes |
|---|---|---|---|---|
| `SUPABASE_URL` | scraper, social, kb | required to persist | S | Project URL |
| `SUPABASE_KEY` | scraper, social, kb | required to persist | S | **service_role** key. PostgREST only — cannot run DDL |
| `SUPABASE_DB_URL` | `apply_migration` only | admin-only | — | Direct Postgres URI. See [migrations](../data/migrations.md#how-they-are-applied) |
| `NEXT_PUBLIC_SUPABASE_URL` | web | **required** | Vercel | Client-exposed |
| `NEXT_PUBLIC_SUPABASE_ANON_KEY` | web | **required** | Vercel | Client-exposed; RLS-restricted, public by design |
| `SUPABASE_SERVICE_ROLE_KEY` | web admin surfaces | required for `/admin` | Vercel | **Never prefix with `NEXT_PUBLIC_`.** Unset → `supabaseAdmin()` throws and all admin surfaces fail |

Without `SUPABASE_URL`/`SUPABASE_KEY` the scraper still returns live results — it just persists nothing
and serves nothing from cache. The social pipeline, by contrast, exits 1 on every subcommand except
`build --dry-run`.

---

## Scraper behaviour and tuning

| Var | Default | CI | Purpose |
|---|---|---|---|
| `FRESHNESS_HOURS` | 24 | — | Freshness-cache window |
| `HTTP_MAX_CONCURRENCY` | 8 | — | Shared semaphore across the whole fan-out |
| `HTTP_TIMEOUT_SECONDS` | 20 | — | |
| `USER_AGENT` | `scraper-mcp/0.1 (+research bot)` | — | Sent on every request and used for robots evaluation. **Keep it accurate** |
| `ENABLED_SOURCES` / `DISABLED_SOURCES` | empty (all on) | — | Allow / deny lists by source name |
| `EVENT_TIMEZONE` | → `IG_TIMEZONE` → `America/Denver` | V (kb step only) | The timestamp invariant's zone. **Absent from `.env.example`** |
| `GEOCODE_VENUES` | `true` | — | **Absent from `.env.example`** |
| `GEOCODE_MAX_PER_RUN` | 25 | — | Nominatim is ~1 req/s. Leftovers are picked up next run |

> **Two wiring gaps here.** `ENABLED_SOURCES` / `DISABLED_SOURCES` are not plumbed into CI, so **there
> is no way to disable a misbehaving source in CI without a commit.** And `EVENT_TIMEZONE` is passed to
> the knowledge-base step but **not to the scrape step**, so the scrape runs on the hardcoded default
> and setting the repo variable would have no effect on it.

---

## Scheduling

Read by `scheduler.py`. **None of these appear in `.env.example`** — they are documented in the module
docstring and in [scraper-engine.md](../components/scraper-engine.md#scheduler-schedulerpy).

| Var | Shape / default | CI |
|---|---|---|
| `SCHEDULE_LOCATIONS` | **Semicolon**-separated. Default `"El Paso, TX;Ciudad Juarez, Chihuahua, Mexico"` | V |
| `SCHEDULE_LOCATION` | Singular back-compat override; **loses** to the plural | V |
| `SCHEDULE_TOPICS` | Comma-separated. Default `"AI,technology,business"` | V |
| `SCHEDULE_DAYS` | Int. `7` in code; **raised to 240 via repo variable** | V |

> **Trap:** a leftover `SCHEDULE_LOCATION` variable silently collapses the job to one city and kills
> Juárez coverage. Check it first if Juárez goes empty.

---

## Event, trend and research sources

| Var | Provider | CI |
|---|---|---|
| `TICKETMASTER_API_KEY` | developer.ticketmaster.com — Discovery API, free | S |
| `TAVILY_API_KEY` | tavily.com, free tier | S |
| `BRAVE_API_KEY` | brave.com/search/api, free tier | S |
| `REDDIT_CLIENT_ID` / `REDDIT_CLIENT_SECRET` | reddit.com/prefs/apps, app type **script** | S |
| `REDDIT_USER_AGENT` | self-authored | V |
| `YOUTUBE_API_KEY` | Google Cloud → YouTube Data API v3 | S |

DuckDuckGo, Hacker News and Google Trends need no keys. Ticketmaster is the **primary** events
provider — without it, coverage falls back to the keyless web and directory sources.

---

## Meta / Instagram

Provider: a Meta Developer app plus a linked Business/Creator Instagram account. Full setup:
[meta-instagram-onboarding.md](../meta-instagram-onboarding.md).

| Var | CI | Notes |
|---|---|---|
| `IG_ACCESS_TOKEN` | S | Long-lived, ~60 days. **Rotated by hand** — nothing in CI can rewrite a repo secret |
| `IG_BUSINESS_ACCOUNT_ID` | S | Use the **live-verified id from a real `/me` response**, not the number the developer console's linking table shows — they differ |
| `META_APP_ID` / `META_APP_SECRET` | S | Only used for `/debug_token` introspection and the Facebook-flow refresh. **Currently moot** — introspection is unsupported on this Graph host |
| `THREADS_ACCESS_TOKEN` / `THREADS_USER_ID` | — | Read by config and exposed by a helper, but **no caller uses them** |

---

## Instagram carousel behaviour

All repo **variables**.

| Var | Default | Purpose |
|---|---|---|
| `IG_TIMEZONE` | `America/Denver` | "Today" must be the local calendar day, not the runner's UTC one |
| `IG_HANDLE` | `epchisme.com` | Cover footer and caption link. Display-only |
| `IG_SLIDES_BUCKET` | `ig-slides` | The private Supabase bucket |
| `IG_MIN_SLIDES` | 4 | A hard **floor**, not a target — below it the build writes a `skipped` row and posts nothing |
| `IG_MAX_SLIDES` | 9 | Plus the cover = Instagram's 10-item cap |
| `IG_SLIDE_RETENTION_DAYS` | 7 | `prune` cutoff |
| `IG_SUGGESTED_PUBLISH_HOUR` | 17 | Local hour proposed for `scheduled_for` |
| `IG_AUTO_APPROVE` | `false` | **Opt-out posting master switch** |
| `IG_AUTO_APPROVE_HOUR` | = `IG_SUGGESTED_PUBLISH_HOUR` | The deadline hour |
| `IG_AUTOPOST` | `false` | Publishes at **build** time, **no Telegram ping at all** |
| `IG_DIGEST_SLOTS` | empty | e.g. `"morning:11,evening:18"`. Empty = one unnamed digest |

`IG_AUTOPOST` vs `IG_AUTO_APPROVE` is the single most confusable pair in the system — see the
[glossary](../glossary.md#ig_autopost-vs-ig_auto_approve).

Four of these are **also read independently on the Node side** and must agree across both
environments: `IG_TIMEZONE`, `IG_MIN_SLIDES`, `IG_AUTO_APPROVE_HOUR`, `IG_SLIDES_BUCKET`.

---

## Notifications

| Var | Kind | CI | Notes |
|---|---|---|---|
| `TELEGRAM_BOT_TOKEN` | secret | S + Vercel | From `@BotFather`. Missing → **the pipeline keeps publishing, blind** |
| `TELEGRAM_CHAT_ID` | secret | S + Vercel | **Comma-separated.** First entry = where notifications are sent; the whole list = chats permitted to act |
| `TELEGRAM_WEBHOOK_SECRET` | secret | S + Vercel | Must be **byte-identical in three places**: `.env`, Vercel, and the `setWebhook` call. A mismatch 401s every delivery with no outward sign except `getWebhookInfo`'s `last_error_message` |
| `SITE_BASE_URL` | **variable** | V (both workflows) | **Must be the final `www` host.** See [the trap](#the-empty-string-trap) |
| `RESEND_API_KEY` | secret | S | Resend — the fallback channel for "Telegram itself is broken" alerts |
| `NOTIFY_ADMIN_EMAIL` | secret | S | |
| `NOTIFY_EMAIL_FROM` | variable | V | Default is the Resend sandbox sender |
| `IG_NOTIFY_SECRET` | secret | S + Vercel | Signs the review link. Unset → no review link is minted, **email notify no-ops**, Telegram still works |
| `IG_NOTIFY_TTL_HOURS` | variable | V | Default 36 |

---

## Google Sheets knowledge base

| Var | Default | CI |
|---|---|---|
| `KB_SHEET_ID` | none — unset means not wired up | S |
| `KB_SHEET_TAB` | `events` | V |
| `KB_HORIZON_DAYS` | 60 | V |
| `KB_LOCATION` | `El Paso` | V |
| `GOOGLE_SERVICE_ACCOUNT_JSON` | none | S — the whole key file as one value |
| `GOOGLE_APPLICATION_CREDENTIALS` | none | — path alternative, local only |

> **The step everyone misses:** share the sheet with the **service account's email as an Editor**. It
> has no access until you do.

---

## Web app

### Client-exposed (inlined into the browser bundle at build time)

| Var | Missing ⇒ |
|---|---|
| `NEXT_PUBLIC_SUPABASE_URL` | Total failure — no reads, no auth |
| `NEXT_PUBLIC_SUPABASE_ANON_KEY` | Same |
| `NEXT_PUBLIC_SITE_URL` | Falls back to `VERCEL_URL`, then `localhost`. **In production this silently poisons every absolute URL** — sitemap, robots, canonical, OG, JSON-LD. A loud warning fires in production |
| `NEXT_PUBLIC_GOOGLE_MAPS_API_KEY` | `/map` renders the missing-key text. Needs **Maps JavaScript API** enabled and HTTP-referrer restrictions for the domain and `localhost` |

**Changing a `NEXT_PUBLIC_*` var requires a redeploy, not a restart.**

### Server-only

| Var | Missing ⇒ |
|---|---|
| `SUPABASE_SERVICE_ROLE_KEY` | `/admin`, `/admin/ig`, the review page and the Telegram webhook all fail |
| `ADMIN_EMAILS` | Empty allowlist → **nobody is an admin**; every admin action throws |
| `IG_NOTIFY_SECRET` | Every review magic link reads "expired or invalid" |
| `TELEGRAM_WEBHOOK_SECRET` | **Fails closed** — every webhook POST gets 401; all Telegram buttons dead |
| `TELEGRAM_CHAT_ID` | Empty allowlist → every update silently ignored, and dispatch-failure alerts go nowhere |
| `TELEGRAM_BOT_TOKEN` | No confirmations, no pickers, buttons never cleared |
| `GH_DISPATCH_TOKEN` | `triggerWorkflowJob` returns silently. "Publish now" and rebuilds fall back to the next 30-minute sweep |
| `IG_SLIDES_BUCKET` | Unset is fine (`??` default). **An empty string is not** — slide previews silently break |
| `IG_MIN_SLIDES`, `IG_TIMEZONE`, `IG_AUTO_APPROVE_HOUR` | Safe defaults |

> **Nine of these are absent from `web/.env.example`**: the five Telegram/IG ones, `GH_DISPATCH_TOKEN`,
> and the three `IG_*` tuning vars. `GH_DISPATCH_TOKEN` appears in **no** `.env.example` and **no**
> workflow — it exists only in the code and in
> [social-automation-onboarding.md](../social-automation-onboarding.md).

### Creating `GH_DISPATCH_TOKEN`

From `docs/social-automation-onboarding.md:105-126`: `github.com/settings/personal-access-tokens/new`,
**Fine-grained tokens** (not a GitHub App), switch the resource owner to the **org** that owns the
repo, scope **Actions: Read and write** only, longest available expiration. Fine-grained tokens
*require* an expiry, so this will break on a schedule unless someone is watching.

---

## Secrets hygiene

`.gitignore` covers `.env`, `.env.local`, `*.token.json`, `.secrets/`, `.vercel`. The workflow states
the rule plainly: *"Secrets/vars are read from the repo's Actions settings — never commit real keys."*

**A real credential leak happened and was fixed.** Every Graph call carries its token in the query
string, httpx puts the failing URL into the exception, and that string was written to `ig_posts.error`,
sent to Telegram, and logged — so one transient publish failure *"published a live `IG_ACCESS_TOKEN` to
the database, the CI log and the chat at once."* `redact_secrets()` now scrubs credential query params
where the message is built. **Any new code that writes or broadcasts exception text must go through
it.**

---

## Vercel CLI gotchas

When fixing env vars under pressure (`docs/social-automation-onboarding.md:62-87`):

- `vercel env add NAME production` works non-interactively, but adding to multiple environments asks a
  follow-up `? Git branch?` prompt that **hangs** if the value was piped in. Feed a blank second line:
  `{ echo "value"; echo; } | vercel env add NAME preview` — or use the dashboard, which has no branch
  prompt.
- The CLI needs a one-time **device-auth login**; a human must visit the `vercel.com/oauth/device` URL.
  Budget for one human click.
- Confirm with `vercel env ls` that the var landed in the right environment before assuming a redeploy
  will see it. **New vars only reach deployments created after the var was added.**

---

*Verified against commit `9157646` (2026-09-06). Last updated 2026-09-10.*
