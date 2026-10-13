-- OAuth token families (refresh-token rotation with replay detection) and
-- RFC 7009 token revocation.
--
-- Forward migration; no earlier migration is edited.
--
-- Every authorization-code exchange starts a token family: the access token and
-- refresh token it issues share one grant_id, and every token issued by a
-- refresh of that family keeps the same grant_id. OAuth 2.1 (section 4.3.1)
-- requires public clients to rotate refresh tokens and lets the server detect
-- replay; here a replayed (already used or revoked) refresh token revokes the
-- whole family, so a stolen refresh token stops working for both the thief and
-- the legitimate client as soon as either uses it a second time.
--
-- li_rotate_refresh_token does the whole rotation in one transaction under a
-- row lock on the presented refresh token: it consumes the token, revokes the
-- family's previous access token(s) and inserts the new access and refresh
-- tokens (hashes only). A concurrent replay of the same token waits for the
-- lock, then sees it used and revokes the family including the tokens just
-- issued, so no newly issued token can escape a family revocation.
--
-- Rows written before this migration have grant_id null (a legacy token).
-- Their family cannot be reconstructed: refreshing one starts a new family
-- with a fresh grant_id, and the legacy access token issued alongside it is not
-- identifiable, so it is not revoked early and expires on its own (access
-- tokens live one hour).
--
-- Nothing secret is stored: token columns hold SHA-256 hex digests.

alter table public.li_access_tokens add column if not exists grant_id uuid;
alter table public.li_refresh_tokens add column if not exists grant_id uuid;
alter table public.li_refresh_tokens add column if not exists revoked_at timestamptz;

create index if not exists li_access_tokens_grant_idx
  on public.li_access_tokens(grant_id) where grant_id is not null;
create index if not exists li_refresh_tokens_grant_idx
  on public.li_refresh_tokens(grant_id) where grant_id is not null;

-- The limiter keys of the OAuth, account, action, case and activation buckets
-- become HMAC-SHA256 digests under LI_RATE_LIMIT_HMAC_KEY (hosted/ratelimit.py).
-- Remove the unsalted SHA-256 digests written before; they are dictionary-
-- reversible for IPv4 addresses. The windows are at most one hour long, and the
-- key change resets them anyway. Trial buckets were already keyed and stay.
delete from public.li_rate_limit_events
 where bucket in ('oauth_register', 'oauth_authorize', 'oauth_complete', 'oauth_token', 'account',
                  'actions', 'cases', 'activation_ip', 'activation_domain');

