-- Correct expiration checks after connection-lock waits; prior migrations unchanged.
-- Atomic OAuth disconnect and grant-bound token issuance. No browser-role access.
-- Revocation and issuance share a user/client lock. Deleting consumed grants
-- prevents a refresh/code exchange already in flight from undoing disconnect.
create or replace function public.li_issue_oauth_tokens(
  p_grant_kind text, p_grant_hash text, p_access jsonb, p_refresh jsonb
) returns boolean language plpgsql security definer set search_path = public as $$
declare v_user uuid; v_client text; v_scope text; v_resource text;
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
    select scope, resource into v_scope, v_resource from public.li_refresh_tokens
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
  insert into public.li_access_tokens(token_hash,user_id,client_id,scope,resource,expires_at)
    values(p_access->>'token_hash',v_user,v_client,v_scope,v_resource,(p_access->>'expires_at')::timestamptz);
  insert into public.li_refresh_tokens(token_hash,user_id,client_id,scope,resource,expires_at)
    values(p_refresh->>'token_hash',v_user,v_client,v_scope,v_resource,(p_refresh->>'expires_at')::timestamptz);
  -- Once issued, the consumed parent is removed so issuance cannot be replayed.
  if p_grant_kind = 'authorization_code' then
    delete from public.li_oauth_codes where code_hash = p_grant_hash;
  else delete from public.li_refresh_tokens where token_hash = p_grant_hash; end if;
  return true;
end $$;

revoke all on function public.li_issue_oauth_tokens(text,text,jsonb,jsonb) from public, anon, authenticated;
grant execute on function public.li_issue_oauth_tokens(text,text,jsonb,jsonb) to service_role;
