-- 0021_border_estimator.sql — storage for the Chisme estimate: every signal, every
-- estimate, each source's earned trust, and the crowd's messages as received.
--
-- Additive only (docs/data/migrations.md, invariants §8): four new tables and one new
-- function. No existing table, column or function is touched. Re-runnable: every create
-- is `if not exists` or `or replace`.
--
-- Same posture as 0013–0017: RLS on with zero policies (deny-all), so only the
-- service-role key reads or writes. Design: docs/border-estimator/SPEC.md §3, §4 and §6;
-- why the estimate fuses signals instead of taking the freshest is ADR-0013.

-- ---------------------------------------------------------------------------
-- border_signals — one row per signal observation (SPEC §3).
--
-- Every claim any source makes about a lane, in one shape, so fusion and the replay
-- harness read one table whatever the kind: published (CBP and the sites), measured
-- (cameras, Maps), crowd, and position. border_readings is left as it is: alerts, deltas
-- and border_typical_waits are built on it.
--
-- content_hash is the upsert key, as in border_readings, so the same claim is never
-- stored twice. The writer derives it from everything that makes a claim distinct (port,
-- lane, direction, kind, source, state, minutes, low, high, observed_at, reporter_hash),
-- so two sources saying the same thing at the same moment are still two rows.
-- It dedupes only a claim with a fixed stamp, such as CBP between its hourly updates. A
-- claim dated from the read gets a new observed_at, so a new row, on every poll: a lane
-- with no number, a stated age counted back from the read (pasosfronterizos), a number
-- with a stamp ahead of the clock or none (signals._observed_at). On the fixtures that
-- is 29 of 47 signals, so this table grows with every poll whether or not anything
-- changed, and border_prune_estimator below is what bounds it.
--
-- observed_at is when the claim was true (CBP's own stamp, a frame's capture time); it
-- is what freshness decays from. captured_at is when we stored it, as in border_readings.
-- The replay harness needs both: replaying a past poll must use only what was captured
-- by then, or a CBP number stamped well before we first read it would leak into earlier
-- estimates.
-- minutes, low and high are NULL when the signal has no number. A measured signal always
-- carries low and high; a crowd "llevo 40 min" is low = 40, high = NULL (SPEC §5.3).
-- independent has no default on purpose: it is set from evidence (SPEC §5.2), and a CBP
-- mirror that defaulted to true would vote.
-- reporter_hash is for crowd and position signals only: an opaque hash, never a name or
-- a handle (Q-14).
-- evidence holds the raw inputs for audit; for a crowd signal that includes the parsed
-- message (SPEC §3). Q-14 keeps the sender hash and parsed values 30 days, so crowd and
-- position rows go after 30 days, with their reports, not after the 60 of the rest.
-- ---------------------------------------------------------------------------
create table if not exists border_signals (
    id            uuid primary key default gen_random_uuid(),
    port_number   text not null references border_ports(port_number),
    lane          text not null,
    direction     text not null,             -- north | south
    kind          text not null,             -- published | measured | crowd | position
    source        text not null,             -- cbp, pasosfronterizos, cam:pdn-north, ...
    state         text not null,             -- open | closed | no_data
    minutes       int,
    low           int,
    high          int,
    observed_at   timestamptz not null,
    independent   boolean not null,
    reporter_hash text,
    evidence      jsonb not null default '{}',
    content_hash  text not null unique,
    captured_at   timestamptz not null default now()
);

-- Fusion on every poll and the replay harness both ask for "every signal observed in a
-- window", across all lanes at once.
create index if not exists border_signals_observed_idx
    on border_signals (observed_at desc);

-- ---------------------------------------------------------------------------
-- border_estimates — one row per lane, direction and fusion run (SPEC §4).
--
-- Computed and stored on every poll, with shown = false until that bridge and direction
-- meet the Q-6 target (SPEC §4.6, ADR-0013). Until then only grading reads it, so every
-- reply, ranking and sheet row stays exactly what it is today.
--
-- state follows CBP for closures, and closure_conflict marks a lane CBP calls closed
-- while a measured signal sees traffic moving: flagged, never resolved (SPEC §4.4, Q-2).
-- minutes, low, high and confidence are NULL when there is no number (closed, no data).
-- method names the fusion that produced the row (fusion_v1), so a row computed under old
-- weights is never graded as if it came from new ones. No default: the writer states it.
-- inputs lists every signal that counted, with its weight and the trust behind it: the
-- "how much each counted" every estimate must be able to show (SPEC §1). It cites a
-- signal by id and source, never by reporter_hash or its evidence: Q-14 keeps a sender
-- hash and a parsed message 30 days, and estimates are kept longer.
-- ---------------------------------------------------------------------------
create table if not exists border_estimates (
    id               uuid primary key default gen_random_uuid(),
    port_number      text not null references border_ports(port_number),
    lane             text not null,
    direction        text not null,          -- north | south
    state            text not null,          -- open | closed | no_data
    closure_conflict boolean not null default false,
    minutes          int,
    low              int,
    high             int,
    confidence       text,                   -- high | medium | low
    method           text not null,          -- fusion_v1
    inputs           jsonb not null default '[]',
    shown            boolean not null default false,
    computed_at      timestamptz not null default now()
);

-- The newest estimate for a lane, and the one in force when a timed crossing started,
-- which is how each crossing grades the estimate (SPEC §4.6, §8).
create index if not exists border_estimates_lane_idx
    on border_estimates (port_number, lane, direction, computed_at desc);

