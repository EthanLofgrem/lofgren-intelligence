# Public privacy and data handling

This document is the engineering data-handling contract for the hosted Lofgren
Intelligence MCP service. It is not a substitute for jurisdiction-specific
legal review before a broad commercial launch.

## Data the hosted service stores

- account identity from Supabase Auth (user id and email);
- OAuth client registrations plus hashed authorization/access/refresh tokens;
- research objectives, user-supplied inline evidence, findings, knowledge maps,
  provenance, reports and research receipts;
- intelligence-unit usage and actual-cost measurements;
- Stripe customer/subscription identifiers and webhook receipts for paid users;
- content-free request telemetry (method, path, status, duration, correlation id).

Raw MCP bearer tokens, Supabase service-role credentials, model-provider keys,
Stripe secret keys and webhook secrets are not stored in research records.

## Why it is stored

The service uses this data to authenticate a caller, isolate tenants, perform
requested research, preserve reproducible evidence/receipts, enforce quotas,
measure cost, support billing and diagnose service failures.

Research data is not used as evidence for another customer. Tenant identity is
part of durable run identity at the storage boundary even when two users
produce the same deterministic research id.

## External processors / providers

A request may send the minimum required material to services the user chooses
or the deployment config enables, including:

- Supabase for identity and durable storage;
- the deployment/hosting provider;
- Stripe for paid account billing;
- a configured model provider;
- a configured search provider;
- public websites or open-data services explicitly used by the research job.

Provider usage must stay within the source/provider's terms and the LI evidence
receipt should identify what was used.

## User controls

The hosted account page exposes:

- authenticated export of account, entitlement, runs and usage records;
- permanent account deletion behind an exact confirmation phrase.

Deletion first closes the account to new work: every OAuth access token is
revoked, refresh tokens and unused authorization codes are consumed, and the
entitlement is deactivated so no new usage can be reserved. Queued research
jobs are cancelled and running ones are asked to stop at their next stage;
usage awaiting reconciliation is settled, and an open reservation that can no
longer belong to live work is released. While a job or a reservation has not
settled, nothing is deleted: the request is refused with
`account_deletion_pending` and a retry completes it (the account stays closed
in the meantime).

For a paid account, deletion then attempts to cancel its Stripe subscription.
If cancellation fails, deletion stops rather than deleting the account while
leaving a chargeable subscription behind. Deleting the Supabase Auth user then
cascades LI account, entitlement, token, run, discovery, artifact, action,
outcome, improvement, case, research-job, reservation and usage records through
foreign keys, and the deletion is verified. Retrying a deletion that already
completed is safe.

After deletion, a Stripe event that ends a subscription of the deleted account
is receipted without effect. An event that would grant paid access to an
account that no longer exists is refused and left for an operator: whether
such a subscription is cancelled or refunded is not decided by LI.

## Retention

Research/account data is retained while the account exists so receipts and
project history remain reproducible. Account deletion removes LI-hosted
account/run/token/usage records through the database cascade. Records that are
not tied to the user are kept: Stripe webhook receipts (event id, type and
payload hash only), the Founding Free activation counter (activation numbers
are never reused), registered OAuth clients and hashed, time-pruned rate-limit
keys. Provider-side
records (for example Stripe financial records) may have independent legal or
operational retention requirements.

Backups must follow the same access controls and retention policy. A public
release is not certified until backup/restore and deletion behavior are tested
against the chosen production storage configuration.

## Security posture

- service-role/database and Stripe secrets are server-side only;
- public tables have RLS enabled and no direct anon/authenticated grants;
- OAuth uses Authorization Code + PKCE with opaque hashed tokens;
- MCP access tokens are resource-bound;
- remote source reads block local/private address space and pin a validated DNS
  result for each outbound connection;
- request/input sizes, quotas and rate limits fail closed;
- current V1 public research never receives authority to execute external
  actions.

Security issues should be handled through the repository's SECURITY.md process.
