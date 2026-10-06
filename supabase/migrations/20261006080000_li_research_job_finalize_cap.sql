-- Bounded recovery for research jobs stuck in final settlement.
--
-- li_reclaim_research_jobs (20261006070000_li_research_jobs) requeues a job
-- whose lease expired in the checkpoint phases 'result_saved' or 'finalizing'
-- without using an attempt, so a worker that crashes every time it settles
-- such a job had it requeued forever. Each such reclaim is now counted in a
-- separate column, finalize_reclaims. Once a job has been requeued for
-- settlement p_max_finalize_reclaims times, the next expired lease ends it:
--
--   * status 'failed', error_code 'SETTLEMENT_ABANDONED';
--   * a saved result is kept: result_run_id is the checkpoint's run, which stays
--     readable (get_job_status shows it, and the run is in li_runs);
--   * the work is accounted without a worker: when the reservation is still
--     open, an 'unsettled' marker carrying the checkpoint's units, known cost and
--     unpriced components (plus 'settlement_abandoned'), and for a saved result
--     its run id, which li_settle_usage (reconcile) charges exactly once; a
--     reservation that is already settled or already marked is left alone; no
--     recorded work releases it.
--
-- Added after the applied migrations; never edit an applied migration in place.

alter table public.li_research_jobs
  add column if not exists finalize_reclaims integer not null default 0
    check (finalize_reclaims >= 0);

-- The reclaim function gains the cap as a third argument; drop the two-argument
-- version so a call can never resolve to the uncapped one.
drop function if exists public.li_reclaim_research_jobs(integer, integer);

-- Internal: account a job whose settlement was abandoned. Not executable by any
-- API role; only li_reclaim_research_jobs calls it.
create or replace function public.li_research_job_abandon_settlement(p_job public.li_research_jobs)
returns void
language plpgsql
set search_path = public
as $$
declare
  v_saved boolean;
  v_units numeric;
  v_cost numeric;
  v_unpriced jsonb;
begin
  v_saved := coalesce(p_job.checkpoint ->> 'phase', '') = 'result_saved';
  v_units := greatest(
    coalesce(p_job.cost_so_far, 0),
    case when jsonb_typeof(p_job.checkpoint -> 'units') = 'number'
         then (p_job.checkpoint ->> 'units')::numeric else 0 end,
    0);
  v_cost := case when jsonb_typeof(p_job.checkpoint -> 'known_cost_usd') = 'number'
                 then greatest((p_job.checkpoint ->> 'known_cost_usd')::numeric, 0) else 0 end;
  v_unpriced := case when jsonb_typeof(p_job.checkpoint -> 'unpriced') = 'array'
                     then p_job.checkpoint -> 'unpriced' else '[]'::jsonb end;
  if not v_saved then
    v_unpriced := v_unpriced || jsonb_build_array('partial_run');
  end if;
  v_unpriced := v_unpriced || jsonb_build_array('settlement_abandoned');

  if v_units > 0 or v_cost > 0 then
    update public.li_usage_reservations
       set status = 'unsettled',
           run_id = case when v_saved then p_job.result_run_id else null end,
           pending_units = v_units,
           pending_known_cost_usd = v_cost,
           pending_unpriced_components = v_unpriced,
           marked_at = now(),
           held_until = null
     where id = p_job.reservation_id
       and status in ('reserved', 'expired', 'released');
  else
    update public.li_usage_reservations
       set status = 'released', settled_at = now(), held_until = null
     where id = p_job.reservation_id
       and status = 'reserved';
  end if;
  update public.li_usage_reservations set held_until = null where id = p_job.reservation_id;
end;
$$;

revoke all on function public.li_research_job_abandon_settlement(public.li_research_jobs)
  from public, anon, authenticated, service_role;

