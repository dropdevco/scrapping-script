-- 0006_ig_kind_slot.sql — post kind + digest slot (ADDITIVE ONLY; no drops,
-- no deletes, no alterations of existing columns).
--
-- Previously "one live post per post_date" was enforced by a single-column
-- unique index — correct for "exactly one digest a day," wrong once a digest
-- can run in more than one time slot (morning + evening) and a breaking-news
-- post needs to exist independent of the digest slot entirely.
--
-- kind distinguishes the two: 'digest' is the existing scheduled carousel,
-- 'breaking' is reserved for a future hand-picked single-event post — this
-- migration only makes the column and index ready for it, it does not add
-- any code path that writes kind='breaking' yet.
--
-- slot names WHICH digest, when there's more than one in a day ('morning',
-- 'evening', whatever IG_DIGEST_SLOTS is configured with). NULL means "the
-- one unnamed digest" — every row written before this migration is exactly
-- that, so the new index below collapses to the same uniqueness guarantee
-- those old rows already had.

alter table public.ig_posts add column if not exists
  kind text not null default 'digest' check (kind in ('digest','breaking'));
alter table public.ig_posts add column if not exists slot text;

-- Replaces ig_posts_live_date_idx. Every existing row has kind='digest' and
-- slot=NULL, so coalesce(slot,'') collapses to the exact same key as the old
-- single-column index — behavior for all current data is UNCHANGED. This
-- only adds two new capabilities: a second digest slot (a different `slot`
-- value) can coexist with the first on the same post_date, and 'breaking'
-- rows are entirely unconstrained (any number per day), since the partial
-- index only applies to kind='digest'.
drop index if exists ig_posts_live_date_idx;
create unique index if not exists ig_posts_live_digest_slot_idx
    on public.ig_posts (post_date, coalesce(slot, ''))
    where status in ('draft','approved','publishing','published') and kind = 'digest';
