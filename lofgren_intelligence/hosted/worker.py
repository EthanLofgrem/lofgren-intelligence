"""Durable research worker: ``python -m lofgren_intelligence.hosted.worker``.

An always-on process that polls the Supabase job queue (li_research_jobs). Each
cycle it reclaims jobs whose lease expired (li_reclaim_research_jobs), settles
pending usage markers (reconcile_unsettled_usage), then claims runnable jobs up
to its concurrency limit and runs each one with PublicService.run_research_job
while a lease thread heartbeats it. SIGTERM/SIGINT stop claiming; running jobs
are asked to requeue at their next stage boundary (WORKER_SHUTDOWN, no attempt
used) and the process waits up to LI_WORKER_SHUTDOWN_GRACE_SECONDS for them.
See docs/WORKER_RUNTIME.md.
"""

from __future__ import annotations

import argparse
import logging
import os
import signal
import socket
import sys
import threading
import uuid
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Any, Callable

from .jobs import JobLease, JobSettings

_LOG = logging.getLogger("lofgren_intelligence.worker")


def default_worker_id() -> str:
    return f"{socket.gethostname()[:80]}:{os.getpid()}:{uuid.uuid4().hex[:8]}"


class Worker:
    def __init__(
        self,
        store: Any,
        settings: JobSettings | None = None,
        *,
        worker_id: str | None = None,
        service_factory: Callable[[Any], Any] | None = None,
    ) -> None:
        from .service import PublicService

        self.store = store
        self.settings = settings or JobSettings.from_env()
        self.worker_id = worker_id or default_worker_id()
        self._service_factory = service_factory or PublicService
        self.stopping = threading.Event()
        self._active: dict[str, JobLease] = {}
        self._active_lock = threading.Lock()
        self._futures: set[Future] = set()
        self._pool: ThreadPoolExecutor | None = None

    def _service(self) -> Any:
        service = self._service_factory(self.store)
        service.job_settings = self.settings
        return service

    # ---- one job ----------------------------------------------------------------------

    def run_job(self, job: dict[str, Any]) -> dict[str, Any]:
        """Run one claimed job under a heartbeating lease. Never raises an Exception."""
        lease = JobLease(self.store, job, self.worker_id, self.settings)
        if self.stopping.is_set():
            lease.shutdown.set()
        with self._active_lock:
            self._active[lease.job_id] = lease
        lease.start()
        try:
            return self._service().run_research_job(job, lease)
        except Exception:
            # An unexpected crash: stop heartbeating; the lease expires and the job is reclaimed.
            _LOG.exception("research job %s crashed in the worker", lease.job_id)
            return {"status": "crashed"}
        finally:
            lease.stop()
            with self._active_lock:
                self._active.pop(lease.job_id, None)

    # ---- housekeeping -----------------------------------------------------------------

    def housekeeping(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        try:
            out["reclaimed"] = self.store.reclaim_research_jobs(self.settings.reclaim_limit,
                                                                self.settings.queue_ttl_seconds)
        except Exception:
            _LOG.warning("reclaiming expired research jobs failed; retrying next cycle")
            out["reclaimed"] = None
        try:
            out["reconciled"] = self._service().reconcile_unsettled_usage()["reconciled"]
        except Exception:
            _LOG.warning("usage reconciliation failed; retrying next cycle")
            out["reconciled"] = None
        return out

    def claim(self) -> dict[str, Any] | None:
        try:
            return self.store.claim_research_job(self.worker_id, self.settings.lease_seconds,
                                                 self.settings.hold_grace_seconds)
        except Exception:
            _LOG.warning("claiming a research job failed; retrying next cycle")
            return None

    def run_once(self) -> dict[str, Any]:
        """One synchronous cycle: housekeeping, then claim and run jobs until none is runnable
        or `concurrency` jobs ran (used by tests and by --once)."""
        out = self.housekeeping()
        results = []
        for _ in range(self.settings.concurrency):
            if self.stopping.is_set():
                break
            job = self.claim()
            if job is None:
                break
            results.append(self.run_job(job))
        out["jobs"] = results
        return out

    # ---- the long-running loop --------------------------------------------------------

    def _free_slots(self) -> int:
        self._futures = {f for f in self._futures if not f.done()}
        return self.settings.concurrency - len(self._futures)

    def request_stop(self, *_: Any) -> None:
        if not self.stopping.is_set():
            _LOG.info("worker %s stopping: no new claims; running jobs requeue at their next stage",
                      self.worker_id)
        self.stopping.set()
        with self._active_lock:
            for lease in self._active.values():
                lease.shutdown.set()

    def run_forever(self) -> int:
        self._pool = ThreadPoolExecutor(max_workers=self.settings.concurrency, thread_name_prefix="li-job")
        try:
            while not self.stopping.is_set():
                self.housekeeping()
                while self._free_slots() > 0 and not self.stopping.is_set():
                    job = self.claim()
                    if job is None:
                        break
                    self._futures.add(self._pool.submit(self.run_job, job))
                self.stopping.wait(self.settings.poll_seconds)
        finally:
            self.request_stop()
            pending = [f for f in self._futures if not f.done()]
            if pending:
                from concurrent.futures import wait
                wait(pending, timeout=self.settings.shutdown_grace_seconds)
            self._pool.shutdown(wait=False, cancel_futures=True)
        return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m lofgren_intelligence.hosted.worker",
                                     description="Run the durable research worker against the Supabase job queue.")
    parser.add_argument("--once", action="store_true", help="run one cycle and exit")
    parser.add_argument("--concurrency", type=int, default=None, help="override LI_WORKER_CONCURRENCY")
    args = parser.parse_args(argv)
    logging.basicConfig(level=os.environ.get("LI_WORKER_LOG_LEVEL", "INFO"),
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    from dataclasses import replace

    from .store import StoreError, SupabaseStore

    settings = JobSettings.from_env()
    if args.concurrency is not None:
        settings = replace(settings, concurrency=max(1, min(32, args.concurrency)))
    try:
        store = SupabaseStore()
    except StoreError as exc:
        sys.stderr.write(f"worker cannot start: {exc}\n")
        return 2
    worker = Worker(store, settings)
    if args.once:
        worker.run_once()
        return 0
    signal.signal(signal.SIGTERM, worker.request_stop)
    signal.signal(signal.SIGINT, worker.request_stop)
    _LOG.info("worker %s started (concurrency %s, lease %ss, heartbeat %ss)", worker.worker_id,
              settings.concurrency, settings.lease_seconds, settings.heartbeat_seconds)
    return worker.run_forever()


if __name__ == "__main__":
    raise SystemExit(main())
