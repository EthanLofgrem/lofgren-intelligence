"""Durable research jobs: settings, the frozen job input and the worker's lease.

The job queue lives in Supabase (`li_research_jobs`, migration
20261006070000_li_research_jobs.sql). The HTTP/MCP request only enqueues a job
(PublicService.start_research); an always-on worker process
(`python -m lofgren_intelligence.hosted.worker`) claims it under a lease,
heartbeats the lease while the research runs and finishes it. See
docs/WORKER_RUNTIME.md.
"""

from __future__ import annotations

import logging
import os
import re
import threading
from dataclasses import dataclass
from typing import Any

JOB_INPUT_SCHEMA = "lofgren.research-job/1"
JOB_KINDS = ("investigate",)
TERMINAL_STATUSES = ("succeeded", "failed", "cancelled")
JOB_STATUSES = ("queued", "running", "cancel_requested") + TERMINAL_STATUSES

# Only these request keys reach the research run. Everything else a client sends
# is dropped before the input is frozen into the job row.
SOURCE_ARG_KEYS = ("texts", "urls", "search", "lat", "lon", "fetch_orbits", "imagery", "max_spend_usd")

# Idempotency keys are stored and filtered on; keep them to a plain, bounded charset.
IDEMPOTENCY_KEY_RE = re.compile(r"^[A-Za-z0-9._:\-]{1,190}$")

_LOG = logging.getLogger("lofgren_intelligence.worker")


def _int_env(name: str, default: int, minimum: int) -> int:
    raw = os.environ.get(name, "").strip()
    try:
        value = int(raw) if raw else default
    except ValueError:
        return default
    return max(minimum, value)


def _float_env(name: str, default: float, minimum: float) -> float:
    raw = os.environ.get(name, "").strip()
    try:
        value = float(raw) if raw else default
    except ValueError:
        return default
    return max(minimum, value)


@dataclass(frozen=True)
class JobSettings:
    """Timings and limits for enqueue, leases and the worker loop (all overridable by env)."""

    lease_seconds: int = 120
    heartbeat_seconds: float = 30.0
    hold_grace_seconds: int = 300
    max_attempts: int = 3
    # Requeues allowed for a job whose worker keeps dying while it settles (checkpoint
    # phase result_saved/finalizing); the next expired lease fails it with
    # SETTLEMENT_ABANDONED, keeping the result and leaving an unsettled-usage marker.
    max_finalize_reclaims: int = 3
    queue_ttl_seconds: int = 86_400
    backoff_base_seconds: int = 30
    backoff_max_seconds: int = 900
    concurrency: int = 2
    poll_seconds: float = 5.0
    reclaim_limit: int = 100
    shutdown_grace_seconds: float = 25.0

    @classmethod
    def from_env(cls) -> "JobSettings":
        lease = _int_env("LI_WORKER_LEASE_SECONDS", 120, 10)
        heartbeat = _float_env("LI_WORKER_HEARTBEAT_SECONDS", 30.0, 1.0)
        # A heartbeat must land well inside the lease or a healthy worker loses its job.
        heartbeat = min(heartbeat, lease / 3.0)
        return cls(
            lease_seconds=lease,
            heartbeat_seconds=heartbeat,
            hold_grace_seconds=_int_env("LI_RESERVATION_HOLD_GRACE_SECONDS", 300, 0),
            max_attempts=min(10, _int_env("LI_JOB_MAX_ATTEMPTS", 3, 1)),
            max_finalize_reclaims=min(10, _int_env("LI_JOB_MAX_FINALIZE_RECLAIMS", 3, 1)),
            queue_ttl_seconds=_int_env("LI_JOB_QUEUE_TTL_SECONDS", 86_400, 60),
            backoff_base_seconds=_int_env("LI_JOB_BACKOFF_BASE_SECONDS", 30, 0),
            backoff_max_seconds=_int_env("LI_JOB_BACKOFF_MAX_SECONDS", 900, 0),
            concurrency=min(32, _int_env("LI_WORKER_CONCURRENCY", 2, 1)),
            poll_seconds=_float_env("LI_WORKER_POLL_SECONDS", 5.0, 0.1),
            reclaim_limit=_int_env("LI_WORKER_RECLAIM_LIMIT", 100, 1),
            shutdown_grace_seconds=_float_env("LI_WORKER_SHUTDOWN_GRACE_SECONDS", 25.0, 0.0),
        )

    def backoff(self, attempts: int) -> int:
        """Exponential backoff after the given (1-based) failed attempt, capped."""
        exponent = max(0, int(attempts) - 1)
        return int(min(self.backoff_max_seconds, self.backoff_base_seconds * (2 ** min(exponent, 16))))


