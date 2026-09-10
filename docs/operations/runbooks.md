# Runbooks

Diagnostic playbooks for the failures this system actually has. Every command here is verified against
the CLI parsers.

**Start by reading the failure message.** This codebase reports *why* it did something unusually well —
a skipped build says exactly what it counted, and a health check names the host it expected.

---

## Command surface

```bash
# Scraper
python -m scraper.scheduler [all|events|trends]
python -m scraper.kb export [--days N --location X --dry-run --out FILE]
python -m scraper.apply_migration <file.sql>
python -m scraper.backfill_geocode [--repair --limit N] [--dry-run]
python -m scraper.backfill_merge_duplicates --dry-run        # ALWAYS dry-run first
python -m scraper.backfill_{categories,full_descriptions,event_timezones,missing_event_times,ticketmaster_images}

# Social
python -m scraper.social build [--date D --kind K --dry-run --out DIR]
python -m scraper.social publish [--date D --dry-run]
python -m scraper.social apply-edits [--post-id ID]
python -m scraper.social autoapprove [--date D]
python -m scraper.social prune
python -m scraper.social metrics
python -m scraper.social check-token [--min-days N]
python -m scraper.social check-telegram
python -m scraper.social telegram-webhook [--set]
python -m scraper.social refresh-token

# What is actually running
python -c "from scraper.sources.registry import status; [print(s) for s in status()]"
```

---

## The daily scrape produced nothing, or the carousel skipped

**Read the skip message first.** A real one:

> *"9 approved El Paso event(s) today / 1 event(s) have a usable photo / skipping: only 1 usable
> slide(s), minimum is 4."*

That is the system working correctly — `IG_MIN_SLIDES` is a hard floor, because a 3-slide "today in El
Paso" reads worse than posting nothing. A thin day legitimately posts nothing and writes a `skipped`
row.

1. **Check the photo gate.** Roughly half of live events carry no usable photo once dead links are
   filtered. `MIN_ABS_SIDE` is 320px, so a 305px thumbnail is rejected outright. This exact case was a
   real incident: Ticketmaster returned a small `ARTIST_PAGE` image first in an unordered list while a
   2048px original sat further down. Fixed; `backfill_ticketmaster_images.py` repairs stored rows and
   re-running is a no-op.
2. **Preview without touching anything:**
   ```bash
   python -m scraper.social build --dry-run --out ./_preview
   ```
   Then open the JPEGs. The fastest way to sanity-check any rendering or selection change.
3. **Re-run the scrape live:** `python -m scraper.scheduler events`. This is the documented way these
   bugs get found against production.
4. **Check the `runs` table.** Its `source_counts` is the only thing that distinguishes "a source found
   nothing" from "a source is silently broken".
5. **If Juárez specifically is empty:** check `vars.SCHEDULE_LOCATION` in repo settings — a leftover
   singular variable silently collapses the job to one city.
6. **If the horizon format is empty:** it needs `SCHEDULE_DAYS` raised; it builds cleanly on demand and
   skips gracefully otherwise.
7. **Remember the social pipeline is hardcoded to El Paso** (`CITY = "El Paso"`), so a Juárez event
   whose `location` lacks "El Paso" can never reach a carousel regardless of the scrape.

---

## Telegram notifications stopped arriving

The best-documented failure in the repo. Full background:
[social-automation-onboarding.md](../social-automation-onboarding.md).

```bash
python -m scraper.social check-telegram      # exits non-zero when unhealthy
python -m scraper.social telegram-webhook    # inspect the registration
python -m scraper.social telegram-webhook --set   # re-register
```

**Healthy looks like** `pending_update_count: 0` and no `last_error_message`.

The three classic causes, in order of how much time they have historically cost:

| Cause | Signature | Fix |
|---|---|---|
| **Redirect** | `"last_error_message": "Wrong response from the webhook: 308 Permanent Redirect"` | `setWebhook` accepts a bare apex, but **Telegram does not follow redirects when delivering.** `SITE_BASE_URL` must be the final `www` host. `--set` refuses a redirecting URL and names the host it should have used |
| **Secret mismatch** | Every delivery 401s, with no sign except `getWebhookInfo`'s `last_error_message` | `TELEGRAM_WEBHOOK_SECRET` must be byte-identical in `.env`, Vercel, and the `setWebhook` call |
| **Chat not allowlisted** | An unauthorized tap looks exactly like a delivered-but-ignored one — the webhook returns `{ok: true}` | Add the chat id to the comma-separated `TELEGRAM_CHAT_ID`, in **both** the repo secret and Vercel |

`check-telegram` reports over **email**, not Telegram, because *"an alert delivered over the channel it
is reporting on is not an alert."* It also exits non-zero so GitHub emails the repo owner.

**If you are re-creating the bot from scratch:** bots cannot message you first — you must send the bot
any message before it may send you anything. And read the chat id back from `getUpdates` rather than
guessing; before that first message it returns `{"result":[]}`, not an error.

---

## An Instagram token expired or died

```bash
python -m scraper.social check-token       # fails under --min-days (default 14)
python -m scraper.social refresh-token     # extends and PRINTS the new token
```

Then **paste the new token into the `IG_ACCESS_TOKEN` repo secret by hand** — nothing in CI can rotate
a repo secret on its own. Run the refresh well before the 60-day window closes, e.g. every 45 days. The
token must be ≥24h old and still valid: *"a refresh is not a resurrection."*

**Two historical traps — know these before trusting any check:**

1. `/debug_token` is a *Facebook-Login* endpoint, and `graph.instagram.com` answers it with a **500
   regardless of token health.** The check failed on every run and would have kept failing through a
   genuine expiry. Liveness is now probed with `/me`, and expiry is reported as "not introspectable for
   this token type" rather than silently passing.
2. Earlier, `token_expires_in_days()` **caught** the introspection failure and returned `None`, which
   `check_token()` reported as "expiry unknown, non-expiring" and exited 0 — *"the safety net built
   specifically to catch this never fired,"* hiding a **two-day outage** where the token was actually
   blocked.

**If reconfiguring from scratch:** use the live-verified account id from a real `/me` response, not the
number the developer console's linking table shows — they differ.

**Still open:** the long-lived exchange (`ig_exchange_token`) fails with "Session key invalid" for
console-issued tokens. Wrong secret, invalid token and unaccepted invite have all been explicitly ruled
out. **Do not re-derive this** — start from `docs/meta-instagram-onboarding.md:290-312`, whose leading
suggestion is to try the real OAuth redirect flow instead of the console's quick-test token.

---

## The Google Sheets export failed

**First: it does not fail the workflow.** The step is `continue-on-error` and skipped on trends-only
runs, so look in the step log, not at the run's status.

```bash
python -m scraper.kb export --dry-run          # row count, as-of date, first three titles
python -m scraper.kb export --out events.csv   # dump rows to CSV
```

| Symptom | Meaning |
|---|---|
| Exit 1, *"No upcoming events found — refusing to publish an empty sheet"* | **By design.** An export that finds nothing is a *scrape* problem, not a signal to wipe the knowledge base. **Diagnose the scrape**, not the export |
| Exit 1, *"Sheets export unavailable: …"* | Credentials. Check `KB_SHEET_ID` and `GOOGLE_SERVICE_ACCOUNT_JSON` — and the most common cause by far: **the sheet must be shared with the service account's email as an Editor** |
| GHL rejects the import with *"Required field cannot be null or empty"* | A blank cell reached the sheet. Every cell is supposed to fall back to `"Not listed"` |

`--out` is the recovery route when credentials break: it writes the CSV **before** attempting the
Sheets write, so the CSV survives and can be uploaded by hand.

**House rule:** any change to stored event data is followed by `python -m scraper.kb export`.

---

## "Publish now" or a Telegram button does nothing immediate

`GH_DISPATCH_TOKEN` is the usual cause. `triggerWorkflowJob` returns silently when the token is absent,
and on a non-2xx it logs and pushes a Telegram alert naming the token:

> *"Couldn't trigger {job} on GitHub Actions ({status}) — check GH_DISPATCH_TOKEN."*

Fine-grained tokens **require** an expiration, so this breaks on a schedule unless someone is watching.

**Functionally the failure costs latency, not correctness** — the sweep's `apply-edits` and `publish`
steps are the guarantee. The button's database write still happened; it just waits for the next
30-minute tick.

Other possibility: a repo rename or fork. The owner, repo and workflow filename are hardcoded
constants.

---

## A deploy broke the site

> **There is no documented rollback procedure.** Vercel's dashboard can promote a previous deployment;
> that is currently tribal knowledge rather than a written runbook. Writing one is tracked in
> [known-gaps.md](../known-gaps.md).

1. **Verify locally first, always:** `cd web && npm run lint && npm run build` — required for routing,
   server-component, Supabase-query or Next-config changes.
2. **Absolute URLs pointing at `*.vercel.app`** (sitemap, robots, canonical, OG, JSON-LD) is the known
   signature of `NEXT_PUBLIC_SITE_URL` being unset in Vercel Production. This shipped once and a
   GoHighLevel crawler reported "no sitemap found". Fix: set it to `https://www.epchisme.com` and
   **redeploy** — it is inlined at build time. Verify from outside with `www.epchisme.com/robots.txt`.
3. **"Could not find a relationship between 'events' and 'venues'"** → migration `0002_venues.sql` has
   not been applied. Not a code bug.
4. **The map shows no pins** → `select count(*) from venues where lat is not null`. If it is low, run
   `python -m scraper.backfill_geocode`.
5. **"Redirect URI mismatch"** → the OAuth app is missing the exact `…/auth/callback` URL. Remember
   there are **two** allowlists in two different dashboards: Google Cloud Console and Supabase.
6. **Nobody can sign in with Google** → in Google Cloud Console's "Google Auth Platform" UI, check
   **Audience → Publishing status is "In production"**. "Testing" silently blocks every non-allowlisted
   account.
7. **Local sign-in fails but production works** → `.claude/launch.json` has `"autoPort": true`, so the
   dev server may not be on 3000, which is what the dev callback is registered against. Free the port
   and set `autoPort: false`.

---

## A duplicate or wrong-looking event

| Symptom | Likely cause |
|---|---|
| The same event appears twice | Two rows with different `content_hash` — usually different URLs from two ticketing sites that the fuzzy matcher did not merge. Inspect with `backfill_merge_duplicates --dry-run` |
| An event is on the wrong day | Almost always a timezone issue. Check whether the source publishes a naive wall clock or a real instant, and re-read [ADR-0002](../architecture/adr/0002-local-timestamp-invariant.md) |
| Description is truncated | Cross-run merge never overwrites a present-but-truncated value. Run `backfill_full_descriptions`. If it is newly broken, suspect the two Bootstrap-class CSS selectors or the `__NEXT_DATA__` key search |
| Start time is exactly midnight | The listing gave a date only. `backfill_missing_event_times` visits the detail page. These rows never self-heal |
| A pin is in the wrong city | Region-matching or the geocode fallback. **The failure mode does not throw** — re-run a live pass and eyeball `location`. See [ADR-0005](../architecture/adr/0005-region-restriction-everywhere.md) |
| An event 404s on its own page with a valid id | The region filter applies to `fetchEvent` too. Intended |

---

## Sanity checks

```bash
# What sources are active right now?
python -c "from scraper.sources.registry import status; [print(s) for s in status()]"

# Is the token alive?
python -m scraper.social check-token

# Is the notification channel healthy?
python -m scraper.social check-telegram

# What would today's carousel look like?
python -m scraper.social build --dry-run --out ./_preview

# What would the knowledge base say?
python -m scraper.kb export --dry-run

# Are absolute URLs right in production?
curl -s https://www.epchisme.com/robots.txt
```

---

*Verified against commit `9157646` (2026-09-06). Last updated 2026-09-10.*
