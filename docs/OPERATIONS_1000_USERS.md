# First 1,000 Users — Operational Readiness Contract

Status: **Pre-production gate**  
Owner: Lofgren Intelligence  
Base: `build/v2-discovery`  
Initial base SHA: `b8593e246a0315175bd28cf25e6be6673b6645bf`

> Ported unchanged from `ops/first-1000-readiness` (`e3768bc`) to the
> `release/public-v1-v6` line by `build/public-hardening`. On this line V3-V6
> are offered by the hosted service because each passes its own certification
> gate on the release SHA; the section 2 rule (advertise only what is proven on
> that SHA) still applies. Public readiness is decided by
> `scripts/public_mcp_gate.py`, not by this document.

This document defines the minimum evidence required before Lofgren Intelligence is exposed to its first 1,000 users. It is a gate, not a launch claim.

## 1. Release invariant

Public service is allowed only when all applicable terms are true:

```
PublicReady =
  V1Certified
  AND V2Certified
  AND V3ReadyOrExplicitlyNotRequiredForOfferedFeatures
  AND ReproducibleBuild
  AND AuthBoundaryVerified
  AND TenantIsolationVerified
  AND AbuseControlsVerified
  AND BudgetControlsVerified
  AND ObservabilityVerified
  AND BackupRecoveryVerified
  AND CapacityVerified
  AND IncidentRunbookVerified
  AND PrivacyAndDataHandlingDocumented
  AND RollbackVerified
  AND ExactReleaseSHAPinned
```

A failed or unknown term means `PublicReady = false`.

## 2. Product boundary for the first cohort

The public surface must advertise only capabilities proven on the release SHA. V1 evidence research and any subsequently certified V2 discovery capabilities may be offered. V3 build, V4 execution, V5 learning, or V6 routing capabilities must not be presented as live merely because contracts or placeholders exist.

Generated hypotheses are not facts. Simulations are not observations. Discovery output cannot bypass V1 verification. Analytical components must not gain execution authority.

## 3. Service architecture required before public traffic

The open-source Python package is currently a library/CLI/MCP core. A hosted service for 1,000 users needs an explicit service boundary around it. Before launch, document and test:

- request/API boundary and versioned schemas;
- authentication and account/session lifecycle;
- tenant/user isolation;
- durable job state and idempotency;
- bounded queues/workers for long research jobs;
- per-user and global concurrency limits;
- per-request timeout and cancellation;
- spend/work-unit caps enforced server-side;
- provider credentials held only server-side;
- structured error contracts;
- health/readiness endpoints;
- request IDs and research/job IDs carried through logs and receipts;
- durable receipt/artifact storage and retention policy;
- deletion/export path for user-owned data;
- provider outage/degradation behavior.

The core package must remain usable without hosted-service credentials.

## 4. Security gate

Before launch, retain evidence for:

- secrets scan on the exact release SHA;
- dependency vulnerability scan;
- untrusted input/path/URL tests;
- SSRF protections for network retrieval;
- output/path traversal protections;
- prompt/model output treated as untrusted data;
- no model response may grant authority;
- authorization checked at the action boundary, not only the UI;
- cross-user access tests for every durable user object;
- rate limits and abuse ceilings;
- request/body/file-size ceilings;
- safe archive/file handling;
- audit records for privileged changes;
- production secrets absent from source, fixtures, logs and receipts.

No production secret may be required by deterministic offline CI.

## 5. Reliability gate

Test at least these failure modes:

- model provider timeout, 429, 5xx and malformed response;
- search provider timeout or unavailable key;
- source URL unavailable or returns oversized content;
- worker crash during a job;
- duplicate/retried request;
- cancellation;
- partial evidence acquisition;
- corrupted/tampered receipt;
- storage unavailable;
- database unavailable;
- queue saturation;
- budget exhausted mid-plan;
- dependency returns stale/invalid data.

