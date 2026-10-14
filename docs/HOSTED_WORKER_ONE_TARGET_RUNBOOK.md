# Exactly one hosted synthetic worker acceptance

Prepared 2026-10-13 against source `410f16c1f6aab707bf9136d9c69e913d1c6402a4` (remote equivalent `0074472d896fab72af029fa6c2cf449ca0c2ae77`, identical source tree `a4694aa75d03482c499f9051e1c10c81f82cdb4a`). This is preparation; no target was created, secrets installed or job executed by this document.

## Existing host and cost boundary

Use one **manual standard GitHub-hosted Linux Actions job** in this public repository, with no cache/artifact uploads or provider calls. GitHub's authoritative billing documentation, checked 2026-10-13, says standard GitHub-hosted runners in public repositories are free: https://docs.github.com/en/billing/concepts/product-billing/github-actions. This finite acceptance execution is not always-on worker hosting or a customer SLA. Enforce the zero-new-cost policy at the host/account level too; a recorded accounting value of zero is not independent provider billing evidence.

Do not run the generic `worker --once`: it performs global recovery and selects queue work across users. Use only `targeted_worker --target PRIVATE_TARGET.json`, whose targeted SQL checks identity, complete frozen input, approval, charter, reservation and entitlement before claiming.

Vercel is not an interchangeable host for this exact CLI: deployed functions ordinarily have no `.git` checkout, while this runner verifies `git rev-parse HEAD`. Do not remove that identity check to make a Vercel run appear successful. A future Vercel execution adapter needs authenticated private invocation, independently verified deployment identity, process termination proof and a bounded request duration; no privileged public route is proposed here.

## Exact missing configuration and authority

1. Owner-restricted GitHub environment `li-staging-worker-proof`, permitting only the reviewed proof ref, with required owner review where the account plan supports it. Verify actual environment protection rather than assuming it exists. If unavailable, retain the blocker; do not put a service-role secret in an unrestricted repository scope.
2. Staging-only secret `LI_STAGING_SUPABASE_SERVICE_ROLE_KEY`. Owner installs this directly from their authorized secret manager/dashboard. Never print, paste into a public issue, extract from Vercel, or reuse production credentials.
3. Environment setting `LI_STAGING_SUPABASE_URL=https://pzoxinhycjquaalflbwe.supabase.co` and accepted full source SHA. No identity/provider secret belongs in install or checkout steps.
4. One private selected-target document, stored as restricted environment secret `LI_STAGING_WORKER_TARGET_JSON`, containing the six existing runner fields. Source texts must be synthetic, no personal/customer information; complete approved input must match the existing job exactly. Do not expose this in workflow dispatch inputs or logs.
5. An independently authorized synthetic account, valid latest case charter and explicit selected-case approval, active entitlement, reserved zero-known-cost units and exactly one fresh queued approved-charter job. Create through the normal authorized service/API path. Do not fabricate consumed approval records through SQL just to pass acceptance.
6. A reviewed manually invoked workflow, exact workflow SHA and application SHA, disabled until the above configuration and approved target exist. The preparation template is `deploy/github/targeted-worker-proof.yml.example`, outside the Actions directory and cannot run as published.

## Before execution

Verify all applicable source CI and PostgreSQL proof at the accepted SHA; verify applied targeted-claim migration and service-role-only grants. Pin actions and the bootstrap dependency installer to separately reviewed versions/checksums before turning the template into a workflow; the disabled preparation template requires configured source SHA and independently verified installer checksum.

Freeze unrelated worker activity for the short proof window. Privately retain canonical read-only preflight records for the selected job, approval, charter and reservation. Record a deterministic digest of **all other** `li_research_jobs` rows, excluding only the exact selected job. The read-only comparison query, with UUID supplied privately, is:

```sql
select count(*) as unrelated_job_count,
       md5(coalesce(jsonb_agg(to_jsonb(j) order by j.id)::text, '[]')) as unrelated_jobs_digest
from public.li_research_jobs j
where j.id <> '<PRIVATE_SELECTED_JOB_UUID>'::uuid;
```

This verifies every unrelated job column, including status, attempts, leases and timestamps. No raw queue rows are public artifacts. Record the snapshot time; without a stable queue window, a changed digest is inconclusive rather than evidence that this runner changed a row.

## Execution and independent readback

Exactly one dispatch, standard runner, five-minute workflow ceiling. The runner has a 90-second child ceiling plus five-second TERM grace, then KILL and reap. Use frozen `uv.lock` install before secrets enter scope. Owner-private target file has mode 0600 in runner temporary storage and is deleted in `finally`; no public artifacts or raw child logs.

The child's allowlisted environment forces heuristic research, external execution false and configured infrastructure/retrieval prices zero. It receives only the required staging backend key, not paid model/search keys. PASS requires the existing independently validated persisted receipt, source-passage commitments, knowledge map, approval/charter/run linkage and settled usage.

After completion, independently read the selected job/run/approval/reservation/event via authorized read-only backend tools. Recompute native receipt integrity and source binding using the same accepted application version; do not merely read a stored `receipt_intact` flag. Compare the unrelated-row count and digest with preflight. Reopen the persisted result. Repeat claim of the same selected target must return no job; do not execute a second job to establish this.

Retain only sanitized proof: accepted source and workflow SHA, host/run ID, elapsed time, selected result persisted YES/NO, receipt integrity YES/NO, settled matching units YES/NO, known-cost accounting zero YES/NO, unrelated count/digest unchanged YES/NO, reopened YES/NO. A fixed runner PASS alone does not establish unrelated-row preservation, hosted isolation, always-on operation or real AI-client compatibility.

## Failure handling

No-job, missing secret, identity mismatch, store error, timeout, corrupt receipt or unsettled/mismatched result is a failed acceptance. A killed attempt is pending bounded recovery; do not retry the runner automatically or call global reconciliation. Inspect only the selected lease/reservation and prepare a separately reviewed targeted recovery if required. Preserve UNKNOWN for unavailable account or host access.

## Prepared executable template update (2026-10-13)

The template remains outside `.github/workflows` and is not activated. It now resolves all action references to immutable commits, retrieved from the publishers' GitHub tag API: checkout v4.2.2 `11bd71901bbe5b1630ceea73d27597364c9af683`, setup-python v5.6.0 `a26af69be951a213d495a4c3e4e4022e16d87065`, setup-uv v6.0.1 `6b9c6063abd6010835644d4c2e1bef4cf5cd0fca`. This confirms provenance, not an exhaustive third-party code certification. uv is pinned to 0.6.17 and caching disabled; frozen dependency install runs before staging secrets enter step scope. The first step requires a 64-character hexadecimal `LI_STAGING_UV_ARCHIVE_SHA256`; setup-uv receives it as its checksum input. It must be independently verified for the uv 0.6.17 Linux x86_64 archive before activation.

Repository variable `LI_STAGING_WORKER_PROOF_ENABLED` is absent/false by default and must be exactly `true` for the job to start. The protected environment variable `LI_STAGING_WORKER_ACCEPTED_SHA` must contain the full independently reviewed dispatch commit; the first step rejects another SHA before checkout, installer or privileged step. Job dispatch does not accept an arbitrary target or environment name. The only secret-consuming step receives only the restricted environment's two required secrets. Missing protections, credentials, approved target, archive checksum or provider billing evidence remain blockers. No dispatch was performed.
