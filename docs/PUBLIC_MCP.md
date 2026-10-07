# Public MCP service

This document describes the hosted/public distribution layer for the certified
Lofgren Intelligence V1-V6 stack on the `release/public-v1-v6` line.

## Truthful capability boundary

Public operation exposes only capabilities that are implemented and certified.
On this line the hosted MCP service exposes V1 Evidence Intelligence, V2
Discovery Intelligence (hosted discovery results report contract
`lofgren.mcp/2`), V3 production (`build_artifact`, `get_artifact`), V4
authorized execution (`propose_action`, browser approval, `execute_action`),
V5 outcome measurement and V6 improvement evaluation, each certified by its
own gate (`certify`, `certify --v2` ... `certify --v6`, `certify-boundary`).
Certified versions are not the same as public readiness: the public release
gate (`scripts/public_mcp_gate.py`) still needs deployment evidence.

The exact hosted tool list is generated, not hand-maintained:
[`docs/CAPABILITIES.json`](CAPABILITIES.json) comes from
`python -m lofgren_intelligence.hosted.capabilities`, which reads the
registry `build_mcp` serves and assigns each tool its level. A test keeps the
registry, the manifest and this list equal.

- **V1** (Evidence intelligence (research and verification)): `cancel_research`, `case_status`, `clarify_objective`, `compile_objective`, `export_knowledge_map2`, `export_state`, `find_contradictions`, `find_gaps`, `get_finding`, `get_job_status`, `get_receipt`, `investigate`, `plan_research`, `render_report`, `satellite_passes`, `start_research`, `trace_claim`, `verify_claim`.
- **V2** (Discovery intelligence): `analyze_sensitivity`, `create_v3_handoff`, `discover`, `find_connections`, `find_discovery_gaps`, `find_prior_art`, `generate_candidates`, `generate_hypotheses`, `get_discovery_receipt`, `optimize_solution`, `render_discovery_report`, `simulate_candidate`, `verify_discovery`.
- **V3** (Production (verified artifacts)): `build_artifact`, `get_artifact`.
- **V4** (Authorized execution (browser-approved actions)): `action_status`, `execute_action`, `propose_action`.
- **V5** (Outcome measurement): `get_outcome`, `measure_outcome`.
- **V6** (Reviewed improvement evaluation): `evaluate_improvement`, `get_improvement`.
- **account** (Account, usage and billing): `account_status`, `billing_portal`, `create_checkout`, `pricing`, `usage_status`.

`create_checkout` is registered but refuses until `LI_BILLING_ENABLED` is set
and the paid plan passes the P95 economic gate.

Not proven by this repository: deployment, real clients (no real third-party
MCP client session is verified), backup/restore, the Supabase owner settings
(email confirmation, CAPTCHA), and PublicMCPReady (the public release gate has
not passed).

## Intelligence Cases (clarification and charter approval)

