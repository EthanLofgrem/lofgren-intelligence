-- Lofgren Intelligence public MCP foreign-key indexes.
-- Added after the initial public_mcp migration was applied; never edit the
-- applied migration in place.

create index if not exists li_access_tokens_user_idx
  on public.li_access_tokens(user_id);
create index if not exists li_access_tokens_client_idx
  on public.li_access_tokens(client_id);

create index if not exists li_oauth_codes_user_idx
  on public.li_oauth_codes(user_id);
create index if not exists li_oauth_codes_client_idx
  on public.li_oauth_codes(client_id);

create index if not exists li_refresh_tokens_user_idx
  on public.li_refresh_tokens(user_id);
create index if not exists li_refresh_tokens_client_idx
  on public.li_refresh_tokens(client_id);

create index if not exists li_discoveries_run_fk_idx
  on public.li_discoveries(user_id, research_id);

create index if not exists li_usage_events_run_fk_idx
  on public.li_usage_events(user_id, run_id);
