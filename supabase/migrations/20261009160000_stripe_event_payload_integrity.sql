-- A Stripe event id is idempotent only when its payload is identical.
-- Forward-only hardening: preserve the original applied migration and replace
-- the RPC with the same signature and entitlement transaction.
create or replace function public.li_apply_stripe_entitlement_event(
  p_event_id text,
  p_event_type text,
  p_payload_hash text,
  p_user_id uuid,
  p_customer_id text,
  p_subscription_id text,
  p_active boolean,
  p_plan_id text,
  p_quota_units_per_week numeric
)
returns boolean
language plpgsql
security definer
set search_path = public
as $$
declare
  v_inserted text;
  v_existing_hash text;
begin
  insert into public.li_billing_events(stripe_event_id, event_type, payload_hash)
  values (p_event_id, p_event_type, p_payload_hash)
  on conflict (stripe_event_id) do nothing
  returning stripe_event_id into v_inserted;

  if v_inserted is null then
    select payload_hash into v_existing_hash
      from public.li_billing_events
     where stripe_event_id = p_event_id;
    if v_existing_hash is distinct from p_payload_hash then
      raise exception 'Stripe event id was reused with a different payload'
        using errcode = '22000';
    end if;
    return false;
  end if;

  if p_user_id is not null then
    insert into public.li_entitlements(
      user_id, kind, plan_id, active, quota_units_per_week,
      stripe_customer_id, stripe_subscription_id, updated_at
    )
    values (
      p_user_id,
      case when p_active then 'paid' else 'paid_required' end,
      p_plan_id,
      p_active,
      case when p_active then p_quota_units_per_week else 0 end,
      p_customer_id,
      p_subscription_id,
      now()
    )
    on conflict (user_id) do update set
      kind = excluded.kind,
      plan_id = excluded.plan_id,
      active = excluded.active,
      quota_units_per_week = excluded.quota_units_per_week,
      stripe_customer_id = coalesce(excluded.stripe_customer_id, public.li_entitlements.stripe_customer_id),
      stripe_subscription_id = coalesce(excluded.stripe_subscription_id, public.li_entitlements.stripe_subscription_id),
      updated_at = now();
  end if;

  return true;
end;
$$;

revoke all on function public.li_apply_stripe_entitlement_event(text,text,text,uuid,text,text,boolean,text,numeric)
  from public, anon, authenticated;
grant execute on function public.li_apply_stripe_entitlement_event(text,text,text,uuid,text,text,boolean,text,numeric)
  to service_role;
