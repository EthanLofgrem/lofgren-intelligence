# Public release status

Checkpoint: 2026-10-11 America/Phoenix. This page is a dated evidence report, not a live dashboard.

**Pre-public hosted verification; no committed launch date.** The local developer package is available. The dedicated authenticated MCP endpoint is reachable. Full customer delivery remains unverified.

## Current code and deployment

Stable MCP application: **b3204f00ba84d8133743fa1e9398a89225190dad** at https://lofgren-intelligence-mcp.vercel.app/mcp. Its Vercel build is READY; all eight ordinary preflight checks passed. All three Python matrix jobs, all five dedicated lifecycle gates, real PostgreSQL proof and operational CI passed on this exact commit. Anonymous MCP calls correctly receive LI authorization errors.

The hardening work is in [PR #39](https://github.com/EthanLofgrem/lofgren-intelligence/pull/39). Default-branch source and historical releases are different baselines; use an explicit revision when reproducing results.

## What changed

- OAuth request/grant validation and response cache prevention.
- Bounded anonymous trial admission, shared fail-closed accounting and SSRF regressions; trial remains disabled.
- Worker crash prevention for invalid wait settings and tested recovery/settlement behavior.
- Numeric evidence verification now retains derived-link provenance rather than propagating approximate support transitively. A six-order, repeated reload/reverification regression passes. Historical graphs without provenance markers require rebuilding from original evidence.
- [Explicit source-watch refresh](SOURCE_WATCH.md), offline by default, comparing bounded public-source hashes and flagging changes for review. It does not automatically install code or decide that a law changed.

Latest verifier-source full suite: 1,126 tests passed. Offline checks: 25 passed with zero network attempts. These are separate overlapping checks, not a summed public-delivery score.

## Remaining release requirements

| Area | Status | Required proof |
| --- | --- | --- |
| Endpoint reachability/discovery | PASS on b3204f | Retest after any deployment |
| Hosted authenticated OAuth | PARTIAL | Actual consent, PKCE exchange, refresh, expiry and revocation |
| Ordinary ChatGPT/Claude client | NOT RUN | Successful supported client connection and result/receipt retrieval |
| Always-on hosted worker | NOT RUN | Authorized job completion, persistence, restart recovery and settlement |
| Anonymous trial | DISABLED | Hard wall-time/distributed runtime, cost/privacy and real installation acceptance |
| External execution | DISABLED | Durable effect fencing before enablement |
| OAuth disconnect | Separate tested candidate | PR #40 passed matrix/PG/ops; new staging migration remains unapplied due connector request-state errors |
| Full public release | NOT SATISFIED | Required hosted/operational evidence, trusted signing and owner-approved scope |

A secure ChatGPT sign-in choice returned Google, but automatic approval review blocked provider navigation. No successful sign-in or actual client acceptance is claimed. Two Supabase migration requests returned Invalid or expired requestState; follow-up history/RPC checks confirmed no change. No paid host, live billing or external effects were enabled.

## Learn, support and updates

See [six-step conformance](RELEASE_CONFORMANCE_CURRENT.md), [Claude master prompt](CLAUDE_PRE_WORKER_MASTER_PROMPT.md), [architecture](ARCHITECTURE.md), [evidence protocol](EVIDENCE_PROTOCOL.md) and [roadmap](ROADMAP.md). Report sanitized reproducible defects through [GitHub issues](https://github.com/EthanLofgrem/lofgren-intelligence/issues).

There is no verified donation destination or committed launch date. A paid worker would run LI research jobs and preserve findings/receipts; it does not buy users or by itself establish public readiness. Current construction authorization is zero cost.

Legal changes require jurisdiction, effective dates, authoritative sources and reviewed applicability. Source byte changes are review signals, not proof of factual or legal conclusions. Continuous hosted monitoring remains unimplemented; updates do not silently rewrite findings, policy, authority or production code.
