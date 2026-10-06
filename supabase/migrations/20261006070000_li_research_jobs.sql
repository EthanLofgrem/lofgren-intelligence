-- Durable research execution: a leased job queue for hosted research.
--
-- investigate used to execute research inline in the HTTP request, and the
-- hosting function is capped at 60 seconds. start_research now enqueues a job
-- (li_enqueue_research_job) and returns its id at once; an always-on worker
-- (python -m lofgren_intelligence.hosted.worker) claims it with a lease
-- (li_claim_research_job, FOR UPDATE SKIP LOCKED), heartbeats the lease while it
-- runs (li_heartbeat_research_job) and finishes it (li_complete_research_job,
-- li_fail_research_job). A worker that dies stops heartbeating; its lease
-- expires and li_reclaim_research_jobs requeues the job (or fails it once its
-- attempts are exhausted). li_request_cancel_research_job cancels a queued job
-- at once and asks a running one to stop at its next stage.
--
-- The job's usage reservation is taken at enqueue and held for the job's whole
-- life: li_usage_reservations.held_until is pushed forward at enqueue, claim,
-- heartbeat and requeue, and li_reserve_usage (re-created here) no longer
-- expires a reservation whose hold is still live. A job that ends without a
-- worker (cancelled while queued, lease expired with no attempts left, queue
-- wait expired) releases its reservation when no work was recorded, or records
-- the recorded work as an 'unsettled' marker that li_settle_usage charges
-- exactly once (usage_settlement migration).
--
-- Added after the applied migrations; never edit an applied migration in place.

alter table public.li_usage_reservations
  add column if not exists held_until timestamptz,
  add column if not exists job_id uuid;

create table if not exists public.li_research_jobs (
  id uuid primary key,
  user_id uuid not null references auth.users(id) on delete cascade,
  case_id uuid references public.li_cases(id) on delete set null,
  kind text not null check (kind in ('investigate')),
  idempotency_key text not null check (length(idempotency_key) between 1 and 200),
  status text not null default 'queued' check (status in (
    'queued','running','succeeded','failed','cancelled','cancel_requested'
  )),
  input jsonb not null,
  lease_owner text,
  lease_expires_at timestamptz,
  heartbeat_at timestamptz,
  attempts integer not null default 0 check (attempts >= 0),
  max_attempts integer not null default 3 check (max_attempts between 1 and 10),
  checkpoint jsonb not null default '{}'::jsonb,
  reservation_id uuid not null references public.li_usage_reservations(id) on delete cascade,
  result_run_id text,
  error_code text check (error_code is null or length(error_code) between 1 and 64),
  cost_so_far numeric not null default 0 check (cost_so_far >= 0),
  not_before timestamptz not null default now(),
  queue_expires_at timestamptz not null,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  started_at timestamptz,
  finished_at timestamptz,
  cancel_requested_at timestamptz,
  constraint li_research_jobs_user_idempotency_key unique (user_id, idempotency_key)
);
create index if not exists li_research_jobs_queue_idx
  on public.li_research_jobs(not_before, created_at) where status = 'queued';
create index if not exists li_research_jobs_lease_idx
  on public.li_research_jobs(lease_expires_at) where status in ('running','cancel_requested');
create index if not exists li_research_jobs_user_created_idx
  on public.li_research_jobs(user_id, created_at desc);

alter table public.li_research_jobs enable row level security;
revoke all on table public.li_research_jobs from anon, authenticated;
grant select, insert, update, delete on public.li_research_jobs to service_role;

-- Same contract as the usage_settlement version; a reservation held by a live
-- job (held_until in the future) is no longer expired after one hour.
create or replace function public.li_reserve_usage(
  p_id uuid,
  p_user_id uuid,
  p_operation text,
  p_units numeric,
  p_window_seconds integer default 604800
)
returns boolean
language plpgsql
security definer
set search_path = public
as $$
declare
  v_quota numeric;
  v_active boolean;
  v_used numeric;
  v_reserved numeric;
