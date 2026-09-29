-- 0001_border_waits.sql — border wait times for the Chisme pipeline.
--
-- House standard (docs 07): additive only. No drops, no deletes, no alterations of
-- existing columns. Safe to run against a live database with data.
--
-- These tables are prefixed border_ and do not touch events, venues, trends, runs,
-- ig_posts, ig_post_metrics or ig_post_edits.

create extension if not exists pgcrypto;

-- ---------------------------------------------------------------------------
-- border_ports — the registry CBP does not ship.
-- The feed leaves crossing_name empty for port 240221 and carries no coordinates,
-- no Spanish names and no local aliases, so we keep them here.
-- ---------------------------------------------------------------------------
create table if not exists border_ports (
    port_number text primary key,
    name        text not null,
    name_es     text not null,
    aliases     text[] not null default '{}',
    lat         double precision,
    lng         double precision,
    mx_side     text,
    sort_order  int not null default 100,
    active      boolean not null default true,
    created_at  timestamptz not null default now()
);

-- ---------------------------------------------------------------------------
-- border_readings — one row per (port, lane, CBP update).
--
-- Append-only. content_hash is the upsert key, so re-polling between CBP updates
-- inserts nothing: CBP refreshes about hourly while we poll far more often.
-- state: open | closed | no_data. delay_minutes is NULL unless state = 'open'.
-- ---------------------------------------------------------------------------
create table if not exists border_readings (
    id             uuid primary key default gen_random_uuid(),
    port_number    text not null references border_ports(port_number),
    lane           text not null,
    state          text not null,
    delay_minutes  int,
    lanes_open     int,
    cbp_updated_at timestamptz,
    cbp_update_label text,
    stamp_ahead_of_clock boolean not null default false,
    captured_at    timestamptz not null default now(),
    content_hash   text not null unique,
    raw            jsonb not null default '{}'
);

create index if not exists border_readings_port_lane_idx
    on border_readings (port_number, lane, captured_at desc);
create index if not exists border_readings_captured_idx
    on border_readings (captured_at desc);

-- ---------------------------------------------------------------------------
-- border_runs — the observability log, same role as public.runs.
-- Per-port counts are the only way to tell "CBP reported nothing for this bridge"
-- apart from "our poll silently broke".
-- ---------------------------------------------------------------------------
create table if not exists border_runs (
    id           uuid primary key default gen_random_uuid(),
    tool         text not null,
    params       jsonb not null default '{}',
    port_counts  jsonb not null default '{}',
    readings_written int not null default 0,
    status       text not null,
    error        text,
    started_at   timestamptz not null default now()
);

create index if not exists border_runs_started_idx on border_runs (started_at desc);

-- ---------------------------------------------------------------------------
-- Row-level security — stated explicitly for every table (docs 07 §RLS).
--
-- The pipeline reads with the service-role key, which bypasses RLS. Nothing here
-- is meant to be readable by the anon key, so all three tables get RLS on with
-- zero policies (deny-all), the same shape as ig_posts.
-- ---------------------------------------------------------------------------
alter table border_ports    enable row level security;
alter table border_readings enable row level security;
alter table border_runs     enable row level security;

-- ---------------------------------------------------------------------------
-- Seed the six El Paso–Juárez ports. Idempotent: on conflict do nothing, so
-- re-running never clobbers a hand-corrected name or pin.
-- ---------------------------------------------------------------------------
insert into border_ports (port_number, name, name_es, aliases, lat, lng, mx_side, sort_order) values
    ('240202', 'Paso del Norte',        'Paso del Norte (Santa Fe)',       array['PDN','Santa Fe','Puente Santa Fe'],        31.7513, -106.4871, 'Ciudad Juárez',        10),
    ('240203', 'Ysleta–Zaragoza',       'Zaragoza–Ysleta',                 array['Zaragoza','Ysleta','Zaragoza-Ysleta'],     31.6704, -106.3255, 'Ciudad Juárez',        20),
    ('240201', 'Bridge of the Americas','Puente Libre (Córdova–Américas)', array['BOTA','Puente Libre','Free Bridge'],       31.7566, -106.4525, 'Ciudad Juárez',        30),
    ('240204', 'Stanton–Lerdo',         'Lerdo–Stanton',                   array['Stanton','Lerdo','Good Neighbor'],         31.7580, -106.4840, 'Ciudad Juárez',        40),
    ('240801', 'Santa Teresa',          'Jerónimo–Santa Teresa',           array['Santa Teresa','San Jerónimo','Jerónimo'],  31.7833, -106.6950, 'San Jerónimo, Chih.',  50),
    ('240221', 'Tornillo–Guadalupe',    'Guadalupe–Tornillo',              array['Tornillo','Guadalupe','Marcelino Serna'],  31.3906, -106.0870, 'Guadalupe, Chih.',     60)
on conflict (port_number) do nothing;