-- Rotate one refresh token. Returns a jsonb object whose "status" is
--   'rotated'  : the presented token was consumed, the family's earlier access
--                tokens were revoked and the new pair was inserted; the object
--                also carries user_id, client_id, scope, resource and grant_id;
--   'replayed' : the presented token was already used or revoked; the whole
--                family (every refresh and access token with its grant_id) is
--                now revoked;
--   'invalid'  : unknown or expired token, or invalid arguments; nothing changed.
-- A token presented by a client other than the one it was issued to is
-- consumed and its family revoked (a public client never legitimately holds
-- another client's refresh token), and the answer is 'invalid'.
create or replace function public.li_rotate_refresh_token(
  p_token_hash text,
  p_client_id text,
  p_new_refresh_hash text,
  p_new_access_hash text,
  p_access_seconds integer,
  p_refresh_seconds integer
)
returns jsonb
language plpgsql
security definer
set search_path = public
as $$
declare
  v_row public.li_refresh_tokens;
  v_grant uuid;
  v_now timestamptz := now();
begin
  if p_token_hash is null or p_token_hash !~ '^[0-9a-f]{64}$'
     or p_new_refresh_hash is null or p_new_refresh_hash !~ '^[0-9a-f]{64}$'
     or p_new_access_hash is null or p_new_access_hash !~ '^[0-9a-f]{64}$'
     or p_new_refresh_hash = p_token_hash or p_new_access_hash = p_token_hash
     or p_new_refresh_hash = p_new_access_hash
     or p_client_id is null or length(p_client_id) = 0
     or p_access_seconds is null or p_access_seconds not between 60 and 86400
     or p_refresh_seconds is null or p_refresh_seconds not between 60 and 31536000 then
    return jsonb_build_object('status', 'invalid');
  end if;

  select * into v_row from public.li_refresh_tokens where token_hash = p_token_hash for update;
  if not found then
    return jsonb_build_object('status', 'invalid');
  end if;
  v_grant := v_row.grant_id;

  if v_row.used_at is not null or v_row.revoked_at is not null or v_row.client_id <> p_client_id then
    if v_grant is not null then
      update public.li_refresh_tokens
         set revoked_at = coalesce(revoked_at, v_now), used_at = coalesce(used_at, v_now)
       where grant_id = v_grant;
      update public.li_access_tokens
         set revoked_at = v_now
       where grant_id = v_grant and revoked_at is null;
    else
      update public.li_refresh_tokens
         set revoked_at = coalesce(revoked_at, v_now), used_at = coalesce(used_at, v_now)
       where token_hash = p_token_hash;
    end if;
    if v_row.client_id <> p_client_id and v_row.used_at is null and v_row.revoked_at is null then
      return jsonb_build_object('status', 'invalid');
    end if;
    return jsonb_build_object('status', 'replayed');
  end if;

  if v_row.expires_at <= v_now then
    return jsonb_build_object('status', 'invalid');
  end if;

  update public.li_refresh_tokens set used_at = v_now where token_hash = p_token_hash;
  if v_grant is null then
    v_grant := gen_random_uuid();
  else
    update public.li_access_tokens
       set revoked_at = v_now
     where grant_id = v_grant and revoked_at is null;
  end if;

  insert into public.li_access_tokens(token_hash, user_id, client_id, scope, resource, expires_at, grant_id)
  values (p_new_access_hash, v_row.user_id, v_row.client_id, v_row.scope, v_row.resource,
          v_now + make_interval(secs => p_access_seconds), v_grant);
  insert into public.li_refresh_tokens(token_hash, user_id, client_id, scope, resource, expires_at, grant_id)
  values (p_new_refresh_hash, v_row.user_id, v_row.client_id, v_row.scope, v_row.resource,
          v_now + make_interval(secs => p_refresh_seconds), v_grant);

  return jsonb_build_object(
    'status', 'rotated',
    'user_id', v_row.user_id,
    'client_id', v_row.client_id,
    'scope', v_row.scope,
    'resource', v_row.resource,
    'grant_id', v_grant
  );
end;
$$;

revoke all on function public.li_rotate_refresh_token(text, text, text, text, integer, integer)
  from public, anon, authenticated;
grant execute on function public.li_rotate_refresh_token(text, text, text, text, integer, integer)
  to service_role;

-- RFC 7009 revocation of one token presented by client p_client_id.
-- Returns 'refresh_token' or 'access_token' for the kind of token found, or
-- 'none' when nothing issued to that client has that hash (unknown, invalid
-- arguments, or issued to another client: nothing changes). Revocation is
-- idempotent: an already-revoked token answers its kind again.
--   * refresh token: the token and its whole family (every refresh and access
--     token with its grant_id) are revoked; a legacy token without a family is
--     revoked alone;
--   * access token: that access token is revoked (its refresh token stays
--     usable, as RFC 7009 section 2.1 allows).
create or replace function public.li_revoke_oauth_token(p_token_hash text, p_client_id text)
returns text
language plpgsql
security definer
set search_path = public
as $$
declare
  v_row public.li_refresh_tokens;
  v_now timestamptz := now();
begin
  if p_token_hash is null or p_token_hash !~ '^[0-9a-f]{64}$'
     or p_client_id is null or length(p_client_id) = 0 then
    return 'none';
  end if;

  select * into v_row from public.li_refresh_tokens
   where token_hash = p_token_hash and client_id = p_client_id
     for update;
  if found then
    if v_row.grant_id is not null then
      update public.li_refresh_tokens
         set revoked_at = coalesce(revoked_at, v_now), used_at = coalesce(used_at, v_now)
       where grant_id = v_row.grant_id;
      update public.li_access_tokens
         set revoked_at = v_now
       where grant_id = v_row.grant_id and revoked_at is null;
    else
      update public.li_refresh_tokens
         set revoked_at = coalesce(revoked_at, v_now), used_at = coalesce(used_at, v_now)
       where token_hash = p_token_hash;
    end if;
    return 'refresh_token';
  end if;

  update public.li_access_tokens
     set revoked_at = coalesce(revoked_at, v_now)
   where token_hash = p_token_hash and client_id = p_client_id;
  if found then
    return 'access_token';
  end if;
  return 'none';
end;
$$;

revoke all on function public.li_revoke_oauth_token(text, text) from public, anon, authenticated;
grant execute on function public.li_revoke_oauth_token(text, text) to service_role;
