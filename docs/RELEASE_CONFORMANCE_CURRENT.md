# Current six-step verification checklist

Checkpoint: 2026-10-11 America/Phoenix. Dated execution evidence, not a signed public-release certificate.

Stable MCP application: **b3204f00ba84d8133743fa1e9398a89225190dad**. All three Python versions, all five dedicated push gates, real PostgreSQL proof and operational CI passed. The stable alias was assigned to this exact READY build. All eight ordinary hosted preflight checks passed on that exact stable revision.

Five agents audited OAuth, trial abuse, workers, public documentation and change review. An alternate-model source review found a numeric verifier crash; its correction preserves direct/derived provenance and passed 1,126 full-suite tests plus 94 focused tests. These overlapping suites are not added together. Earlier offline verification passed 25 checks with zero network attempts. Source refresh has six passing behavioral tests with injected retrievals.

| Requested step | Accomplishment | Remaining proof |
| --- | --- | --- |
| 1. Authentication | OAuth parsing/grants/cache regressions; discovery and negative hosted checks; atomic disconnect candidate | Successful hosted consent, PKCE, refresh, expiry and revocation |
| 2. Actual MCP client | Ordinary HTTP protocol preflight; secure ChatGPT sign-in attempted | ChatGPT acceptance NOT RUN: Google access rejected by automatic approval review |
| 3. Trial review | Bounded admission, fail-closed shared accounting, SSRF, forwarding-header and concurrency regressions | Trial remains disabled; hard wall-time, distributed hosting and real install unproven |
| 4. Worker preparation | Blueprint/schema/offline verification | No running worker; pin prepared configuration to tested API before deployment; paid creation not authorized |
| 5. Recovery and execution | Local crash/reclaim, cancellation, persistence, settlement and deletion regressions | Hosted completion/restart/reopening/settlement NOT RUN |
| 6. Release reconciliation | Versioned status, conformance matrix, reviewed update path and Claude handoff | Hosted acceptance, operational evidence and owner-trusted signing still required |

## Separate OAuth disconnect candidate

[PR #40](https://github.com/EthanLofgrem/lofgren-intelligence/pull/40), b9097d176539ce1d0aca16f74734a17295e914f1, passed combined Python matrix, real PostgreSQL and operational CI. The five dedicated release jobs skip on that separate non-release branch; do not describe them as run. Its new three-RPC migration remains unapplied. Two apply_migration requests returned `Invalid or expired requestState`; subsequent migration history and RPC checks confirmed no change. Keep its API off the stable endpoint until the connector is repaired and the migration is applied/verified.

## Next actions

Repair Supabase mutation request state, apply the tested migration and verify service-role-only grants, then integrate the disconnect candidate and run release gates. Complete secure sign-in and actual MCP acceptance. Continue source-watch review without automatic law conclusions or code installation. Obtain a genuinely available zero-cost host or retain hosted worker acceptance as BLOCKED; no paid service was created.

The paid worker would run LI jobs and save findings/receipts; it does not purchase users or establish public readiness. No live donation destination or committed launch date is verified.

See [Claude master prompt](CLAUDE_PRE_WORKER_MASTER_PROMPT.md), [public status](PUBLIC_STATUS.md), [source watch](SOURCE_WATCH.md) and [disconnect deployment order](https://github.com/EthanLofgrem/lofgren-intelligence/blob/build/oauth-atomic-disconnect/docs/OAUTH_DISCONNECT.md).