The service must fail closed where evidence, authority or budget is uncertain. A provider failure must not be rendered as a normal successful research result.

## 6. Capacity gate for 1,000 users

Do not equate 1,000 registered users with 1,000 simultaneous jobs. Define and record the expected workload before testing:

- registered users;
- daily active users;
- peak requests/minute;
- peak concurrent interactive requests;
- peak concurrent research jobs;
- median and p95 job duration;
- median and p95 provider calls/job;
- median and p95 work units/job;
- artifact/receipt bytes/job.

Run repeatable load tests against a non-production environment with external providers stubbed first, then a bounded provider-backed test if authorized.

At minimum retain:

- scenario definition;
- test tool/version;
- target SHA;
- start/end time;
- offered load;
- successful requests;
- rejected/rate-limited requests;
- error count by class;
- p50/p95/p99 latency;
- queue depth;
- CPU/memory where available;
- provider-call count;
- estimated and actual cost where available.

The test passes only against declared thresholds. Do not invent thresholds after observing the result.

## 7. Cost and abuse containment

Every externally triggered job must have a pre-execution estimate and a hard server-side ceiling. Required controls:

- maximum standard work units/job;
- maximum heavy operations/job;
- maximum external spend/job;
- per-account rolling limits;
- global emergency budget ceiling;
- bounded retries with backoff;
- no unbounded agent loops;
- no recursive job creation without a hard depth/count limit;
- idempotency for billable submissions;
- external costs recorded separately from internal work units.

A rejected or failed job must not be represented as completed billable work.

## 8. Observability

Minimum production telemetry:

- request count and status;
- job count by state;
- latency distributions;
- queue depth/age;
- provider latency/error rate;
- evidence acquisition failures;
- verification failures;
- receipt-integrity failures;
- budget denials;
- rate-limit events;
- authentication/authorization denials;
- application exceptions;
- release SHA/version.

Logs must use stable correlation IDs and must not intentionally contain secrets or unnecessary user content.

## 9. Incident and rollback gate

Before launch, demonstrate:

1. identify the deployed SHA;
2. disable new expensive jobs;
3. place the service in degraded/read-only mode where appropriate;
4. rollback to the last certified release;
5. confirm health after rollback;
6. preserve receipts/audit evidence;
7. communicate an incident without claiming unknown facts.

Define severity, owner, escalation, and recovery evidence in the deployment repository/runbook once hosting is selected.

## 10. Data and privacy gate

Before accepting user data, document:

- what data is accepted;
- why it is needed;
- where it is stored;
- retention;
- deletion/export behavior;
- third-party processors/providers;
- whether user data is sent to model/search/data providers;
- treatment of uploaded files;
- telemetry/logging content;
- physical-world and sensor authorization rules.

The system must preserve the existing rule against tracking individual people, homes or vehicles and must require authorization for user-owned sensor access.

## 11. CI/release evidence

For every release candidate retain:

```
repository
branch
exact SHA
parent SHA
version
schema versions
V1 regression result
V1 certification result
V2 result (when applicable)
security/static checks
package build/install result
fresh-environment result
capacity result
rollback result
known limitations
PublicReady = PASS|FAIL
```

A release report must distinguish tests that actually ran from planned tests.

## 12. Initial 1,000-user rollout

Use staged exposure rather than opening the full cohort at once. Promotion between stages requires reviewing errors, latency, cost, abuse signals and correctness receipts. Keep a kill switch for new research jobs.

Suggested operational cohorts are 10, 50, 200, 500, then 1,000 registered users, but these numbers are not certification evidence and may be changed based on measured capacity. Never expand solely because a calendar date arrived.

## 13. Current state

At creation of this contract, `build/v2-discovery` is based on certified V1.1 but its head commit describes V2 as documentation-only. Therefore:

`PublicReady = false`.

This is expected. The purpose of this branch is to make the remaining operational work explicit and testable while V2 is constructed.
