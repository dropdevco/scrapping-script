-- Close a gap from the 0001/0009 tables: `runs` and `ig_post_edits` were never
-- locked down like `ig_posts` (0004) and `ig_post_metrics` (0008) were. Both
-- are internal operational state (a scraper run log, and Telegram edit
-- intents) with no reason to be readable by anon/authenticated clients.
--
-- Same posture as the other two: RLS on, zero policies. service_role (the
-- scraper and admin server actions) bypasses RLS entirely and is unaffected.
alter table public.runs enable row level security;
alter table public.ig_post_edits enable row level security;
