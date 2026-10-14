-- Correct previously-applied revocation without editing its migration.
-- An invalid historical credential must not disconnect a fresh consent.
create or replace function public.li_revoke_oauth_token(p_token_hash text, p_client_id text)
returns void language plpgsql security definer set search_path = public as $$
declare v_user uuid;
begin
  -- Resolve only the lock identity first; validity is rechecked UNDER the lock.
  select user_id into v_user from public.li_access_tokens
    where token_hash = p_token_hash and client_id = p_client_id;
  if v_user is null then
    select user_id into v_user from public.li_refresh_tokens
      where token_hash = p_token_hash and client_id = p_client_id;
  end if;
  if v_user is null then return; end if;
  perform pg_advisory_xact_lock(hashtextextended('li_oauth:' || v_user::text || ':' || p_client_id, 0));
  if not exists (
      select 1 from public.li_access_tokens where token_hash = p_token_hash
        and user_id = v_user and client_id = p_client_id
        and revoked_at is null and expires_at > clock_timestamp()
    ) and not exists (
      select 1 from public.li_refresh_tokens where token_hash = p_token_hash
        and user_id = v_user and client_id = p_client_id
        and used_at is null and expires_at > clock_timestamp()
    ) then return; end if;
  -- Reentrant acquisition inside disconnect preserves one lock for the transaction.
  perform public.li_disconnect_oauth_client(v_user, p_client_id);
end $$;
revoke all on function public.li_revoke_oauth_token(text,text) from public, anon, authenticated;
grant execute on function public.li_revoke_oauth_token(text,text) to service_role;
