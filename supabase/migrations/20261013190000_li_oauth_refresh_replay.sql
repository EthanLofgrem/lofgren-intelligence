-- OAuth refresh rotation: revoke the superseded access token, and treat a reused
-- refresh token as replay.
--
-- Forward-only, on top of 20261012060000_oauth_disconnect,
-- 20261012202113_oauth_revocation_replay and 20261013050420_oauth_issuance_expiry
-- (already applied on staging). No earlier migration is edited. It adds no second
-- token-family scheme: the unit of revocation stays the one those migrations use,
-- a user's connection of one client (li_disconnect_oauth_client), which RFC 7009
-- revocation (li_revoke_oauth_token) and account disconnect already revoke.
--
-- 1. Superseded access token. li_issue_oauth_tokens records on each new access
--    token the hash of the refresh token issued with it (refresh_token_hash).
--    When a refresh token is consumed, the access token issued with it is revoked
--    in the same transaction, under the user/client lock: the client has just
--    shown that it holds the newer credential. Access tokens written before this
--    migration carry no refresh_token_hash; they are not revoked early and expire
--    on their own (access tokens live one hour).
-- 2. Replay. li_issue_oauth_tokens deletes the consumed refresh token (so issuance
--    cannot be replayed), which left nothing to recognise a second presentation
--    by. It now keeps a tombstone (hash, user, client, original expiry) in
--    li_oauth_consumed_refresh. li_oauth_refresh_replay, called by the token
--    endpoint only after li_consume_refresh_token found nothing, recognises a
--    tombstoned token, or one consumed by a concurrent request that has not issued
--    yet, and then revokes the user's connection of that client, including tokens
--    just issued to the legitimate holder. Validity is re-checked under the same
--    advisory lock that issuance and disconnect take, so concurrent replays and
--    issuance serialise: either issuance finds its parent deleted and fails, or the
--    tokens it issued are revoked.
-- 3. Disconnect also deletes the client's tombstones, so a credential from before
--    a disconnect can never revoke a later, fresh consent (the rule of
--    20261012202113_oauth_revocation_replay).
--
-- Nothing secret is stored: token columns hold SHA-256 hex digests.

alter table public.li_access_tokens add column if not exists refresh_token_hash text;
create index if not exists li_access_tokens_refresh_hash_idx
  on public.li_access_tokens(refresh_token_hash) where refresh_token_hash is not null;

create table if not exists public.li_oauth_consumed_refresh (
  token_hash text primary key,
  user_id uuid not null references auth.users(id) on delete cascade,
  client_id text not null references public.li_oauth_clients(client_id) on delete cascade,
  consumed_at timestamptz not null default clock_timestamp(),
  expires_at timestamptz not null
);
create index if not exists li_oauth_consumed_refresh_owner_idx
  on public.li_oauth_consumed_refresh(user_id, client_id);
create index if not exists li_oauth_consumed_refresh_expires_idx
  on public.li_oauth_consumed_refresh(expires_at);

alter table public.li_oauth_consumed_refresh enable row level security;
revoke all on table public.li_oauth_consumed_refresh from public, anon, authenticated;
grant select, insert, update, delete on public.li_oauth_consumed_refresh to service_role;
revoke usage on type public.li_oauth_consumed_refresh from public, anon, authenticated;
grant usage on type public.li_oauth_consumed_refresh to service_role;

-- Limiter keys of the client-keyed buckets become HMAC-SHA256 digests under
-- LI_RATE_LIMIT_HMAC_KEY (hosted/ratelimit.py). Remove the unsalted SHA-256
-- digests written before; they are dictionary-reversible for IPv4 addresses. The
-- windows are at most one hour long, and the key change resets them anyway. Trial
-- buckets were already keyed and stay.
delete from public.li_rate_limit_events
 where bucket in ('oauth_register', 'oauth_authorize', 'oauth_complete', 'oauth_token', 'oauth_revoke',
                  'account', 'actions', 'cases', 'activation_ip', 'activation_domain');

