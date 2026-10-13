-- Supabase CLI unavailable locally; UTC timestamp generated for a new additive migration.
-- Manual synthetic acceptance only: no global queue/housekeeping access.
create or replace function public.li_claim_targeted_research_job(
 p_id uuid, p_user_id uuid, p_case_id uuid, p_content_hash text,
 p_expected_input jsonb, p_worker text
) returns jsonb language plpgsql security definer set search_path=public as $$
declare v_row public.li_research_jobs;
begin
 if p_worker is null or length(p_worker) not between 1 and 200
    or p_content_hash is null or p_content_hash !~ '^[0-9a-f]{64}$'
    or p_expected_input is null then return null; end if;
 -- Serialize with charter revisions before reading the latest version.
 perform 1 from public.li_cases where id=p_case_id and user_id=p_user_id for update skip locked;
 if not found then return null; end if;
 select j.* into v_row
 from public.li_research_jobs j
 join public.li_cases c on c.id=j.case_id and c.user_id=j.user_id
 join public.li_case_approvals a on a.id::text=j.input->'case'->>'approval_id'
 join public.li_case_charters ch on ch.case_id=c.id and ch.version=a.charter_version
 join public.li_usage_reservations r on r.id=j.reservation_id and r.user_id=j.user_id
 join public.li_entitlements e on e.user_id=j.user_id
 where j.id=p_id and j.user_id=p_user_id and j.case_id=p_case_id
 and j.input=p_expected_input and j.input->>'schema'='lofgren.research-job/1'
 and j.input->>'execution_inputs'='approved_charter'
 and j.input->'case'->>'case_id'=p_case_id::text
 and j.input->'case'->>'content_hash'=p_content_hash
 and a.user_id=p_user_id and a.case_id=p_case_id
 and a.content_hash=p_content_hash and ch.content_hash=p_content_hash and ch.user_id=p_user_id
 and j.input->'case'->>'charter_version'=a.charter_version::text
 and a.charter_version=(select max(version) from public.li_case_charters where case_id=p_case_id)
 and a.revoked_at is null and a.consumed_at is not null and a.expires_at>now()
 and a.run_status='started' and a.run_id is null
 and j.status='queued' and j.not_before<=now() and j.queue_expires_at>now()
 and j.attempts=0 and j.cost_so_far=0 and j.lease_owner is null and j.lease_expires_at is null
 and r.status='reserved' and r.job_id=j.id and e.active=true
 and jsonb_typeof(j.input->'args')='object'
 and not exists(select 1 from jsonb_object_keys(j.input->'args') k where k not in ('texts','max_spend_usd'))
 and j.input->>'objective'=ch.charter->>'objective'
 and j.input->'args'->'texts'=ch.charter->'sources'->'texts'
 and jsonb_typeof(j.input->'args'->'texts')='object'
 and (select count(*) from jsonb_object_keys(j.input->'args'->'texts')) between 1 and 4
 and octet_length((j.input->'args'->'texts')::text)<=16000
 and not exists(select 1 from jsonb_each(j.input->'args'->'texts') t where jsonb_typeof(t.value)<>'string' or length(t.key) not between 1 and 200)
 for update of j,c,a,ch,r,e skip locked;
 if not found then return null; end if;
 update public.li_research_jobs set status='running',lease_owner=p_worker,
 lease_expires_at=now()+interval '120 seconds', heartbeat_at=now(),attempts=1,max_attempts=1,
 started_at=coalesce(started_at,now()),updated_at=now() where id=v_row.id returning * into v_row;
 update public.li_usage_reservations set held_until=v_row.lease_expires_at+interval '30 seconds'
 where id=v_row.reservation_id;
 return to_jsonb(v_row);
end $$;
revoke all on function public.li_claim_targeted_research_job(uuid,uuid,uuid,text,jsonb,text) from public,anon,authenticated;
grant execute on function public.li_claim_targeted_research_job(uuid,uuid,uuid,text,jsonb,text) to service_role;
