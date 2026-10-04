# Public MCP service

This document describes the hosted/public distribution layer for the certified
Lofgren Intelligence V1 engine.

## Truthful capability boundary

Public operation exposes only capabilities that are implemented and certified.
At the branch baseline used for this build, V1 Evidence Intelligence and the
V1→V2 boundary are certified. V2 is partial; V3–V6 are not public capability
claims.

## Identity and OAuth

Remote MCP uses OAuth 2.1-style Authorization Code + PKCE (S256). MCP clients
may dynamically register as public clients. End users authenticate through
Supabase Auth; the server validates the Supabase session before issuing a
one-time authorization code. MCP access/refresh tokens are opaque and only
hashes are persisted.

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
blocks obvious SSRF destinations (private, loopback, link-local, multicast and
reserved networks) and re-checks redirects. The public V1 service never
authorizes external actions; it runs research/verification only.

Database tables use RLS with no anonymous/authenticated policies. The hosted
service uses the service-role key; browser clients do not receive it.

## Public-readiness principle

A passing build or successful deployment is not enough. Public readiness
requires exact-SHA CI, hosted-service tests, database migration verification,
OAuth interoperability, tenant isolation, quota/abuse checks, Stripe sandbox
certification for paid-required accounts, health/readiness, rollback evidence
and a staging journey.
