-- Storage for the editorial layer: an Instagram-only content pillar, a
-- generated one-line clarification for opaque titles, and a cached judgement
-- about whether a venue is a local independent or a national chain.
--
-- Everything here is CACHE. The pipeline runs identically with all of these
-- NULL/empty -- the columns exist so a judgement is paid for once per event or
-- venue rather than once per build, and so a human can override one by editing
-- a row directly.
--
-- Additive only, per docs/data/migrations.md: no drops, no alterations, safe
-- against the live database.
--
-- RLS: none of these tables' policies need changing. `events` and `venues`
-- already expose approved/public rows via their existing select policies and
-- these columns carry nothing sensitive; `ig_posts` has RLS on with zero
-- policies and is reached only by service_role.

-- The six content pillars (Arts & Culture, Live Music, Sports, Fitness &
-- Activities, Family, Food & Drink). DELIBERATELY NOT a replacement for
-- events.categories, which the website filter, the submission form and the KB
-- export own and which keeps every value it has. A pillar is an additional
-- label used only by the Instagram pipeline.
alter table public.events add column if not exists content_tags text[] not null default '{}';
alter table public.events add column if not exists content_tags_source text;  -- rule|council|manual
-- The site filters categories with an array-overlap query and has never had an
-- index for it; the pillar filter should not repeat that.
create index if not exists events_content_tags_idx on public.events using gin (content_tags);

-- One plain line saying what an opaque event actually is ("Austin Jimmy
-- Murphy" -> "award-winning singer/songwriter performing blues, jazz and
-- americana"). Only written when the title does not explain itself AND there
-- is a real description to summarise from -- a model asked to explain an event
-- it knows nothing about invents, which is worse than silence.
alter table public.events add column if not exists blurb text;
alter table public.events add column if not exists blurb_source text;         -- council|manual
alter table public.events add column if not exists blurb_checked_at timestamptz;

-- Local independent vs national chain, judged once and cached forever.
-- is_local is deliberately TRI-STATE: NULL means unjudged and must behave
-- exactly as today (no penalty, no filtering).
alter table public.venues add column if not exists chain_scope text;          -- local|regional|national|unknown
alter table public.venues add column if not exists is_local boolean;
alter table public.venues add column if not exists localness_reason text;
alter table public.venues add column if not exists localness_checked_at timestamptz;
alter table public.venues add column if not exists localness_source text;     -- rule|council|manual

-- What the council decided about one post, kept for audit. A column rather
-- than a table (cf. ig_post_edits in 0009): verdicts are written once by a
-- single process, so there is no lost-update problem to design around.
alter table public.ig_posts add column if not exists council_verdicts jsonb not null default '{}';
