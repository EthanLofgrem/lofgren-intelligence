# Release conformance status

Snapshot: 2026-10-11 America/Phoenix. Application candidate `e232ced165a31ab66124decb1f0fe9ea4b9057ac`.

This document separates observed engineering checks from signed release proof. Root-session deployment observations are reported here; this reviewer did not independently operate the hosted account. All 36 terms remain **UNPROVEN for the signed public gate**: no complete operator-trusted signed bundle was supplied. A reachable API is not hosted-worker or real-client proof.

| Gate term | Implementation | Automated check | Observed evidence / remaining proof | Signed status |
| --- | --- | --- | --- | --- |
| CoreV1Certified | `CLI certify` | `Dedicated exact-head CI / local gates` | Local or CI evidence; no hosted conformance inferred | UNPROVEN |
| V1BoundaryCertified | `scripts/boundary_gate.py` | `Dedicated exact-head CI / local gates` | Local or CI evidence; no hosted conformance inferred | UNPROVEN |
| V2Certified | `scripts/v2_gate.py` | `Dedicated exact-head CI / local gates` | Local or CI evidence; no hosted conformance inferred | UNPROVEN |
| V3Certified | `scripts/v3_gate.py` | `Dedicated exact-head CI / local gates` | Local or CI evidence; no hosted conformance inferred | UNPROVEN |
| V4Certified | `scripts/v4_gate.py` | `Dedicated exact-head CI / local gates` | Local or CI evidence; no hosted conformance inferred | UNPROVEN |
| V5Certified | `scripts/v5_gate.py` | `Dedicated exact-head CI / local gates` | Local or CI evidence; no hosted conformance inferred | UNPROVEN |
| V6Certified | `scripts/v6_gate.py` | `Dedicated exact-head CI / local gates` | Local or CI evidence; no hosted conformance inferred | UNPROVEN |
| FullV1V6RemoteIntegrationCertified | `hosted/journey.py` | `tests/test_public_hosted.py` | NOT RUN: signed-in hosted six-stage case | UNPROVEN |
| CurrentMCPProtocolCertified | `hosted/mcp_sdk.py` | `tests/test_hosted_mcp_sdk.py` | Preflight passes; complete authenticated negotiation NOT RUN | UNPROVEN |
| InstalledWheelMCPCertified | `scripts/installed_mcp_smoke.py` | `installed-wheel smoke` | Installed smoke reported; retain final candidate artifact | UNPROVEN |
| OAuthPKCECertified | `hosted/auth.py` | `tests/test_public_hosted.py` | Discovery passes; consent/exchange/refresh/revoke journey NOT RUN | UNPROVEN |
| DurableRunsCertified | `hosted/store.py` | `tests/pg/test_migrations_pg.py` | PostgreSQL CI; hosted disconnect/reopen NOT RUN | UNPROVEN |
| DurableLifecycleCertified | `hosted/worker.py` | `tests/test_public_hosted.py` | Local recovery tested; hosted worker BLOCKED | UNPROVEN |
| TenantIsolationCertified | `hosted/service.py; store.py` | `tests/pg/test_migrations_pg.py` | Local/PG checks; two hosted principals NOT RUN | UNPROVEN |
| HumanActionApprovalCertified | `hosted/service.py` | `tests/test_public_ops_hardening.py` | External execution disabled; concurrent effect fencing unresolved | UNPROVEN |
| FoundingFreeExactly1000Certified | `hosted/entitlements.py` | `tests/pg/test_migrations_pg.py` | Synthetic slots tested; hosted acceptance NOT RUN | UNPROVEN |
| User1001PaidRequiredCertified | `hosted/entitlements.py` | `tests/test_public_hosted.py` | Synthetic boundary; commercial policy unresolved | UNPROVEN |
| QuotaEnforcementCertified | `hosted/service.py; store.py` | `tests/test_public_hosted.py` | Local/PG settlement; hosted recovery NOT RUN | UNPROVEN |
| RateLimitCertified | `hosted/ratelimit.py` | `tests/test_public_hardening.py` | Hosted parallel/global abuse proof NOT RUN | UNPROVEN |
| SSRFSafe | `hosted/security.py` | `tests/test_public_hardening.py` | Regression tests; final hosted source-path evidence needed | UNPROVEN |
| InputLimitsCertified | `hosted/web_app.py` | `tests/test_public_hardening.py` | Input regressions; final deployed negative evidence needed | UNPROVEN |
| ActualCOGSMeteringCertified | `hosted/costing.py` | `tests/test_public_hosted.py` | Real provider reconciled costs NOT RUN | UNPROVEN |
| PaidPlanEconomicsCertified | `hosted/economics.py` | `scripts/v6_gate.py` | Approved pricing and real cost samples BLOCKED | UNPROVEN |
| StripeSandboxJourneyCertified | `hosted/stripe.py` | `tests/test_public_hosted.py` | Real sandbox lifecycle NOT RUN; live billing disabled | UNPROVEN |
| WebhookSecurityCertified | `hosted/stripe.py` | `tests/test_public_hardening.py` | Local signatures; sandbox delivery/replay NOT RUN | UNPROVEN |
| DatabaseMigrationCertified | `supabase/migrations` | `tests/pg/test_migrations_pg.py` | Real PostgreSQL CI; verify every final staging migration | UNPROVEN |
| MigrationLedgerAligned | `supabase/migrations` | `tests/pg/test_migrations_pg.py` | Atomic migration reported applied; final history reconciliation needed | UNPROVEN |
| RemoteClientInteropCertified | `hosted/mcp_sdk.py` | `scripts/li_mcp_preflight.py` | Real ChatGPT/Claude client NOT RUN | UNPROVEN |
| HealthReadinessCertified | `hosted/web_app.py` | `scripts/li_mcp_preflight.py` | PASS observed: e232 readiness and eight preflight checks | UNPROVEN |
| ObservabilityCertified | `hosted/service.py; worker.py` | `tests/test_public_ops_hardening.py` | Hosted alert/diagnostic acceptance NOT RUN | UNPROVEN |
| BackupRestoreCertified | `hosted/certification.py requirement` | `No demonstrated restore artifact` | NOT RUN: isolated restore and result retrieval | UNPROVEN |
| RollbackCertified | `hosted/certification.py requirement` | `No demonstrated rollback artifact` | NOT RUN: deployed API/worker/schema compatible rollback | UNPROVEN |
| SecretsBoundaryCertified | `hosted/security.py` | `tests/test_public_hardening.py` | Secret privately saved; final log/export boundary checks needed | UNPROVEN |
| ExactHeadCIPassing | `.github/workflows` | `Exact e232 CI` | PASS observed: tests 38083433452, PG 38083433453, operations 38083433497 | UNPROVEN |
| ExactSHAPinned | `dependency lock and deployment git source` | `Clean checkout / wheel checks` | API pinned e232; no running worker identity | UNPROVEN |
| DeployedSHAMatches | `health/readiness release identity` | `scripts/li_mcp_preflight.py` | PASS API e232; worker NOT RUN | UNPROVEN |

## Integration recommendation

Review PR #38 on `712339a129b913d675ab0e0a719a0e1eba018a69`; retain freshness enforcement and merge only after its regressions pass on the combined candidate. The review fix rechecks artifact bytes against the manifest digest during freshness evaluation so changed files cannot reuse prior signature authentication.

Do not waive the commercial gate terms merely because an initial release is free. A different free-only release scope requires an explicit reviewed policy and evidence requirements; never silently mark existing terms true.

Next operational proof: authenticated OAuth journey, hosted worker case/receipt/settlement and recovery, ordinary client compatibility, backup/restore and rollback. Preserve external execution disabled until concurrent effect fencing is proven.
