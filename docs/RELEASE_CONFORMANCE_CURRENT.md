# Current six-step verification checklist

Checkpoint: 2026-10-13 UTC (2026-10-12 America/Phoenix). Dated execution evidence, not a signed public-release certificate.

Candidate: remote **0074472d896fab72af029fa6c2cf449ca0c2ae77**, local equivalent **410f16c1f6aab707bf9136d9c69e913d1c6402a4**, tree **a4694aa75d03482c499f9051e1c10c81f82cdb4a**, [draft PR #45](https://github.com/EthanLofgrem/lofgren-intelligence/pull/45). Later commits require fresh applicable checks; these results do not certify a moving branch.

Frozen-source evidence: 1,163 full-suite tests passed; 78 focused checks and 25 offline contracts passed. Counts overlap. Real PostgreSQL proof passed 68 tests with no skips. All eight test CI jobs, four operations jobs and PostgreSQL proof passed on the remote candidate. Dedicated lifecycle gates executed. A prior test process that ran while source changed is excluded from final evidence.

The rebuilt Preview `dpl_47e7Vny1vTghKZhAF39wXbjTFsR7` reports this exact candidate at `/readyz`; health and readiness returned 200. Its trial returned 404 with no-store. Branch-specific Preview flags are `LI_TRIAL_ISOLATION_MODE=required`, `LI_TRIAL_ISOLATION_VERIFIED=false`, `LI_TRIAL_ENABLED=false`. Stable MCP remains **b3204f00ba84d8133743fa1e9398a89225190dad**. No public promotion occurred.

| Requested step | Verified scope | Remaining proof |
| --- | --- | --- |
| 1. Authentication | Atomic disconnect/replay and expiry-after-lock-wait fixes; 15 staging migrations; restricted service-role-only RPC grants | Hosted consent, PKCE, refresh, expiry, revocation and separate-user isolation |
| 2. Actual MCP client | Local stdio subprocess and SDK/HTTP harness checks | Ordinary independently authorized AI-client initialize, tool listing and safe first use; product sessions UNKNOWN/NOT RUN |
| 3. Trial review | Published schema bounds, shared accounting, SSRF, fixed child watchdog and credential allowlist tested locally | Exact Vercel launch, timeout, kill/reap proof; controlled Preview activation; distributed and real-client acceptance |
| 4. Worker preparation | Exact-target claim, frozen inline-source boundary and receipt/accounting verification; PostgreSQL targeted-claim tests | Exactly one approved synthetic hosted target; persisted result and unrelated-job invariance |
| 5. Recovery and execution | Local cancellation, crash/reclaim, persistence and settlement regressions | Hosted interruption/reopening/recovery and settlement evidence; external execution remains disabled |
| 6. Release reconciliation | Exact source/tree/CI identity, migration byte matching, truthful no-account public intent | All applicable hosted gates, owner-trusted signing, approved release scope and exact-source promotion |

## Resolved historical blockers

Fresh authorized Supabase migration requests succeeded. Disconnect, revocation replay, issuance expiry and targeted worker claim are now installed in staging. Earlier expired request-state failures are historical observations, not a current claim that these migrations are absent.

Vercel environment API access still returned 403; the authorized dashboard path successfully configured the isolated Preview. This resolves configuration, not API permission. No Google 502 root cause or completed ordinary-client sign-in is established.

## Release criteria

Public research must require no LI login, signup or payment. Optional GitHub identity, selected repository access and AI-client authorization are distinct concerns. Owner credentials must never become a shared user identity.

If GitHub onboarding is advertised, a separate authorized test user must have completed the full connection flow and successfully used LI, with repository-access boundaries and revocation verified.

Do not advertise an unverified connection destination. Do not infer universal model compatibility from MCP support or an SDK harness. See [client evidence](CLIENT_COMPATIBILITY.md), [issue register](REMAINING_ACCEPTANCE_REGISTER.md), [public status](PUBLIC_STATUS.md) and [Claude master prompt](CLAUDE_PRE_WORKER_MASTER_PROMPT.md).
