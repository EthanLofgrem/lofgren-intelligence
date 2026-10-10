# Worker runtime (durable research jobs)

The hosted HTTP function is capped at 60 seconds (`vercel.json`,
`maxDuration`). Research that is not bounded well under that runs as a durable
job instead of inside the request.

## Process model

- **Request side** (`start_research`): validates, plans, reserves usage
  (`li_reserve_usage`), consumes a case approval when there is one, and
  enqueues a row in `li_research_jobs` (`li_enqueue_research_job`) with the
  frozen inputs. It returns `job_id` at once. The same `idempotency_key` returns
  the same job and never queues a second one.
- **Worker side**: `python -m lofgren_intelligence.hosted.worker` is a
  long-running process. Each cycle it reclaims expired leases
  (`li_reclaim_research_jobs`), settles pending usage markers
  (`reconcile_unsettled_usage`), then claims jobs
  (`li_claim_research_job`, `FOR UPDATE SKIP LOCKED`) up to its concurrency
  limit. Each job runs the same research core as the synchronous path
  (`PublicService._execute_research`) using only the frozen job input. A lease
  thread heartbeats (`li_heartbeat_research_job`). Every stage boundary
  checkpoints progress and checks for a cancel request, an exhausted budget and
  shutdown.
- **Clients** poll `get_job_status` and may call `cancel_research`.
  `--once` runs a single cycle.

The synchronous `investigate` path stays for bounded work only. That means at
most `LI_SYNC_MAX_NETWORK_TASKS` (default 2) source calls that leave the
process, each with a 15-second fetch timeout, and at most
`LI_SYNC_MAX_WORK_UNITS` (default 40) estimated work units. Larger work is
refused with `ASYNC_REQUIRED` before anything is reserved.

## Accounting

The job's reservation is taken at enqueue and held for the job's whole life.
`li_usage_reservations.held_until` is pushed forward at enqueue (queue TTL),
at claim and at every heartbeat (lease plus grace), and on requeue.
`li_reserve_usage` never expires a reservation whose hold is live. The job
finishes by the M1 rules:

- If it is refused or cancelled before any work, the reservation is released.
- If work began and then stopped (cancelled, budget exhausted, failed), the
  units checkpointed so far (`cost_so_far`) are charged once, with no run id.
- If the result was saved, the job is settled with `li_finalize_usage`. If
  that refuses or fails, an unsettled marker is written and
  `li_settle_usage` reconciles it later, in this job or in a later worker
  cycle.

A job that ends with no worker present is accounted inside the database by the
same rules. This covers a queued job that is cancelled, a lease that expires
with no attempts left, and a job that waits past the queue TTL.

The worker stores its run and `result_saved` checkpoint atomically using
`li_save_research_job_result` (migration `20261010200818_li_atomic_job_result`).
The RPC locks the job, checks its live lease and user identity, and writes both
records in the same transaction. Expired or foreign leases cannot persist results.
A lost RPC response leaves the worker waiting for lease recovery rather than
charging a failure: reclaim observes the committed checkpoint, or retries research
if neither record committed. Deploy this migration before deploying this worker.

Checkpoint phases `result_saved` and `finalizing` make finishing idempotent. A
worker that resumes a reclaimed job in one of these phases settles and
finishes it without running the research again. Such a requeue uses no
attempt, so it is counted separately (`finalize_reclaims`, migration
20261006080000_li_research_job_finalize_cap). After
`LI_JOB_MAX_FINALIZE_RECLAIMS` requeues, the next expired lease fails the job
with `SETTLEMENT_ABANDONED` instead of requeueing it forever: a saved result is
kept (`result_run_id`, shown by `get_job_status`), and the recorded work is
left as an unsettled-usage marker (with the run id when the result was saved)
that the next reconcile charges exactly once. A reservation that was already
settled or marked is left alone.

## Retries, leases and timings (environment)

