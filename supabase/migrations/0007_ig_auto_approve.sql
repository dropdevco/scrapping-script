-- Opt-out posting.
--
-- Until now a draft shipped only if a human tapped Approve, and that did not
-- survive contact with a busy week: over one sample month 17 of 25 built
-- drafts were never touched and expired unseen, which is why the account
-- posted on five scattered days instead of daily. Inverting the default —
-- a draft ships unless someone stops it — is the fix, and it needs a deadline
-- to hang off.

-- When an untouched draft becomes 'approved' by itself. NULL means "never
-- auto-approve", which is what every pre-existing row gets: this migration
-- must not retroactively publish a backlog of stale drafts.
alter table public.ig_posts add column if not exists auto_approve_at timestamptz;

-- Deliberately NOT reused from scheduled_for, even though both will hold
-- 17:00 local at first. They answer different questions and already move
-- independently: publishIgPostNowRow drags scheduled_for to now(), which on a
-- shared column would be indistinguishable from "the deadline arrived", and a
-- pending Telegram edit needs to push the deadline back without also delaying
-- the post.

-- Records whether a post went out because someone chose it or because nobody
-- objected. Worth knowing before trusting engagement numbers: an auto-approved
-- post is a weaker signal of editorial intent than a hand-picked one.
alter table public.ig_posts add column if not exists approved_by text
  check (approved_by in ('human', 'auto'));

-- The event window this post was built from.
--
-- post_date means "the day this POSTS" — _publish_one's staleness guard
-- depends on that reading, and it stays. But for a weekend or monthly digest
-- the events live in a different window entirely, so the window cannot be
-- re-derived from post_date alone. Storing it makes a later rebuild (dropping
-- an event, swapping a photo) able to re-query exactly the same source rows.
alter table public.ig_posts add column if not exists window_start timestamptz;
alter table public.ig_posts add column if not exists window_end   timestamptz;

-- The auto-approve sweep runs every 30 minutes and asks only this question,
-- so keep it off a sequential scan. Partial: non-draft rows are never candidates.
create index if not exists ig_posts_auto_approve_idx
  on public.ig_posts (auto_approve_at) where status = 'draft';
