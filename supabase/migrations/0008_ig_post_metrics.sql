-- Post-publish performance, so "is this working?" stops being a matter of
-- opinion. Especially now that posting is opt-out: a pipeline that ships on
-- its own needs a feedback signal that does not depend on anyone remembering
-- to look at the account.

create table if not exists public.ig_post_metrics (
    id uuid primary key default gen_random_uuid(),
    post_id uuid not null references public.ig_posts(id) on delete cascade,

    -- Two snapshots per post. t24 is the headline number; t72 catches the
    -- slower tail (saves and shares keep accruing long after likes stop) and
    -- is what makes "did this one keep travelling?" answerable.
    window_label text not null check (window_label in ('t24', 't72')),
    fetched_at timestamptz not null default now(),

    -- Columns AND raw. Columns keep the admin query and any future scoring
    -- trivial; raw means Meta renaming or retiring a metric costs a column of
    -- NULLs rather than the underlying data. That is not hypothetical here:
    -- `impressions` was already retired in favour of `views` on this account,
    -- confirmed live 2026-09-04.
    likes int,
    comments int,
    saves int,
    reach int,
    shares int,
    views int,
    total_interactions int,
    -- The two that measure whether a post did anything for the account rather
    -- than just for itself.
    profile_visits int,
    follows int,

    raw jsonb not null default '{}'::jsonb,
    error text
);

-- One row per (post, window): makes a re-run an upsert rather than a
-- duplicate, which matters because the collector is deliberately driven by
-- "published long enough ago and missing this window" rather than by a cron
-- firing at exactly the right minute.
create unique index if not exists ig_post_metrics_post_window_idx
  on public.ig_post_metrics (post_id, window_label);

create index if not exists ig_post_metrics_fetched_idx
  on public.ig_post_metrics (fetched_at desc);

-- Same posture as ig_posts: service_role only, no policies.
alter table public.ig_post_metrics enable row level security;
