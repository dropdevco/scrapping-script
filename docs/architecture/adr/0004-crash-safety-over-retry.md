# ADR-0004: Crash safety and idempotency over retry-by-default

- **Status:** Accepted
- **Date:** 2026-08-06 (`366d857`), amended 2026-09-03 (`7051940`)
- **References:** `src/scraper/social/publish.py`, `src/scraper/social/__main__.py:703-820`, `src/scraper/core/storage.py:399-467`

## Context

Publishing a carousel is three sequential Meta Graph API calls — create a child container per slide,
create a carousel container listing them, publish the carousel. The process can die between any two
of them. Meanwhile the publish sweep runs every 30 minutes and a slow run *will* eventually overlap
the next one.

The asymmetry that drives everything here: **a duplicate public Instagram post costs far more than a
missed one.** A missed post is invisible. A duplicate is visible to the audience, cannot be quietly
undone, and makes the account look broken.

## Decision

**A state machine with terminal states.** `draft → approved → publishing → published`, with terminal
`rejected`, `skipped`, `expired`, `failed`. Partial unique indexes allow at most one *live* post per
`(post_date, slot)` for digests and per `(kind, period_key)` for everything else — while deliberately
excluding the terminal-but-discarded states, so a rejected or skipped row never blocks rebuilding
that date.

**Compare-and-swap on every transition.** `UPDATE … WHERE id = ? AND status = '<expected>'` returning
affected rows. Zero rows means somebody else got there first, and losing is handled cleanly rather
than overwritten. Two reasons, both in code: overlapping runs would otherwise double-post
(`storage.py:402-405`), and **a human tapping Cancel at 16:59:59 must beat the 17:00 sweep**
(`storage.py:451-453`).

**The container id is persisted before the publish call.** `on_container` is awaited with the carousel
container id *before* `media_publish` (`publish.py:157-158`):

> "That ordering is the crash-safety hook: if the process dies mid-publish, the persisted container
> id is what tells recovery 'this one reached Meta — do not blindly retry it', because a duplicate
> public post costs far more than a missed one."

**Staleness is checked before any network call.** `publish` verifies `post_date == today` first — a
post approved late that only succeeds the next morning would otherwise publish "TOMORROW IN EL PASO —
Aug 6" (built Aug 5 as advance notice for Aug 6's events) on Aug 6 itself, with the whole point of the
early warning gone. Described in the handoff as *the single most important guard in the feature.*

**Idempotent-by-query, not by cron precision.** Metrics collection selects "published long enough ago
**and** missing this window" rather than "published exactly N hours ago", so a skipped run backfills
on the next sweep instead of losing the window forever. Bounded to 30 days so enabling a new window
does not trigger a backfill over the account's whole history.

**Retention prunes on a lag, not at publish time.** One prefix sweep cleans up published, rejected,
expired **and** orphaned uploads (upload succeeded, row insert failed) with no per-status
bookkeeping — and it leaves a half-failed publish something to retry against.

## The 2026-09-03 amendment

Under opt-in, the publisher wrote `status='failed'` on the first error. Because `failed` is terminal —
`approved_ready_to_publish` only selects `approved` — **this made the `attempts < 3` cap dead code.**
One transient hiccup (Meta refusing to fetch the signed slide URLs is the common one) permanently lost
that day's post. It **took out 3 of 25 days in one sample month**, and nobody noticed because under
opt-in most days were never published anyway.

Amended: a retryable error returns the row to `approved` for the next sweep.

```
retryable = attempts < MAX_PUBLISH_ATTEMPTS and not reached_meta
```

**Never retried once `ig_creation_id` exists** — that id is persisted before `media_publish` precisely
so recovery knows the carousel reached Meta.

## Alternatives rejected

| Rejected | Why |
|---|---|
| Blanket retry on failure | Risks a duplicate public post once the carousel has reached Meta. |
| Treating `failed` as the first-error destination | Made the attempt cap dead code and lost 3 of 25 days. |
| Relying on the workflow's `concurrency` group alone | `concurrency: ig-daily` is explicitly described as belt-and-braces: *"The CAS claim in the publisher is the real guard; this just avoids the wasted work of racing to it."* A queued run still eventually executes. |
| Advisory locks / `SELECT … FOR UPDATE` | PostgREST offers no locking. CAS on a status column is the available primitive. |
| Deleting rejected/expired rows to free the unique index | Loses the audit trail. Excluding them from the *partial* index achieves the same thing without deletion. |
| Pruning slides at publish time | Would not clean up orphaned uploads from half-failed builds, and would destroy a failed publish's ability to retry. |

## Consequences

**Child containers degrade rather than fail.** A child that errors or never readies is skipped; the
whole post fails only if survivors fall below `ig_min_slides`, raising with the ratio
(`publish.py:135-149`).

**Children are created sequentially, never with `gather()`** — Graph rate-limits bursts and the
semaphore is shared with every other fetch in the process.

**`dry_run` is genuinely useful.** It builds and validates every container, then stops short of
publishing; the containers expire harmlessly in 24h. That exercises token scope, URL reachability and
aspect-ratio acceptance with zero risk — and it hands the row back to `approved` so a real run can
still publish it.

**A rebuild that loses its final CAS abandons rather than overwrites.** Zero rows means a human (or
the sweep) approved the post mid-rebuild, so the carousel may already be at Meta
(`storage.py:560-563`).

**Gap.** `publish_carousel`'s child-skip and `min_children` logic has **no test coverage** — only the
token helpers are tested in `test_publish.py`. Tracked in [known-gaps.md](../../known-gaps.md).
