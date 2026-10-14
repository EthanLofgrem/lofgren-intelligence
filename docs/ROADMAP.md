# Roadmap and construction boundaries

This roadmap describes engineering work, not a release certification. See
[public status](PUBLIC_STATUS.md) for dated deployed evidence and
[release conformance](RELEASE_CONFORMANCE_STATUS.md) for unsatisfied terms.
No public launch date is committed.

## Implemented layers

| Layer | Available implementation | Next delivery boundary |
| --- | --- | --- |
| V1 — Research and Verification | Scoped objectives, evidence, claims, contradictions, unknowns, reports and receipts | Complete authenticated hosted research and retrieve durable results |
| V2 — Discovery | Problem frames, prior-art assessment, hypotheses, candidates and supported simulation/optimization | Preserve evidence applicability and distinguish predictions from observations |
| V3 — Production | Supported specifications, deterministic artifacts and verification | Demonstrate supported artifacts through the actual client journey |
| V4 — Authorized Execution | Action proposals, authority checks and approval structures | External execution remains disabled until durable duplicate-effect fencing is proven |
| V5 — Outcome Measurement | Observation records and comparisons with expectations | Demonstrate honest observations; do not infer causation from comparison |
| V6 — Improvement Evaluation | Bounded improvement candidates and review proposals | No autonomous model router or self-promoting production upgrade is claimed |

Not every objective needs every layer. A research-only request should not
fabricate execution or outcome data to fill a six-stage diagram. Local stdio
and hosted registries expose different tool sets; consult the actual manifest.

## Current release priorities

1. Integrate reviewed hardening into one candidate and run exact-head gates,
   including installed-wheel and real PostgreSQL checks.
2. Deploy the OAuth fixes and repeat ordinary requests. Complete consent,
   PKCE, refresh, revocation and an actual supported MCP client connection.
3. Review the restricted anonymous trial: shared atomic budgets, runtime,
   concurrency, source limits, privacy and failure behavior. Keep it disabled
   until the supported scope is approved and demonstrated.
4. Complete hosted-worker acceptance, recovery, cancellation, settlement and
   result reopening. A local worker is useful testing but not hosted proof.
5. Reconcile the release evidence, restore/rollback tests and owner signing.

Current authorization is **zero cost**: no paid worker, paid provider usage or
live billing. Infrastructure preparation can continue without creation. A
running hosted worker still needs an available, approved operating environment.

## Evidence and software updates

The [change-aware update guide](CHANGE_AWARE_UPDATES.md) includes an offline
receipt comparison helper and a governed reassessment procedure. It does not
continuously monitor sources, interpret worldwide laws or deploy new code.

Scheduled monitoring requires durable tenant-owned monitor records, source
versions, bounded cadence, budget admission, cancellation, retention and
failure visibility. Legal updates also require authoritative sources,
jurisdiction and effective dates. These are future delivery requirements,
not currently advertised coverage.

## Contribution discipline

Use a scoped branch and pull request, preserve other contributors' work, and
retain regression evidence for fixed defects. Source changes require a new
candidate identity. Production updates require tested deployment identities;
old CI results must not certify new source. Do not weaken authorization,
assertions or release terms to obtain a green badge.
