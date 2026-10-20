# Public MCP operating and release runbook

A successful build is not public readiness. This runbook defines the evidence
required before the Lofgren Intelligence MCP service can be opened to users.

## Release identity

Every release candidate has one immutable Git SHA. CI, database migration,
staging deployment, remote MCP client tests and the promoted production
deployment must all name that exact SHA. A deployment that cannot report or be
mapped to the tested SHA is not releasable.

## Required gate

Run:

```bash
python scripts/public_mcp_gate.py \
  --evidence public-mcp-evidence.json \
  --trust-policy operator-trust-policy.json
```

Unknown is false. Public launch requires `PublicMCPReady = TRUE`.

The code gate covers the certified core, current MCP protocol, OAuth/PKCE,
durability, tenant isolation, exact first-1000 Founding Free allocation,
account 1001 paid requirement, quota/rate/input/SSRF controls, actual COGS,
paid-plan economics, Stripe sandbox lifecycle, database migration, real remote
client interoperability, health/readiness, observability, backup/restore,
rollback, secret boundaries and exact/deployed SHA binding.

## Deployment sequence

1. Pin the reviewed release SHA.
2. Run all Python matrix tests and V1/V1→V2 gates on that SHA.
3. Apply the LI migration to the dedicated LI staging database.
4. Configure staging-only secrets; never copy secrets into Git.
5. Deploy the same SHA to protected staging.
6. Run OAuth + remote MCP journeys from real clients.
7. Prove user 1 and user 1000 receive Founding Free and user 1001 is
   `paid_required` using an isolated staging counter/database.
8. Run Stripe test-mode checkout → signed webhook → entitlement → failed
   payment → access refusal → subscription recovery → cancellation/portal.
9. Run tenant-isolation negatives for runs, receipts, exports and tokens.
10. Run backup → destructive staging change → restore → receipt verification.
11. Capture deployment SHA, health/readiness and rollback target.
12. Evaluate the full public gate.
13. Only after an owner release decision, promote the already-tested artifact.

Before requesting that owner decision, assemble the candidate-bound activation
and rollback handoff and validate it:

```bash
python scripts/validate_activation_package.py \
  --package activation-package.json \
  --output activation-package-check.json
```

The package references, by SHA-256 digest, real receipts for exact-head CI,
migration-ledger alignment, health/readiness, observability, disposable
backup/restore, rollback rehearsal, ordinary remote clients, tenant isolation
and the hosted V1-V6 journey. It also pins the staging origin, candidate SHA,
distinct rollback SHA, forward migrations and compatibility statement. The
validator never signs evidence, never authorizes release and always reports
`PublicMCPReady: false`; it only determines whether the handoff is complete
enough to request the owner's activation decision.

Each receipt must identify its slot with `proof_kind` (for example
`backup_restore`), `candidate_sha`, strict boolean `passed: true` and a
nonempty `result_id`. These fields are a handoff format check, not verifier
authentication. A self-authored receipt is not independent proof; the signed
36-term gate with the operator's trust policy remains required. Migration
versions must be unique even when filenames differ.

## Rollback

Keep the last known-good deployment id/SHA and the migration compatibility
statement with every release. Application rollback must not silently roll the
database backward. If a database migration is not backward compatible, deploy
a forward repair migration; never edit an already-applied migration.

Billing is fail-closed. During an incident, disable new checkout with
`LI_BILLING_ENABLED=false` without deleting existing Stripe state.

## Incident priorities

1. Cross-tenant access or leaked secret: block traffic/affected capability,
   rotate credentials, preserve logs, investigate before reopening.
2. Incorrect billing/entitlement: disable checkout, preserve webhook receipts,
   reconcile Stripe against LI entitlement state.
3. Evidence-integrity failure: stop affected research capability; do not
   downgrade a verification failure to a normal result.
4. Provider outage: surface a gap/unavailable provider rather than inventing
   evidence.
5. Capacity issue: return bounded/rate-limited failure; do not bypass quotas.

## Observability

Public HTTP responses carry a correlation id. Runtime logs record method, path,
status and duration without research contents. Durable research receipts and
usage events provide job-level audit evidence. Production alert thresholds and
log-retention policy must be configured on the selected host before
ObservabilityCertified may become true.

Server faults are never reported as client errors and never name internals.
Missing configuration answers `503` `server_error`; an unreachable or failing
database, identity provider or Stripe API answers `503`
`temporarily_unavailable`. Both carry `Retry-After`, `Cache-Control: no-store`
and one generic description. The exact cause (for example which variable is
missing) is written to the server log only. `/readyz` lists generic missing
components (`database`, `authorization_provider`, `public_base_url`,
`release_sha`, `rate_limit_key`) and logs the variable names
(`rate_limit_key`: `LI_RATE_LIMIT_HMAC_KEY` is missing or shorter than 32
characters; without it registration, consent completion and activation fail
closed, see `docs/PUBLIC_MCP.md`). No value is ever reported. A bearer token on a route that does
not use it is ignored; an unknown token on `/mcp` is `401 invalid_token` with a
`resource_metadata` challenge. `/mcp` and every `/oauth/*` response is
`no-store` (token and revocation responses add `Pragma: no-cache`); OAuth metadata documents
are `public, max-age=300`.

CORS allows the public base URL plus the documented browser MCP client
origins `https://chatgpt.com`, `https://chat.openai.com` and
`https://claude.ai`. `LI_MCP_CLIENT_ORIGINS` (comma-separated; empty for none)
replaces that client list and `LI_ALLOWED_ORIGINS` adds origins. `*` is never
accepted, credentials are never allowed, and unknown origins are refused.

## Backup / restore

Use the database provider's supported backup mechanism. Certification requires
an actual staging restore into a disposable database followed by:

- account/entitlement count reconciliation;
- tenant isolation tests;
- receipt integrity verification on restored runs;
- Founding Free activation counter reconciliation;
- OAuth/billing secret material remains external to the database backup.

## Secrets

Required hosted secrets belong in the deployment provider's encrypted
environment configuration. Service-role and Stripe secret values must never be
exposed to browser code. The Supabase publishable key is intentionally public;
its authority is bounded by Supabase Auth/RLS.

Rotate a secret when exposure is suspected and re-run the affected gate.
