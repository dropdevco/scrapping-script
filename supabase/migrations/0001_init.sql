-- scraper-mcp initial schema
-- Run in the Supabase SQL editor, or via `supabase db push` with the CLI.

-- ── events ───────────────────────────────────────────────────────────────────
create table if not exists public.events (
    id           uuid primary key default gen_random_uuid(),
    source       text not null,
    source_id    text,
    title        text not null,
    description  text,
    start_time   timestamptz,
    end_time     timestamptz,
    venue        text,
    location     text,
    url          text,
    image_url    text,
    categories   text[] default '{}',
    raw          jsonb  default '{}'::jsonb,
    content_hash text not null unique,          -- upsert key (dedupe across sources/runs)
    first_seen   timestamptz not null default now(),
    last_seen    timestamptz not null default now()
);

create index if not exists events_location_idx   on public.events using gin (to_tsvector('simple', coalesce(location, '')));
create index if not exists events_start_time_idx  on public.events (start_time);
create index if not exists events_last_seen_idx   on public.events (last_seen);

-- ── trends / content ideas ───────────────────────────────────────────────────
create table if not exists public.trends (
    id           uuid primary key default gen_random_uuid(),
    source       text not null,
    platform     text not null,                 -- reddit | hackernews | youtube | google_trends | instagram | threads
    topic        text,
    title        text not null,
    summary      text,
    url          text,
    score        double precision,
    engagement   jsonb default '{}'::jsonb,
    captured_for text,                           -- original request topic, for grouping
    raw          jsonb default '{}'::jsonb,
    content_hash text not null unique,
    captured_at  timestamptz not null default now()
);

create index if not exists trends_platform_idx     on public.trends (platform);
create index if not exists trends_captured_for_idx on public.trends (captured_for);
create index if not exists trends_captured_at_idx  on public.trends (captured_at);
create index if not exists trends_score_idx        on public.trends (score desc);

-- ── run log (observability) ──────────────────────────────────────────────────
create table if not exists public.runs (
    id            uuid primary key default gen_random_uuid(),
    tool          text not null,
    params        jsonb default '{}'::jsonb,
    source_counts jsonb default '{}'::jsonb,
    status        text not null,
    error         text,
    started_at    timestamptz not null default now()
);

-- ── keep events.last_seen current on every upsert ────────────────────────────
create or replace function public.touch_last_seen() returns trigger as $$
begin
    new.last_seen := now();
    -- preserve original first_seen across upserts
    if tg_op = 'UPDATE' then
        new.first_seen := old.first_seen;
    end if;
    return new;
end;
$$ language plpgsql;

drop trigger if exists events_touch_last_seen on public.events;
create trigger events_touch_last_seen
    before insert or update on public.events
    for each row execute function public.touch_last_seen();

-- ── OPTIONAL: later semantic-search upgrade (left commented; needs pgvector) ──
-- create extension if not exists vector;
-- alter table public.events add column if not exists embedding vector(1536);
-- alter table public.trends add column if not exists embedding vector(1536);