-- Recover jobs whose worker died. Unchanged from 20261006070000 except for jobs
-- in a finishing phase ('result_saved', 'finalizing'): each requeue is counted
-- in finalize_reclaims, and once p_max_finalize_reclaims (1..10, default 3)
-- requeues were used the job fails with SETTLEMENT_ABANDONED, keeps its result
-- and leaves an unsettled-usage marker for reconcile. Returns the affected job ids.
create or replace function public.li_reclaim_research_jobs(
  p_limit integer,
  p_queue_ttl_seconds integer,
  p_max_finalize_reclaims integer default 3
)
returns jsonb
language plpgsql
security definer
set search_path = public
as $$
declare
  v_row public.li_research_jobs;
  v_ids jsonb := '[]'::jsonb;
  v_finishing boolean;
  v_until timestamptz;
  v_cap integer;
begin
  v_until := now() + make_interval(secs => greatest(coalesce(p_queue_ttl_seconds, 86400), 1));
  v_cap := least(greatest(coalesce(p_max_finalize_reclaims, 3), 1), 10);

  for v_row in
    select * from public.li_research_jobs
     where status in ('running', 'cancel_requested')
       and lease_expires_at <= now()
     order by lease_expires_at, id
     for update skip locked
     limit greatest(coalesce(p_limit, 100), 1)
  loop
    v_finishing := coalesce(v_row.checkpoint ->> 'phase', '') in ('result_saved', 'finalizing');
    if v_finishing and v_row.finalize_reclaims >= v_cap then
      update public.li_research_jobs
         set status = 'failed',
             error_code = 'SETTLEMENT_ABANDONED',
             result_run_id = case when v_row.checkpoint ->> 'phase' = 'result_saved'
                                       and length(coalesce(v_row.checkpoint ->> 'run_id', '')) > 0
                                  then v_row.checkpoint ->> 'run_id' else result_run_id end,
             finalize_reclaims = finalize_reclaims + 1,
             lease_owner = null, lease_expires_at = null, finished_at = now(), updated_at = now()
       where id = v_row.id
      returning * into v_row;
      perform public.li_research_job_abandon_settlement(v_row);
    elsif not v_finishing and (v_row.status = 'cancel_requested' or v_row.attempts >= v_row.max_attempts) then
      update public.li_research_jobs
         set status = case when v_row.status = 'cancel_requested' then 'cancelled' else 'failed' end,
             error_code = case when v_row.status = 'cancel_requested' then 'CANCELLED' else 'LEASE_EXPIRED' end,
             lease_owner = null, lease_expires_at = null, finished_at = now(), updated_at = now()
       where id = v_row.id
      returning * into v_row;
      perform public.li_research_job_account(v_row, lower(v_row.error_code));
    else
      update public.li_research_jobs
         set status = 'queued', lease_owner = null, lease_expires_at = null,
             finalize_reclaims = finalize_reclaims + case when v_finishing then 1 else 0 end,
             not_before = now(), queue_expires_at = v_until, updated_at = now()
       where id = v_row.id
      returning * into v_row;
      update public.li_usage_reservations set held_until = v_until where id = v_row.reservation_id;
    end if;
    v_ids := v_ids || to_jsonb(v_row.id::text);
  end loop;

  for v_row in
    select * from public.li_research_jobs
     where status = 'queued' and queue_expires_at <= now()
     order by queue_expires_at, id
     for update skip locked
     limit greatest(coalesce(p_limit, 100), 1)
  loop
    update public.li_research_jobs
       set status = 'failed', error_code = 'QUEUE_EXPIRED', finished_at = now(), updated_at = now()
     where id = v_row.id
    returning * into v_row;
    perform public.li_research_job_account(v_row, 'queue_expired');
    v_ids := v_ids || to_jsonb(v_row.id::text);
  end loop;

  return v_ids;
end;
$$;

revoke all on function public.li_reclaim_research_jobs(integer,integer,integer) from public, anon, authenticated;
grant execute on function public.li_reclaim_research_jobs(integer,integer,integer) to service_role;
