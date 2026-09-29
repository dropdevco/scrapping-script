-- 0003_border_alerts_crossings_typical.sql — alerts that survive a restart, real
-- crossing times, "normal for this hour", and one retention job for all of it.
--
-- House standard (docs 07): additive only. New tables and functions; no existing table
-- or column is altered. RLS on with zero policies, like 0001.

-- ---------------------------------------------------------------------------
-- border_alerts — drop alerts that have fired.
--
-- Kept in memory only, a restart forgot which alerts were already sent while the
-- previous wait survived through border_readings, so the same drop fired again after
-- every deploy. id is derived from (port, lane, limit, the reading that crossed), so
-- writing the same alert twice, from two processes or after a restart, is a no-op.
-- rearmed_at stays NULL while the lane is still below its limit + margin: that NULL
-- is what keeps a wait wobbling around 30 minutes from alerting on every reading.
-- ---------------------------------------------------------------------------
create table if not exists border_alerts (
    id               text primary key,
    port_number      text not null references border_ports(port_number),
    lane             text not null,
    below            int not null,
    minutes          int not null,
    previous_minutes int,
    reading_hash     text not null,
    detected_at      timestamptz not null default now(),
    rearmed_at       timestamptz
);

create index if not exists border_alerts_detected_idx on border_alerts (detected_at desc);
create index if not exists border_alerts_armed_idx
    on border_alerts (port_number, lane, below) where rearmed_at is null;

-- ---------------------------------------------------------------------------
-- border_crossings — how long a real crossing took, against what each source said.
--
-- We can tell which source is fresher, never which is right. These rows are the
-- ground truth that settles a "CBP 20, pasosfronterizos 58" disagreement.
-- claims holds every source's opinion at the moment they joined the line, so the
-- comparison is against what we could have known then, not later.
-- reporter is an opaque id from the pipeline (hash it there); no names, no handles.
-- ---------------------------------------------------------------------------
create table if not exists border_crossings (
    id             uuid primary key default gen_random_uuid(),
    port_number    text not null references border_ports(port_number),
    lane           text not null,
    started_at     timestamptz not null,
    finished_at    timestamptz,
    actual_minutes int,
    shown_minutes  int,
    claims         jsonb not null default '[]',
    reporter       text,
    created_at     timestamptz not null default now()
);

create index if not exists border_crossings_finished_idx
    on border_crossings (finished_at desc) where finished_at is not null;

alter table border_alerts    enable row level security;
alter table border_crossings enable row level security;

-- ---------------------------------------------------------------------------
-- border_typical_waits — "normal for this hour", from our own readings.
--
-- Median and 75th percentile per (port, lane, weekday, hour), local time. Today is
-- left out so the baseline is never partly the number it is compared with. A bucket
-- needs min_days distinct days before it is returned: two Tuesdays are an anecdote.
-- dow follows Postgres: 0 = Sunday.
-- ---------------------------------------------------------------------------
create or replace function border_typical_waits(weeks int default 8, min_days int default 3)
returns table (port_number text, lane text, dow int, hour int,
               median_minutes double precision, p75_minutes double precision, days int)
language sql
stable
as $$
    with local as (
        select r.port_number, r.lane, r.delay_minutes,
               r.cbp_updated_at at time zone 'America/Denver' as at_local
          from border_readings r
         where r.state = 'open'
           and r.delay_minutes is not null
           and r.cbp_updated_at is not null
           and not r.stamp_ahead_of_clock
           and r.cbp_updated_at > now() - make_interval(weeks => weeks)
    )
    select port_number, lane,
           extract(dow from at_local)::int  as dow,
           extract(hour from at_local)::int as hour,
           percentile_cont(0.5)  within group (order by delay_minutes) as median_minutes,
           percentile_cont(0.75) within group (order by delay_minutes) as p75_minutes,
           count(distinct at_local::date)::int as days
      from local
     where at_local::date < (now() at time zone 'America/Denver')::date
     group by 1, 2, 3, 4
    having count(distinct at_local::date) >= min_days
$$;

comment on function border_typical_waits(int, int) is
    'Median/p75 wait per port, lane, local weekday and hour over the last N weeks, excluding today.';

-- ---------------------------------------------------------------------------
-- One retention job. border.poll calls it once a day.
--
-- Readings are now kept 60 days, not the 30 that border_prune_readings defaults to:
-- border_typical_waits looks back 8 weeks. ~12,000 rows a day is still ~720,000 rows.
-- border_runs gets a row every refresh and nothing reads it past a couple of weeks.
-- Crossings are never pruned: there will be few of them and each one is precious.
-- ---------------------------------------------------------------------------
create or replace function border_prune(keep_readings_days int default 60,
                                        keep_runs_days int default 14,
                                        keep_alerts_days int default 30)
returns jsonb
language plpgsql
as $$
declare
    readings integer;
    runs integer;
    alerts integer;
begin
    delete from border_readings where captured_at < now() - make_interval(days => keep_readings_days);
    get diagnostics readings = row_count;
    delete from border_runs where started_at < now() - make_interval(days => keep_runs_days);
    get diagnostics runs = row_count;
    -- An alert still waiting to re-arm is live state, whatever its age.
    delete from border_alerts
     where detected_at < now() - make_interval(days => keep_alerts_days)
       and rearmed_at is not null;
    get diagnostics alerts = row_count;
    return jsonb_build_object('readings', readings, 'runs', runs, 'alerts', alerts);
end;
$$;

comment on function border_prune(int, int, int) is
    'Delete old readings, runs and re-armed alerts. Returns how many rows went from each.';
