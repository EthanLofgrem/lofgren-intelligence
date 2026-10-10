-- Fence result persistence and its recovery checkpoint in one transaction.
-- A lost HTTP response is recovered by reclaiming result_saved, without rerunning.
create or replace function public.li_save_research_job_result(
  p_id uuid, p_worker text, p_run jsonb, p_checkpoint jsonb,
  p_lease_seconds integer, p_hold_grace_seconds integer
)
returns jsonb language plpgsql security definer set search_path = public
as $$
declare
  v_job public.li_research_jobs;
  v_run public.li_runs;
begin
  select * into v_job from public.li_research_jobs where id = p_id for update;
  if not found or p_worker is null or length(btrim(p_worker)) = 0
      or v_job.lease_owner is distinct from p_worker
      or v_job.status not in ('running','cancel_requested')
      or v_job.lease_expires_at is null or v_job.lease_expires_at <= clock_timestamp()
      or p_lease_seconds is null or p_lease_seconds <= 0 then
    return null;
  end if;
  if jsonb_typeof(p_run) is distinct from 'object'
      or jsonb_typeof(p_checkpoint) is distinct from 'object'
      or p_checkpoint->>'phase' is distinct from 'result_saved'
      or p_checkpoint->>'run_id' is distinct from p_run->>'run_id'
      or nullif(p_run->>'run_id','') is null
      or p_run->>'user_id' is distinct from v_job.user_id::text
      or p_run->>'objective' is distinct from v_job.input->>'objective'
      or p_run#>>'{snapshot,run_id}' is distinct from p_run->>'run_id'
      or p_run#>>'{receipt,research_id}' is distinct from p_run->>'run_id'
      or (p_checkpoint->>'units')::numeric is null
      or (p_checkpoint->>'units')::numeric < v_job.cost_so_far then
    return null;
  end if;
  -- A replay may acknowledge the committed result, but never replace it.
  if v_job.checkpoint->>'phase' = 'result_saved' then
    if v_job.checkpoint->>'run_id' = p_run->>'run_id' then
      return to_jsonb(v_job);
    end if;
    return null;
  end if;
  v_run := jsonb_populate_record(null::public.li_runs, p_run);
  insert into public.li_runs select v_run.*
    on conflict (user_id,run_id) do update set
      objective=excluded.objective, status=excluded.status, summary=excluded.summary,
      snapshot=excluded.snapshot, receipt=excluded.receipt, knowledge_state=excluded.knowledge_state,
      report=excluded.report, usage_units=excluded.usage_units, known_cost_usd=excluded.known_cost_usd,
      unpriced_components=excluded.unpriced_components, created_at=excluded.created_at;
  update public.li_research_jobs
     set checkpoint = p_checkpoint,
         cost_so_far = greatest(cost_so_far,(p_checkpoint->>'units')::numeric),
         lease_expires_at = clock_timestamp() + make_interval(secs => p_lease_seconds),
         heartbeat_at = clock_timestamp(), updated_at = clock_timestamp()
   where id = p_id returning * into v_job;
  update public.li_usage_reservations
     set held_until = v_job.lease_expires_at + make_interval(secs => greatest(coalesce(p_hold_grace_seconds,0),0))
   where id = v_job.reservation_id;
  return to_jsonb(v_job);
end;
$$;
revoke all on function public.li_save_research_job_result(uuid,text,jsonb,jsonb,integer,integer)
  from public, anon, authenticated;
grant execute on function public.li_save_research_job_result(uuid,text,jsonb,jsonb,integer,integer)
  to service_role;
