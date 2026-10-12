# Public release status

Documentation checkpoint: **2026-10-11**. This is a dated handoff, not a live health dashboard. Recheck the deployed revision and evidence before making release decisions.

## Release classification

**Pre-public hosted verification; no committed launch date.** The local developer package is available. Neither a green unit suite nor reachable OAuth metadata establishes full customer delivery. Public scope must follow demonstrated behavior and Ethan Lofgren's release approval.

Application candidate at this checkpoint: `e232ced165a31ab66124decb1f0fe9ea4b9057ac` (PR #37). The integrated base is `33a318897380bb40bf92efb0302df0db6e59c2aa`. Repository default-branch history must not be confused with the deployed candidate.

## Evidence and remaining work

| Area | Checkpoint status | Evidence or next requirement |
| --- | --- | --- |
| Candidate CI | Reported PASS at exact candidate | Tests `38083433452`, PostgreSQL proof `38083433453`, operational quality `38083433497`; five dedicated V2–V6 jobs ran |
| Dedicated MCP access boundary | Observed PASS in Codex verification | Health, discovery, readiness and unauthorized-request checks; eight preflight checks passed after private server-key configuration |
| Durable jobs and atomic result persistence | Implemented, locally/CI tested | Supabase migration `20261010200818_li_atomic_job_result.sql`; hosted recovery still needs proof |
| Hosted worker | NOT RUN | Separate worker prepared; no verified running hosted worker or completed hosted customer case |
| Ordinary client journey | NOT RUN | Real consent, token exchange, tool discovery, approved execution and result/receipt retrieval |
| Hosted recovery and settlement | NOT RUN | Termination/reclaim, replay, concurrency, cancellation, provider failure and accounting |
| Anonymous no-login trial | Integrated into hardening source; disabled and unverified | Separate restricted endpoint; abuse, shared budgets, runtime, retention and actual client installation remain release requirements |
| External actions | DISABLED | Concurrent duplicate-effect risk is contained; durable execution fencing is required before enablement |
| Billing and donations | NOT VERIFIED | No live billing or verified donation destination advertised |
| Full public release gate | NOT SATISFIED | Required exact-SHA operational evidence and signing must be reconciled with remaining checks |

The CI and deployed checks above are retained checkpoint reports, not independent new tests performed while writing this document. A later fix or deployment must have its own identity and evidence.

## Source work since the deployed checkpoint

The local hardening branch at `db8f79b` contains OAuth grant/scope and error-cache fixes, finite cost-ceiling validation, worker deployment verification, release-evidence freshness review, an offline evidence-change comparison helper and restricted anonymous-trial integration. This source checkpoint is **not a new deployed acceptance proof**. The latest revision and tests must be rechecked before deployment; the trial remains disabled.

Current owner instruction is zero cost. No paid worker, provider spending or live billing should be started. This does not establish that hosted-worker acceptance is complete.

## What public readiness must demonstrate

1. One authenticated, persisted case with scope and budget approval completes on a hosted worker, returns findings and receipts, settles usage and survives reopening.
2. One ordinary supported MCP client completes authorization and retrieves real LI results without a hosting bypass credential.
3. Hosted failures and prohibited access are tested on the same candidate; unsuccessful work never appears complete.
4. Required release evidence, dependency and migration identities, backup/restore, rollback and operating controls meet the reviewed release scope.
5. The founder approves the supported scope. Untested clients and disabled capabilities remain labelled as such.

The initial offering may be free for users while hosting has operating costs. A worker is infrastructure, not an additional AI model, guaranteed customer acquisition or a substitute for source/provider configuration.

## Capabilities without overclaiming

LI preserves claims, evidence, assumptions, contradictions and receipts; supports discovery and bounded artifact generation; and implements governed outcome and improvement structures. It does not guarantee factual accuracy, legal compliance, medical suitability, unlimited internet coverage, autonomous model routing or automatic production self-upgrades.

The local server and hosted registry have different tool sets. Refer to [CAPABILITIES.json](CAPABILITIES.json) and [MCP selection and verification](MCP_SELECTION_AND_VERIFICATION.md), rather than treating tool counts as delivery evidence.

## Support and changes

Watch repository releases and [CHANGELOG.md](../CHANGELOG.md). Report sanitized defects in [GitHub issues](https://github.com/EthanLofgrem/lofgren-intelligence/issues). No verified donation URL is available at this checkpoint. A future support destination must be owner-approved and visibly linked from this repository.

New evidence should lead to a versioned reassessment. New laws or rules require jurisdiction, effective dates and authoritative interpretation; no worldwide automatic legal-update coverage is promised.

## Independent recheck — 2026-10-12 UTC

The dedicated endpoint reports application `b28cdb8a6c412d2c976e7eb9a4b97ad6c345ab4e`. All eight ordinary HTTP preflight checks passed in this recheck. This supersedes the older deployed checkpoint above, but does not establish signed-in OAuth or real-client acceptance. Five independent agents audited OAuth, trial abuse, worker recovery, public documentation and evidence-change review. New source fixes prevent authorization-code response caching, reject unsafe worker waits and flag every prior claim when receipt scope changes. Trial deadlines remain stage checks rather than hard network interruption; trial activation stays disabled pending hosted proof. No paid hosting was started.