begin
  if p_units < 0 or p_window_seconds <= 0 then
    return false;
  end if;

  perform pg_advisory_xact_lock(hashtext(p_user_id::text || ':usage'));

  update public.li_usage_reservations
     set status = 'expired', settled_at = now()
   where user_id = p_user_id
     and status = 'reserved'
     and created_at < now() - interval '1 hour'
     and (held_until is null or held_until < now());

  select quota_units_per_week, active
    into v_quota, v_active
    from public.li_entitlements
   where user_id = p_user_id
   for update;

  if not found or not v_active then
    return false;
  end if;

  select coalesce(sum(units), 0) into v_used
    from public.li_usage_events
   where user_id = p_user_id
     and created_at >= now() - make_interval(secs => p_window_seconds);

  select coalesce(sum(case when status = 'unsettled' then coalesce(pending_units, reserved_units)
                           else reserved_units end), 0)
    into v_reserved
    from public.li_usage_reservations
   where user_id = p_user_id
     and status in ('reserved', 'unsettled');

  if v_used + v_reserved + p_units > v_quota + 1e-9 then
    return false;
  end if;

  insert into public.li_usage_reservations(id, user_id, operation, reserved_units, status)
  values (p_id, p_user_id, p_operation, p_units, 'reserved');

  return true;
end;
$$;

revoke all on function public.li_reserve_usage(uuid,uuid,text,numeric,integer)
  from public, anon, authenticated;
grant execute on function public.li_reserve_usage(uuid,uuid,text,numeric,integer)
  to service_role;

-- Internal: settle a job's reservation when the job ends without a worker.
-- No recorded work: release. Recorded work: an 'unsettled' marker that
-- li_settle_usage charges exactly once. A settled reservation is left alone.
-- Not executable by any API role; only the SECURITY DEFINER job functions call it.
create or replace function public.li_research_job_account(p_job public.li_research_jobs, p_reason text)
returns void
language plpgsql
set search_path = public
as $$
declare
  v_cost numeric;
begin
  v_cost := case when jsonb_typeof(p_job.checkpoint -> 'known_cost_usd') = 'number'
                 then greatest((p_job.checkpoint ->> 'known_cost_usd')::numeric, 0) else 0 end;
  if coalesce(p_job.cost_so_far, 0) > 0 then
    update public.li_usage_reservations
       set status = 'unsettled',
           run_id = null,
           pending_units = p_job.cost_so_far,
           pending_known_cost_usd = v_cost,
           pending_unpriced_components = jsonb_build_array('partial_run', p_reason),
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
end;
$$;

revoke all on function public.li_research_job_account(public.li_research_jobs, text)
  from public, anon, authenticated, service_role;

-- Enqueue once per (user, idempotency key). A repeated key returns the existing
-- job with created = false and binds nothing. The reservation must be the
-- caller's, still 'reserved' and not bound to another job; it is then held for
-- the queue wait. Returns null when the reservation is not usable.
create or replace function public.li_enqueue_research_job(
  p_id uuid,
  p_user_id uuid,
  p_case_id uuid,
  p_kind text,
  p_idempotency_key text,
  p_input jsonb,
  p_reservation_id uuid,
  p_max_attempts integer,
  p_queue_ttl_seconds integer
)
returns jsonb
language plpgsql
security definer
set search_path = public
as $$
declare
  v_row public.li_research_jobs;
  v_until timestamptz;
