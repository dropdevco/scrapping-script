-- 0019_border_poll_dispatch.sql — a reliable 10-minute clock for border-poll.
--
-- GitHub Actions `schedule:` is best-effort: after PR #8 merged, the "*/10" cron fired
-- about once every 3 hours (4 runs in 17 hours), and the 10:20 UTC daily run never fired.
-- Actions cron cannot meet the 10-minute freshness agreed on 2026-09-29, so Postgres keeps
-- the clock instead: pg_cron fires on time and pg_net asks GitHub to run the workflow
-- through its workflow_dispatch API. The workflow's own cron stays as a backup; both share
-- the `border-poll` concurrency group, so a doubled run is harmless.
--
-- Additive only (docs/data/migrations.md): two extensions, one function, three cron jobs.
-- No existing table is touched.
--
-- INERT until a token exists. The function reads a GitHub fine-grained token (Actions:
-- read and write on this repo) from Supabase Vault under the name `github_dispatch_token`
-- and does nothing if it is absent — the engine's usual "ships off" posture. Create it once:
--
--   select vault.create_secret('<token>', 'github_dispatch_token',
--                              'border-poll dispatch (Actions: read/write on dropdevco/scrapping-script)');
--
-- Fine-grained tokens expire, so this stops silently when the token does; see
-- docs/components/border-wait-times.md for the check query that shows it.

create extension if not exists pg_cron;
create extension if not exists pg_net with schema extensions;

create or replace function public.dispatch_border_poll(job text default 'poll')
returns bigint
language plpgsql
security definer
set search_path = public, extensions, vault, pg_temp
as $$
declare
    token text;
    request_id bigint;
begin
    if job not in ('poll', 'daily') then
        raise exception 'unknown border-poll job: %', job;
    end if;

    select decrypted_secret into token
      from vault.decrypted_secrets
     where name = 'github_dispatch_token'
     limit 1;
    if token is null or token = '' then
        raise notice 'github_dispatch_token is not in the vault; border-poll dispatch is off';
        return null;
    end if;

    select net.http_post(
        url     := 'https://api.github.com/repos/dropdevco/scrapping-script/actions/workflows/border_poll.yml/dispatches',
        headers := jsonb_build_object(
            'Authorization', 'Bearer ' || token,
            'Accept', 'application/vnd.github+json',
            'X-GitHub-Api-Version', '2022-11-28',
            'User-Agent', 'chisme-border-poll-dispatch',
            'Content-Type', 'application/json'),
        body    := jsonb_build_object('ref', 'main', 'inputs', jsonb_build_object('job', job)),
        timeout_milliseconds := 5000
    ) into request_id;
    return request_id;
end;
$$;

-- The token is readable through this function, so only the service role and the cron
-- owner may call it — never anon or signed-in users.
revoke all on function public.dispatch_border_poll(text) from public, anon, authenticated;

-- Re-running this file replaces the jobs instead of stacking duplicates.
do $$
declare
    name text;
begin
    foreach name in array array['border-poll-dispatch', 'border-poll-daily-dispatch', 'border-poll-housekeeping']
    loop
        perform cron.unschedule(name) where exists (select 1 from cron.job where jobname = name);
    end loop;
end $$;

select cron.schedule('border-poll-dispatch', '*/10 * * * *',
                     $$select public.dispatch_border_poll('poll')$$);
-- Retention and the live "does CBP still parse" check, ~04:20 in El Paso.
select cron.schedule('border-poll-daily-dispatch', '20 10 * * *',
                     $$select public.dispatch_border_poll('daily')$$);
-- pg_cron keeps every run forever; one row per 10 minutes is ~4,300 a month.
select cron.schedule('border-poll-housekeeping', '40 10 * * *',
                     $$delete from cron.job_run_details where end_time < now() - interval '7 days'$$);
