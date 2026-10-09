# Plan catalog

Source: `lofgren_intelligence/billing/catalog.py`, version `CATALOG_VERSION`
(currently `2026-10-06.1`). `docs/PLAN_CATALOG.json` is generated from it
(`python -m lofgren_intelligence.billing.catalog --write`) and
`tests/test_plan_catalog.py` keeps the two equal. Monetary values are read from
`billing/pricing.py` (`PLANS`) and are unchanged.

Rule: unknown = false. A value no code or owner decision fixes is
`"undecided"`; nothing substitutes a default for it, and a paid grant, checkout
or economic certification that needs it fails closed.

## Fields

| Field | Meaning |
| --- | --- |
| `id`, `name` | Catalog id (also the entitlement `plan_id`) and display name. |
| `status` | `available` (sold or granted now) or `planned` (shown only). |
| `price_usd`, `currency`, `billing_interval` | Plan fee in USD; `none` (free), `usage` (pay as you go) or `month`. |
| `rate_usd_per_work_unit`, `heavy_job_price_usd`, `included_heavy_jobs` | Per-use prices from `pricing.PLANS`. |
| `billable_unit` | `intelligence_unit` (see below). |
| `allowance.units` / `window` | Units per window, and the window (`rolling`, 604800 s, `UTC`), or `undecided`. |
| `allowance.operator_override_env` | The env var that may override a *decided* allowance (only where one existed before the catalog). Never decides an undecided one. The granted value is stored in the entitlement's `quota_units_per_week`. |
| `allowance.unenforced_proposal` | The old `entry_limit` figure, recorded for the owner, not applied. |
| `heavy_work` | Heavy-job size (400 WU) and how heavy work counts against the allowance. |
| `concurrency` | Per-user MCP request rate (`LI_MCP_REQUESTS_PER_MINUTE`, default 60) and concurrent-job limit. |
| `overage` | What happens past the allowance. |
| `cancellation` | Implemented behavior when a subscription ends; refund/proration policy. |
| `checkout_mode` | `subscription`, `undecided` (cannot be checked out) or null (no checkout). |
| `stripe_price_env` | Names of the env vars holding the test and live Stripe price id. Never ids. `LI_STRIPE_MODE` selects the set. |
| `eligibility` | Founding Free cohort: activation numbers 1–1000, then `paid_required`. |

## Plans (2026-10-06.1)

| Plan | Status | Price | Allowance | Overage | Checkout |
| --- | --- | --- | --- | --- | --- |
| Founding Free | available | $0 | 500 units / rolling 7 days, UTC (override `LI_FOUNDER_WEEKLY_UNITS`) | hard cap (QUOTA_EXCEEDED) | none |
| Pay as you go | planned | $0.0312 / WU, heavy $12.48 | undecided | undecided | undecided |
| Researcher | planned | $49.99 / month, $0.0156 / WU, 4 heavy, extra $9.36 | undecided (override `LI_PAID_WEEKLY_UNITS` only once decided) | undecided | subscription |
| Good Idea | planned | $79.99 / month, $0.0050 / WU, 6 heavy, extra $6.24 | undecided | undecided | subscription |

## Entries vs units (finding)

- **Unit.** The hosted service meters one quantity, the intelligence unit
  (`li_usage_events.units`, reserved by `li_reserve_usage` over a rolling
  604800-second window). `investigate` reserves `ResearchPlan.estimated_work_units`
  and settles `CostLedger.total_units`; `discover` settles ledger units. Those
  are pricing work units, so for research **1 WU = 1 intelligence unit**. Ad hoc
  tools charge `ADHOC_UNIT_COSTS` per call (2/5/5/5/1); `build_artifact` 10,
  `measure_outcome` and `evaluate_improvement` 5.
- **Entry.** `Plan.entry_limit` (Free 25/week, PAYG 500/week, Researcher
  400/week, Good Idea 20,000/month) is only printed by the CLI and was quoted
  on the pricing page. No code counts, meters or enforces entries, and nothing
  maps an entry to units. The old page text priced an entry at the per-WU rate,
  but the hosted quota never read `entry_limit` (Founding Free grants 500
  units, not 25 entries).
- **Conclusion.** They are different measures and the code implies no
  conversion, so `units_per_entry` is `undecided`. The page now shows the WU
  rate and the unit allowance only.

Conflicts resolved: Founding Free is the implemented 500 units (not 25
entries); the paid webhook no longer grants `LI_PAID_WEEKLY_UNITS` (default
2000) under `LI_PAID_PLAN_ID`; checkout no longer uses a single
`LI_STRIPE_PRICE_ID` but one env var per plan and mode.

## Consumers

- Pricing page (`hosted/site.py`) renders every card from the catalog.
- Quota defaults (`hosted/entitlements.py`): Founding Free from the catalog;
  a paid entitlement whose plan is unknown or undecided is refused.
- Checkout (`service.checkout`, `stripe.create_checkout`): allowlist =
  available + subscription + decided allowance + configured price; only
  `plan_id` is accepted from clients. A Price ID must identify exactly one
  available plan; duplicate mappings make every affected plan unsellable.
- Webhook (`stripe.apply_webhook`): subscription price id -> catalog plan ->
  catalog allowance; an unknown price, planned plan or undecided allowance
  grants nothing. An ambiguously configured price also grants nothing.
- Economic gate (`economics.certify_paid_plan(samples, plan)`): adds
  plan-not-available, undecided-allowance and price/allowance-mismatch refusals.
- `pricing` MCP tool returns the catalog.

## Owner decisions needed

1. Allowance per paid plan (Pay as you go, Researcher, Good Idea): units and window.
2. Entry -> unit conversion, or retire "entries" in favor of units.
3. How heavy work (400 WU) and included heavy jobs count against an allowance.
4. Overage policy per paid plan (hard cap, metered billing at the plan rate, or other).
5. Which plan(s) open first (status -> `available`), and Pay as you go's checkout mode.
6. Concurrent-job limits per plan; refund/proration on cancellation.
