-- 0005_ig_scheduling.sql — a suggested/best-effort publish time per post
-- (ADDITIVE ONLY; no drops, no deletes, no alterations of existing columns).
--
-- Previously "approved" meant "publish whenever the next sweep finds it" —
-- correct for testing, but wrong for a real account where posting at 3am is
-- worse than not posting at all. scheduled_for lets the build job suggest a
-- sensible time (see __main__.py's _suggested_schedule — defaults to 5pm
-- local, or "now" if it's already past that), which "Approve" accepts as-is
-- and "Publish now" overrides to the current instant. NULL means "no
-- restriction" — every row from before this migration keeps behaving exactly
-- as it did.

alter table public.ig_posts add column if not exists scheduled_for timestamptz;