-- ---------------------------------------------------------------------------
-- border_source_trust — each source's trust, earned against timed crossings (SPEC §4.2).
--
-- Recomputed daily from crossings.accuracy() over the last 30 days. One row per source,
-- direction and day, so every estimate can cite the trust it used, and the ranking of
-- sources is this table ordered by trust. Real sources only: the "chisme" claim that
-- accuracy() grades is today's consensus answer, derived from the sources themselves,
-- so ranking it among them, or weighting it in fusion, would grade them against
-- themselves.
-- direction because ROADMAP 2.3 computes trust per direction: CBP publishes northbound
-- only (Q-4), so a source's record is not the same both ways.
-- day is the El Paso date of the computation, never the UTC one (invariants §1).
-- n is the number of graded crossings behind the row. mae, bias and within_10 are NULL
-- while n = 0, when trust is the prior alone.
-- ---------------------------------------------------------------------------
create table if not exists border_source_trust (
    source     text not null,
    direction  text not null,                -- north | south
    day        date not null,
    n          int not null,
    mae        double precision,             -- minutes
    bias       double precision,             -- minutes; positive means it over-states the wait
    within_10  double precision,             -- share of crossings within 10 minutes, 0 to 1
    prior      double precision not null,    -- the hand-set starting trust [A]
    trust      double precision not null,    -- (n * empirical + k * prior) / (n + k)
    primary key (source, direction, day)
);

-- ---------------------------------------------------------------------------
-- border_reports — every inbound crowd message, as received (SPEC §5.3, §5.4, §6).
--
-- Privacy (Q-14): "hashed sender id, parsed values and raw text, kept 30 days". The
-- sender is only ever a hash, made in the pipeline before the row is written. raw_text is
-- kept to debug the parser, and lat / lng are a shared position in the line. The whole
-- row goes after 30 days (border_prune_estimator), which also meets SPEC §6's "parsed
-- values and outcomes ... >= 30 days" [A]: crowd health is graded over a rolling 30 days
-- (SPEC §8), and trust is earned from border_crossings, never from reports.
-- parsed holds the parser's values (bridge, lane, minutes, elapsed or finished).
-- raw_text is NULL for a message with no text, such as a shared location [A].
-- reporter_hash is NULL only for a channel that gives no sender id [A].
-- outcome is NULL until decided. Corroboration needs a second reporter within 20
-- minutes (SPEC §4.3), so "uncorroborated" can only be final some time after receipt.
-- ---------------------------------------------------------------------------
create table if not exists border_reports (
    id            uuid primary key default gen_random_uuid(),
    channel       text not null,             -- one of Q-10's: Telegram, web form, Instagram, WhatsApp
    reporter_hash text,
    received_at   timestamptz not null default now(),
    raw_text      text,
    lat           double precision,
    lng           double precision,
    parsed        jsonb not null default '{}',
    outcome       text                       -- counted | uncorroborated | rate_limited | implausible | unparsed
);

-- ---------------------------------------------------------------------------
-- The estimator's retention, shaped like border_prune in 0015: one function, rows
-- counted from when we stored them, a count per table returned.
--
-- keep_days (signals and estimates) is 60 [A], the same as border_readings: SPEC §8
-- grades over a rolling 30 days, and the replay harness (SPEC §9) gets twice that to
-- justify a weight change. Over 4,200 signal rows a day (29 of the fixtures' 47 are new
-- on every poll) and about 4,500 estimate rows [A: the 10-minute poll of SPEC §7, the
-- fixture feed's 31 lanes] is ~550,000 rows at 60 days, the order of border_readings.
-- keep_reports_days is Q-14's 30, for reports and for the crowd and position signals
-- made from them, which carry the sender hash and the parsed message.
--
-- NOTHING CALLS THIS YET. storage.py must call it from the daily `poll --prune`, next to
-- border_prune, before any channel stores a real message (ROADMAP 3.3, 3.7); until then
-- the 30-day promise is not kept. It runs as the caller, never as its owner, so with RLS
-- on and no policies an anon call through the API deletes nothing.
-- ---------------------------------------------------------------------------
create or replace function border_prune_estimator(keep_days int default 60,
                                                  keep_reports_days int default 30)
returns jsonb
language plpgsql
as $$
declare
    signals integer;
    estimates integer;
    reports integer;
begin
    delete from border_signals
     where captured_at < now() - make_interval(days => keep_days)
        or (kind in ('crowd', 'position')
            and captured_at < now() - make_interval(days => keep_reports_days));
    get diagnostics signals = row_count;
    delete from border_estimates where computed_at < now() - make_interval(days => keep_days);
    get diagnostics estimates = row_count;
    delete from border_reports where received_at < now() - make_interval(days => keep_reports_days);
    get diagnostics reports = row_count;
    return jsonb_build_object('signals', signals, 'estimates', estimates, 'reports', reports);
end;
$$;

comment on function border_prune_estimator(int, int) is
    'Remove signals and estimates older than keep_days, and reports and crowd/position signals older than keep_reports_days. Returns how many rows went from each table.';

-- ---------------------------------------------------------------------------
-- Row-level security: on, with zero policies (deny-all), like every border_ table.
-- ---------------------------------------------------------------------------
alter table border_signals      enable row level security;
alter table border_estimates    enable row level security;
alter table border_source_trust enable row level security;
alter table border_reports      enable row level security;

comment on table border_signals is
    'Every claim any source makes about a lane (SPEC §3); upserted on content_hash.';
comment on table border_estimates is
    'The Chisme estimate per lane, direction and poll; shown = false until it meets the Q-6 target.';
comment on table border_source_trust is
    'Daily trust per source and direction, earned against border_crossings (SPEC §4.2).';
comment on table border_reports is
    'Inbound crowd messages; each row, with its raw_text, position and sender hash, goes after 30 days (Q-14).';