begin
  if p_idempotency_key is null or length(p_idempotency_key) not between 1 and 200
     or p_queue_ttl_seconds is null or p_queue_ttl_seconds <= 0 then
    return null;
  end if;

  perform pg_advisory_xact_lock(hashtext(p_user_id::text || ':research-job:' || p_idempotency_key));

  select * into v_row
    from public.li_research_jobs
   where user_id = p_user_id and idempotency_key = p_idempotency_key;
  if found then
    return to_jsonb(v_row) || jsonb_build_object('created', false);
  end if;

  v_until := now() + make_interval(secs => p_queue_ttl_seconds);
  update public.li_usage_reservations
     set held_until = v_until, job_id = p_id
   where id = p_reservation_id
     and user_id = p_user_id
     and status = 'reserved'
     and job_id is null;
  if not found then
    return null;
  end if;

  insert into public.li_research_jobs(
    id, user_id, case_id, kind, idempotency_key, status, input, max_attempts,
    reservation_id, queue_expires_at
  )
  values (
    p_id, p_user_id, p_case_id, p_kind, p_idempotency_key, 'queued', p_input,
    greatest(1, least(coalesce(p_max_attempts, 3), 10)), p_reservation_id, v_until
  )
  returning * into v_row;

  return to_jsonb(v_row) || jsonb_build_object('created', true);
end;
$$;

revoke all on function public.li_enqueue_research_job(uuid,uuid,uuid,text,text,jsonb,uuid,integer,integer)
  from public, anon, authenticated;
grant execute on function public.li_enqueue_research_job(uuid,uuid,uuid,text,text,jsonb,uuid,integer,integer)
  to service_role;

-- Claim the next runnable job. Concurrent workers never claim the same row
-- (FOR UPDATE SKIP LOCKED). The lease and the reservation hold start now.
create or replace function public.li_claim_research_job(
  p_worker text,
  p_lease_seconds integer,
  p_hold_grace_seconds integer
)
returns jsonb
language plpgsql
security definer
set search_path = public
as $$
declare
  v_row public.li_research_jobs;
begin
  if p_worker is null or length(p_worker) not between 1 and 200
     or p_lease_seconds is null or p_lease_seconds <= 0 then
    return null;
  end if;

  select * into v_row
    from public.li_research_jobs
   where status = 'queued'
     and not_before <= now()
     and queue_expires_at > now()
   order by not_before, created_at, id
   for update skip locked
   limit 1;
  if not found then
    return null;
  end if;

  update public.li_research_jobs
     set status = 'running',
         lease_owner = p_worker,
         lease_expires_at = now() + make_interval(secs => p_lease_seconds),
         heartbeat_at = now(),
         attempts = attempts + 1,
         started_at = coalesce(started_at, now()),
         updated_at = now()
   where id = v_row.id
  returning * into v_row;

  update public.li_usage_reservations
     set held_until = v_row.lease_expires_at + make_interval(secs => greatest(coalesce(p_hold_grace_seconds, 0), 0))
   where id = v_row.reservation_id;

  return to_jsonb(v_row);
end;
$$;

revoke all on function public.li_claim_research_job(text,integer,integer) from public, anon, authenticated;
grant execute on function public.li_claim_research_job(text,integer,integer) to service_role;

-- Extend the lease (and the reservation hold) of a job this worker still owns,
-- optionally recording a checkpoint and the cumulative units recorded so far.
-- Returns null when the lease is lost (expired or taken by another worker).
-- The returned status tells the worker whether a cancel was requested.
create or replace function public.li_heartbeat_research_job(
  p_id uuid,
  p_worker text,
  p_lease_seconds integer,
  p_hold_grace_seconds integer,
  p_checkpoint jsonb,
  p_cost_so_far numeric
)
returns jsonb
language plpgsql
security definer
set search_path = public
as $$
declare
  v_row public.li_research_jobs;
begin
  if p_lease_seconds is null or p_lease_seconds <= 0 or (p_cost_so_far is not null and p_cost_so_far < 0) then
    return null;
  end if;

  update public.li_research_jobs
     set lease_expires_at = now() + make_interval(secs => p_lease_seconds),
         heartbeat_at = now(),
         checkpoint = coalesce(p_checkpoint, checkpoint),
         cost_so_far = greatest(cost_so_far, coalesce(p_cost_so_far, cost_so_far)),
         updated_at = now()
   where id = p_id
     and lease_owner = p_worker
     and status in ('running', 'cancel_requested')
     and lease_expires_at > now()
  returning * into v_row;
  if not found then
    return null;
  end if;

  update public.li_usage_reservations
     set held_until = v_row.lease_expires_at + make_interval(secs => greatest(coalesce(p_hold_grace_seconds, 0), 0))
   where id = v_row.reservation_id;

  return to_jsonb(v_row);
