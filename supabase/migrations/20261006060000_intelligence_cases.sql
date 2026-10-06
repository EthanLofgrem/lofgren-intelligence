-- Intelligence Cases: server-recorded clarification, versioned Case Charters and
-- browser-recorded, one-time charter approvals.
--
-- A client can no longer authorize research by sending `approved: true`. The
-- only authority for serious research is an li_case_approvals row that a signed
-- in account owner recorded on the /cases/{id} browser page. It binds the
-- principal, the case, the exact charter version and content hash, the
-- approved scope and budget, and an expiry. Starting research consumes it once
-- (li_consume_case_approval); a charter edit revokes it.
--
-- Added after the applied migrations; never edit an applied migration in place.

create table if not exists public.li_cases (
  id uuid primary key,
  user_id uuid not null references auth.users(id) on delete cascade,
  objective text not null check (length(objective) between 1 and 8000),
  status text not null check (status in (
    'clarifying','ready_for_approval','approved','research_started','research_complete','research_failed'
  )),
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);
create index if not exists li_cases_user_created_idx on public.li_cases(user_id, created_at desc);

create table if not exists public.li_case_charters (
  case_id uuid not null references public.li_cases(id) on delete cascade,
  user_id uuid not null references auth.users(id) on delete cascade,
  version integer not null check (version >= 1),
  content_hash text not null check (content_hash ~ '^[0-9a-f]{64}$'),
  charter jsonb not null,
  answers jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now(),
  constraint li_case_charters_case_version_key primary key (case_id, version)
);
create index if not exists li_case_charters_user_idx on public.li_case_charters(user_id, case_id);

create table if not exists public.li_case_approvals (
  id uuid primary key,
  case_id uuid not null references public.li_cases(id) on delete cascade,
  user_id uuid not null references auth.users(id) on delete cascade,
  charter_version integer not null check (charter_version >= 1),
  content_hash text not null check (content_hash ~ '^[0-9a-f]{64}$'),
  scope jsonb not null,
  budget_usd numeric not null check (budget_usd >= 0),
  budget_units numeric not null check (budget_units >= 0),
  expires_at timestamptz not null,
  approved_at timestamptz not null default now(),
  consumed_at timestamptz,
  revoked_at timestamptz,
  token_hash text not null unique check (token_hash ~ '^[0-9a-f]{64}$'),
  idempotency_key text,
  run_id text,
  run_status text check (run_status is null or run_status in ('started','complete','failed')),
  foreign key (case_id, charter_version) references public.li_case_charters(case_id, version) on delete cascade
);
create index if not exists li_case_approvals_case_idx
  on public.li_case_approvals(case_id, charter_version, approved_at desc);
create index if not exists li_case_approvals_user_idx on public.li_case_approvals(user_id, case_id);
-- At most one live (unconsumed, unrevoked) approval per case.
create unique index if not exists li_case_approvals_one_live_idx
  on public.li_case_approvals(case_id) where consumed_at is null and revoked_at is null;

create table if not exists public.li_case_events (
  id bigint generated always as identity primary key,
  case_id uuid not null references public.li_cases(id) on delete cascade,
  user_id uuid not null references auth.users(id) on delete cascade,
  kind text not null check (length(kind) between 1 and 64),
  charter_version integer,
  detail jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now()
);
create index if not exists li_case_events_case_idx on public.li_case_events(case_id, created_at);

alter table public.li_cases enable row level security;
alter table public.li_case_charters enable row level security;
alter table public.li_case_approvals enable row level security;
alter table public.li_case_events enable row level security;

revoke all on table public.li_cases from anon, authenticated;
revoke all on table public.li_case_charters from anon, authenticated;
revoke all on table public.li_case_approvals from anon, authenticated;
revoke all on table public.li_case_events from anon, authenticated;

grant select, insert, update, delete on public.li_cases to service_role;
grant select, insert, update, delete on public.li_case_charters to service_role;
grant select, insert, update, delete on public.li_case_approvals to service_role;
grant select, insert, update, delete on public.li_case_events to service_role;
grant usage, select on all sequences in schema public to service_role;

