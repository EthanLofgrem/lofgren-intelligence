# Lofgren Intelligence architecture conformance

Ethan Lofgren is the founder, owner and final release authority. The website
and MCP interface must use one authorized engine. The durable case preserves
scope, versioned charters, ownership, approval, execution state, evidence,
usage and history. Research approval never grants external-action authority.
V1–V6 are capabilities used as applicable, not mandatory stages for every
question. Persisted evidence determines completion; a receipt proves record
integrity, not source truth. Improvement evaluation never silently mutates
production. Public access, live billing and production promotion require
explicit owner approval for the candidate and actions.

This matrix records implementation and proof separately. PASS is scoped to
the listed observation; component tests do not certify the integrated public
service. The current release classification is **NOT READY** for the full
public contract. Protected API staging works; the full protected product
journey has not been proven.

## Baseline

- Base commit: `afa74bec8bb444822411f4369e50006392ab1556`.
- Package: 0.7.0; core dependencies empty; hosted runtime uses `uv.lock`.
- Protected staging: `https://lofgren-intelligence-staging.vercel.app`.
- Last observed deployment: `dpl_AAp7itLAxFpeKefvZHkpSBDzK5NC`.
- Observed `/healthz` and `/readyz`: HTTP 200, ready true, exact base SHA.
- Readiness performs V1–V6 data-table reads; it does not test every SQL RPC,
  tenant boundary, worker, OAuth lifecycle or customer workflow.
- Database project: `pzoxinhycjquaalflbwe`; ten applied migrations through
  `20261006090000_li_revoke_public_defaults`; 23 LI tables have RLS.
- No public LI table/function grants; three LI sequences deny anonymous and
  authenticated USAGE. The service role bypasses RLS, so application ownership
  checks remain necessary.
- Hosted tool registration: 45. Registration is not client interoperability.
- Local V1–V6 certification and boundary commands pass on the base SHA.
- Base-merge CI and full local suite are not yet verified. The earlier full
  suite was interrupted by automatic approval review over `db.example` egress;
  synthetic test keys and mocks were inspected, but no passing result is claimed.

## Boundary matrix

| Requirement | Implementation | Automated proof | Hosted/integration check | Evidence/status |
|---|---|---|---|---|
| One authorized engine | `hosted/service.py`, `hosted/web_app.py` | `tests/test_case_approval.py` | Compare MCP and browser records for same owner | PARTIAL: charter browser uses PublicService; demo console remains static |
| Durable case and charter | `hosted/cases.py`, `hosted/store.py` | `tests/test_case_approval.py` | Reopen case after browser and worker restart | NOT RUN on hosted service |
| Approval controls scope/budget | `PublicService._authorized_case`, case approval SQL | `tests/test_case_approval.py` | Reject stale, consumed, foreign or altered approval | Component implementation present; hosted NOT RUN |
| Separate external action approval | `hosted/service.py`, action routes | `tests/test_public_hosted.py` | Contained V4 approval/action/receipt journey | Hosted NOT RUN |
| Durable execution | `hosted/jobs.py`, `hosted/worker.py`, job SQL | `tests/test_durable_jobs.py` | Kill worker; reclaim lease; reconnect; cancel | BLOCKED: running worker host not provisioned |
| Honest exactly-once accounting | `hosted/service.py`, settlement SQL | `tests/test_usage_settlement.py` | Concurrent workers, failure after save, cancellation | Hosted NOT RUN |
| Tenant isolation | `hosted/store.py`, ownership checks | case, jobs and hosted tests | Foreign IDs, export and result paths | SQL grants/RLS PASS for inspected configuration; end-to-end NOT RUN |
| Evidence and upstream integrity | V1–V6 modules, receipts | certification commands and boundary tests | One hosted six-stage case with safe synthetic action | Local code terms PASS; hosted chain NOT RUN |
| Real customer inspection | charter page and details handler | `tests/test_case_approval.py` | Sign in; inspect persisted charter and history | This increment adds history and `/workspace` case intake/list/resume; hosted candidate NOT RUN |
| Real workspace lifecycle | `hosted/web_app.py`, `hosted/service.py` | approval, job and shell tests | Create, run, cancel, retrieve actual results | PARTIAL: `/workspace` saves objectives and answers, lists owned cases and links approval/history; approved durable submission, job status/saved summary and cancellation are implemented; saved V1 reports, findings, source traces and receipt inspection are implemented. Hosted execution remains NOT RUN. `/app` stays isolated demo |
| MCP/OAuth lifecycle | hosted MCP and OAuth handlers | hosted auth/protocol tests | Actual ChatGPT, Claude and Codex sessions | Discovery endpoints PASS; authenticated client lifecycle NOT RUN |
| Economic limits and billing | plan catalog, entitlements, reservations, Stripe | billing/quota tests | Measured costs and sandbox lifecycle | BLOCKED: owner decisions; excluded from current authorized work |
| Owner operations and recovery | worker CLI, deployment preparation, operational tooling | operational tests | Restore and rollback rehearsal; restricted diagnostics | NOT RUN; full owner console incomplete |
| Public readiness | release gate collectors and signed evidence | exact-SHA gates | Full evidence ledger, trust policy, owner promotion | BLOCKED; no public release claim |

