# ADR-0003: Human approval for posting, inverted to opt-out

- **Status:** Accepted
- **Date:** Stage 1 2026-08-06 (`366d857`), Stage 2 2026-08-07 (`85093ad`), Stage 3 2026-09-03 (`7051940`)
- **References:** `src/scraper/social/__main__.py`, `src/scraper/core/config.py:127-141`

This decision evolved in three stages. **The reasoning changes at each stage**, so all three are
recorded — the final state is not comprehensible without the path.

## Context

The carousel publishes to a real public Instagram account. An automated post that is wrong — a
cancelled event, a mis-parsed time, a cropped flyer, someone else's photo — is visible to the
audience and cannot be quietly fixed. At the same time, a pipeline that requires a human every day
only works if that human actually shows up every day.

## Stage 1 — opt-in review (2026-08-06)

**Decided.** `build` renders slides and files a `draft`. A human reviews the actual rendered images
and caption — over Telegram (slide photos plus inline Approve/Reject buttons) or a token-gated email
link — and approval triggers publishing.

**Why.** Explicit product direction: *approve manually until the output is trusted, then automate
100% by flipping one config value.* The system was designed so going unattended is **a one-variable
flip, not a future rewrite**: `IG_AUTOPOST=true` makes `build` call **the exact same `publish()`** a
human's tap triggers — not a parallel code path (`config.py:106-110`).

**Also decided here:** Telegram is the auth boundary for its own buttons, so the common path needs no
token; email has no channel-level guarantee, so its link carries an HMAC-signed expiring token
(`notify.py:1-15`).

## Stage 2 — "publish now" must mean now (2026-08-07)

**Problem.** "Publish now" only flipped `scheduled_for` and waited for the next sweep — up to 30
minutes, daytime only. *Not "now" by any real definition.*

**Decided.** The database write still happens first (so the sweep remains a correct fallback), and
the action *additionally* fires a GitHub `workflow_dispatch`. Both mechanisms converge on the
identical downstream code path.

**Rejected: shortening the cron.** *"A 1-minute cron still isn't 'now', and burns far more CI minutes
than an on-demand call ever would."* The generalised rule, from
`docs/social-automation-onboarding.md:128-146`:

> "Eventually, unattended" → a scheduled sweep. "Right now, because a human asked" → an on-demand
> trigger. Don't try to fake this by shortening the sweep interval.

## Stage 3 — inversion to opt-out (2026-09-03)

**Why it changed.** Opt-in *"did not survive contact with a busy month — **17 of the last 25 drafts
were built, notified, and never touched**, which is why the account posted on five scattered days
instead of daily."*

**Decided.** A draft carries an `auto_approve_at` deadline. If the deadline arrives with the row
still `draft`, the `autoapprove` sweep reads the silence as consent and approves it, stamping
`approved_by = 'auto'` so editorial intent stays distinguishable from opt-out shipping. Cancelling,
postponing and editing all beat it through the same CAS idiom.

The notification wording changed with it, deliberately: under opt-out the lead line reads *"Goes out
automatically at {when} unless you cancel"*, because *"a post that ships unless you stop it,
announced by a message whose main button says 'Approve', trains exactly the wrong habit"*
(`notify.py:161-168`).

### Two switches, not one

The most confusable pair in the system, and the distinction is deliberate (`config.py:127-141`):

| Switch | Behaviour |
|---|---|
| `IG_AUTOPOST` | Publishes at **build** time and **skips the Telegram ping entirely**. No review window at all. |
| `IG_AUTO_APPROVE` | Keeps the ping and the all-day window. Only removes the requirement that someone tap Approve. |

Both ship `false`. If `IG_AUTOPOST` is on, the post is gone before `IG_AUTO_APPROVE` is ever
consulted — which is why `autoapprove` *announces that it is inert* rather than looking like a silent
no-op.

### Why the sweep is a step, not its own cron

`autoapprove` runs as a step immediately before `publish` in the same job, for two stated reasons: a
post crossing its deadline ships in that same run instead of up to 30 minutes later, and there is no
second schedule to keep DST-correct — the deadline is a stored `timestamptz`, so the comparison is
right in both halves of the year no matter when the sweep fires (`social/__main__.py:588-593`).

## Alternatives rejected

| Rejected | Why |
|---|---|
| Fully automated from the start | Output was not yet trusted; a bad public post is not quietly fixable. |
| Staying opt-in | Measured failure: 17 of 25 drafts expired unseen. |
| A separate auto-approve cron | A second schedule is a second place to get DST wrong. |
| Making `IG_AUTOPOST` the opt-out switch | It skips review entirely; opt-out is supposed to *keep* the review window and only drop the mandatory tap. |
| Shortening the publish sweep to fake immediacy | See Stage 2. |

## Consequences

**The review window became load-bearing**, which surfaced three latent bugs in the same commit:

1. `failed` was written on the first publish error, making the `attempts < 3` cap dead code — one
   transient Meta refusal permanently lost the day, **taking out 3 of 25 days**. See
   [ADR-0004](0004-crash-safety-over-retry.md).
2. `sendMediaGroup` shared a `try` block with the message carrying the buttons, so **one unreachable
   slide URL silently swallowed the only way to act on the draft from a phone.** They are independent
   sends now, buttons first.
3. Telegram's HTTP-200-with-`ok:false` replies were unchecked in both languages.

**It motivated metrics collection** ([ADR-0009](0009-edit-intents-as-rows.md)): *"posting ships
without anyone's say-so, so 'is this working?' needs an answer that does not depend on someone
remembering to open the app."*

**Safety properties of the migration.** `IG_AUTO_APPROVE` ships off, and pre-existing drafts have a
NULL deadline which is **explicitly not read as "overdue"** — otherwise enabling the feature would
have published a month of stale backlog on first run (`storage.py:426-429`).

**A held-back post alerts once, not every sweep.** The `held:` marker in `ig_posts.error` is what
stops the sweep nagging every 30 minutes for the rest of the day.
