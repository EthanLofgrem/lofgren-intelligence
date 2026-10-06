-- Durable settlement for usage reservations.
--
-- li_finalize_usage (usage_reservations migration) settles a reservation only
-- while it is 'reserved' and only within the weekly quota. Work that already
-- ran and whose result was already persisted must still be charged when that
-- call is refused (the actual units overran the quota, the reservation expired)
-- or fails (the database answer was lost). The service then records the actual
-- usage on the reservation as an 'unsettled' marker (li_mark_usage_unsettled)
-- and settles it exactly once (li_settle_usage). The usage event id is the
-- reservation id, so a retry can never charge twice, and an unsettled marker
-- never expires.
--
-- Added after the applied migrations; never edit an applied migration in place.

alter table public.li_usage_reservations
  add column if not exists run_id text,
  add column if not exists pending_units numeric check (pending_units is null or pending_units >= 0),
  add column if not exists pending_known_cost_usd numeric
    check (pending_known_cost_usd is null or pending_known_cost_usd >= 0),
  add column if not exists pending_unpriced_components jsonb,
  add column if not exists marked_at timestamptz;

alter table public.li_usage_reservations
  drop constraint if exists li_usage_reservations_status_check;
alter table public.li_usage_reservations
  add constraint li_usage_reservations_status_check
  check (status in ('reserved','settled','released','expired','unsettled'));

create index if not exists li_usage_reservations_unsettled_idx
  on public.li_usage_reservations(marked_at) where status = 'unsettled';

-- Same contract as before; usage awaiting settlement now holds quota too.
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
     and created_at < now() - interval '1 hour';

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

-- Same contract as before; usage awaiting settlement counts against the quota.
create or replace function public.li_finalize_usage(
  p_id uuid,
  p_run_id text,
  p_actual_units numeric,
  p_known_cost_usd numeric,
  p_unpriced_components jsonb
)
returns boolean
language plpgsql
security definer
set search_path = public
as $$
declare
  v_row public.li_usage_reservations;
  v_quota numeric;
  v_used numeric;
  v_other_reserved numeric;
begin
  if p_actual_units < 0 or p_known_cost_usd < 0 then
    return false;
  end if;

  select * into v_row
    from public.li_usage_reservations
   where id = p_id
   for update;

  if not found or v_row.status <> 'reserved' then
    return false;
  end if;

  perform pg_advisory_xact_lock(hashtext(v_row.user_id::text || ':usage'));

  select quota_units_per_week into v_quota
    from public.li_entitlements
   where user_id = v_row.user_id
   for update;

  select coalesce(sum(units), 0) into v_used
    from public.li_usage_events
   where user_id = v_row.user_id
     and created_at >= now() - interval '7 days';

  select coalesce(sum(case when status = 'unsettled' then coalesce(pending_units, reserved_units)
                           else reserved_units end), 0)
    into v_other_reserved
    from public.li_usage_reservations
   where user_id = v_row.user_id
     and status in ('reserved', 'unsettled')
     and id <> p_id;

  if v_used + v_other_reserved + p_actual_units > v_quota + 1e-9 then
    return false;
  end if;

  insert into public.li_usage_events(
    id, user_id, run_id, operation, units, known_cost_usd, unpriced_components
  )
  values (
    p_id, v_row.user_id, p_run_id, v_row.operation, p_actual_units,
    p_known_cost_usd, coalesce(p_unpriced_components, '[]'::jsonb)
  );

  update public.li_usage_reservations
     set status = 'settled', actual_units = p_actual_units, settled_at = now()
   where id = p_id;

  return true;
end;
$$;

revoke all on function public.li_finalize_usage(uuid,text,numeric,numeric,jsonb)
  from public, anon, authenticated;
grant execute on function public.li_finalize_usage(uuid,text,numeric,numeric,jsonb)
  to service_role;

-- Record the actual usage of work that already ran on a reservation that could
-- not be finalized. Acts on any reservation that is not yet settled; returns
-- false when it is settled already (nothing to reconcile) or unknown.
create or replace function public.li_mark_usage_unsettled(
  p_id uuid,
  p_run_id text,
  p_actual_units numeric,
  p_known_cost_usd numeric,
  p_unpriced_components jsonb
)
returns boolean
language plpgsql
security definer
set search_path = public
as $$
begin
  if p_actual_units is null or p_actual_units < 0
     or p_known_cost_usd is null or p_known_cost_usd < 0 then
    return false;
  end if;
  update public.li_usage_reservations
     set status = 'unsettled',
         run_id = p_run_id,
         pending_units = p_actual_units,
         pending_known_cost_usd = p_known_cost_usd,
         pending_unpriced_components = coalesce(p_unpriced_components, '[]'::jsonb),
         marked_at = now()
   where id = p_id
     and status in ('reserved', 'expired', 'released', 'unsettled');
  return found;
end;
$$;

revoke all on function public.li_mark_usage_unsettled(uuid,text,numeric,numeric,jsonb)
  from public, anon, authenticated;
grant execute on function public.li_mark_usage_unsettled(uuid,text,numeric,numeric,jsonb)
  to service_role;

-- Idempotent reconcile: settle an unsettled marker exactly once, charging the
-- recorded actual usage without a quota check (the work already ran).
-- Returns 'settled', 'already_settled', 'not_unsettled' or 'missing'.
create or replace function public.li_settle_usage(p_id uuid)
returns text
language plpgsql
security definer
set search_path = public
as $$
declare
  v_row public.li_usage_reservations;
begin
  select * into v_row from public.li_usage_reservations where id = p_id for update;
  if not found then
    return 'missing';
  end if;
  if v_row.status = 'settled' then
    return 'already_settled';
  end if;
  if v_row.status <> 'unsettled' then
    return 'not_unsettled';
  end if;

  perform pg_advisory_xact_lock(hashtext(v_row.user_id::text || ':usage'));

  insert into public.li_usage_events(
    id, user_id, run_id, operation, units, known_cost_usd, unpriced_components
  )
  values (
    p_id, v_row.user_id, v_row.run_id, v_row.operation, v_row.pending_units,
    coalesce(v_row.pending_known_cost_usd, 0),
    coalesce(v_row.pending_unpriced_components, '[]'::jsonb)
  )
  on conflict (id) do nothing;

  update public.li_usage_reservations
     set status = 'settled', actual_units = v_row.pending_units, settled_at = now()
   where id = p_id;

  return 'settled';
end;
$$;

revoke all on function public.li_settle_usage(uuid) from public, anon, authenticated;
grant execute on function public.li_settle_usage(uuid) to service_role;