## This increment: persisted history inspection

`PublicService.case_charter_details` exposes a bounded public projection of
persisted events after verifying case ownership. The existing authenticated
browser handler returns it with `Cache-Control: no-store`. The charter page
renders it using `textContent`, never HTML injection. The response shows the
latest 200 events and explicitly reports truncation. Internal payloads and
approval tokens are excluded. This does not provide a complete audit export,
full multi-stage dashboard. The workspace separately provides approved durable job submission, status, cancellation and saved V1 result/receipt inspection.

Proof command: `python -m unittest tests.test_vendor_supabase tests.test_account_deletion.AccountPageScriptTests tests.test_case_approval tests.test_web_shell -q`: 102 focused security, approval, workspace and shell tests pass locally. Generated workspace JavaScript passes `node --check`. Browser interaction proof remains NOT RUN.
Negative cases include missing/forged sessions, another user's case, wrong
tenant/case rows returned by a faulty store, and private fields on event rows.

## Next executable blockers

1. Verify this increment's CI and dedicated gates on an integrated exact SHA.
2. Provision the portable worker on an approved always-on host; verify restart,
   cancellation, recovery and settlement against the staging database.
3. Verify saved result/receipt inspection in an authenticated hosted journey;
   complete multi-stage artifact inspection only through PublicService.
4. Test an authenticated real-client case and full OAuth lifecycle. Platform
   deployment protection currently blocks ordinary external MCP clients.
5. Produce full recovery and release evidence before owner-approved public
   exposure. Billing, allowances and signing remain owner-controlled and
   outside this increment.

No universal AI-client compatibility, scientific validation, measured customer
performance or live revenue is established by this record.

The workspace calls PublicService for objective/answer persistence and a bounded owned-case index. It never executes research during case creation. Server identity overrides client-supplied owner fields; revisions use expected charter versions. GET results are no-store. Signing in recovers cases; saved charters reopen clarification forms. No migrated SQL or billing change is required.

Workspace job controls reuse PublicService durable submission/status/cancellation. Starting uses only the route case ID; caller-supplied source, budget, objective or owner cannot replace the server-approved charter. Duplicate starts replay the same persisted job. Synthetic tests prove queueing, replay, foreign-session refusals and queued cancellation; actual hosted worker execution and authenticated browser journey are NOT RUN.

## Saved result inspection increment

The workspace composes existing persisted research through PublicService: report, findings, contradictions, unknowns, claim traces and receipt. A queued job returns result_available=false; saved results retain the actual failed/succeeded job status. Receipt integrity is recomputed on read instead of trusting a cached flag. Shared run reads independently check row ownership and run/snapshot identity, including MCP report, receipt and state exports. The browser renders source text with textContent, never HTML. No schema, billing or public-access change. Tests cover pending results, retained results after job failure, tampered receipts, unauthorized sessions and faulty-store identity mismatches. Hosted signed-in journey remains NOT RUN.
