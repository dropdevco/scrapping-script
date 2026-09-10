# ADR-0011: Swallowed failures are made loud, everywhere

- **Status:** Accepted
- **Date:** Recognised as a pattern 2026-08-12 (`6d02158`, `7abde58`); applied since
- **References:** `src/scraper/social/publish.py:170-226`, `src/scraper/social/telegram.py:1-16`, `web/src/lib/ig/githubDispatch.ts:21-38`

A cross-cutting principle rather than a single structural decision. It earns an ADR because **the same
bug class was found and fixed in five places, in two languages**, and the reasoning was generalised
explicitly each time.

## Context

This system is unattended. It runs on cron, publishes to a public account, and reports to a Telegram
chat. The canonical statement of the problem is in `notify.py:40-44`:

> "a failure that the code already caught and handled correctly (logged, written to the DB) is still
> invisible to whoever's waiting on Telegram: the bot went quiet, and **'went quiet' and 'is fine' look
> identical** unless something says otherwise."

The five instances:

| Instance | The swallow | Cost |
|---|---|---|
| `token_expires_in_days()` caught an introspection failure and returned `None`, which `check_token()` reported as "expiry unknown, non-expiring" and exited 0 | *"The safety net built specifically to catch this never fired."* | A **two-day outage** with a blocked token (`OAuthException`, "API access blocked") while the check ran green |
| `fetch` does not reject on non-2xx, so an expired `GH_DISPATCH_TOKEN` returned 401 and the call looked successful | *"Found this auditing for the same failure-swallowing pattern just fixed in `publish.token_expires_in_days` — **same bug, two languages.**"* | "Publish now" silently stopped being immediate |
| Telegram answers **HTTP 200 with `{"ok": false}`** for "chat not found", "bot was blocked", "bot was kicked from the supergroup" | A status-only check reads every one as success | Notifications silently stopped |
| `alert()` itself went through a second copy of `sendMessage` that checked neither status nor `ok` | *"the alert about a broken dispatch could itself fail without a trace"* | The alerting path was unmonitored |
| `/debug_token` 500s on `graph.instagram.com` **regardless of token health** | The check "cried wolf" on every run | Would have kept failing through a genuine expiry — an alarm that is always on is no alarm |

Note the last one is the **inverse** failure, and it matters: making introspection fatal was correct
for `graph.facebook.com` and wrong here, where the call fails always. Loud is not the same as noisy.

## Decision

Four rules, applied system-wide.

**1. Never convert a failure into a benign-looking value.** `token_identity()` deliberately lets its
exception propagate — *"failing this call IS the finding."* `token_expires_in_days` propagates too;
`None` now means only *"this token type does not expire"*, and only a genuinely successful response
with no `expires_at` field can produce it.

**2. Check the status, and the body, on every HTTP call.** `fetch` needs an explicit status check.
Telegram needs an `ok` check on top of that — `telegram.call()` is the single chokepoint for every
Telegram request in Python, and `tgCall` is its counterpart in TypeScript, both created by collapsing
duplicated unchecked copies.

**3. When a check cannot be performed, say so — do not pass silently.**
`TOKEN_INTROSPECTION_SUPPORTED` is computed from the Graph host, and when introspection is unavailable
`check-token` reports that explicitly *"so nobody reads a green check as 'expiry verified'."*

**4. The alert channel must not depend on the thing it reports on.** *"An alert delivered over the
channel it is reporting on is not an alert."* So `check-telegram` reports over **email** (Resend) and
exits non-zero so **GitHub itself emails the repo owner** — *"Both, because either alone gets missed."*

## The structural expression

The `preflight` job is the cleanest embodiment and is worth understanding as a pattern
(`ig_daily.yml:89-94`):

- It is **not** a dependency of `build` — *"a broken notification channel must not stop today's draft
  being made."*
- It is **fatal on failure** — so GitHub's own failed-job email becomes the one alert path that does
  not depend on Telegram working.

Why a broken notification channel is worth failing a job over, given opt-out posting: *"a broken
channel means posts ship with nobody able to stop them, which is worse than a noisy CI failure."*

## Alternatives rejected

| Rejected | Why |
|---|---|
| Log and continue | Logs are not read unless something points at them. This is the failure mode, not a fix for it. |
| Make every check fatal | The `/debug_token` case shows the cost: a check that always fails trains everyone to ignore it. Distinguish "cannot check" from "check failed". |
| Alert only over Telegram | An alert over the broken channel is not an alert. |
| Alert only by email | Email alone gets missed; the non-zero exit is the redundant path. |
| `preflight` as a `needs:` dependency of `build` | Would let a dead notification channel block a draft that is otherwise fine. |

## Consequences

**Cost: noise.** `preflight` fires on **all 28 publish-sweep ticks per day** plus the calendar crons,
so a genuinely unhealthy webhook produces ~28 failed-job emails a day rather than one. Accepted for
now; noted in [known-gaps.md](../../known-gaps.md).

**Loudness must not leak credentials.** Making failures visible means writing error text to
`ig_posts.error` and broadcasting it to Telegram and email — and every Graph URL carries
`?access_token=…`, which httpx puts into every exception. One transient publish failure **published a
live access token to the database, the CI log and the chat at once** before this was fixed. Redaction
is therefore a precondition of this ADR, not an unrelated concern — see
[invariants §4](../invariants.md#error-text-is-redacted-before-it-is-written-or-sent).

**Where to apply it next.** Any new `fetch`/`httpx` call, any new third-party integration, and any
`except` block that returns a default instead of raising. If a caller cannot distinguish "it worked"
from "it failed and I handled it", that is this bug.