-- Issuance: identical to 20261013050420 except for refresh_token_hash, the
-- revocation of the superseded access token and the tombstone.
create or replace function public.li_issue_oauth_tokens(
  p_grant_kind text, p_grant_hash text, p_access jsonb, p_refresh jsonb
) returns boolean language plpgsql security definer set search_path = public as $$
declare v_user uuid; v_client text; v_scope text; v_resource text; v_expires timestamptz;
begin
  if p_grant_kind = 'authorization_code' then
    select user_id, client_id into v_user, v_client from public.li_oauth_codes where code_hash = p_grant_hash;
  elsif p_grant_kind = 'refresh_token' then
    select user_id, client_id into v_user, v_client from public.li_refresh_tokens where token_hash = p_grant_hash;
  else raise exception 'invalid grant kind'; end if;
  if v_user is null then return false; end if;
  perform pg_advisory_xact_lock(hashtextextended('li_oauth:' || v_user::text || ':' || v_client, 0));
  if p_grant_kind = 'authorization_code' then
    select scope, resource into v_scope, v_resource from public.li_oauth_codes
      where code_hash = p_grant_hash and user_id = v_user and client_id = v_client and used_at is not null and expires_at > clock_timestamp();
  else
    select scope, resource, expires_at into v_scope, v_resource, v_expires from public.li_refresh_tokens
      where token_hash = p_grant_hash and user_id = v_user and client_id = v_client and used_at is not null and expires_at > clock_timestamp();
  end if;
  if not found then return false; end if;
  if v_scope <> 'mcp' or v_resource is null or v_resource = '' then raise exception 'invalid grant'; end if;
  if p_access->>'user_id' is distinct from v_user::text or p_refresh->>'user_id' is distinct from v_user::text
     or p_access->>'client_id' is distinct from v_client or p_refresh->>'client_id' is distinct from v_client
     or p_access->>'scope' is distinct from v_scope or p_refresh->>'scope' is distinct from v_scope
     or p_access->>'resource' is distinct from v_resource or p_refresh->>'resource' is distinct from v_resource then
    raise exception 'grant identity mismatch';
  end if;
  if coalesce(p_refresh->>'token_hash', '') !~ '^[0-9a-f]{64}$'
     or coalesce(p_access->>'token_hash', '') !~ '^[0-9a-f]{64}$' then
    raise exception 'invalid token hash';
  end if;
  insert into public.li_access_tokens(token_hash,user_id,client_id,scope,resource,expires_at,refresh_token_hash)
    values(p_access->>'token_hash',v_user,v_client,v_scope,v_resource,(p_access->>'expires_at')::timestamptz,
           p_refresh->>'token_hash');
  insert into public.li_refresh_tokens(token_hash,user_id,client_id,scope,resource,expires_at)
    values(p_refresh->>'token_hash',v_user,v_client,v_scope,v_resource,(p_refresh->>'expires_at')::timestamptz);
  if p_grant_kind = 'authorization_code' then
    delete from public.li_oauth_codes where code_hash = p_grant_hash;
  else
    -- The access token issued with the consumed refresh token is superseded.
    update public.li_access_tokens set revoked_at = coalesce(revoked_at, clock_timestamp())
      where refresh_token_hash = p_grant_hash and user_id = v_user and client_id = v_client;
    -- Keep a tombstone so a second presentation is recognised as replay.
    insert into public.li_oauth_consumed_refresh(token_hash, user_id, client_id, expires_at)
      values(p_grant_hash, v_user, v_client, v_expires)
      on conflict (token_hash) do nothing;
    -- Once issued, the consumed parent is removed so issuance cannot be replayed.
    delete from public.li_refresh_tokens where token_hash = p_grant_hash;
  end if;
  -- Opportunistic prune of tombstones past their refresh token's own expiry.
  delete from public.li_oauth_consumed_refresh where expires_at <= clock_timestamp();
  return true;
end $$;

-- Disconnect: identical to 20261012060000 plus the client's tombstones.
create or replace function public.li_disconnect_oauth_client(p_user_id uuid, p_client_id text)
returns void language plpgsql security definer set search_path = public as $$
begin
  if p_user_id is null or p_client_id is null or p_client_id = '' then raise exception 'identity required'; end if;
  perform pg_advisory_xact_lock(hashtextextended('li_oauth:' || p_user_id::text || ':' || p_client_id, 0));
  update public.li_access_tokens set revoked_at = coalesce(revoked_at, now()) where user_id = p_user_id and client_id = p_client_id;
  delete from public.li_refresh_tokens where user_id = p_user_id and client_id = p_client_id;
  delete from public.li_oauth_codes where user_id = p_user_id and client_id = p_client_id;
  delete from public.li_oauth_consumed_refresh where user_id = p_user_id and client_id = p_client_id;
end $$;

-- Replay: true when p_token_hash is a refresh token of p_client_id that was already
-- consumed (tombstoned, or consumed by a request that has not issued yet); the
-- user's connection of that client is then revoked. Unknown, expired and other
-- clients' tokens answer false and change nothing.
create or replace function public.li_oauth_refresh_replay(p_token_hash text, p_client_id text)
returns boolean language plpgsql security definer set search_path = public as $$
declare v_user uuid;
begin
  if p_token_hash is null or p_token_hash !~ '^[0-9a-f]{64}$' or p_client_id is null or p_client_id = '' then
    return false;
  end if;
  -- Resolve only the lock identity first; the replay is re-checked UNDER the lock.
  -- One statement, one snapshot: an issuance that commits meanwhile replaces the
  -- consumed row by its tombstone atomically, so exactly one of the two is seen.
  select owner into v_user from (
    select user_id as owner from public.li_oauth_consumed_refresh
      where token_hash = p_token_hash and client_id = p_client_id
    union all
    select user_id from public.li_refresh_tokens
      where token_hash = p_token_hash and client_id = p_client_id and used_at is not null
  ) found_owner limit 1;
  if v_user is null then return false; end if;
  perform pg_advisory_xact_lock(hashtextextended('li_oauth:' || v_user::text || ':' || p_client_id, 0));
  if not exists (
      select 1 from public.li_oauth_consumed_refresh where token_hash = p_token_hash
        and user_id = v_user and client_id = p_client_id and expires_at > clock_timestamp()
    ) and not exists (
      select 1 from public.li_refresh_tokens where token_hash = p_token_hash
        and user_id = v_user and client_id = p_client_id and used_at is not null
    ) then return false; end if;
  -- Reentrant acquisition inside disconnect preserves one lock for the transaction.
  perform public.li_disconnect_oauth_client(v_user, p_client_id);
  return true;
end $$;

revoke all on function public.li_issue_oauth_tokens(text,text,jsonb,jsonb) from public, anon, authenticated;
revoke all on function public.li_disconnect_oauth_client(uuid,text) from public, anon, authenticated;
revoke all on function public.li_oauth_refresh_replay(text,text) from public, anon, authenticated;
grant execute on function public.li_issue_oauth_tokens(text,text,jsonb,jsonb) to service_role;
grant execute on function public.li_disconnect_oauth_client(uuid,text) to service_role;
grant execute on function public.li_oauth_refresh_replay(text,text) to service_role;
