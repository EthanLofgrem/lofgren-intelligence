-- Lofgren Intelligence public MCP service
-- Durable identity, OAuth, exact Founding Free allocation, tenant-scoped runs,
-- rolling usage, abuse controls and billing receipts.
--
-- This migration has not been applied to production. Keep it atomic and
-- fail-closed: public/authenticated roles receive no direct table access.

create table if not exists public.li_activation_counter (
  singleton boolean primary key default true check (singleton),
  next_number bigint not null check (next_number >= 1)
);
insert into public.li_activation_counter(singleton, next_number)
values (true, 1)
on conflict (singleton) do nothing;

create table if not exists public.li_accounts (
  user_id uuid primary key references auth.users(id) on delete cascade,
  email text,
  activation_number bigint not null unique check (activation_number >= 1),
  created_at timestamptz not null default now()
);

create table if not exists public.li_entitlements (
  user_id uuid primary key references auth.users(id) on delete cascade,
  kind text not null check (kind in ('founding_free','paid_required','paid')),
  plan_id text not null default 'founding_free',
  active boolean not null default true,
  quota_units_per_week numeric not null default 500 check (quota_units_per_week >= 0),
  stripe_customer_id text,
  stripe_subscription_id text,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create table if not exists public.li_oauth_clients (
  client_id text primary key,
  client_name text not null,
  redirect_uris jsonb not null,
  grant_types jsonb not null,
  response_types jsonb not null,
  token_endpoint_auth_method text not null check (token_endpoint_auth_method = 'none'),
  active boolean not null default true,
  created_at timestamptz not null default now()
);

create table if not exists public.li_oauth_codes (
  code_hash text primary key,
  user_id uuid not null references auth.users(id) on delete cascade,
  client_id text not null references public.li_oauth_clients(client_id) on delete cascade,
  redirect_uri text not null,
  code_challenge text not null,
  scope text not null default 'mcp',
  expires_at timestamptz not null,
  used_at timestamptz,
  created_at timestamptz not null default now()
);

create table if not exists public.li_access_tokens (
  token_hash text primary key,
  user_id uuid not null references auth.users(id) on delete cascade,
  client_id text not null references public.li_oauth_clients(client_id) on delete cascade,
  scope text not null default 'mcp',
  expires_at timestamptz not null,
  revoked_at timestamptz,
  created_at timestamptz not null default now()
);

create table if not exists public.li_refresh_tokens (
  token_hash text primary key,
  user_id uuid not null references auth.users(id) on delete cascade,
  client_id text not null references public.li_oauth_clients(client_id) on delete cascade,
  scope text not null default 'mcp',
  expires_at timestamptz not null,
  used_at timestamptz,
  created_at timestamptz not null default now()
);

-- Research IDs are deterministic evidence receipts and may be identical for
-- two users. Tenant identity is therefore part of the primary key.
create table if not exists public.li_runs (
  user_id uuid not null references auth.users(id) on delete cascade,
  run_id text not null,
  objective text not null,
  status text not null check (status in ('complete','stopped','failed')),
  summary jsonb not null,
  snapshot jsonb not null,
  receipt jsonb not null,
  knowledge_state jsonb not null,
  report text not null,
  usage_units numeric not null default 0 check (usage_units >= 0),
  known_cost_usd numeric not null default 0 check (known_cost_usd >= 0),
  unpriced_components jsonb not null default '[]'::jsonb,
  created_at timestamptz not null default now(),
  primary key (user_id, run_id)
);

create index if not exists li_runs_user_created_idx on public.li_runs(user_id, created_at desc);

create table if not exists public.li_usage_events (
  id uuid primary key,
  user_id uuid not null references auth.users(id) on delete cascade,
  run_id text,
  operation text not null,
  units numeric not null default 0 check (units >= 0),
  known_cost_usd numeric not null default 0 check (known_cost_usd >= 0),
  unpriced_components jsonb not null default '[]'::jsonb,
  created_at timestamptz not null default now(),
  foreign key (user_id, run_id) references public.li_runs(user_id, run_id) on delete cascade
);

create index if not exists li_usage_user_time_idx on public.li_usage_events(user_id, created_at desc);

create table if not exists public.li_rate_events (
  id bigint generated always as identity primary key,
  user_id uuid not null references auth.users(id) on delete cascade,
  bucket text not null,
  created_at timestamptz not null default now()
);
create index if not exists li_rate_events_user_bucket_time_idx
  on public.li_rate_events(user_id, bucket, created_at desc);

create table if not exists public.li_billing_events (
  stripe_event_id text primary key,
  event_type text not null,
  payload_hash text not null,
  processed_at timestamptz not null default now()
);

alter table public.li_activation_counter enable row level security;
alter table public.li_accounts enable row level security;
alter table public.li_entitlements enable row level security;
alter table public.li_oauth_clients enable row level security;
alter table public.li_oauth_codes enable row level security;
alter table public.li_access_tokens enable row level security;
alter table public.li_refresh_tokens enable row level security;
alter table public.li_runs enable row level security;
alter table public.li_usage_events enable row level security;
alter table public.li_rate_events enable row level security;
alter table public.li_billing_events enable row level security;

revoke all on table public.li_activation_counter from anon, authenticated;
revoke all on table public.li_accounts from anon, authenticated;
revoke all on table public.li_entitlements from anon, authenticated;
revoke all on table public.li_oauth_clients from anon, authenticated;
revoke all on table public.li_oauth_codes from anon, authenticated;
revoke all on table public.li_access_tokens from anon, authenticated;
revoke all on table public.li_refresh_tokens from anon, authenticated;
revoke all on table public.li_runs from anon, authenticated;
revoke all on table public.li_usage_events from anon, authenticated;
revoke all on table public.li_rate_events from anon, authenticated;
revoke all on table public.li_billing_events from anon, authenticated;

grant select, insert, update, delete on all tables in schema public to service_role;
grant usage, select on all sequences in schema public to service_role;

-- Serializes first activation only. The counter advances only after a distinct
-- account is inserted in the same transaction, so exactly activation numbers
-- 1..1000 receive Founding Free even under concurrent signups or retries.
create or replace function public.li_activate_account(p_user_id uuid, p_email text default null)
returns setof public.li_accounts
language plpgsql
security definer
set search_path = public
as $$
declare
  v_num bigint;
  v_row public.li_accounts;
begin
  select * into v_row from public.li_accounts where user_id = p_user_id;
  if found then
    return next v_row;
    return;
  end if;

  select next_number into v_num
    from public.li_activation_counter
   where singleton = true
   for update;

  -- Re-check after acquiring the serializing lock: another concurrent request
  -- for the same user may have completed while this transaction waited.
  select * into v_row from public.li_accounts where user_id = p_user_id;
  if found then
    return next v_row;
    return;
  end if;

  insert into public.li_accounts(user_id, email, activation_number)
  values (p_user_id, p_email, v_num)
  returning * into v_row;

  update public.li_activation_counter
     set next_number = v_num + 1
   where singleton = true;

  insert into public.li_entitlements(user_id, kind, plan_id, active, quota_units_per_week)
  values (
    p_user_id,
    case when v_num <= 1000 then 'founding_free' else 'paid_required' end,
    case when v_num <= 1000 then 'founding_free' else 'paid_required' end,
    case when v_num <= 1000 then true else false end,
    case when v_num <= 1000 then 500 else 0 end
  );

  return next v_row;
end;
$$;

revoke all on function public.li_activate_account(uuid,text) from public, anon, authenticated;
grant execute on function public.li_activate_account(uuid,text) to service_role;

create or replace function public.li_consume_oauth_code(p_code_hash text)
returns setof public.li_oauth_codes
language plpgsql
security definer
set search_path = public
as $$
declare
  v_row public.li_oauth_codes;
begin
  update public.li_oauth_codes
     set used_at = now()
   where code_hash = p_code_hash
     and used_at is null
     and expires_at > now()
  returning * into v_row;
  if v_row.code_hash is not null then
    return next v_row;
  end if;
end;
$$;

revoke all on function public.li_consume_oauth_code(text) from public, anon, authenticated;
grant execute on function public.li_consume_oauth_code(text) to service_role;

create or replace function public.li_consume_refresh_token(p_token_hash text)
returns setof public.li_refresh_tokens
language plpgsql
security definer
set search_path = public
as $$
declare
  v_row public.li_refresh_tokens;
begin
  update public.li_refresh_tokens
     set used_at = now()
   where token_hash = p_token_hash
     and used_at is null
     and expires_at > now()
  returning * into v_row;
  if v_row.token_hash is not null then
    return next v_row;
  end if;
end;
$$;

revoke all on function public.li_consume_refresh_token(text) from public, anon, authenticated;
grant execute on function public.li_consume_refresh_token(text) to service_role;

-- Atomic rolling-window limiter used before authenticated MCP calls.
create or replace function public.li_take_rate_limit(
  p_user_id uuid,
  p_bucket text,
  p_limit integer,
  p_window_seconds integer
)
returns boolean
language plpgsql
security definer
set search_path = public
as $$
declare
  v_count integer;
begin
  if p_limit <= 0 or p_window_seconds <= 0 then
    return false;
  end if;

  perform pg_advisory_xact_lock(hashtext(p_user_id::text || ':' || p_bucket));

  delete from public.li_rate_events
   where user_id = p_user_id
     and bucket = p_bucket
     and created_at < now() - make_interval(secs => greatest(p_window_seconds, 60) * 2);

  select count(*) into v_count
    from public.li_rate_events
   where user_id = p_user_id
     and bucket = p_bucket
     and created_at >= now() - make_interval(secs => p_window_seconds);

  if v_count >= p_limit then
    return false;
  end if;

  insert into public.li_rate_events(user_id, bucket) values (p_user_id, p_bucket);
  return true;
end;
$$;

revoke all on function public.li_take_rate_limit(uuid,text,integer,integer) from public, anon, authenticated;
grant execute on function public.li_take_rate_limit(uuid,text,integer,integer) to service_role;
