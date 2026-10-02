-- Two new post formats: a weekly per-pillar spotlight (six carousels every
-- Sunday, one per content pillar, previewing the week ahead) and a hype post
-- (a standalone carousel for a single event judged to deserve its own post --
-- a headline touring artist, a major convention, a championship-level local
-- game -- built at most once per qualifying event, not on a fixed cadence).
--
-- This migration FORMALIZES schema that was already applied by hand directly
-- against the live database before this file existed: `ig_posts.kind`'s CHECK
-- already allowed 'weekly' and 'hype', `ig_posts.pillar` already existed, and
-- the live unique index was already named `ig_posts_live_period_pillar_idx`
-- in exactly the shape below. Nothing here should change production's actual
-- schema -- every statement is written to be a no-op against the live
-- database and a from-scratch build-out against a fresh one. Recorded
-- honestly rather than silently, per the verification contract: undocumented
-- drift is worse than documented catch-up.
--
-- Additive only, per docs/data/migrations.md: no drops of data-bearing
-- objects, no alterations of existing columns. The one index drop below
-- replaces it with a strict superset, same precedent as 0006's single drop.
--
-- RLS: no policy changes needed. `events` and `ig_posts` already have the
-- posture this needs; the new columns carry nothing sensitive.

-- Constraint name verified against the live database before writing this
-- (same discipline 0010 used): `ig_posts_kind_check`.
alter table public.ig_posts drop constraint if exists ig_posts_kind_check;
alter table public.ig_posts add constraint ig_posts_kind_check
  check (kind in ('digest', 'breaking', 'weekend', 'monthly', 'horizon', 'weekly', 'hype'));

-- Which pillar a 'weekly' post covers. NULL for every other kind -- same role
-- `slot` plays for digest, kept as its own column because the live unique
-- index below already keys on it rather than on `slot`.
alter table public.ig_posts add column if not exists pillar text;

-- One live post per (kind, period, pillar): six weekly posts for the same
-- ISO week -- one per pillar -- must NOT collide the way a bare (kind,
-- period_key) index would. Strict superset of 0010's
-- ig_posts_live_period_idx (weekend/monthly/horizon always pass pillar=NULL,
-- so their behaviour is unchanged); the old index is dropped in favour of
-- this one rather than kept alongside it.
drop index if exists ig_posts_live_period_idx;
create unique index if not exists ig_posts_live_period_pillar_idx
  on public.ig_posts (kind, period_key, coalesce(pillar, ''))
  where status in ('draft', 'approved', 'publishing', 'published')
    and kind <> 'digest'
    and period_key is not null;

-- Hype judgement, cached once per event -- same tri-state convention as
-- venues.is_local: NULL means unjudged and must behave exactly as today (no
-- event is ever treated as hype-worthy by default). hype_posted_at is the
-- de-dup guard so the same event is never spotlighted twice.
alter table public.events add column if not exists is_hype boolean;
alter table public.events add column if not exists hype_reason text;
alter table public.events add column if not exists hype_source text;          -- rule|council|manual
alter table public.events add column if not exists hype_checked_at timestamptz;
alter table public.events add column if not exists hype_posted_at timestamptz;