-- Create a case and its first charter version in one transaction.
create or replace function public.li_create_case(
  p_case_id uuid,
  p_user_id uuid,
  p_objective text,
  p_status text,
  p_content_hash text,
  p_charter jsonb,
  p_answers jsonb
)
returns integer
language plpgsql
security definer
set search_path = public
as $$
begin
  insert into public.li_cases(id, user_id, objective, status)
  values (p_case_id, p_user_id, p_objective, p_status);
  insert into public.li_case_charters(case_id, user_id, version, content_hash, charter, answers)
  values (p_case_id, p_user_id, 1, p_content_hash, p_charter, coalesce(p_answers, '{}'::jsonb));
  insert into public.li_case_events(case_id, user_id, kind, charter_version, detail)
  values (p_case_id, p_user_id, 'case_created', 1, jsonb_build_object('content_hash', p_content_hash));
  return 1;
end;
$$;

revoke all on function public.li_create_case(uuid,uuid,text,text,text,jsonb,jsonb)
  from public, anon, authenticated;
grant execute on function public.li_create_case(uuid,uuid,text,text,text,jsonb,jsonb)
  to service_role;

-- Optimistic-concurrency revision: succeeds only when p_expected_version is the
-- latest version of a case owned by p_user_id. Returns the new version, -1 on a
-- version conflict and 0 when the case is not the caller's. Every live approval
-- for the case is revoked: an edited charter is not the approved charter.
create or replace function public.li_revise_case(
  p_case_id uuid,
  p_user_id uuid,
  p_expected_version integer,
  p_status text,
  p_content_hash text,
  p_charter jsonb,
  p_answers jsonb
)
returns integer
language plpgsql
security definer
set search_path = public
as $$
declare
  v_latest integer;
begin
  perform 1 from public.li_cases where id = p_case_id and user_id = p_user_id for update;
  if not found then
    return 0;
  end if;

  select max(version) into v_latest from public.li_case_charters where case_id = p_case_id;
  if v_latest is null or p_expected_version is null or v_latest <> p_expected_version then
    insert into public.li_case_events(case_id, user_id, kind, charter_version, detail)
    values (p_case_id, p_user_id, 'revision_conflict', v_latest,
            jsonb_build_object('expected_version', p_expected_version));
    return -1;
  end if;

  insert into public.li_case_charters(case_id, user_id, version, content_hash, charter, answers)
  values (p_case_id, p_user_id, v_latest + 1, p_content_hash, p_charter, coalesce(p_answers, '{}'::jsonb));

  update public.li_case_approvals
     set revoked_at = now()
   where case_id = p_case_id and consumed_at is null and revoked_at is null;

  update public.li_cases set status = p_status, updated_at = now() where id = p_case_id;

  insert into public.li_case_events(case_id, user_id, kind, charter_version, detail)
  values (p_case_id, p_user_id, 'charter_revised', v_latest + 1,
          jsonb_build_object('content_hash', p_content_hash));
  return v_latest + 1;
end;
$$;

revoke all on function public.li_revise_case(uuid,uuid,integer,text,text,jsonb,jsonb)
  from public, anon, authenticated;
grant execute on function public.li_revise_case(uuid,uuid,integer,text,text,jsonb,jsonb)
  to service_role;

-- Record a browser approval of the exact latest charter version and content hash.
-- Returns true when recorded; false when the case is not the caller's, the version
-- is not the latest or the content hash does not match.
create or replace function public.li_approve_case_charter(
  p_id uuid,
  p_case_id uuid,
  p_user_id uuid,
  p_charter_version integer,
  p_content_hash text,
  p_scope jsonb,
  p_budget_usd numeric,
  p_budget_units numeric,
  p_expires_at timestamptz,
  p_token_hash text
)
returns boolean
language plpgsql
security definer
set search_path = public
as $$
declare
  v_latest integer;
  v_hash text;
begin
  perform 1 from public.li_cases where id = p_case_id and user_id = p_user_id for update;
  if not found then
    return false;
  end if;

  select version, content_hash into v_latest, v_hash
    from public.li_case_charters
   where case_id = p_case_id
   order by version desc
   limit 1;
  if v_latest is null or v_latest <> p_charter_version or v_hash <> p_content_hash
     or p_expires_at <= now() then
    return false;
  end if;

  update public.li_case_approvals
     set revoked_at = now()
   where case_id = p_case_id and consumed_at is null and revoked_at is null;

  insert into public.li_case_approvals(
    id, case_id, user_id, charter_version, content_hash, scope, budget_usd, budget_units,
    expires_at, token_hash
  )
  values (
    p_id, p_case_id, p_user_id, p_charter_version, p_content_hash, p_scope, p_budget_usd,
    p_budget_units, p_expires_at, p_token_hash
  );

  update public.li_cases set status = 'approved', updated_at = now() where id = p_case_id;

  insert into public.li_case_events(case_id, user_id, kind, charter_version, detail)
  values (p_case_id, p_user_id, 'charter_approved', p_charter_version,
          jsonb_build_object('approval_id', p_id, 'content_hash', p_content_hash,
                             'budget_usd', p_budget_usd, 'budget_units', p_budget_units,
                             'expires_at', p_expires_at));
  return true;
