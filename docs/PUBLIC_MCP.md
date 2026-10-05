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
`LI_ECON_MIN_SAMPLES`, default 100, and `LI_TARGET_GROSS_MARGIN`, default
0.65). Any usage sample with an unpriced component fails the gate.

The server expects:

- `STRIPE_SECRET_KEY`
- `STRIPE_WEBHOOK_SECRET`
- `LI_STRIPE_PRICE_ID`
- `LI_PAID_PLAN_ID`
- `LI_PAID_WEEKLY_UNITS`

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
`li_reserve_usage` before running and settles actual units afterwards. An
explicit stored weekly quota of 0 blocks; only a missing quota falls back to
the plan default. `satellite_passes` is bounded to 168 hours, 20 element sets
and 20,000 characters of TLE text per call, with valid coordinates and
elevation; it and the ad hoc `simulate_candidate`, `analyze_sensitivity`,
`optimize_solution` and `find_prior_art` tools require an active entitlement
with weekly quota remaining (they are gated, not metered).

Weekly usage sums and privacy exports read every row page by page, so they are
not truncated at the PostgREST max-rows setting. Database failures reach
callers only as `database request failed (HTTP <status>)` or `database is
unreachable`, never with table, column or constraint names.

Rate limits: `/mcp` is limited per user in the database
(`li_take_rate_limit`). The endpoints reachable before a user is known —
`/oauth/register` (10/min), `/oauth/authorize` (60/min),
`/oauth/authorize/complete` (20/min), `/oauth/token` (60/min),
`/account/export` and `/account/delete` (20/min together),
`/actions/{id}/details` and `/actions/{id}/approve` (60/min together) — are
limited per client IP and answer `429` with `Retry-After`. These limits are
held in process memory and are therefore **per instance**: with N concurrent
serverless instances the effective limit is up to N times higher and a cold
start resets it. A global limit needs the hosting edge (firewall/WAF) or a
database-backed bucket. Limits can be changed with
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
