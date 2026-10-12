# Current six-step verification checklist

Checkpoint: **2026-10-11, America/Phoenix**. These are observed or reported
results from this construction session, not a live dashboard or a signed
public-release certification. Recheck identities before acting.

## Evidence identities

- Source candidate `6f53287`: PostgreSQL proof and operational CI passed;
  Python matrix and clean installed-wheel checks passed. Dedicated gate jobs
  triggered by the push were still running at this checkpoint.
- Local validation: **1,119 tests passed**, **25 offline checks passed with
  zero network attempts**, and V1–V6 source certifications passed. These are
  distinct reported runs; do not sum them into a unique test count.
- Stable dedicated hosted endpoint: all eight ordinary-client preflight checks
  passed on `b28cdb8`. This does not establish authenticated client delivery.
- A `6f53287` Preview deployment is ready. Its OAuth issuer uses the stable
  canonical origin, so testing discovery as though the unique Preview host
  were the issuer produces an expected origin mismatch. Test the stable alias
  after assignment; do not change the issuer merely to make that probe pass.
- ChatGPT's shared browser is signed out. No actual signed-in ChatGPT MCP
  installation or research journey was observed.
- No running hosted worker is verified. An approved zero-cost hosted worker
  environment is unavailable at this checkpoint; paid creation is outside the
  current zero-cost instruction.

## Requested steps 1–6

| Step | What was accomplished | Status and remaining acceptance |
| --- | --- | --- |
| 1. End-to-end authentication | OAuth source regressions and local suites passed; stable-host discovery/preflight passed | **PARTIAL**: actual consent, PKCE exchange, refresh rotation/replay, expiry and revocation still need observed hosted proof |
| 2. Actual MCP client connection | Protocol/source checks and ordinary HTTP preflight passed | **NOT RUN**: signed-in ChatGPT or another ordinary client must discover tools and complete authorized operations; shared ChatGPT browser is signed out |
| 3. Anonymous-trial review | Trial integration and hardening are included in tested source | **PARTIAL**: keep trial disabled until final runtime/concurrency, shared-budget, failure/privacy and real-install evidence passes; source tests are not distributed hosting proof |
| 4. Worker deployment preparation | Worker configuration/schema and offline deployment checks were reviewed | **PREPARED**: deployment and hosted identity/startup/shutdown remain unverified; no zero-cost host is available |
| 5. Execution and recovery testing | Local authorization, recovery, settlement and regression work contributed to the passing source suite | **LOCAL PASS / HOSTED NOT RUN**: hosted completion, restart/reclaim, cancellation, accounting and reopening require a running worker and approved case |
| 6. Release evidence reconciliation | Source, deployed and unobserved requirements are separated in documentation and evidence tooling | **PARTIAL**: final exact-head gates, alias retest, hosted acceptance, required operational evidence and owner signing remain unsatisfied |

Independent subagents reviewed LI and exercised its boundaries. They use the
same available agent model; this is independent review, **not proof that an
external Claude or other AI product connected to the hosted MCP service**.

## Next executable checks

1. Confirm every dedicated gate finishes successfully on `6f53287`, retaining
   job identities and checking for skipped gates. Any subsequent code changes
   require their own exact-head validation.
2. Assign the approved candidate to the stable dedicated alias and repeat the
   eight ordinary preflight checks against its canonical OAuth origin. Retain
   the actual deployed revision; readiness alone does not prove a customer run.
3. Complete secure owner sign-in in the prepared ChatGPT browser, then observe
   actual connection, OAuth consent and supported authorized operations. Do not
   put credentials in chat, manufacture a principal or substitute a harness
   for the real client.
4. Continue isolated trial and worker failure tests without paid infrastructure
   or paid provider calls. A local worker can find defects but cannot satisfy
   hosted-worker acceptance.
5. When an approved operating environment becomes available, run one persisted,
   approved hosted case through findings, receipt, settlement and reopen, then
   test recovery and forbidden access on that same revision.
6. Reconcile final retained evidence with the required public scope and signing.
   No launch date or full-public-readiness claim is supported by this checkpoint.

## Handoff and public explanation

Use the [Claude master prompt](CLAUDE_PRE_WORKER_MASTER_PROMPT.md),
[public status](PUBLIC_STATUS.md), [roadmap](ROADMAP.md),
[release term inventory](RELEASE_CONFORMANCE_STATUS.md) and
[change-aware update guide](CHANGE_AWARE_UPDATES.md).

The worker runs LI's Python pipeline and saves durable results; it does not buy
users, model intelligence or revenue. Its value must be demonstrated through
completed jobs, recovery and usable findings. No paid worker was started in
this zero-cost workstream. Continuous law monitoring, silent software
self-upgrades, external execution and unverified donation destinations are
not advertised as established capabilities.

## Subsequent verification

All three Python matrix jobs and all five dedicated push lifecycle gates passed on `6f53287b56d1498d4d9b2f17423624b9ae1d8cef` (push tests run `38211832518`). PostgreSQL proof `38211832532` and operational quality `38211832490` passed. The dedicated stable MCP alias now serves that candidate; all eight ordinary preflight checks passed. The new source-watch CLI has six passing offline behavioral tests and remains a source increment beyond that deployed revision.

The secure ChatGPT sign-in prompt returned Google as the selected option. Automatic approval review then denied accessing Google authentication because it did not recognize trusted provider-specific sign-in approval. No successful ChatGPT sign-in or client acceptance is claimed.
