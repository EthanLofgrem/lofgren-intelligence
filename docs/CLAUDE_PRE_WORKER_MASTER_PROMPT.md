# Claude Code master prompt — complete LI acceptance at zero cost

## Mission and authority

Ethan Lofgren is founder and release authority. Lofgren Intelligence is the
release priority. Implement, independently test and fix executable defects;
produce inspectable evidence for the supported release scope.

Current authorization is **ZERO COST**. Do not create paid infrastructure,
start paid model/provider calls, activate live billing or expand public trial
spending. No running hosted worker is verified. Preserve disabled external
execution and disabled anonymous trial until their specific gates pass.

Preserve Research and Verification, Discovery, Production, Authorized
Execution, Outcome Measurement and Improvement Evaluation. These are available
layers, not mandatory stages: never fabricate actions, outcomes or improvement
data merely to fill the loop.

## Current state — verify identities before work

Repository: https://github.com/EthanLofgrem/lofgren-intelligence
Integration branch: build/li-release-hardening-oct11.
Public source checkpoint: `9583917`, including source-watch and conformance work.
Fetch current refs, inspect AGENTS.md and working-tree changes, and review all
commits after that checkpoint. Coordinate with Codex; use isolated worktrees
and preserve other agents' changes.

Stable authenticated MCP endpoint:
https://lofgren-intelligence-mcp.vercel.app/mcp

Deployed application checkpoint: `6f53287`.
- All three Python matrix jobs and all five dedicated push gates passed.
- Real PostgreSQL proof and operational CI passed.
- Clean installed-wheel checks passed.
- All eight ordinary hosted preflight checks passed on the stable alias.
- The private backend key is configured; it is no longer a missing-key blocker.

Local checkpoint: 1,119 tests passed, 25 offline checks passed with zero network
attempts, and V1–V6 source certifications passed. These are distinct reported
runs; do not add them into a unique total. New source-watch work has six tests.
Newer source still needs its own exact-head CI and deployment acceptance.

An atomic OAuth disconnect fix is separate work in progress. Its migration
and hosted behavior are NOT deployed-proven. Inspect its actual branch,
patch, database grants and regressions before integrating or applying anything.

ChatGPT client acceptance is NOT proven. The secure sign-in choice returned
Google, but automatic approval review blocked navigation to that provider.
Do not bypass that review or retry the same blocked route. Use an available
supported secure sign-in alternative or report the exact required owner action.
Independent same-model subagents are reviewers, not external Claude/ChatGPT
product connection proof.

## Priority 1 — finish authentication and a real client connection

Review the pending disconnect work for atomic revocation, races, failure
behavior, tenant ownership and grants. Test revoke-versus-refresh, stale grants,
replay, partial failure and reconnect behavior. Do not treat the UI's success
message as evidence that tokens were revoked.

On the final deployed candidate, observe the actual supported consent and S256
PKCE flow, one-time code use, token resource/scope validation, refresh rotation,
replay rejection, expiry and revocation. Use an authorized staging identity;
never manufacture authority or bypass consent. Keep tokens out of logs.

Complete one ordinary supported MCP client connection: initialize, tools/list,
request assessment and an authorized non-worker operation. Record the client
and protocol versions, connection steps, response evidence and limits. A Python
harness, health response or proper anonymous 401 does not establish ChatGPT
installation. No Vercel administrative bypass credential belongs in a client.

## Priority 2 — trial safety and truthful source updates

Inspect integrated trial implementation and all subsequent fixes rather than
repeating old review notes as current defects. Keep private tools, cases,
account, billing, actions and persistent customer state inaccessible.

Prove shared atomic budget admission, per-client abuse controls, bounded
concurrency and actual runtime/source/output limits. Test storage failure,
malformed responses, forged proxy headers, rotating sessions, parallel calls,
replay, provider failure and disconnect. IP and MCP session IDs are not identity.
Document actual global capacity and what happens when the ceiling is exhausted.
Do not enable or increase trial spending under the zero-cost instruction.

Review the source-watch implementation and its six tests. Verify source
fingerprints, changed/unchanged/unavailable classifications, review dependencies,
retention and privacy. A refresh command is not continuous scheduling or legal
interpretation. Laws require jurisdiction, effective date and authoritative
sources. Changed web content does not prove a changed binding rule.

No silent production self-upgrades, policy widening or source-triggered
external effects. New code/rules require reviewed versioned releases. Preserve
historical receipts and findings; reassessment produces a linked new version.

## Priority 3 — worker preparation and recovery without charges

Inspect the existing Render worker configuration and offline verifier. Preserve
pinned checkout, locked dependencies, exact build/start commands and honest
SIGTERM behavior. No extra database or Redis without demonstrated need.
Connected Render tooling provides an alternative to the failed Google browser
path; do not confuse the failed old Docker web service with a running worker.

Continue isolated local testing of approved charter, budget reservation, job
claim/checkpoint, competing workers, lease reclaim, cancellation, lost response
after commit, durable result reopening and exactly-once settlement. A local
worker can find defects; it cannot satisfy hosted-worker acceptance.

Use only an existing approved zero-cost operating environment if one becomes
available. Otherwise mark hosted execution BLOCKED. Do not provision paid
hosting. When operationally possible, acceptance requires one persisted,
approved hosted case producing findings and receipt, settling actual usage,
surviving reopen and recovery, on the exact API/worker revisions.

External execution stays disabled: durable duplicate-effect fencing must be
implemented and independently proven before any enablement.

## Priority 4 — reconcile release evidence and public explanation

Use docs/RELEASE_CONFORMANCE_CURRENT.md and the 36-term release inventory.
Distinguish source implementation, local/CI evidence, deployed observations
and signed accepted artifacts. Missing, stale, unsigned and unobserved terms
remain unsatisfied. Do not silently waive commercial terms for a free scope;
a revised scope needs explicit reviewed requirements.

Complete permitted restore/rollback preparation and test isolated resources.
Owner holds release-signing authority; do not forge signatures or retrieve
private signing material merely to display it.

Keep README, PUBLIC_STATUS, ROADMAP and this handoff consistent with evidence.
Explain what LI does, how evidence is inspected, actual limitations, current
work and release conditions. No invented launch date, donation destination,
customer count, autonomous law coverage or client success.

## Validation and delivery

Fix each verified defect with a meaningful regression. Preserve the first
failure and explain its cause. Do not weaken authorization or assertions.

On one final candidate run relevant regressions, full suite, offline checks,
clean wheel, real PostgreSQL proof, operational checks and all dedicated gates.
Inspect job execution and skips. Repeat stable-origin hosted preflight after
deployment; the canonical OAuth issuer is the stable origin, so a unique
Preview host is not automatically a valid issuer probe.

Deliver:
- Exact source, API, worker and schema identities.
- Defects fixed and actual commands/results.
- Observed authentication/client and recovery outcomes.
- PASS, FAIL, BLOCKED and NOT RUN requirements.
- The smallest actionable next proof or owner action.

Start with current refs and the pending atomic disconnect fix. Then prioritize
real hosted authentication and an ordinary client connection. Do every
independent zero-cost task while preserving honest operational blockers.

## Additional independently discovered defect
Numeric corroboration previously propagated transitively and crashed on independent observations 100/114/129. The new verifier increment marks derived edges and preserves pairwise relations through reload/reverification. Its 1,126-test full suite and 94 focused tests passed. Require exact-head CI after integration. Historical graphs lacking provenance markers must be rebuilt from original evidence rather than silently assumed to have correct direct support.
