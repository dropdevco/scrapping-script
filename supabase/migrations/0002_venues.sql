-- 0002_venues.sql — venue/location-centric upgrade (ADDITIVE ONLY; no drops, no deletes,
-- no alterations of existing columns; safe to run on a live database with data).
--
-- 1. public.venues table keyed by a stable address-based hash
-- 2. events.venue_id / events.status / events.submitted_by
-- 3. backfill venues from existing events, link events.venue_id
-- 4. RLS for the public frontend (service_role bypasses RLS; scraper unaffected)
--
-- address_hash rule (MUST stay in sync with src/scraper/core/storage.py::_address_hash):
--   sha1( lower(trim(coalesce(address,''))) || '|' || lower(trim(coalesce(venue_name,''))) )
-- where "address" is the formatted full address string (events.location) and
-- "venue_name" is the venue display name (events.venue), hex-encoded.

create extension if not exists pgcrypto;  -- for digest()

-- ── 1. venues ────────────────────────────────────────────────────────────────
create table if not exists public.venues (
    id           uuid primary key default gen_random_uuid(),
    name         text,                        -- venue display name
    address      text,                        -- full formatted address
    city         text,
    region       text,
    postal       text,
    country      text,
    lat          double precision,
    lng          double precision,
    address_hash text not null unique,        -- stable natural key (see rule above)
    created_at   timestamptz default now()
);

create index if not exists venues_city_idx    on public.venues (city);
create index if not exists venues_lat_lng_idx on public.venues (lat, lng);

-- ── 2. events: venue link + moderation columns ───────────────────────────────
alter table public.events add column if not exists venue_id     uuid references public.venues(id);
alter table public.events add column if not exists status       text not null default 'approved';  -- scraped rows are approved
alter table public.events add column if not exists submitted_by uuid;                              -- auth.users id for user submissions

create index if not exists events_status_idx   on public.events (status);
create index if not exists events_venue_id_idx on public.events (venue_id);

-- ── 3. backfill venues from existing events, then link events.venue_id ───────
-- The hash is computed from events.location / events.venue — the exact strings the
-- Python scraper hashes — so backfilled rows and future scraper upserts converge on
-- the same venues. Structured fields (city/region/postal/country/lat/lng) are
-- enriched from events.raw for the sources whose raw shape is known.
with candidates as (
    select
        e.venue    as name,
        e.location as address,
        case e.source
            when 'events_ticketmaster' then e.raw #> '{_embedded,venues,0}'
            when 'events_web'          then case when jsonb_typeof(e.raw -> 'location') = 'object'
                                                 then e.raw -> 'location' end
        end        as v,
        e.source   as source,
        encode(digest(
            lower(trim(coalesce(e.location, ''))) || '|' || lower(trim(coalesce(e.venue, ''))),
            'sha1'
        ), 'hex')  as address_hash
    from public.events e
    where coalesce(trim(e.location), '') <> '' or coalesce(trim(e.venue), '') <> ''
),
enriched as (
    select
        c.address_hash,
        c.name,
        c.address,
        case c.source
            when 'events_ticketmaster' then c.v -> 'city'  ->> 'name'
            when 'events_web'          then c.v -> 'address' ->> 'addressLocality'
        end as city,
        case c.source
            when 'events_ticketmaster' then c.v -> 'state' ->> 'stateCode'
            when 'events_web'          then c.v -> 'address' ->> 'addressRegion'
        end as region,
        case c.source
            when 'events_ticketmaster' then c.v ->> 'postalCode'
            when 'events_web'          then c.v -> 'address' ->> 'postalCode'
        end as postal,
        case c.source
            when 'events_ticketmaster' then c.v -> 'country' ->> 'countryCode'
            when 'events_web'          then case when jsonb_typeof(c.v -> 'address' -> 'addressCountry') = 'string'
                                                 then c.v -> 'address' ->> 'addressCountry'
                                                 else c.v -> 'address' -> 'addressCountry' ->> 'name' end
        end as country,
        case c.source
            when 'events_ticketmaster' then nullif(trim(c.v -> 'location' ->> 'latitude'), '')
            when 'events_web'          then nullif(trim(c.v -> 'geo' ->> 'latitude'), '')
        end as lat_txt,
        case c.source
            when 'events_ticketmaster' then nullif(trim(c.v -> 'location' ->> 'longitude'), '')
            when 'events_web'          then nullif(trim(c.v -> 'geo' ->> 'longitude'), '')
        end as lng_txt
    from candidates c
),
distinct_venues as (
    -- one row per address_hash; prefer the row that has coordinates
    select distinct on (address_hash)
        address_hash,
        name,
        address,
        city,
        region,
        postal,
        country,
        case when lat_txt ~ '^-?[0-9]+(\.[0-9]+)?$' then lat_txt::double precision end as lat,
        case when lng_txt ~ '^-?[0-9]+(\.[0-9]+)?$' then lng_txt::double precision end as lng
    from enriched
    order by address_hash, (lat_txt is null), (city is null)
)
insert into public.venues (name, address, city, region, postal, country, lat, lng, address_hash)
select name, address, city, region, postal, country, lat, lng, address_hash
from distinct_venues
on conflict (address_hash) do nothing;

-- Link events to their venue via the same computed hash.
-- Events with no venue info (virtual events) keep venue_id null.
update public.events e
set venue_id = v.id
from public.venues v
where e.venue_id is null
  and (coalesce(trim(e.location), '') <> '' or coalesce(trim(e.venue), '') <> '')
  and v.address_hash = encode(digest(
        lower(trim(coalesce(e.location, ''))) || '|' || lower(trim(coalesce(e.venue, ''))),
        'sha1'
      ), 'hex');

-- ── 4. RLS for the public frontend ───────────────────────────────────────────
-- service_role bypasses RLS, so the scraper (service key) keeps working unchanged.
alter table public.events enable row level security;
alter table public.venues enable row level security;
alter table public.trends enable row level security;

-- events: everyone can read approved rows
create policy events_select_approved on public.events
    for select
    to anon, authenticated
    using (status = 'approved');

-- events: authenticated users can read their own submissions regardless of status
create policy events_select_own on public.events
    for select
    to authenticated
    using (submitted_by = auth.uid());

-- events: authenticated users may only insert pending rows attributed to themselves
create policy events_insert_pending on public.events
    for insert
    to authenticated
    with check (status = 'pending' and submitted_by = auth.uid());

-- venues: public read
create policy venues_select_all on public.venues
    for select
    to anon, authenticated
    using (true);

-- venues: authenticated insert (user submissions may reference a brand-new venue)
create policy venues_insert_authenticated on public.venues
    for insert
    to authenticated
    with check (true);

-- trends: public read, no public writes
create policy trends_select_all on public.trends
    for select
    to anon, authenticated
    using (true);
