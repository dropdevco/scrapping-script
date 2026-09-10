# Migrations

Ten migrations, `0001_init` through `0010_ig_post_kinds`, applied by hand. There is no migration
framework, no tracking table, and no automation — **which is exactly why the discipline below matters.**

---

## The house standard: additive only

Stated in `0002_venues.sql:1-2` and restated in 0003 through 0006:

> No drops, no deletes, no alterations of existing columns. Safe to run against a live database with
> data.

In ten migrations there is exactly **one** drop, and it is of an *index*, replaced in the same file by
a superset — behaviour-preserving by construction because every existing row collapsed to the old key.

Every new column gets a default or is nullable, so pre-existing rows keep behaving exactly as before.
Several migrations say so explicitly in a comment; copy that habit.

---

## Inventory

| # | File | What it changed | What it tells you |
|---|---|---|---|
| 0001 | `init` | `events`, `trends`, `runs`; indexes; the `touch_last_seen()` trigger. Leaves a commented pgvector block | The original product was a generic scraper-to-Supabase pipeline, not yet a consumer site |
| 0002 | `venues` | `pgcrypto`; `venues`; `events.venue_id/status/submitted_by`; a CTE backfill mining `events.raw` per source; **all RLS and all six policies** | The pivot to a public, venue-and-map-centric frontend with submissions and moderation |
| 0003 | `ticket_links` | `events.ticket_links jsonb` | The same real event is scraped from several ticketing sites; keep every purchase link on one row instead of one card per site |
| 0004 | `ig_posts` | The table, its status CHECK, the live-date partial unique index; RLS **with zero policies** | The carousel pipeline arrives as internal state, explicitly never public |
| 0005 | `ig_scheduling` | `scheduled_for` | "Approved" stopped meaning "publish whenever the sweep finds it" — posting at 3am is worse than not posting. NULL preserves the old behaviour exactly |
| 0006 | `ig_kind_slot` | `kind`, `slot`; **drops** the live-date index for a `(post_date, slot)` one | First multi-format groundwork. The one drop in the repo |
| 0007 | `ig_auto_approve` | `auto_approve_at`, `approved_by`, `window_start/end`; the auto-approve index | Posting inverted to opt-out, driven by measurement: 17 of 25 drafts expired unseen. NULL deadlines on existing rows are deliberate — the migration cannot retroactively publish a backlog |
| 0008 | `ig_post_metrics` | The table, unique + fetched indexes; RLS, no policies | Opt-out publishing needs a feedback signal that does not depend on anyone looking |
| 0009 | `ig_post_edits` | The table + pending index; `photo_overrides`, `caption_is_custom`. **Does not enable RLS** | Telegram-driven edits, shaped by the serverless/Pillow split |
| 0010 | `ig_post_kinds` | Widens the `kind` CHECK to five values; `period_key`; the live-period index | Four formats over one renderer |

---

## Idempotency: partial, and know where

Every `create table`, `create index`, `alter table add column` and `drop index` uses
`if not exists` / `if exists`. `create or replace function` plus `drop trigger if exists` make 0001
fully re-runnable.

**Two things are not idempotent:**

1. **`create policy` has no `if not exists` form.** Re-running `0002_venues.sql` on a database that
   already has the policies fails with `policy … already exists`.
2. **`0010`'s `add constraint`** is guarded only by the `drop constraint if exists` on the line above,
   which makes the pair re-runnable but not line-independent.

Both are safe in practice, because of how migrations are applied — see the next section.

The 0002 backfill itself *is* idempotent: `on conflict (address_hash) do nothing`, and the linking
update is guarded by `where e.venue_id is null`.

---

## How they are applied

```bash
pip install -e ".[admin]"          # once — brings in psycopg
python -m scraper.apply_migration supabase/migrations/0003_ticket_links.sql
```

**Why not the normal Supabase key?** The `SUPABASE_KEY` used everywhere else talks to PostgREST, which
can insert, update and delete rows but **cannot run DDL**. The Supabase MCP tool available in some
environments is read-only too — *including its own `apply_migration` action*. Do not re-attempt DDL by
either path.

**`SUPABASE_DB_URL`** is required and is used by nothing else at runtime. Getting it:

- Supabase Dashboard → the **Connect** button at the top of the project page (*not* under Settings) →
  **Direct** tab → the **Session pooler** variant specifically.
- The plain `db.<ref>.supabase.co` hostname is IPv6-only and has not resolved from this environment;
  the pooler hostname does.
- The password in the URI Supabase shows is a literal `[YOUR-PASSWORD]` placeholder — Supabase never
  reveals the real one. Get it from the owner or reset it in the same area.
- URL-encode special characters in the password: `urllib.parse.quote(pw, safe="")`.

**Execution semantics:** the whole file is read as one string and executed as a single `cur.execute()`
inside one connection, then committed. **A migration file is therefore all-or-nothing** — a failure
anywhere rolls the entire file back. That is what makes the non-idempotent `create policy` in 0002 safe
to retry-and-fail rather than dangerous.

The alternative is pasting into the Supabase SQL Editor, which works fine for a one-off.

---

## Shipping a schema change

1. **Write the next numbered file**: `supabase/migrations/NNNN_description.sql`, zero-padded, strictly
   sequential.
2. **Keep it additive.** No drops, no deletes, no alterations of existing columns.
3. **Give every new column a default or make it nullable**, so pre-existing rows keep behaving exactly
   as before — and say so in a comment, as 0005, 0006 and 0007 all do.
4. **Use `if not exists` / `if exists`** on everything that supports it.
5. **If you must touch a named constraint, verify its real name against the live database first.** From
   `0010:7-11`: a name collision makes it `..._check1`, and `drop … if exists` on the wrong name
   silently drops nothing — so the subsequent `add constraint` fails or, worse, the old constraint
   survives.
6. **Consider RLS explicitly.** New tables do not get it by default, and two existing tables are
   missing it. If the table should be invisible to the anon key, enable RLS with zero policies (that is
   a deny-all) as 0004 and 0008 do.
7. **Apply it**, then verify the change from a real query rather than assuming.
8. **Update the code and [schema.md](schema.md) in the same commit.**
9. **Run `python -m scraper.kb export`** if the change touches stored event data, per the standing
   rule.

### Backfills

If the change needs existing rows repaired, write a separate `backfill_*.py` script rather than doing
it in SQL. The pattern is established: `--dry-run` first, re-runnable, and derive corrected values from
each row's own preserved `raw` payload rather than guessing. `backfill_event_timezones.py` is the model
— it re-runs the *fixed* parser over the original provider fields, so rows that were always correct
come out untouched.

Always `--dry-run` before `backfill_merge_duplicates.py`; it issues DELETEs.

---

## Known gaps in this area

- **No migration-tracking table.** Which migrations have been applied to the live project is tracked
  only by humans. Adding a `schema_migrations` table (and backfilling it with 0001–0010) would be a
  small, high-value change.
- **`runs` and `ig_post_edits` have RLS disabled.** See
  [schema.md](schema.md#two-tables-with-rls-never-enabled).
- **Storage buckets are not in version control.** `ig-slides` exists because someone called
  `create_bucket()` once; a fresh environment has no bucket.
- **No rollback procedure is documented anywhere**, for migrations or for deploys.

All tracked in [known-gaps.md](../known-gaps.md).

---

*Verified against commit `9157646` (2026-09-06). Last updated 2026-09-10.*
