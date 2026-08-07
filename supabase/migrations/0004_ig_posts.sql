-- 0004_ig_posts.sql — daily Instagram carousel post state (ADDITIVE ONLY;
-- no drops, no deletes, no alterations of existing tables; safe on a live database).
--
-- One row per day's carousel. The build job renders slides and inserts a 'draft';
-- a human approves it in /admin/ig; the publish job claims and pushes it to the
-- Instagram Graph API.
--
-- Why each of the less obvious columns exists:
--
--   slide_keys     Recurring events get a NEW uuid every day ("Toddler Storytime"
--                  on Mon and Tue are different rows), so event_ids alone cannot
--                  answer "did we already post this exact thing last week?".
--                  These are content-derived keys (title-without-dates + venue)
--                  that stay stable across occurrences — the only thing that makes
--                  recurrence suppression possible.
--
--   ig_creation_id Written BEFORE calling media_publish. If the process dies mid
--                  -publish we cannot know whether Instagram accepted the post;
--                  the presence of this id is what tells the recovery path
--                  "this one reached Meta, do NOT auto-retry it" (a duplicate
--                  public post is far more costly than a missed one).
--
--   attempts       A deterministically-failing row (dead slide URL, revoked token)
--                  would otherwise be retried by every 30-minute sweep forever,
--                  burning real Graph API containers against the 100/day limit.
--
--   claimed_at     Lease timestamp for the compare-and-swap claim, so a row left
--                  in 'publishing' by a crashed runner can be safely reclaimed.

create table if not exists public.ig_posts (
    id             uuid primary key default gen_random_uuid(),
    post_date      date not null,
    status         text not null default 'draft'
                   check (status in ('draft','approved','publishing','published',
                                     'failed','rejected','skipped','expired')),
    slide_paths    text[] not null default '{}',   -- storage object paths; [0] = cover
    event_ids      uuid[] not null default '{}',
    slide_keys     text[] not null default '{}',
    caption        text   not null default '',
    ig_creation_id text,
    ig_media_id    text,
    attempts       int    not null default 0,
    claimed_at     timestamptz,
    error          text,
    created_at     timestamptz not null default now(),
    published_at   timestamptz
);

-- At most one LIVE post per day. Terminal-but-discarded states (rejected,
-- expired, skipped, failed) are excluded so a bad build can simply be rebuilt
-- for the same date without first deleting anything.
create unique index if not exists ig_posts_live_date_idx on public.ig_posts (post_date)
    where status in ('draft','approved','publishing','published');

create index if not exists ig_posts_status_idx on public.ig_posts (status, post_date);

-- RLS on with NO policies: anon/authenticated can see nothing at all, and the
-- service_role key used by the scraper and the admin server actions bypasses RLS
-- entirely. Post drafts are internal state, never public.
alter table public.ig_posts enable row level security;
