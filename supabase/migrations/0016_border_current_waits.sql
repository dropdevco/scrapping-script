-- 0016_border_current_waits.sql — the crossing times as they stand now, one row per
-- bridge and lane, for the Google Sheet → GoHighLevel knowledge base.
--
-- House standard (docs 07): additive only. One new table; nothing existing is altered.
--
-- border_readings (0013) is the history: CBP's own numbers, one row per CBP update,
-- which is what deltas, alerts and border_typical_waits are built on. It is the wrong
-- shape for the knowledge base: the bot needs "what is Zaragoza SENTRI right now",
-- which there means "the newest row for that lane", and it needs the number a follower
-- would actually be told — reconciled with pasosfronterizos, which refreshes every few
-- minutes while CBP republishes about hourly.
--
-- So border.poll upserts this table on every run (every 10 minutes from border-poll):
-- every lane CBP lists for every bridge, open or not, so a lane never silently drops
-- out of the knowledge base — a shut lane reads "cerrado", not a missing row.
--
-- The text columns are never NULL or empty on purpose. GoHighLevel's importer rejects
-- a row with any blank cell (docs/architecture/invariants.md §6), and times are written
-- as absolute dates ("29 sep 2026, 2:10 p. m."), never "hace 5 min", which is false by
-- the time anyone asks. The numeric columns may be NULL (a closed lane has no minutes);
-- a Sheet should read the text columns.
create table if not exists border_current_waits (
    port_number    text not null references border_ports(port_number),
    lane           text not null,
    bridge_es      text not null,
    bridge_en      text not null,
    lane_es        text not null,
    lane_en        text not null,
    state          text not null,            -- open | closed | no_data
    wait_es        text not null,            -- "38 min", "1 h 15", "cerrado", "sin datos"
    wait_en        text not null,            -- "38 min", "1 h 15", "closed", "no data"
    summary_es     text not null,            -- one sentence the bot can quote
    summary_en     text not null,
    minutes        int,                      -- what a follower is told; NULL unless open
    cbp_minutes    int,                      -- CBP's own figure, kept for audit
    source         text not null,            -- where `minutes` came from: cbp | pasosfronterizos
    lanes_open     int,
    cbp_updated_at timestamptz,
    checked_at     timestamptz not null,     -- when border.poll last wrote this row
    primary key (port_number, lane)
);

-- Same posture as every border_ table: RLS on, zero policies. The Sheet automation and
-- the pipeline read with the service-role key.
alter table border_current_waits enable row level security;

comment on table border_current_waits is
    'Current wait per bridge and lane, upserted by border.poll every run; the knowledge-base feed.';