class LeaseLost(Exception):
    """This worker no longer holds the job's lease (it expired or another worker reclaimed it)."""


class JobCancelled(Exception):
    """The job's owner requested a cancel; the worker stops at the stage boundary."""


class JobBudgetExhausted(Exception):
    """The units recorded so far passed the job's budget; the worker stops at the stage boundary."""


class WorkerShutdown(Exception):
    """The worker is stopping (SIGTERM); the job is requeued at the stage boundary."""


class JobLease:
    """The worker's hold on one claimed job.

    `beat()` extends the lease (and with it the usage reservation's hold) and can
    record a checkpoint; a background thread calls it every `heartbeat_seconds`
    while the research runs. A heartbeat that finds the lease gone marks it lost:
    the worker must then stop without finishing or accounting, because the job
    now belongs to whoever reclaimed it.
    """

    def __init__(self, store: Any, job: dict[str, Any], worker_id: str, settings: JobSettings) -> None:
        self.store = store
        self.job_id = str(job["id"])
        self.worker_id = worker_id
        self.settings = settings
        self.cancel_requested = str(job.get("status")) == "cancel_requested"
        self.lost = False
        self.shutdown = threading.Event()
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def beat(self, checkpoint: dict[str, Any] | None = None, cost_so_far: float | None = None) -> dict[str, Any]:
        with self._lock:
            if self.lost:
                raise LeaseLost(self.job_id)
            row = self.store.heartbeat_research_job(
                self.job_id, self.worker_id, self.settings.lease_seconds, self.settings.hold_grace_seconds,
                checkpoint, cost_so_far,
            )
            if row is None:
                self.lost = True
                raise LeaseLost(self.job_id)
            if str(row.get("status")) == "cancel_requested":
                self.cancel_requested = True
            return row

    def _run(self) -> None:
        while not self._stop.wait(self.settings.heartbeat_seconds):
            try:
                self.beat()
            except LeaseLost:
                _LOG.warning("lease lost for research job %s", self.job_id)
                return
            except Exception:
                # The store is unreachable; keep trying until the lease would expire.
                _LOG.warning("heartbeat failed for research job %s; retrying", self.job_id)

    def start(self) -> "JobLease":
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, name=f"lease-{self.job_id}", daemon=True)
            self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)


def public_job_view(job: dict[str, Any]) -> dict[str, Any]:
    """What a tenant may read about their own job (no lease owner, input or reservation)."""
    status = str(job.get("status"))
    return {
        "kind": "research_job",
        "job_id": str(job.get("id")),
        "status": status,
        "done": status in TERMINAL_STATUSES,
        "job_kind": job.get("kind"),
        "case_id": job.get("case_id"),
        "idempotency_key": job.get("idempotency_key"),
        "attempts": int(job.get("attempts") or 0),
        "max_attempts": int(job.get("max_attempts") or 0),
        "run_id": job.get("result_run_id"),
        "error_code": job.get("error_code"),
        "cost_units_so_far": float(job.get("cost_so_far") or 0.0),
        "stage": (job.get("checkpoint") or {}).get("stage") if isinstance(job.get("checkpoint"), dict) else None,
        "created_at": job.get("created_at"),
        "updated_at": job.get("updated_at"),
        "finished_at": job.get("finished_at"),
    }