end;
$$;

revoke all on function public.li_heartbeat_research_job(uuid,text,integer,integer,jsonb,numeric)
  from public, anon, authenticated;
grant execute on function public.li_heartbeat_research_job(uuid,text,integer,integer,jsonb,numeric)
  to service_role;

-- Mark a job this worker owns as succeeded with its persisted run. The worker
-- settles the reservation (li_finalize_usage, or an unsettled marker plus
-- li_settle_usage) before calling this. A cancel requested after the last
-- stage does not undo finished work: the job reports succeeded.
create or replace function public.li_complete_research_job(
  p_id uuid,
  p_worker text,
  p_run_id text,
  p_cost_so_far numeric
)
returns jsonb
language plpgsql
security definer
set search_path = public
as $$
declare
  v_row public.li_research_jobs;
begin
  if p_run_id is null or length(p_run_id) = 0 or p_cost_so_far is null or p_cost_so_far < 0 then
    return null;
  end if;

  update public.li_research_jobs
     set status = 'succeeded',
         result_run_id = p_run_id,
         cost_so_far = p_cost_so_far,
         error_code = null,
         lease_owner = null,
         lease_expires_at = null,
         finished_at = now(),
         updated_at = now()
   where id = p_id
     and lease_owner = p_worker
     and status in ('running', 'cancel_requested')
  returning * into v_row;
  if not found then
    return null;
  end if;

  update public.li_usage_reservations set held_until = null where id = v_row.reservation_id;
  return to_jsonb(v_row);
end;
$$;

revoke all on function public.li_complete_research_job(uuid,text,text,numeric) from public, anon, authenticated;
grant execute on function public.li_complete_research_job(uuid,text,text,numeric) to service_role;

-- End an attempt that did not succeed. A retryable failure with attempts left
-- (and no cancel requested) requeues the job after the backoff and keeps the
-- reservation held; WORKER_SHUTDOWN (a worker stopping on SIGTERM between
-- stages) requeues without using up an attempt. Otherwise the job ends: 'cancelled' for p_error_code
-- CANCELLED (sent only after the worker saw a cancel request), else 'failed'. The worker settles the
-- reservation first; any reservation still open is accounted here as a safety net.
create or replace function public.li_fail_research_job(
  p_id uuid,
  p_worker text,
  p_error_code text,
  p_retryable boolean,
  p_backoff_seconds integer,
  p_cost_so_far numeric,
  p_checkpoint jsonb,
  p_queue_ttl_seconds integer
)
returns jsonb
language plpgsql
security definer
set search_path = public
as $$
declare
  v_row public.li_research_jobs;
  v_until timestamptz;
begin
  if p_error_code is null or length(p_error_code) not between 1 and 64
     or p_cost_so_far is null or p_cost_so_far < 0 then
    return null;
  end if;

  select * into v_row
    from public.li_research_jobs
   where id = p_id
     and lease_owner = p_worker
     and status in ('running', 'cancel_requested')
   for update;
  if not found then
    return null;
  end if;

  if coalesce(p_retryable, false) and p_error_code <> 'CANCELLED' and v_row.status = 'running'
     and (v_row.attempts < v_row.max_attempts or p_error_code = 'WORKER_SHUTDOWN') then
    v_until := now() + make_interval(secs => greatest(coalesce(p_backoff_seconds, 0), 0))
                     + make_interval(secs => greatest(coalesce(p_queue_ttl_seconds, 86400), 1));
    update public.li_research_jobs
       set status = 'queued',
           lease_owner = null,
           lease_expires_at = null,
           attempts = case when p_error_code = 'WORKER_SHUTDOWN' then greatest(attempts - 1, 0) else attempts end,
           error_code = p_error_code,
           cost_so_far = greatest(cost_so_far, p_cost_so_far),
           checkpoint = coalesce(p_checkpoint, checkpoint),
           not_before = now() + make_interval(secs => greatest(coalesce(p_backoff_seconds, 0), 0)),
           queue_expires_at = v_until,
           updated_at = now()
     where id = p_id
    returning * into v_row;
    update public.li_usage_reservations set held_until = v_until where id = v_row.reservation_id;
    return to_jsonb(v_row);
  end if;

  update public.li_research_jobs
     set status = case when p_error_code = 'CANCELLED' then 'cancelled' else 'failed' end,
         lease_owner = null,
         lease_expires_at = null,
         error_code = p_error_code,
         cost_so_far = greatest(cost_so_far, p_cost_so_far),
         checkpoint = coalesce(p_checkpoint, checkpoint),
         finished_at = now(),
         updated_at = now()
   where id = p_id
  returning * into v_row;
  perform public.li_research_job_account(v_row, lower(p_error_code));
  update public.li_usage_reservations set held_until = null where id = v_row.reservation_id;
  return to_jsonb(v_row);
