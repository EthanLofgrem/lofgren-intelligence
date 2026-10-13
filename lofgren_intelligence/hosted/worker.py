"""Durable research worker: ``python -m lofgren_intelligence.hosted.worker``.

An always-on process that polls the Supabase job queue (li_research_jobs). Each
cycle it reclaims jobs whose lease expired (li_reclaim_research_jobs), settles
pending usage markers (reconcile_unsettled_usage), then claims runnable jobs up
to its concurrency limit and runs each one with PublicService.run_research_job
in a daemon thread while a lease thread heartbeats it.

Shutdown. SIGTERM/SIGINT only set a flag (the handler takes no lock and does no
I/O); the main loop polls it, stops claiming and asks running jobs to requeue at
their next stage boundary (WORKER_SHUTDOWN, no attempt used). It waits at most
LI_WORKER_SHUTDOWN_GRACE_SECONDS from the signal for them; a job still running
after that is abandoned (its lease stops heartbeating, expires and another worker
reclaims it; the abandoned attempt can neither finish nor charge) and the process
exits at once, well before the host's SIGKILL.

Store errors. A startup self-check makes one cheap authenticated call. Rejected
credentials (HTTP 401/403) or unusable configuration exit nonzero at once; during
the loop LI_WORKER_MAX_AUTH_FAILURES consecutive refused cycles exit nonzero.
Transient faults (network, 5xx) back off exponentially, logging the cause. Logs
never contain the key or the URL. ``--once`` exits nonzero when it could not
reach or authenticate to the store. See docs/WORKER_RUNTIME.md.
"""

from __future__ import annotations

import argparse
import logging
import os
import re
import signal
import socket
import sys
import threading
import time
import uuid
from typing import Any, Callable

from .jobs import JobLease, JobSettings

_LOG = logging.getLogger("lofgren_intelligence.worker")

EXIT_OK = 0
EXIT_STORE_REJECTED = 2  # auth/config: wrong key, missing configuration (retrying cannot help)
EXIT_STORE_UNREACHABLE = 3  # --once could not reach the store (transient)

_STOP_POLL_SLICE = 0.2
_URL_RE = re.compile(r"\b[a-z][a-z0-9+.\-]*://\S+", re.I)


def default_worker_id() -> str:
    return f"{socket.gethostname()[:80]}:{os.getpid()}:{uuid.uuid4().hex[:8]}"


