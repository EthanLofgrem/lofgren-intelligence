# Public MCP service

This document describes the hosted/public distribution layer for the certified
Lofgren Intelligence V1 engine.

## Truthful capability boundary

Public operation exposes only capabilities that are implemented and certified.
On this line the hosted service exposes V1 Evidence Intelligence and V2
Discovery Intelligence (hosted discovery results report contract
`lofgren.mcp/2`). V3 Production Intelligence is certified for the local
CLI/stdio server (`lofgren.mcp/3`: `build_artifact`, `verify_artifact`,
`get_artifact_file`, `get_production_receipt`, `create_v4_handoff`) but is not
yet exposed by the hosted service. V4–V6 are not public capability claims
here. See `docs/PUBLIC_READINESS_AUDIT.md`.

## Identity and OAuth

Remote MCP uses OAuth 2.1-style Authorization Code + PKCE (S256). MCP clients
may dynamically register as public clients. End users authenticate through
Supabase Auth; the server validates the Supabase session before issuing a
one-time authorization code. MCP access/refresh tokens are opaque and only
hashes are persisted.

The authorization page names the requesting client and the host its redirect
URI points to. A code is issued only after the user clicks to sign in or to
approve; an existing browser session is never used to authorize silently.
Because client registration is open, client names are self-declared, so the
redirect host is the part users should check. Registered redirect URIs must be
https, or plain http to exactly `localhost`, `127.0.0.1` or `[::1]` (lookalike
hosts such as `localhost.example.com` are refused).

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
its current status (`active`/`trialing` grant access, anything else does not).
The webhook handler therefore also needs `STRIPE_SECRET_KEY`; if Stripe cannot
be read the event is refused and Stripe retries it.

The server expects:

- `STRIPE_SECRET_KEY`
- `STRIPE_WEBHOOK_SECRET`
- `LI_STRIPE_PRICE_ID`
- `LI_PAID_PLAN_ID`
- `LI_PAID_WEEKLY_UNITS`

The economic gate that unlocks checkout also needs `LI_PAID_MONTHLY_USD`,
`LI_PAYMENT_FEE_PERCENT`, `LI_PAYMENT_FEE_FIXED_USD` (optional
`LI_ECON_MIN_SAMPLES`, default 100, and `LI_TARGET_GROSS_MARGIN`, default
0.65). Any usage sample with an unpriced component fails the gate.

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
link-local, shared/CGNAT, multicast, reserved and IPv4-in-IPv6 forms of those
are refused), pins the checked address for the connection and re-checks
redirects. `satellite_passes` is bounded to 168 hours and 20 element sets per
call; it and the ad hoc simulation, sensitivity, optimization and prior-art
tools require an active entitlement with weekly quota remaining. The public V1 service never
authorizes external actions; it runs research/verification only.

Database tables use RLS with no anonymous/authenticated policies. The hosted
service uses the service-role key; browser clients do not receive it.

## Public-readiness principle

A passing build or successful deployment is not enough. Public readiness
requires exact-SHA CI, hosted-service tests, database migration verification,
OAuth interoperability, tenant isolation, quota/abuse checks, Stripe sandbox
certification for paid-required accounts, health/readiness, rollback evidence
and a staging journey.
