-- Minimal Supabase-compatible STUB for plain PostgreSQL 15/16.
--
-- WHAT THIS IS
--   Just enough of the Supabase platform for supabase/migrations to apply and
--   run on a stock postgres:16 container (GitHub Actions `services: postgres`):
--     * the API roles anon, authenticated and service_role (NOLOGIN), with
--       service_role BYPASSRLS as on Supabase;
--     * authenticator: a LOGIN, NOINHERIT role that can only SET ROLE to the
--       three API roles (the path PostgREST uses on Supabase);
--     * li_migrator: a NON-superuser owner that applies the migrations, standing
--       in for Supabase's non-superuser `postgres` role, so SECURITY DEFINER
--       functions run as an ordinary table owner, not as a superuser;
--     * Supabase's default privileges in schema public: every table, sequence
--       and function the migrator creates is granted to anon, authenticated and
--       service_role unless a migration revokes it. This is deliberately as
--       permissive as hosted Supabase, so the privilege tests prove the
--       migrations' own REVOKEs, not a locked-down stub;
--     * an `auth` schema with auth.users and auth.uid() reading the session
--       GUCs request.jwt.claim.sub / request.jwt.claims, like Supabase.
--
-- WHAT THIS IS NOT
--   It is a stub. It proves SQL and RPC behaviour, locking, concurrency and
--   grants on real PostgreSQL. It does NOT prove Supabase's hosted platform:
--   GoTrue auth (sign-up, email confirmation, JWT issuance and verification),
--   the RLS platform behaviour behind PostgREST, PostgREST's JWT-to-role switch
--   or its HTTP/JSON mapping, the hosted `postgres` role's exact attributes, or
--   Supabase extensions. Those still need a hosted Supabase project.
--
-- The migrations reference only auth.users(id) (foreign keys). email and
-- email_confirmed_at exist so tests can create realistic users; nothing else
-- of GoTrue's auth.users is modelled.
--
-- Roles are cluster-wide and created idempotently; everything else is per
-- database. Run as a superuser on a clean database before the migrations.

do $$
begin
  if not exists (select 1 from pg_roles where rolname = 'anon') then
    create role anon nologin noinherit;
  end if;
  if not exists (select 1 from pg_roles where rolname = 'authenticated') then
    create role authenticated nologin noinherit;
  end if;
  if not exists (select 1 from pg_roles where rolname = 'service_role') then
    create role service_role nologin noinherit bypassrls;
  end if;
  if not exists (select 1 from pg_roles where rolname = 'authenticator') then
    create role authenticator login noinherit;
  end if;
  if not exists (select 1 from pg_roles where rolname = 'li_migrator') then
    create role li_migrator nologin nosuperuser nocreatedb nocreaterole nobypassrls;
  end if;
end
$$;

grant anon, authenticated, service_role to authenticator;

-- Schema public: the API roles may use it; only the migrator creates in it.
grant usage on schema public to anon, authenticated, service_role;
grant usage, create on schema public to li_migrator;

-- Supabase's default privileges for objects created in public.
alter default privileges for role li_migrator in schema public
  grant all on tables to anon, authenticated, service_role;
alter default privileges for role li_migrator in schema public
  grant all on sequences to anon, authenticated, service_role;
alter default privileges for role li_migrator in schema public
  grant all on functions to anon, authenticated, service_role;

-- auth schema (GoTrue owns it on Supabase).
create schema if not exists auth;

create table if not exists auth.users (
  id uuid primary key,
  email text,
  email_confirmed_at timestamptz,
  created_at timestamptz not null default now()
);

-- Supabase's auth.uid(): the JWT subject from the request GUCs, or null.
create or replace function auth.uid()
returns uuid
language sql
stable
as $$
  select nullif(
    coalesce(
      current_setting('request.jwt.claim.sub', true),
      (nullif(current_setting('request.jwt.claims', true), '')::jsonb ->> 'sub')
    ),
    ''
  )::uuid
$$;

grant usage on schema auth to anon, authenticated, service_role, li_migrator;
grant execute on function auth.uid() to anon, authenticated, service_role, li_migrator;
-- The migrations' foreign keys need REFERENCES on auth.users.
grant references on auth.users to li_migrator;
