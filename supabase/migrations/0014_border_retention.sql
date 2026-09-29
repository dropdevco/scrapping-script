-- 0002_border_retention.sql — keep border_readings from growing without bound.
--
-- House standard (docs 07): additive only. No drops, no deletes, no alterations of
-- existing columns. This file adds functions and an index; it changes no table.

-- ---------------------------------------------------------------------------
-- Readings are a rolling window, not an archive. Deltas need the previous reading
-- and drop alerts need the one before that; nothing reads beyond a few days.
-- 42 lanes every 5 minutes is about 12,000 rows a day, so 30 days is ~360,000 —
-- small, but unbounded growth on a free database is how projects die quietly.
-- ---------------------------------------------------------------------------
create or replace function border_prune_readings(keep_days int default 30)
returns integer
language plpgsql
as $$
declare
    removed integer;
begin
    delete from border_readings
     where captured_at < now() - make_interval(days => keep_days);
    get diagnostics removed = row_count;
    return removed;
end;
$$;

comment on function border_prune_readings(int) is
    'Delete readings older than keep_days. Returns how many rows went.';

-- ---------------------------------------------------------------------------
-- The one query the service runs on every lane: newest reading first.
-- The existing index covers (port_number, lane, captured_at desc); this partial
-- index keeps the "last usable number" lookup off the closed and no-data rows.
-- ---------------------------------------------------------------------------
create index if not exists border_readings_open_idx
    on border_readings (port_number, lane, captured_at desc)
    where state = 'open';

-- ---------------------------------------------------------------------------
-- What the collector has been doing lately, in one query.
-- ---------------------------------------------------------------------------
create or replace view border_recent_activity as
select date_trunc('hour', captured_at) as hour,
       count(*)                        as readings,
       count(*) filter (where state = 'open')    as open_lanes,
       count(*) filter (where state = 'no_data') as dark_lanes,
       count(distinct port_number)     as ports
  from border_readings
 where captured_at > now() - interval '48 hours'
 group by 1
 order by 1 desc;

comment on view border_recent_activity is
    'Hourly collector activity for the last 48 hours. Empty hours mean the poller stopped.';