end;
$$;

revoke all on function public.li_fail_research_job(uuid,text,text,boolean,integer,numeric,jsonb,integer)
  from public, anon, authenticated;
grant execute on function public.li_fail_research_job(uuid,text,text,boolean,integer,numeric,jsonb,integer)
  to service_role;

-- Tenant cancel. Queued: cancelled now (nothing ran in this attempt; recorded
-- work from earlier attempts is charged, otherwise the reservation is released).
-- Running: cancel_requested, honoured by the worker at its next stage.
-- Finished: unchanged. Returns null for an unknown job or another user's job.
create or replace function public.li_request_cancel_research_job(p_id uuid, p_user_id uuid)
returns jsonb
language plpgsql
security definer
set search_path = public
as $$
declare
  v_row public.li_research_jobs;
begin
  select * into v_row
    from public.li_research_jobs
   where id = p_id and user_id = p_user_id
   for update;
  if not found then
    return null;
  end if;

  if v_row.status = 'queued' then
    update public.li_research_jobs
       set status = 'cancelled', error_code = 'CANCELLED', cancel_requested_at = now(),
           finished_at = now(), updated_at = now()
     where id = p_id
    returning * into v_row;
    perform public.li_research_job_account(v_row, 'cancelled');
  elsif v_row.status = 'running' then
    update public.li_research_jobs
       set status = 'cancel_requested', cancel_requested_at = now(), updated_at = now()
     where id = p_id
    returning * into v_row;
  end if;

  return to_jsonb(v_row);
end;
$$;

revoke all on function public.li_request_cancel_research_job(uuid,uuid) from public, anon, authenticated;
grant execute on function public.li_request_cancel_research_job(uuid,uuid) to service_role;

-- Recover jobs whose worker died: an expired lease is requeued while attempts
-- remain (or while the result is already saved and only settlement is left);
-- otherwise the job ends (cancelled when a cancel was requested, else failed
-- with LEASE_EXPIRED) and its reservation is accounted. Queued jobs that waited
-- past their queue expiry fail with QUEUE_EXPIRED. Returns the affected job ids.
create or replace function public.li_reclaim_research_jobs(p_limit integer, p_queue_ttl_seconds integer)
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
begin
  v_until := now() + make_interval(secs => greatest(coalesce(p_queue_ttl_seconds, 86400), 1));

  for v_row in
    select * from public.li_research_jobs
     where status in ('running', 'cancel_requested')
       and lease_expires_at <= now()
     order by lease_expires_at, id
     for update skip locked
     limit greatest(coalesce(p_limit, 100), 1)
  loop
    v_finishing := coalesce(v_row.checkpoint ->> 'phase', '') in ('result_saved', 'finalizing');
    if not v_finishing and (v_row.status = 'cancel_requested' or v_row.attempts >= v_row.max_attempts) then
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

revoke all on function public.li_reclaim_research_jobs(integer,integer) from public, anon, authenticated;
grant execute on function public.li_reclaim_research_jobs(integer,integer) to service_role;
