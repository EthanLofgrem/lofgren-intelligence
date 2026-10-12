# Claude Code master prompt — finish pre-worker verification

## Mission
Lofgren Intelligence is the only release priority. Implement, test and fix the
next executable blocker. Preserve Research and Verification, Discovery,
Production, Authorized Execution, Outcome Measurement and Improvement
Evaluation. Stages are available capabilities, not mandatory fabricated steps.

## Verified baseline
Application e232ced165a31ab66124decb1f0fe9ea4b9057ac, PR #37.
Vercel deployment dpl_DP5k99xLKjN7HDEr3a3U7HAQSumF assigned to
https://lofgren-intelligence-mcp.vercel.app.
SUPABASE_SERVICE_ROLE_KEY is now saved privately as a Preview-only Secret.
Eight hosted preflight checks passed, including exact-SHA readiness.
Do not continue reporting the key as missing.

Render has only the failed Docker web service on old main. No worker exists.
deploy/li-staging-worker at dee19e831828348156f4a1cc97e6044afca4bc79
prepares a separate worker pinned to e232ced.
Its YAML passes Render's official JSON Schema.
Paid worker creation remains unapproved; do not provision it.

Q36 trial branch build/li-trial-endpoint:
6e609c55f19a3231633ab24e4c03389a985662e5, one commit ahead of e232ced.
Codex independently ran its 24 tests successfully using fixtures.
The trial is NOT deployed or tested in ChatGPT.

## Codex verification in this increment
- 84 hosted/auth/worker/source/surface tests passed after installing locked deps.
- 24 offline tests passed with zero network attempts.
- 80 hardening/settlement/V4/V6 tests passed.
- 16 V5/routing tests passed.
- Trial: 24 tests passed in a separate worktree.
These suites overlap; do not add them into a unique full-suite count.
Initial runs had missing dependencies and one incorrect V5 module name;
corrected runs passed. Do not misreport those as application defects.

Live negative checks on deployed e232ced:
malformed token form 400; invalid synthetic refresh 400;
invalid synthetic MCP token 401.
OAuth errors lacked cache-control. The accompanying source patch fixes
the shared error helper and adds regression assertions. It is not deployed.
Review/integrate this increment before building the next exact SHA.

## Six workstreams
1. Authenticate a synthetic staging customer through the actual supported
browser flow. Prove S256 exchange, one-time code use, refresh rotation/replay,
expiry and revocation. Never manufacture identity or bypass consent.
Local FakeStore proofs are not hosted proofs. Keep tokens out of logs.

2. Complete a real supported MCP client connection. Discover tools, assess a
request and exercise an authorized non-worker operation. Record exact client
and protocol versions. Python SDK tests do not prove ChatGPT installation.
If owner interaction is required, prepare the exact connection and secure
handoff; label that requirement BLOCKED rather than claiming completion.

3. Harden Q36 before enabling it:
- Its default global ceiling of $10 divided by $2 worst-case allocation allows
only FIVE research calls per day. This is safe containment, not a launch-scale
free offer. Separate platform-price estimates from actual infrastructure cost.
- OperatorSourcesAdapter fetch_all retrieves every configured URL (up to 3),
contradicting the documented one-upstream-request bound. Enforce the promised
bound or revise the bound and provide matching runtime/tests.
- Stage-boundary deadlines do not interrupt an already running operation.
Prove actual worst-case wall time, redirects, retries and body limits.
- Prove global atomic admission and bounded concurrent execution across
instances; test malformed storage responses, parallel requests and outage.
- Verify trusted proxy IP behavior on Vercel and shared assistant egress.
Do not assume IP equals user identity.
- Verify receipts, output limits and source failures honestly.
- Keep trial tools separate from private cases, jobs, billing and actions.
- Keep trial disabled until the evidence and cost policy support enabling it.

4. Worker preparation: official schema validation already passed. Verify
pinned checkout/build/start commands with the current locked dependencies,
Render Python compatibility and SIGTERM behavior. Never replace this with
an unpinned deploy. No extra database or Redis is needed without evidence.
Do not claim hosted-worker proof before a Render process completes a case.

5. Run isolated local execution and recovery: approved charter, job claim,
saved checkpoint, lost response after commit, competing workers, cancellation,
lease reclaim, actual usage settlement and result reopening. Exercise V1-V6
where applicable. V4 external execution stays disabled: duplicate-effect
fencing is still incomplete. Never perform external effects for a green test.

6. Reconcile all 36 signed public-gate terms. PR #38
(task/li-operations-evidence-ledger, 712339a...) is a draft available for review;
do not duplicate it blindly. Distinguish raw test evidence from signed accepted
artifacts. All unsigned/missing/unobserved terms remain unsatisfied.
Hosted auth, real-client, worker, restore/rollback and other remote requirements
cannot be inferred from local suites. Owner holds signing authority.

## Delivery
Use isolated worktrees and coordinate routing ownership with Codex.
Review PRs #33/#34/#36/#37/#38 individually; no blind draft merges.
Preserve all approval, tenant, resource/scope and no-store protections.
Run relevant regressions, full suite, clean wheel and exact-head CI with every
dedicated gate actually running. Retest deployed evidence after any code change.
Return candidate SHA, tests, deployed behavior, failures, blockers and the
smallest owner action. No paid hosting, live billing, new provider spending,
public-trial enablement or external execution without specific authorization.

