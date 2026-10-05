-- Hosted V3-V6 lifecycle persistence.
-- Service-role only; user-facing access occurs through authenticated LI service APIs.

create table if not exists public.li_artifacts (
  user_id uuid not null references auth.users(id) on delete cascade,
  artifact_id text not null,
  discovery_id text not null,
  kind text not null,
  artifact jsonb not null,
  receipt jsonb not null,
  v4_handoff jsonb not null,
  created_at timestamptz not null default now(),
  primary key (user_id, artifact_id),
  foreign key (user_id, discovery_id)
    references public.li_discoveries(user_id, discovery_id) on delete cascade
);
create index if not exists li_artifacts_discovery_fk_idx
  on public.li_artifacts(user_id, discovery_id);
create index if not exists li_artifacts_user_created_idx
  on public.li_artifacts(user_id, created_at desc);

create table if not exists public.li_action_proposals (
  user_id uuid not null references auth.users(id) on delete cascade,
  action_id text not null,
  artifact_id text not null,
  request jsonb not null,
  status text not null check (status in (
    'awaiting_approval','approved','executed','failed','rolled_back','rollback_failed'
  )),
  grant_record jsonb,
  approval_record jsonb,
  receipt jsonb,
  v5_handoff jsonb,
  created_at timestamptz not null default now(),
  approved_at timestamptz,
  executed_at timestamptz,
  primary key (user_id, action_id),
  foreign key (user_id, artifact_id)
    references public.li_artifacts(user_id, artifact_id) on delete cascade
);
create index if not exists li_actions_artifact_fk_idx
  on public.li_action_proposals(user_id, artifact_id);
create index if not exists li_actions_user_created_idx
  on public.li_action_proposals(user_id, created_at desc);

create table if not exists public.li_outcomes (
  user_id uuid not null references auth.users(id) on delete cascade,
  outcome_id text not null,
  action_id text not null,
  receipt jsonb not null,
  v6_handoff jsonb not null,
  created_at timestamptz not null default now(),
  primary key (user_id, outcome_id),
  foreign key (user_id, action_id)
    references public.li_action_proposals(user_id, action_id) on delete cascade
);
create index if not exists li_outcomes_action_fk_idx
  on public.li_outcomes(user_id, action_id);
create index if not exists li_outcomes_user_created_idx
  on public.li_outcomes(user_id, created_at desc);

create table if not exists public.li_improvements (
  user_id uuid not null references auth.users(id) on delete cascade,
  improvement_id text not null,
  outcome_id text not null,
  receipt jsonb not null,
  next_cycle jsonb not null,
  created_at timestamptz not null default now(),
  primary key (user_id, improvement_id),
  foreign key (user_id, outcome_id)
    references public.li_outcomes(user_id, outcome_id) on delete cascade
);
create index if not exists li_improvements_outcome_fk_idx
  on public.li_improvements(user_id, outcome_id);
create index if not exists li_improvements_user_created_idx
  on public.li_improvements(user_id, created_at desc);

alter table public.li_artifacts enable row level security;
alter table public.li_action_proposals enable row level security;
alter table public.li_outcomes enable row level security;
alter table public.li_improvements enable row level security;

revoke all on table public.li_artifacts from anon, authenticated;
revoke all on table public.li_action_proposals from anon, authenticated;
revoke all on table public.li_outcomes from anon, authenticated;
revoke all on table public.li_improvements from anon, authenticated;

grant select, insert, update, delete on public.li_artifacts to service_role;
grant select, insert, update, delete on public.li_action_proposals to service_role;
grant select, insert, update, delete on public.li_outcomes to service_role;
grant select, insert, update, delete on public.li_improvements to service_role;