def classify_store_error(exc: BaseException) -> str:
    """'auth' for errors retrying cannot fix (rejected credentials, missing or unusable
    configuration); 'transient' for everything else (network, timeouts, 5xx)."""
    from .store import StoreAuthError, StoreNotConfigured

    return "auth" if isinstance(exc, (StoreAuthError, StoreNotConfigured)) else "transient"


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
        # Written only by the signal handler (plain attribute stores; no lock, no I/O).
        self._stop_signalled = False
        self._stop_signalled_at: float | None = None
        self._active: dict[str, JobLease] = {}
        self._active_lock = threading.Lock()
        self._threads: set[threading.Thread] = set()
        # Store health.
        self.auth_failures = 0
        self.transient_failures = 0
        self.fatal: str | None = None
        self._cycle_errors: list[tuple[str, str, str]] = []
        self.abandoned: list[str] = []

    def _service(self) -> Any:
        service = self._service_factory(self.store)
        service.job_settings = self.settings
        return service

    # ---- store error handling ---------------------------------------------------------

    def _redact(self, text: str) -> str:
        for attr in ("service_key", "publishable_key", "url"):
            secret = getattr(self.store, attr, None)
            if isinstance(secret, str) and len(secret) >= 6:
                text = text.replace(secret, "[redacted]")
        return _URL_RE.sub("[url]", text)

    def _cause(self, exc: BaseException) -> str:
        from .store import StoreError

        if isinstance(exc, StoreError):
            return self._redact(str(exc)) or type(exc).__name__
        return type(exc).__name__

    def _store_failed(self, op: str, exc: BaseException) -> None:
        kind = classify_store_error(exc)
        cause = self._cause(exc)
        self._cycle_errors.append((kind, op, cause))
        if kind == "auth":
            _LOG.error("%s failed: the store refused the worker (%s); check SUPABASE_URL and "
                       "SUPABASE_SERVICE_ROLE_KEY", op, cause)
        else:
            _LOG.warning("%s failed: %s", op, cause)

    def _begin_cycle(self) -> None:
        self._cycle_errors = []

    def _end_cycle(self) -> list[tuple[str, str, str]]:
        errors = list(self._cycle_errors)
        if any(kind == "auth" for kind, _op, _c in errors):
            self.auth_failures += 1
            self.transient_failures = 0
            if self.auth_failures >= self.settings.max_auth_failures and self.fatal is None:
                self.fatal = next(c for kind, _op, c in errors if kind == "auth")
                _LOG.error("the store refused the worker in %s consecutive cycles (%s); exiting",
                           self.auth_failures, self.fatal)
        elif errors:
            self.auth_failures = 0
            self.transient_failures += 1
            _LOG.warning("store unavailable (%s); backing off %.2fs", errors[0][2], self.poll_delay())
        else:
            self.auth_failures = 0
            self.transient_failures = 0
        return errors

    def poll_delay(self) -> float:
        base = self.settings.poll_seconds
        if self.transient_failures <= 0:
            return base
        return min(self.settings.max_backoff_seconds, base * (2 ** min(self.transient_failures, 16)))

    def self_check(self) -> tuple[str, str] | None:
        """One cheap authenticated store call. None when it passed (or the store has no
        check); otherwise (kind, secret-free cause) with kind 'auth' or 'transient'."""
        check = getattr(self.store, "check_connection", None)
        if check is None:
            return None
        try:
            check()
        except Exception as exc:
            return classify_store_error(exc), self._cause(exc)
        return None

    # ---- one job ----------------------------------------------------------------------

    def run_job(self, job: dict[str, Any]) -> dict[str, Any]:
        """Run one claimed job under a heartbeating lease. Never raises an Exception."""
        lease = JobLease(self.store, job, self.worker_id, self.settings)
        with self._active_lock:
            self._active[lease.job_id] = lease
        # Checked after registering: either request_stop sees this lease or we see the stop.
        if self.stopping.is_set():
            lease.shutdown.set()
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
                                                                self.settings.queue_ttl_seconds,
                                                                self.settings.max_finalize_reclaims)
        except Exception as exc:
            self._store_failed("reclaiming expired research jobs", exc)
            out["reclaimed"] = None
        try:
            out["reconciled"] = self._service().reconcile_unsettled_usage()["reconciled"]
        except Exception as exc:
            self._store_failed("usage reconciliation", exc)
            out["reconciled"] = None
        return out

    def claim(self) -> dict[str, Any] | None:
        try:
            return self.store.claim_research_job(self.worker_id, self.settings.lease_seconds,
                                                 self.settings.hold_grace_seconds)
        except Exception as exc:
            self._store_failed("claiming a research job", exc)
            return None

    def run_once(self) -> dict[str, Any]:
        """One synchronous cycle: housekeeping, then claim and run jobs until none is runnable
        or `concurrency` jobs ran (used by tests and by --once). `store_errors` lists the
        (kind, operation, cause) of every store call of the cycle that failed."""
        self._begin_cycle()
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
        out["store_errors"] = self._end_cycle()
        return out

    # ---- stopping ---------------------------------------------------------------------

    def on_signal(self, signum: int = 0, frame: Any = None) -> None:
        """Signal handler: async-signal-safe. Sets flags only (no lock, no I/O, no logging);
        the main loop polls them and does the actual stop."""
        if self._stop_signalled_at is None:
            self._stop_signalled_at = time.monotonic()
        self._stop_signalled = True

    def request_stop(self, *_: Any) -> None:
        """Stop claiming and ask running jobs to requeue at their next stage boundary.
        Takes locks: call it from normal code, never from a signal handler."""
        if not self.stopping.is_set():
            _LOG.info("worker %s stopping: no new claims; running jobs requeue at their next stage",
                      self.worker_id)
        self.stopping.set()
        with self._active_lock:
            for lease in self._active.values():
                lease.shutdown.set()

    def _observe_stop(self) -> bool:
        if self._stop_signalled and not self.stopping.is_set():
            self.request_stop()
        return self.stopping.is_set()

    def _sleep(self, seconds: float) -> None:
        deadline = time.monotonic() + seconds
        while not self._observe_stop():
            left = deadline - time.monotonic()
            if left <= 0:
                return
            time.sleep(min(_STOP_POLL_SLICE, left))

    # ---- the long-running loop --------------------------------------------------------

    def _live_threads(self) -> list[threading.Thread]:
        self._threads = {t for t in self._threads if t.is_alive()}
        return list(self._threads)

    def _free_slots(self) -> int:
        return self.settings.concurrency - len(self._live_threads())

    def _spawn(self, job: dict[str, Any]) -> threading.Thread:
        # Daemon: a job still running after the shutdown grace never holds the process open.
        thread = threading.Thread(target=self.run_job, args=(job,), name=f"li-job-{job.get('id')}", daemon=True)
        self._threads.add(thread)
        thread.start()
        return thread

    def _drain(self) -> list[str]:
        """Wait for running jobs until the grace (counted from the signal) ends; abandon the rest."""
        started = self._stop_signalled_at if self._stop_signalled_at is not None else time.monotonic()
        deadline = started + self.settings.shutdown_grace_seconds
        for thread in self._live_threads():
            thread.join(max(0.0, deadline - time.monotonic()))
        if not self._live_threads():
            return []
        with self._active_lock:
            leases = list(self._active.values())
        for lease in leases:
            lease.abandon()
        ids = sorted(lease.job_id for lease in leases)
        _LOG.warning("shutdown grace of %.1fs elapsed with %s job(s) still running (%s); abandoning them: "
                     "their leases expire and another worker reclaims them", self.settings.shutdown_grace_seconds,
                     len(ids), ", ".join(ids) or "none registered")
        return ids

    def run_forever(self) -> int:
        """Run until stopped. Returns EXIT_OK, or EXIT_STORE_REJECTED after
        `max_auth_failures` consecutive cycles the store refused. Never blocks past the
        shutdown grace: unfinished jobs are abandoned to lease expiry and reclaim."""
        try:
            while not self._observe_stop():
                self._begin_cycle()
                self.housekeeping()
                while self._free_slots() > 0 and not self._observe_stop():
                    job = self.claim()
                    if job is None:
                        break
                    self._spawn(job)
                self._end_cycle()
                if self.fatal is not None:
                    break
                self._sleep(self.poll_delay())
        finally:
            self.request_stop()
            self.abandoned = self._drain()
        return EXIT_STORE_REJECTED if self.fatal is not None else EXIT_OK


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m lofgren_intelligence.hosted.worker",
                                     description="Run the durable research worker against the Supabase job queue.")
    parser.add_argument("--once", action="store_true", help="run one cycle and exit")
    parser.add_argument("--concurrency", type=int, default=None, help="override LI_WORKER_CONCURRENCY")
    args = parser.parse_args(argv)
    logging.basicConfig(level=os.environ.get("LI_WORKER_LOG_LEVEL", "INFO"),
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    from dataclasses import replace

    from . import store as store_module
    from .jobs import _float_env

    settings = JobSettings.from_env()
    if args.concurrency is not None:
        settings = replace(settings, concurrency=max(1, min(32, args.concurrency)))
    try:
        # A bounded per-call timeout keeps a signal received mid-call inside the shutdown budget.
        store = store_module.SupabaseStore(timeout=_float_env("LI_WORKER_STORE_TIMEOUT_SECONDS", 15.0, 1.0))
    except store_module.StoreError as exc:
        sys.stderr.write(f"worker cannot start: {exc}\n")
        return EXIT_STORE_REJECTED
    worker = Worker(store, settings)
    problem = worker.self_check()
    if problem is not None and problem[0] == "auth":
        _LOG.error("worker cannot start: the store refused the startup self-check (%s); check SUPABASE_URL "
                   "and SUPABASE_SERVICE_ROLE_KEY", problem[1])
        return EXIT_STORE_REJECTED
    if args.once:
        if problem is not None:
            _LOG.error("worker --once could not reach the store (%s)", problem[1])
            return EXIT_STORE_UNREACHABLE
        out = worker.run_once()
        errors = out.get("store_errors") or []
        if any(kind == "auth" for kind, _op, _c in errors):
            return EXIT_STORE_REJECTED
        if errors:
            _LOG.error("worker --once cycle had store failures (%s)", errors[0][2])
            return EXIT_STORE_UNREACHABLE
        _LOG.info("worker --once cycle complete (%s job(s) run)", len(out.get("jobs") or []))
        return EXIT_OK
    if problem is not None:
        _LOG.warning("store self-check failed (%s); the worker keeps retrying with backoff", problem[1])
    else:
        _LOG.info("store self-check passed")
    signal.signal(signal.SIGTERM, worker.on_signal)
    signal.signal(signal.SIGINT, worker.on_signal)
    _LOG.info("worker %s started (concurrency %s, lease %ss, heartbeat %ss, shutdown grace %ss)", worker.worker_id,
              settings.concurrency, settings.lease_seconds, settings.heartbeat_seconds,
              settings.shutdown_grace_seconds)
    return worker.run_forever()


def _exit_process(code: int) -> None:
    """Exit now. When an abandoned job thread is still alive, skip interpreter teardown
    (which could wait on it) after flushing the logs."""
    if any(t.is_alive() and t.name.startswith("li-job-") for t in threading.enumerate()):
        logging.shutdown()
        for stream in (sys.stdout, sys.stderr):
            try:
                stream.flush()
            except Exception:
                pass
        os._exit(code)
    raise SystemExit(code)


if __name__ == "__main__":
    _exit_process(main())
