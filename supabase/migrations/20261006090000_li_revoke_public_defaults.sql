-- Q30: revoke the default privileges that API roles and PUBLIC hold on li_ objects.
--
-- Forward migration; no earlier migration is edited.
--
-- Supabase's default privileges in schema public grant ALL on every new table,
-- sequence and function to anon, authenticated and service_role, and PostgreSQL
-- itself grants EXECUTE on functions and USAGE on types to PUBLIC. Earlier
-- migrations revoke anon/authenticated on every li_ table and PUBLIC/anon/
-- authenticated on every li_ function, but not on the identity sequences behind
-- li_rate_events, li_rate_limit_events and li_case_events: anon and
-- authenticated could still read (SELECT), advance (USAGE: nextval) and reset
-- (UPDATE: setval) them.
--
-- This sweeps every li_ object in schema public and removes every privilege that
-- PUBLIC, anon or authenticated hold on it:
--   * sequences: ALL (USAGE, SELECT, UPDATE);
--   * tables:    ALL (already revoked from anon/authenticated; PUBLIC swept too);
--   * functions: ALL (EXECUTE) - idempotent with the per-function revokes;
--   * types:     USAGE (the PUBLIC default on every table row type).
-- service_role's explicit grants are not touched; it is granted USAGE on the li_
-- types explicitly so losing the PUBLIC default changes nothing for it.

do $$
declare
  r record;
begin
  for r in
    select n.nspname, c.relname, c.relkind
      from pg_class c
      join pg_namespace n on n.oid = c.relnamespace
     where n.nspname = 'public'
       and c.relname like 'li\_%'
       and c.relkind in ('S', 'r', 'p', 'v', 'm', 'f')
     order by c.relname
  loop
    if r.relkind = 'S' then
      execute format('revoke all on sequence %I.%I from public, anon, authenticated', r.nspname, r.relname);
    else
      execute format('revoke all on table %I.%I from public, anon, authenticated', r.nspname, r.relname);
    end if;
  end loop;

  for r in
    select n.nspname, p.proname, pg_get_function_identity_arguments(p.oid) as args
      from pg_proc p
      join pg_namespace n on n.oid = p.pronamespace
     where n.nspname = 'public'
       and p.proname like 'li\_%'
     order by p.proname, 3
  loop
    execute format('revoke all on function %I.%I(%s) from public, anon, authenticated',
                   r.nspname, r.proname, r.args);
  end loop;

  for r in
    select n.nspname, t.typname
      from pg_type t
      join pg_namespace n on n.oid = t.typnamespace
     where n.nspname = 'public'
       and t.typname like 'li\_%'
       and t.typelem = 0
     order by t.typname
  loop
    execute format('revoke usage on type %I.%I from public, anon, authenticated', r.nspname, r.typname);
    execute format('grant usage on type %I.%I to service_role', r.nspname, r.typname);
  end loop;
end
$$;