| Variable | Default | Meaning |
| --- | --- | --- |
| `LI_WORKER_CONCURRENCY` | 2 | Jobs run at once per process (threads, 1-32). |
| `LI_WORKER_LEASE_SECONDS` | 120 | Lease length; an unrenewed job is reclaimable after this. |
| `LI_WORKER_HEARTBEAT_SECONDS` | 30 | Heartbeat interval (capped at lease/3). |
| `LI_RESERVATION_HOLD_GRACE_SECONDS` | 300 | Reservation hold beyond the lease. |
| `LI_JOB_MAX_ATTEMPTS` | 3 | Attempts before a retryable failure becomes `failed` (1-10). |
| `LI_JOB_MAX_FINALIZE_RECLAIMS` | 3 | Requeues of a job whose worker died while settling it before it fails with `SETTLEMENT_ABANDONED` (1-10). |
| `LI_JOB_BACKOFF_BASE_SECONDS` / `_MAX_SECONDS` | 30 / 900 | Exponential retry backoff. |
| `LI_JOB_QUEUE_TTL_SECONDS` | 86400 | A queued job not started within this fails with `QUEUE_EXPIRED`. |
| `LI_WORKER_POLL_SECONDS` | 5 | Idle poll interval. |
| `LI_WORKER_RECLAIM_LIMIT` | 100 | Expired leases handled per cycle. |
| `LI_WORKER_SHUTDOWN_GRACE_SECONDS` | 25 | Wait for in-flight jobs after SIGTERM. |
| `LI_SYNC_MAX_WORK_UNITS` / `LI_SYNC_MAX_NETWORK_TASKS` | 40 / 2 | Bound on the synchronous path. |

The worker also needs the service's own variables: `SUPABASE_URL`,
`SUPABASE_SERVICE_ROLE_KEY`, the provider and cost-rate variables, and
`LI_RUNTIME_PLAN`.

Provider outages and unexpected errors are retried with backoff until the
attempts run out. Refusals such as an invalid input, a budget overrun or a
case error fail at once. On SIGTERM the worker stops claiming, and running
jobs requeue at their next stage with `WORKER_SHUTDOWN`, which does not use up
an attempt. If a worker is killed, its lease expires and another worker
reclaims the job. Execution is at most one completed run per job, and the
job's single reservation is settled once.

## Scaling

Workers are stateless. Any number of processes may poll the same database
because claims never overlap (`SKIP LOCKED`). Scale by adding processes or by
raising `LI_WORKER_CONCURRENCY`, and keep within the database connection and
model-provider rate limits. Keep `LI_WORKER_LEASE_SECONDS` well above the
heartbeat interval and above the longest single stage, such as one source
fetch.

## Isolation notes

The worker runs with the service-role key and is fully trusted. Jobs from
different tenants share the process. Tenant separation comes from the frozen
input and the user-filtered store calls, not from process isolation. V3's
generated-code test run (`production/sandbox.py`) uses a separate Python
subprocess with a timeout. That subprocess is **not** a security sandbox for
untrusted code, and research jobs do not run it.

## Deployment options

Hosted external action execution is disabled by default. Keep
`LI_EXTERNAL_EXECUTION_ENABLED=false` on public API deployments. Action
proposals, review, and existing receipt inspection remain available. A user
approval does not override this operator restriction.

The exact value `true` is an operator opt-in for an explicitly approved
contained environment, not evidence of public readiness. The present action
path lacks a durable atomic execution claim and ambiguous-result recovery.
Concurrent calls or a crash after a webhook POST can repeat an external
effect; sending an `Idempotency-Key` cannot guarantee an arbitrary recipient
honors it. Do not enable public external execution until these boundaries are
implemented and demonstrated. Local V4 certification uses bounded adapters
and remains separately available.

Any always-on container or VM host that can run
`python -m lofgren_intelligence.hosted.worker` and reach the Supabase database
works. Examples are a container service, a VM under a process supervisor, or a
Kubernetes Deployment. The host must deliver SIGTERM before it kills the
process. The serverless HTTP function cannot run the worker. This repository
does not choose a host, and no worker deployment is verified here.