end;
$$;

revoke all on function public.li_approve_case_charter(uuid,uuid,uuid,integer,text,jsonb,numeric,numeric,timestamptz,text)
  from public, anon, authenticated;
grant execute on function public.li_approve_case_charter(uuid,uuid,uuid,integer,text,jsonb,numeric,numeric,timestamptz,text)
  to service_role;

-- Consume an approval exactly once. Succeeds only for the caller's live,
-- unexpired approval of the case's latest charter version whose token hash
-- matches. Returns the approval row with consumed_now = true; when it was
-- already consumed under the same idempotency key the row is returned with
-- consumed_now = false (a retry, never a second execution); otherwise null.
create or replace function public.li_consume_case_approval(
  p_id uuid,
  p_user_id uuid,
  p_token_hash text,
  p_idempotency_key text
)
returns jsonb
language plpgsql
security definer
set search_path = public
as $$
declare
  v_row public.li_case_approvals;
  v_latest integer;
begin
  if p_idempotency_key is null or length(p_idempotency_key) not between 1 and 200 then
    return null;
  end if;

  select * into v_row
    from public.li_case_approvals
   where id = p_id and user_id = p_user_id and token_hash = p_token_hash
   for update;
  if not found then
    return null;
  end if;

  if v_row.consumed_at is not null then
    if v_row.idempotency_key = p_idempotency_key then
      return to_jsonb(v_row) || jsonb_build_object('consumed_now', false);
    end if;
    return null;
  end if;

  select max(version) into v_latest from public.li_case_charters where case_id = v_row.case_id;
  if v_row.revoked_at is not null or v_row.expires_at <= now() or v_latest <> v_row.charter_version then
    return null;
  end if;

  update public.li_case_approvals
     set consumed_at = now(), idempotency_key = p_idempotency_key, run_status = 'started'
   where id = p_id
  returning * into v_row;

  update public.li_cases set status = 'research_started', updated_at = now() where id = v_row.case_id;

  insert into public.li_case_events(case_id, user_id, kind, charter_version, detail)
  values (v_row.case_id, p_user_id, 'approval_consumed', v_row.charter_version,
          jsonb_build_object('approval_id', p_id, 'idempotency_key', p_idempotency_key));

  return to_jsonb(v_row) || jsonb_build_object('consumed_now', true);
end;
$$;

revoke all on function public.li_consume_case_approval(uuid,uuid,text,text)
  from public, anon, authenticated;
grant execute on function public.li_consume_case_approval(uuid,uuid,text,text)
  to service_role;

-- Bind the research run started from a consumed approval (or record its failure).
create or replace function public.li_record_case_run(
  p_id uuid,
  p_user_id uuid,
  p_run_id text,
  p_run_status text
)
returns boolean
language plpgsql
security definer
set search_path = public
as $$
declare
  v_case uuid;
begin
  if p_run_status not in ('complete','failed') then
    return false;
  end if;
  update public.li_case_approvals
     set run_id = p_run_id, run_status = p_run_status
   where id = p_id and user_id = p_user_id and consumed_at is not null and run_status = 'started'
  returning case_id into v_case;
  if v_case is null then
    return false;
  end if;
  update public.li_cases
     set status = case when p_run_status = 'complete' then 'research_complete' else 'research_failed' end,
         updated_at = now()
   where id = v_case;
  insert into public.li_case_events(case_id, user_id, kind, detail)
  values (v_case, p_user_id, 'research_' || p_run_status,
          jsonb_build_object('approval_id', p_id, 'run_id', p_run_id));
  return true;
end;
$$;

revoke all on function public.li_record_case_run(uuid,uuid,text,text)
  from public, anon, authenticated;
grant execute on function public.li_record_case_run(uuid,uuid,text,text)
  to service_role;