A consequential, broad or underspecified objective (for example "design a
low-cost water purification system") is not researched straight away.
`investigate` and `plan_research` return 3-7 high-information clarification
questions instead, and the work happens through an Intelligence Case.

### Classification and domain question banks

Every objective is classified deterministically (explicit keyword and
structure rules in `lofgren_intelligence/intent/clarification.py`, no model
call). The result is returned as `domain`, `consequence` and
`classification` (`broad_scope`, `domains_matched`, `signals`):

- **Domain** (priority order): `medical`, `legal`, `financial`, `safety`,
  `engineering_design`, `general`. A design objective whose only special
  match is safety keeps the engineering-design bank. An objective that matches
  no domain takes the `general` path; one bank is used per case, so no other
  domain's prompts appear.
- **Consequence** is `high` for medical, legal and safety objectives, for any
  amount of money (a financial objective with a money target), and for public
  impact (public, communities, residents...); otherwise `standard`.
- **Broad scope**: two or more sub-questions, a chain of three or more
  and/or, an enumeration of several open dimensions, ranking words
  ("strongest", "best") or prioritisation ("which ... deserve priority").
- **Rule**: clarification is required for any high-consequence objective, any
  broad objective, and (as before) any short design objective. A simple
  factual question ("What is the capital of France?") skips it.

| Domain | Question keys (highest information first) |
| --- | --- |
| medical | `purpose` (education, research, product or clinician-discussion prep; never individual care), `population`, `focus`, `emphasis`, `evidence_types` (incl. preclinical), `evidence_cutoff`, `depth_budget` |
| financial / commercial | `purpose`, `baseline_assets`, `revenue_definition` (one-time vs MRR, timeframe), `launch_budget` (is spending authorized), `channel_constraints` (is a named platform required or preferred), `risk_and_hours`, `evidence_standard` |
| legal / regulatory | `jurisdiction`, `parties` (by role), `purpose` (research, not legal advice), `time`, `sources` |
| safety / infrastructure | `jurisdiction`, `standards`, `affected_population`, `purpose` |
| engineering_design | `location`, `users`, `problem`, `success`, `cost`, `constraints`, `source_or_environment` (unchanged) |
| general | `purpose`, `scope`, `time_cutoff`, `sources`, `depth_budget` |

Each question carries why it matters and examples. Only unanswered questions
are asked; an answer (including `unknown`) is kept and never asked again. No
question asks for identifiable patient information.

Medical, financial, legal and safety cases carry a fixed `safety_notice`
(medical: no diagnosis or treatment advice, never share identifiable patient
information; financial: no personalised advice or revenue guarantee) and
domain `exclusions` (medical: no dosing or treatment instructions, no patient
contact, no private medical data; financial: no spending, publishing,
customer contact or supplier commitments without approval). A notice never
replaces clarification, evidence verification or approval, and LI is not a
diagnostic or legal-advice engine: a first-person medical or legal request is
flagged `individual_advice_request` and excluded from advice. Every charter
`authorizes: research_only`; operational verbs in the objective (launch,
spend, publish, contact...) are flagged `operational_execution_requested`
and are researched, never executed.

`use a reasonable default` records an explicit, conservative default in the
charter's `defaults_applied` (`recorded_as: default`, `verified: false`);
`unknown` stays in `critical_unknowns`. `READY_FOR_SCOPE_APPROVAL` never
means approved.

### Case flow

1. `clarify_objective` opens a case (`case_id`, charter version 1, content
   hash). Each later call with `case_id` and `expected_version` records the
   answers as a new charter version; an `expected_version` that is not the
   latest is refused (`CASE_VERSION_CONFLICT`). An answer may be a value,
   `unknown`, `skip`, `later`/`ask me later` or `use a reasonable default`;
   unknowns are kept across versions and are not asked again. A simple
   research question is not forced through the interview.
2. When no question is open the charter is ready and `clarify_objective`
   returns an `approval_url` (`/cases/{case_id}`). Approval is **not** an MCP
   call: the account owner signs in on that page (the same account session as
   `/account` and `/actions`, but a separate approval from V4 actions),
   reviews the exact charter version and approves it. The approval row
   (`li_case_approvals`) binds the user, the case, the charter version, the
   content hash, the scope, the budget (`max_spend_usd`, `max_units`) and an
   expiry (`LI_CASE_APPROVAL_TTL_MINUTES`, default 60).
3. `investigate` with the `case_id` starts research only with a valid,
   unexpired, unconsumed approval of the latest charter version owned by the
   caller, and consumes it once (`li_consume_case_approval`). A retry with
   the same `idempotency_key` (default: the approval id) returns the same run
   and never executes twice; another key is refused
   (`CASE_APPROVAL_CONSUMED`). The run uses the approved charter's objective,
   sources and budget, never the values in the call; a plan larger than the
   approved `max_units` is refused (`CASE_BUDGET_EXCEEDED`).
4. Any charter edit, including a budget increase, creates a new version and
   revokes the old approval, so it needs a fresh approval.

A client-sent `case_charter` (formerly honoured when it said
`approved: true`) is ignored: nothing a client sends is authority. Refusals
are typed: `CASE_NOT_FOUND` (also for another account's case),
`CASE_INVALID`, `CASE_NOT_READY`, `CASE_APPROVAL_REQUIRED`,
`CASE_APPROVAL_CONSUMED`, `CASE_BUDGET_EXCEEDED`, `CASE_RUN_FAILED`. Cases,
charter versions, approvals and an audit trail (`li_case_events`) are stored
by migration `20261006060000_intelligence_cases.sql` (RLS enabled, no
anon/authenticated grants). `case_status` reads a case and its approval
state.

## Durable research jobs

`investigate` runs inside one HTTP request, and that request is capped at 60
seconds. It therefore accepts only bounded work. Anything larger is refused
with `ASYNC_REQUIRED`. `start_research` handles that work. It takes the same
inputs plus `idempotency_key` and an optional `max_units` budget. It reserves
usage, enqueues a durable job and returns its `job_id` at once. The same key
returns the same job and never runs it twice.

With a `case_id`, the approval is consumed once at enqueue. The job stores the
approved charter's objective, sources and budget, and the worker uses only
those. `get_job_status` reads a job's status, attempts, units so far and,
once it has succeeded, the run summary. `cancel_research` cancels a queued job
at once. A running job stops at its next stage, is charged only for the work
done and reports `cancelled`. A finished job is not changed.

Refusals are typed: `JOB_NOT_FOUND` (this is also returned for another
account's job), `JOB_INVALID` and `ASYNC_REQUIRED`. Jobs are stored by
migration `20261006070000_li_research_jobs.sql` (plus the settlement cap in
`20261006080000_li_research_job_finalize_cap.sql`), with RLS enabled and no
anon/authenticated grants. `20261006090000_li_revoke_public_defaults.sql` removes
every default privilege PUBLIC, anon and authenticated still held on li_ objects
(the identity sequences, table row types and functions). An always-on worker process runs them; see
[WORKER_RUNTIME.md](WORKER_RUNTIME.md). No worker deployment is verified by
this repository.

## Identity and OAuth

Remote MCP uses OAuth 2.1-style Authorization Code + PKCE (S256). MCP clients
may dynamically register as public clients. End users authenticate through
Supabase Auth; the server validates the Supabase session before issuing a
one-time authorization code. MCP access/refresh tokens are opaque and only
hashes are persisted.

The authorization page names the requesting client and the host its redirect
URI points to. A code is issued only after an explicit click on "Approve and
continue"; signing in, signing up or an existing browser session only reveal
that button and never authorize silently. Because client registration is
open, client names are self-declared, so the redirect host is the part users
should check. Registered redirect URIs must be https, or plain http to exactly
`localhost`, `127.0.0.1` or `[::1]` (lookalike hosts such as
`localhost.example.com`, userinfo, fragments and invalid ports are refused).

Discovery endpoints:

- `/.well-known/oauth-protected-resource`
- `/.well-known/oauth-authorization-server`
- `/oauth/register`
- `/oauth/authorize`
- `/oauth/token`

Remote MCP endpoint: `/mcp`.

## Founding Free rule

Activation is atomic in the database.

- activation numbers 1–1000 → `founding_free`, no card required;
- activation number 1001+ → `paid_required`;
- Founding Free is still quota-limited; free does not mean unbounded compute.

The current migration seeds 500 intelligence units per rolling 7-day period
for Founding Free. This is an operational safety limit and can be deliberately
changed after measured COGS review.

Anti-abuse in front of activation. Activation happens on the first
authorization of a new account (`/oauth/authorize/complete`), and only that
first authorization is checked; re-authorizing an existing account takes
nothing. `li_activate_account` is idempotent per user, so a retry or a
concurrent duplicate never consumes a second slot.

- **Confirmed email.** The Supabase user fetched from `/auth/v1/user` must
  have an email and a non-empty `email_confirmed_at`, otherwise the server
  answers `403 email_not_confirmed` and no slot is taken. The check is on by
  default; only `LI_REQUIRE_CONFIRMED_EMAIL=false` (or `0`/`no`/`off`)
  disables it.
- **Rate limits.** New activations are limited per client IP (5 per hour,
  `LI_IP_RATE_LIMIT_ACTIVATION_IP`) and per email domain (30 per hour,
  `LI_IP_RATE_LIMIT_ACTIVATION_DOMAIN`) through the global store-backed
  limiter (`429` with `Retry-After`). If the limiter cannot answer, the
  activation is refused with `503` (fail closed). The keyed rate-limit
  migration must therefore be applied before sign-ups are opened.
- **Owner settings, not code.** Supabase "Confirm email" and CAPTCHA (Supabase
  Auth bot protection with hCaptcha or Cloudflare Turnstile) are configured by
  the owner in the Supabase dashboard; each is an owner setting this
  repository cannot turn on or verify. The server-side confirmed-email check
  is only meaningful while "Confirm email" is on: with it off, Supabase
  auto-confirms every sign-up. Neither setting is proven by any test here.

## Billing

Stripe-hosted Checkout is implemented behind `LI_BILLING_ENABLED`. Public
deployment must keep this false until a paid plan passes the economic gate:
measured P95 cost, chosen gross-margin floor, test-mode payment-to-entitlement
journey, webhook idempotency, failure/cancel behavior and owner approval.

Stripe delivers webhook events in no guaranteed order, so each
entitlement-changing event re-reads the subscription from Stripe and applies
its current status (`active`/`trialing` grant access, anything else does not);
an older subscription's lapse does not revoke a newer one. A checkout session
grants access only when `payment_status` is `paid` or `no_payment_required`; a
missing payment status grants nothing. The webhook handler therefore also needs
`STRIPE_SECRET_KEY`; if Stripe cannot be read the event is refused and Stripe
retries it. Account deletion cancels an attached subscription first; a
subscription that has already ended (or no longer exists) does not block
deletion, any other Stripe failure stops it before data is removed.

The economic gate that unlocks checkout also needs `LI_PAID_MONTHLY_USD`,
`LI_PAYMENT_FEE_PERCENT`, `LI_PAYMENT_FEE_FIXED_USD` (optional
`LI_ECON_MIN_SAMPLES`, default and minimum 100: it can raise the sample floor
but a lower value is ignored, and `LI_TARGET_GROSS_MARGIN`, default
0.65). Any usage sample with an unpriced component fails the gate.

The server expects:

- `STRIPE_SECRET_KEY`
- `STRIPE_WEBHOOK_SECRET`
- `LI_STRIPE_MODE` (`test` or `live`; must agree with the secret key)
- per plan, `LI_STRIPE_PRICE_ID_<PLAN>_TEST` / `LI_STRIPE_PRICE_ID_<PLAN>_LIVE`
  (names in `docs/PLAN_CATALOG.json`)

Plans, allowances and the price allowlist come from the versioned plan catalog
(`lofgren_intelligence/billing/catalog.py`, `docs/PLAN_CATALOG.md`). Checkout
sells only a catalog plan that is `available`, has a decided allowance and a
price configured for the current mode; clients may send only `plan_id`. A
webhook maps the subscription's price id to its catalog plan and grants that
plan's allowance; an unknown price grants nothing. `LI_STRIPE_PRICE_ID` and
`LI_PAID_PLAN_ID` are no longer read. `LI_FOUNDER_WEEKLY_UNITS` and
`LI_PAID_WEEKLY_UNITS` remain only as operator overrides of a decided catalog
allowance (Founding Free and Researcher respectively); the granted value is
stored in the entitlement's `quota_units_per_week`. An override never decides
an undecided allowance: an undecided plan fails closed.

## Required deployment variables

- `SUPABASE_URL`
- `SUPABASE_SERVICE_ROLE_KEY` (server only)
- `SUPABASE_PUBLISHABLE_KEY`
- `LI_PUBLIC_BASE_URL`
- optional `BRAVE_API_KEY`
- optional model provider variables
- actual-cost rates:
  - `LI_MODEL_INPUT_USD_PER_M_TOKENS`
  - `LI_MODEL_OUTPUT_USD_PER_M_TOKENS`
  - `LI_RETRIEVAL_USD_PER_CALL`
  - `LI_INFRA_USD_PER_RUN`

If material cost components are not configured, runs explicitly report
`cost_fully_priced=false`. That condition must block economic certification.

## Security boundaries

Remote MCP rejects server-local file paths, caps objective/text/URL sizes,
allows outbound reads only to globally routable addresses (private, loopback,
link-local, shared/CGNAT `100.64.0.0/10`, benchmarking, multicast, reserved and
the IPv4-mapped, NAT64, 6to4 and IPv4-compatible IPv6 forms of those are
refused) and re-checks redirects. External actions (V4) run only after the
user approves the exact target and payload in the browser; the approval
expires after ten minutes.

Metered work (`investigate`, `verify_claim`, `discover`, `build_artifact`,
`measure_outcome`, `evaluate_improvement`) reserves units atomically through
`li_reserve_usage` before running and settles actual units afterwards. A
refusal before any work (unknown input, the public per-run cost ceiling)
releases the reservation. A failure after work incurred cost charges what is
known so far (a run that fails part-way is charged its measured units with the
`partial_run` component; work whose result could not be stored is charged
without a run id). Work that ran is charged even when `li_finalize_usage`
refuses or fails: the actual usage is recorded on the reservation as an
`unsettled` marker (`li_mark_usage_unsettled`) that counts against quota and
never expires, and `li_settle_usage` settles it exactly once
(`PublicService.reconcile_usage` / `reconcile_unsettled_usage`; migration
`20261006060100_usage_settlement.sql`). The usage event id is the
reservation id, so a retry never charges twice, and a stored result is kept
when only its settlement failed. An
explicit stored weekly quota of 0 blocks; only a missing quota falls back to
the plan default. `satellite_passes` is bounded to 168 hours, 20 element sets
and 20,000 characters of TLE text per call, with valid coordinates and
elevation.

The ad hoc tools are metered too. Each call requires an active entitlement
with weekly quota remaining, reserves a flat number of intelligence units
through `li_reserve_usage`, and settles that amount through
`li_finalize_usage` only when the call succeeds; a call that fails (bad
arguments, unknown run, corrupt state, a crash) releases its reservation
through `li_release_usage` and is not charged. Unit costs per call:

| Tool | Units per call |
| --- | --- |
| `find_prior_art` | 2 |
| `simulate_candidate` | 5 |
| `analyze_sensitivity` | 5 |
| `optimize_solution` | 5 |
| `satellite_passes` | 1 |

The operator may override a cost with `LI_UNITS_<TOOL>` (for example
`LI_UNITS_SIMULATE_CANDIDATE=8`); a value that is not a finite number >= 0 is
ignored. The values live in `ADHOC_UNIT_COSTS` in
`lofgren_intelligence/hosted/service.py` and a test keeps this table equal to
it. Settled ad hoc usage records `<tool>_compute` as an unpriced component,
so it counts against quota but keeps the paid-plan economic gate closed until
its cost is measured.

Weekly usage sums and privacy exports read every row page by page, so they are
not truncated at the PostgREST max-rows setting. Database failures reach
callers only as `database request failed (HTTP <status>)` or `database is
unreachable`, never with table, column or constraint names.

Rate limits: `/mcp` is limited per user in the database
(`li_take_rate_limit`). The endpoints reachable before a user is known —
`/oauth/register` (10/min), `/oauth/authorize` (60/min),
`/oauth/authorize/complete` (20/min), `/oauth/token` (60/min),
`/account/export` and `/account/delete` (20/min together),
`/actions/{id}/details` and `/actions/{id}/approve` (60/min together),
`/cases/{id}/charter` and `/cases/{id}/approve` (60/min together) — are
limited per client IP and answer `429` with `Retry-After`.

When the store is configured (`SUPABASE_URL` and `SUPABASE_SERVICE_ROLE_KEY`)
these limits are **global**: every request takes a slot through the atomic
SQL function `li_take_keyed_rate_limit` (migration
`20261005191819_keyed_rate_limits.sql`, table `li_rate_limit_events`, RLS
enabled, no anon/authenticated grants), so they hold across serverless
instances and cold starts. Keys are sent as SHA-256 digests of
`<bucket>:<ip>`; raw IPs are not stored, and rows are pruned once outside
their window. Without a configured store (local development) or with
`LI_RATE_LIMIT_BACKEND=memory`, the limiter falls back to process memory,
which is **per instance**. If the store errors, sign-up and activation
endpoints (`/oauth/register`, `/oauth/authorize/complete`) **fail closed**
with `503` and `Retry-After`; the other endpoints fall back to the
per-instance limiter rather than to no limit. Limits can be changed with
`LI_IP_RATE_LIMIT_<BUCKET>` (for example `LI_IP_RATE_LIMIT_OAUTH_REGISTER`).
The client IP is the socket peer unless the operator sets
`LI_CLIENT_IP_HEADER` to a header the platform overwrites (for example
`x-real-ip`); forwarded headers are otherwise ignored because clients control
them.

Database tables use RLS with no anonymous/authenticated policies. The hosted
service uses the service-role key; browser clients do not receive it.

## Public-readiness principle

A passing build or successful deployment is not enough. Public readiness
requires exact-SHA CI, hosted-service tests, database migration verification,
OAuth interoperability, tenant isolation, quota/abuse checks, Stripe sandbox
certification for paid-required accounts, health/readiness, rollback evidence
and a staging journey.
