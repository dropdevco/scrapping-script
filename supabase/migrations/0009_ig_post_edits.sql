-- Pre-post edits requested from Telegram.
--
-- The shape of this table is dictated by one constraint: the Telegram webhook
-- is a stateless serverless handler and cannot run Pillow, but dropping an
-- event or swapping a photo means re-rendering the carousel. So a tap records
-- an INTENT here, and a Python job applies it.

create table if not exists public.ig_post_edits (
    id uuid primary key default gen_random_uuid(),
    post_id uuid not null references public.ig_posts(id) on delete cascade,
    op text not null check (op in ('drop_event', 'swap_photo')),
    payload jsonb not null default '{}'::jsonb,
    requested_at timestamptz not null default now(),
    applied_at timestamptz,
    error text
);

-- A table rather than a `pending_edits` jsonb column on ig_posts, because
-- every webhook delivery is an independent invocation: two taps in quick
-- succession ("drop slide 3", "drop slide 5") doing read-modify-write on one
-- jsonb array is a lost update, and PostgREST offers no locking to prevent it.
-- Inserts never conflict, order themselves by requested_at, and leave an audit
-- trail of what was actually asked for.

-- The auto-approve sweep asks "does this post have unapplied edits?" on every
-- pass, so keep it cheap and partial.
create index if not exists ig_post_edits_pending_idx
  on public.ig_post_edits (post_id) where applied_at is null;

-- Accepted photo swaps, keyed by event id: {"<event_uuid>": "<storage path>"}.
-- Keyed by EVENT rather than by slide index because a later drop_event
-- renumbers every slide after it — an index-keyed override would silently
-- reattach the human's photo to a different event.
alter table public.ig_posts
  add column if not exists photo_overrides jsonb not null default '{}'::jsonb;

-- Set when a human writes the caption by hand, so a rebuild triggered by an
-- unrelated drop_event does not regenerate over their words.
alter table public.ig_posts
  add column if not exists caption_is_custom boolean not null default false;
