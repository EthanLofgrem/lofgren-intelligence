-- Global (cross-instance) sliding-window rate limits for endpoints reachable
-- before a user is known: OAuth client registration, authorization and token
-- exchange, account and action endpoints, and Founding Free activation per IP
-- and per email domain.
--
-- li_rate_events (public_mcp migration) is keyed by auth.users(id) and cannot
-- hold anonymous keys, so this is a separate table. Keys are never stored raw:
-- the service sends a SHA-256 hex digest of "<bucket>:<key>" (an IP address or
-- an email domain). Rows are pruned once outside the window.
--
-- Added after the applied migrations; never edit an applied migration in place.

create table if not exists public.li_rate_limit_events (
  id bigint generated always as identity primary key,
  bucket text not null check (length(bucket) between 1 and 64),
  key_hash text not null check (key_hash ~ '^[0-9a-f]{64}$'),
  created_at timestamptz not null default now()
);

create index if not exists li_rate_limit_events_bucket_key_time_idx
  on public.li_rate_limit_events(bucket, key_hash, created_at desc);
create index if not exists li_rate_limit_events_time_idx
  on public.li_rate_limit_events(created_at);

alter table public.li_rate_limit_events enable row level security;
revoke all on table public.li_rate_limit_events from anon, authenticated;
grant select, insert, update, delete on public.li_rate_limit_events to service_role;
grant usage, select on all sequences in schema public to service_role;

-- Atomic sliding-window take for one (bucket, key).
-- Returns 0 when the request is allowed (and records it), otherwise the whole
-- number of seconds (>= 1) until a slot frees. Invalid arguments fail closed:
-- they are refused, never allowed.
create or replace function public.li_take_keyed_rate_limit(
  p_bucket text,
  p_key_hash text,
  p_limit integer,
  p_window_seconds integer
)
returns integer
language plpgsql
security definer
set search_path = public
as $$
declare
  v_count integer;
  v_oldest timestamptz;
begin
  if p_bucket is null or length(p_bucket) not between 1 and 64
     or p_key_hash is null or p_key_hash !~ '^[0-9a-f]{64}$'
     or p_limit is null or p_limit <= 0
     or p_window_seconds is null or p_window_seconds <= 0 then
    return greatest(coalesce(p_window_seconds, 1), 1);
  end if;

  perform pg_advisory_xact_lock(hashtext('li_rate:' || p_bucket || ':' || p_key_hash));

  delete from public.li_rate_limit_events
   where bucket = p_bucket
     and key_hash = p_key_hash
     and created_at < now() - make_interval(secs => p_window_seconds);

  -- Opportunistic global prune so keys that never return do not accumulate.
  if random() < 0.01 then
    delete from public.li_rate_limit_events
     where created_at < now() - interval '1 day';
  end if;

  select count(*), min(created_at) into v_count, v_oldest
    from public.li_rate_limit_events
   where bucket = p_bucket
     and key_hash = p_key_hash
     and created_at >= now() - make_interval(secs => p_window_seconds);

  if v_count >= p_limit then
    return greatest(
      1,
      ceil(extract(epoch from (v_oldest + make_interval(secs => p_window_seconds) - now())))::integer
    );
  end if;

  insert into public.li_rate_limit_events(bucket, key_hash) values (p_bucket, p_key_hash);
  return 0;
end;
$$;

revoke all on function public.li_take_keyed_rate_limit(text,text,integer,integer)
  from public, anon, authenticated;
grant execute on function public.li_take_keyed_rate_limit(text,text,integer,integer)
  to service_role;
