-- Anonymous /mcp-trial: global, cross-instance bound on concurrently running
-- trial research calls (hosted/trial.py).
--
-- Forward migration; no earlier migration is edited.
--
-- A trial call that passed its admission checks takes one slot (a leased row)
-- before any upstream request and releases it when its worker ends. At most
-- p_max_concurrent unexpired slots exist at any time across every serverless
-- instance: the take is serialised by a transaction-scoped advisory lock. A
-- slot whose instance died is reclaimed when its lease expires, so a crash
-- cannot leak capacity for longer than the lease. The trial's lease (60 s)
-- exceeds its hard wall-time bound (40 s).
--
-- Nothing identifying is stored: a random slot id and two timestamps.

create table if not exists public.li_trial_slots (
  slot_id uuid primary key,
  acquired_at timestamptz not null default clock_timestamp(),
  expires_at timestamptz not null
);

create index if not exists li_trial_slots_expires_idx on public.li_trial_slots(expires_at);

alter table public.li_trial_slots enable row level security;
revoke all on table public.li_trial_slots from public, anon, authenticated;
grant select, insert, update, delete on public.li_trial_slots to service_role;
revoke usage on type public.li_trial_slots from public, anon, authenticated;
grant usage on type public.li_trial_slots to service_role;

-- Take one slot. Returns 0 when the slot was taken, otherwise the whole number
-- of seconds (>= 1) until the earliest lease expires. Invalid arguments and a
-- reused slot id are refused (never admitted).
create or replace function public.li_acquire_trial_slot(
  p_slot_id uuid,
  p_max_concurrent integer,
  p_lease_seconds integer
)
returns integer
language plpgsql
security definer
set search_path = public
as $$
declare
  v_count integer;
  v_earliest timestamptz;
  v_now timestamptz := clock_timestamp();
begin
  if p_slot_id is null
     or p_max_concurrent is null or p_max_concurrent not between 1 and 100
     or p_lease_seconds is null or p_lease_seconds not between 1 and 600 then
    return greatest(coalesce(p_lease_seconds, 1), 1);
  end if;

  perform pg_advisory_xact_lock(hashtext('li_trial_slots'));

  delete from public.li_trial_slots where expires_at <= v_now;

  select count(*), min(expires_at) into v_count, v_earliest from public.li_trial_slots;
  if v_count >= p_max_concurrent then
    return greatest(1, ceil(extract(epoch from (v_earliest - v_now)))::integer);
  end if;

  insert into public.li_trial_slots(slot_id, acquired_at, expires_at)
  values (p_slot_id, v_now, v_now + make_interval(secs => p_lease_seconds))
  on conflict (slot_id) do nothing;
  if not found then
    return p_lease_seconds;
  end if;
  return 0;
end;
$$;

-- Release one slot. True when this call removed it; false when it was already
-- gone (released or reclaimed after its lease expired).
create or replace function public.li_release_trial_slot(p_slot_id uuid)
returns boolean
language plpgsql
security definer
set search_path = public
as $$
begin
  if p_slot_id is null then
    return false;
  end if;
  delete from public.li_trial_slots where slot_id = p_slot_id;
  return found;
end;
$$;

revoke all on function public.li_acquire_trial_slot(uuid,integer,integer) from public, anon, authenticated;
grant execute on function public.li_acquire_trial_slot(uuid,integer,integer) to service_role;
revoke all on function public.li_release_trial_slot(uuid) from public, anon, authenticated;
grant execute on function public.li_release_trial_slot(uuid) to service_role;
