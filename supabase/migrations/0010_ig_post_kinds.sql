-- Four post formats instead of one.
--
-- The daily digest answers "what's on tonight". It cannot answer "what should
-- I get tickets for", because a Tuesday open mic and a stadium show six months
-- out are not the same kind of post and should not look alike.

-- Constraint name verified against the live database before writing this:
-- `ig_posts_kind_check` (Postgres named it when 0006 used an inline
-- `add column ... check (...)`). A collision would have made it
-- ..._check1 and `drop ... if exists` on the wrong name silently drops
-- nothing, leaving the old two-value constraint in force.
alter table public.ig_posts drop constraint if exists ig_posts_kind_check;
alter table public.ig_posts add constraint ig_posts_kind_check
  check (kind in ('digest', 'breaking', 'weekend', 'monthly', 'horizon'));

-- 'digest' stays the name of the daily post. Renaming it to 'daily' would mean
-- a data migration over live rows, a rewrite of 0006's partial unique index,
-- and a change to PostCard's badge logic — all to spell it more nicely.

-- The bucket a non-daily post belongs to: 'YYYY-Www' for weekend, 'YYYY-MM'
-- for monthly, and the target month for horizon.
--
-- Computed by the builder and stored, rather than derived in the index,
-- because date_trunc(text, timestamptz) is STABLE rather than IMMUTABLE and
-- therefore cannot appear in an index predicate at all.
alter table public.ig_posts add column if not exists period_key text;

-- One live post per (kind, period): a second weekend digest for the same
-- weekend is a duplicate, not a second edition. Terminal states are excluded
-- so a rejected or failed attempt can be rebuilt, matching how 0006 treats
-- the daily digest. Daily posts keep their own index and are excluded here.
create unique index if not exists ig_posts_live_period_idx
  on public.ig_posts (kind, period_key)
  where status in ('draft', 'approved', 'publishing', 'published')
    and kind <> 'digest'
    and period_key is not null;
