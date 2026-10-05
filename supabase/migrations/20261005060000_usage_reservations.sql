-- Atomic quota reservations for concurrent hosted MCP work.

create table if not exists public.li_usage_reservations (
  id uuid primary key,
  user_id uuid not null references auth.users(id) on delete cascade,
  operation text not null,
  reserved_units numeric not null check (reserved_units >= 0),
  actual_units numeric,
  status text not null check (status in ('reserved','settled','released','expired')),
  created_at timestamptz not null default now(),
  settled_at timestamptz
);
create index if not exists li_usage_reservations_user_status_time_idx
  on public.li_usage_reservations(user_id, status, created_at desc);

alter table public.li_usage_reservations enable row level security;
revoke all on table public.li_usage_reservations from anon, authenticated;
grant select, insert, update, delete on public.li_usage_reservations to service_role;

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

  select coalesce(sum(reserved_units), 0) into v_reserved
    from public.li_usage_reservations
   where user_id = p_user_id
     and status = 'reserved';

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

  select coalesce(sum(reserved_units), 0) into v_other_reserved
    from public.li_usage_reservations
   where user_id = v_row.user_id
     and status = 'reserved'
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

create or replace function public.li_release_usage(p_id uuid)
returns boolean
language plpgsql
security definer
set search_path = public
as $$
begin
  update public.li_usage_reservations
     set status = 'released', settled_at = now()
   where id = p_id and status = 'reserved';
  return found;
end;
$$;

revoke all on function public.li_release_usage(uuid) from public, anon, authenticated;
grant execute on function public.li_release_usage(uuid) to service_role;
