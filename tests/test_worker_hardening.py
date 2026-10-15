"""Worker hardening (evidence type: FAKE-STORE and LOCAL-PROCESS).

  H1 shutdown grace is enforced ...................... test_h1_*
     A job that never reaches a stage boundary cannot keep the worker past
     LI_WORKER_SHUTDOWN_GRACE_SECONDS: run_forever returns, the process exits, the
     lease expires, another worker reclaims the job, and there is exactly one run and
     one charge (no partial result is ever marked complete).
  H2 store errors are classified and never silent ..... test_h2_*
     Auth/config (401/403, missing or unusable configuration) exit nonzero (at the
     startup self-check, or after LI_WORKER_MAX_AUTH_FAILURES refused cycles);
     transient faults back off with a cause; --once exits nonzero when the store was
     unreachable or refused it; no log line contains the key or the URL.
  H3 the signal handler is async-signal-safe .......... test_h3_*
     It only sets flags (no lock, no I/O); the loop does the stop and keeps the
     behaviour: no new claims, running jobs requeue at their next stage.

No network, Supabase, provider or external call is made: the real SupabaseStore is
exercised with its HTTP transport (json_request) patched.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import textwrap
import threading
import time
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

from lofgren_intelligence.hosted import store as store_module
from lofgren_intelligence.hosted import worker as worker_module
from lofgren_intelligence.hosted.http import HTTPError, JSONResponse
from lofgren_intelligence.hosted.jobs import JobLease, JobSettings
from lofgren_intelligence.hosted.store import (
    StoreAuthError,
    StoreNotConfigured,
    StoreUnavailable,
    SupabaseStore,
)
from lofgren_intelligence.hosted.worker import (
    EXIT_OK,
    EXIT_STORE_REJECTED,
    EXIT_STORE_UNREACHABLE,
    Worker,
    classify_store_error,
)

from .test_durable_jobs import SETTINGS, JobTestBase, at_stage

REPO = Path(__file__).resolve().parents[1]
FAKE_URL = "https://abcdefghijklmnop.supabase.co"
FAKE_KEY = "test-only-not-a-real-service-key-0123456789"
LOOP = JobSettings(**{**SETTINGS.__dict__, "poll_seconds": 0.05, "shutdown_grace_seconds": 0.5})


def run_in_thread(fn):
    box = {}

    def target():
        box["result"] = fn()

    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    return thread, box


# ---------------------------------------------------------------------------
# H1 shutdown grace
# ---------------------------------------------------------------------------

class ShutdownGraceTests(JobTestBase):
    def test_h1_job_stuck_inside_a_stage_is_abandoned_at_the_grace_and_reclaimed_once(self):
        job_id = self.start("stuck")["job_id"]
        entered, hold = threading.Event(), threading.Event()
        self.addCleanup(hold.set)

        def stuck(_ledger):
            entered.set()
            hold.wait(60)  # a long stage: never reaches the next boundary within the grace

        w1 = Worker(self.store, LOOP, worker_id="w1")
        with at_stage("verify", stuck):
            loop, box = run_in_thread(w1.run_forever)
            self.assertTrue(entered.wait(10), "the job never started")
            signalled = time.monotonic()
            w1.on_signal()
            loop.join(LOOP.shutdown_grace_seconds + 5)
            elapsed = time.monotonic() - signalled
            self.assertFalse(loop.is_alive(), "run_forever outlived the shutdown grace")
            self.assertLess(elapsed, LOOP.shutdown_grace_seconds + 1.5)
            self.assertGreaterEqual(elapsed, LOOP.shutdown_grace_seconds - 0.05)
            self.assertEqual(box["result"], EXIT_OK)
            self.assertEqual(w1.abandoned, [job_id])

            # Nothing partial was persisted or charged; the job is still w1's until the lease expires.
            row = self.job(job_id)
            self.assertEqual((row["status"], row["lease_owner"], row["result_run_id"]), ("running", "w1", None))
            self.assertEqual((self.store.runs, self.store.usage), ({}, []))
            self.assertEqual(self.reservation(job_id)["status"], "reserved")
            self.assertEqual(self.store.reclaim_research_jobs(10, 86_400), [], "a live lease is not reclaimed")

            # The lease expires and another worker reclaims and finishes the job once.
            self.clock.advance(SETTINGS.lease_seconds + 1)
            results = self.drain(self.worker("w2"))
            self.assertEqual([r["status"] for r in results], ["succeeded"])
            run_id = self.job(job_id)["result_run_id"]
            self.assertEqual((self.job(job_id)["attempts"], len(self.store.runs), len(self.store.usage)), (2, 1, 1))

            # The abandoned attempt wakes up later: it is fenced and can neither finish nor charge.
            zombies = [t for t in threading.enumerate() if t.name == f"li-job-{job_id}"]
            hold.set()
            for t in zombies:
                t.join(10)
                self.assertFalse(t.is_alive())
        row = self.job(job_id)
        self.assertEqual((row["status"], row["result_run_id"]), ("succeeded", run_id))
        self.assertEqual((len(self.store.runs), len(self.store.usage)), (1, 1), "no double run, no double charge")
        self.assertEqual(self.reservation(job_id)["status"], "settled")
        self.assertAlmostEqual(self.store.usage[0]["units"], row["cost_so_far"])

    def test_h1_job_reaching_a_stage_within_the_grace_requeues_without_using_an_attempt(self):
        job_id = self.start("requeue")["job_id"]
        w1 = Worker(self.store, LOOP, worker_id="w1")

        def signal_then_wait(_ledger):
            w1.on_signal()
            self.assertTrue(w1.stopping.wait(5), "the loop never observed the signal flag")

        with at_stage("verify", signal_then_wait):
            loop, box = run_in_thread(w1.run_forever)
            loop.join(10)
        self.assertFalse(loop.is_alive())
        self.assertEqual((box["result"], w1.abandoned), (EXIT_OK, []))
        row = self.job(job_id)
        self.assertEqual((row["status"], row["attempts"], row["error_code"]), ("queued", 0, "WORKER_SHUTDOWN"))
        self.assertEqual((self.store.runs, self.store.usage), ({}, []))
        self.assertEqual([r["status"] for r in self.drain(self.worker("w2"))], ["succeeded"])
        self.assertEqual((len(self.store.runs), len(self.store.usage)), (1, 1))

    def test_h1_abandoned_lease_refuses_every_later_stage_boundary_without_store_io(self):
        job_id = self.start("fence")["job_id"]
        job = self.store.claim_research_job("w1", 120, 300)
        lease = JobLease(self.store, job, "w1", SETTINGS)
        with patch.object(self.store, "heartbeat_research_job", side_effect=AssertionError("no I/O")):
            lease.abandon()
            from lofgren_intelligence.hosted.jobs import LeaseLost
            with self.assertRaises(LeaseLost):
                lease.beat({"phase": "running"}, 1.0)
        self.assertEqual(self.job(job_id)["lease_owner"], "w1")

    @unittest.skipUnless(hasattr(__import__("signal"), "raise_signal"), "needs signal.raise_signal")
    def test_h1_worker_process_exits_within_the_grace_after_sigterm(self):
        """A real `python` process running worker.main: SIGTERM while a job sleeps forever."""
        script = textwrap.dedent("""
            import os, signal, sys, threading, time
            from lofgren_intelligence.hosted import service as service_module
            from lofgren_intelligence.hosted import store as store_module
            from lofgren_intelligence.hosted import worker as worker_module

            started = threading.Event()

            class StubStore:
                url = "https://stub.invalid"
                service_key = "stub"
                def __init__(self, *a, **kw):
                    self.claimed = False
                def check_connection(self):
                    return None
                def reclaim_research_jobs(self, *a):
                    return []
                def claim_research_job(self, *a):
                    if self.claimed:
                        return None
                    self.claimed = True
                    return {"id": "job-stuck", "status": "running"}
                def heartbeat_research_job(self, *a):
                    return {"status": "running"}

            class StubService:
                def __init__(self, store):
                    self.store = store
                def reconcile_unsettled_usage(self):
                    return {"reconciled": {}}
                def run_research_job(self, job, lease):
                    started.set()
                    time.sleep(120)  # never reaches a stage boundary
                    return {"status": "succeeded"}

            store_module.SupabaseStore = StubStore
            service_module.PublicService = StubService

            def terminate():
                started.wait(30)
                time.sleep(0.3)
                print("SIGNAL_AT", time.time(), flush=True)
                signal.raise_signal(signal.SIGTERM)

            threading.Thread(target=terminate, daemon=True).start()
            worker_module._exit_process(worker_module.main([]))
        """)
        env = {k: v for k, v in os.environ.items() if not k.startswith(("SUPABASE_", "LI_"))}
        env.update({"LI_WORKER_SHUTDOWN_GRACE_SECONDS": "1", "LI_WORKER_POLL_SECONDS": "0.1",
                    "LI_WORKER_CONCURRENCY": "1", "PYTHONPATH": str(REPO)})
        began = time.time()
        proc = subprocess.run([sys.executable, "-c", script], cwd=str(REPO), env=env, capture_output=True,
                              text=True, timeout=60)
        ended = time.time()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        marks = [line.split() for line in proc.stdout.splitlines() if line.startswith("SIGNAL_AT")]
        self.assertEqual(len(marks), 1, proc.stdout + proc.stderr)
        after_signal = ended - float(marks[0][1])
        self.assertLess(after_signal, 1 + 3.0, f"exited {after_signal:.1f}s after SIGTERM (grace 1s)")
        self.assertLess(ended - began, 40, "the stuck job kept the process alive")
        self.assertIn("abandoning", proc.stderr)
        self.assertIn("job-stuck", proc.stderr)


# ---------------------------------------------------------------------------
# H2 store error classification
# ---------------------------------------------------------------------------

class _RefusingStore:
    """Every queue call fails with the given exception."""

    url = FAKE_URL
    service_key = FAKE_KEY

    def __init__(self, exc):
        self.exc = exc
        self.calls = 0

    def _fail(self, *a, **kw):
        self.calls += 1
        raise self.exc

    reclaim_research_jobs = claim_research_job = list_unsettled_usage = _fail

    def check_connection(self):
        raise self.exc


class StoreErrorClassificationTests(unittest.TestCase):
    def store(self):
        return SupabaseStore(url=FAKE_URL, service_key=FAKE_KEY)

    def assert_secret_free(self, text):
        self.assertNotIn(FAKE_KEY, text)
        self.assertNotIn(FAKE_URL, text)
        self.assertNotIn("supabase.co", text)

    def test_h2_http_401_and_403_are_auth_errors_and_5xx_or_network_are_transient(self):
        store = self.store()
        cases = [
            (HTTPError(401, "Invalid API key"), StoreAuthError, "auth"),
            (HTTPError(403, "permission denied"), StoreAuthError, "auth"),
            (HTTPError(500, "boom"), StoreUnavailable, "transient"),
            (HTTPError(503, "unavailable"), StoreUnavailable, "transient"),
            (urllib.error.URLError("name resolution failed"), StoreUnavailable, "transient"),
            (TimeoutError("timed out"), StoreUnavailable, "transient"),
        ]
        for raised, expected, kind in cases:
            with self.subTest(raised=raised), patch.object(store_module, "json_request", side_effect=raised):
                with self.assertRaises(expected) as ctx:
                    store.claim_research_job("w1", 120, 300)
                self.assertEqual(classify_store_error(ctx.exception), kind)
                self.assertIsInstance(ctx.exception, StoreUnavailable, "public endpoints still answer 503")
                self.assert_secret_free(str(ctx.exception))

    def test_h2_startup_self_check_is_one_cheap_authenticated_read(self):
        store = self.store()
        with patch.object(store_module, "json_request", return_value=JSONResponse(200, {}, [])) as call:
            store.check_connection()
        call.assert_called_once()
        url, method = call.call_args.args
        self.assertEqual(method, "GET")
        self.assertTrue(url.startswith(f"{FAKE_URL}/rest/v1/li_research_jobs?"))
        self.assertIn("limit=1", url)
        self.assertEqual(call.call_args.kwargs["headers"]["apikey"], FAKE_KEY)

    def test_h2_self_check_classifies_auth_config_and_transient_failures(self):
        store = self.store()
        cases = [
            (HTTPError(401, "Invalid API key"), StoreAuthError, "auth"),
            (HTTPError(403, "forbidden"), StoreAuthError, "auth"),
            (HTTPError(404, "relation does not exist"), StoreNotConfigured, "auth"),
            (HTTPError(400, "bad"), StoreNotConfigured, "auth"),
            (json.JSONDecodeError("html", "<html>", 0), StoreNotConfigured, "auth"),
            (ValueError("documentation endpoint"), StoreNotConfigured, "auth"),
            (HTTPError(429, "slow down"), StoreUnavailable, "transient"),
            (HTTPError(502, "bad gateway"), StoreUnavailable, "transient"),
            (urllib.error.URLError("connection refused"), StoreUnavailable, "transient"),
        ]
        for raised, expected, kind in cases:
            with self.subTest(raised=raised), patch.object(store_module, "json_request", side_effect=raised):
                with self.assertRaises(expected) as ctx:
                    store.check_connection()
                self.assertEqual(classify_store_error(ctx.exception), kind)
                self.assert_secret_free(str(ctx.exception))
        with patch.object(store_module, "json_request", return_value=JSONResponse(200, {}, {"not": "rest"})):
            with self.assertRaises(StoreNotConfigured):
                store.check_connection()

    def test_h2_repeated_auth_refusals_stop_the_loop_with_a_nonzero_code_and_a_clear_cause(self):
        settings = JobSettings(**{**LOOP.__dict__, "max_auth_failures": 3, "poll_seconds": 0.01})
        store = _RefusingStore(StoreAuthError("database rejected the service credentials (HTTP 401)"))
        worker = Worker(store, settings, worker_id="w1")
        with self.assertLogs("lofgren_intelligence.worker", level="ERROR") as logs:
            loop, box = run_in_thread(worker.run_forever)
            loop.join(10)
        self.assertFalse(loop.is_alive(), "a refused worker must exit, not loop forever")
        self.assertEqual(box["result"], EXIT_STORE_REJECTED)
        self.assertEqual(worker.auth_failures, 3)
        text = "\n".join(logs.output)
        self.assertIn("HTTP 401", text)
        self.assertIn("SUPABASE_SERVICE_ROLE_KEY", text)
        self.assertIn("3 consecutive cycles", text)
        self.assert_secret_free(text)

    def test_h2_transient_faults_back_off_exponentially_with_a_cause_and_keep_running(self):
        settings = JobSettings(**{**LOOP.__dict__, "poll_seconds": 0.01, "max_backoff_seconds": 0.04})
        store = _RefusingStore(StoreUnavailable("database is unreachable"))
        worker = Worker(store, settings, worker_id="w1")
        delays = []
        for _ in range(4):
            worker.run_once()
            delays.append(worker.poll_delay())
        self.assertEqual(delays, [0.02, 0.04, 0.04, 0.04])
        self.assertIsNone(worker.fatal)
        with self.assertLogs("lofgren_intelligence.worker", level="WARNING") as logs:
            loop, box = run_in_thread(worker.run_forever)
            time.sleep(0.3)
            worker.on_signal()
            loop.join(5)
        self.assertEqual(box["result"], EXIT_OK, "transient faults never exit the long-running worker")
        text = "\n".join(logs.output)
        self.assertIn("database is unreachable", text)
        self.assertIn("backing off", text)
        self.assert_secret_free(text)
        worker.store.exc = None  # recovery resets the backoff
        worker.store.reclaim_research_jobs = lambda *a: []
        worker.store.claim_research_job = lambda *a: None
        worker.store.list_unsettled_usage = lambda *a: []
        worker.run_once()
        self.assertEqual(worker.poll_delay(), settings.poll_seconds)

    def test_h2_log_causes_are_redacted_even_if_a_message_carried_the_key_or_url(self):
        leaky = StoreUnavailable(f"failed calling {FAKE_URL}/rest/v1/x?apikey={FAKE_KEY} for {FAKE_KEY}")
        worker = Worker(_RefusingStore(leaky), LOOP, worker_id="w1")
        with self.assertLogs("lofgren_intelligence.worker", level="WARNING") as logs:
            out = worker.run_once()
        self.assertTrue(out["store_errors"])
        self.assert_secret_free("\n".join(logs.output) + repr(out["store_errors"]))
        # A non-store exception is reported by type only (its text may hold anything).
        self.assertEqual(worker._cause(RuntimeError(FAKE_KEY)), "RuntimeError")


class MainExitCodeTests(unittest.TestCase):
    """worker.main end to end with the real SupabaseStore; only its HTTP transport is patched."""

    def setUp(self):
        env = patch.dict(os.environ, {"SUPABASE_URL": FAKE_URL, "SUPABASE_SERVICE_ROLE_KEY": FAKE_KEY,
                                      "LI_WORKER_POLL_SECONDS": "0.01", "LI_WORKER_MAX_AUTH_FAILURES": "2"})
        env.start()
        self.addCleanup(env.stop)

    def run_main(self, argv, transport):
        with patch.object(store_module, "json_request", side_effect=transport) as calls, \
                patch.object(worker_module.signal, "signal") as installed,                 patch.object(worker_module.logging, "basicConfig"), \
                self.assertLogs("lofgren_intelligence.worker", level="INFO") as logs:
            code = worker_module.main(argv)
        text = "\n".join(logs.output)
        self.assertNotIn(FAKE_KEY, text)
        self.assertNotIn(FAKE_URL, text)
        return code, text, calls, installed

    def test_h2_wrong_key_exits_nonzero_at_the_startup_self_check(self):
        for argv in ([], ["--once"]):
            with self.subTest(argv=argv):
                code, text, calls, installed = self.run_main(argv, HTTPError(401, "Invalid API key"))
                self.assertEqual(code, EXIT_STORE_REJECTED)
                self.assertEqual(calls.call_count, 1, "exits after the single self-check call")
                self.assertIn("HTTP 401", text)
                installed.assert_not_called()

    def test_h2_once_exits_nonzero_when_the_store_is_unreachable(self):
        code, text, calls, _ = self.run_main(["--once"], urllib.error.URLError("connection refused"))
        self.assertEqual(code, EXIT_STORE_UNREACHABLE)
        self.assertIn("database is unreachable", text)

    def test_h2_once_exits_nonzero_when_the_cycle_itself_fails(self):
        def transport(url, method, **kw):
            if "li_research_jobs?" in url:
                return JSONResponse(200, {}, [])  # the self-check passes
            raise HTTPError(503, "unavailable")

        code, text, _calls, _ = self.run_main(["--once"], transport)
        self.assertEqual(code, EXIT_STORE_UNREACHABLE)
        self.assertIn("HTTP 503", text)

    def test_h2_once_exits_zero_on_a_healthy_empty_queue(self):
        def transport(url, method, **kw):
            return JSONResponse(200, {}, [] if method == "GET" or "reclaim" in url or "unsettled" in url else None)

        code, _text, _calls, _ = self.run_main(["--once"], transport)
        self.assertEqual(code, EXIT_OK)

    def test_h2_key_revoked_after_start_exits_nonzero_after_n_refused_cycles(self):
        def transport(url, method, **kw):
            if "li_research_jobs?" in url:
                return JSONResponse(200, {}, [])  # valid at startup
            raise HTTPError(401, "JWT expired")

        code, text, _calls, installed = self.run_main([], transport)
        self.assertEqual(code, EXIT_STORE_REJECTED)
        self.assertIn("2 consecutive cycles", text)
        handlers = {c.args[1] for c in installed.call_args_list}
        self.assertEqual(len(handlers), 1)
        self.assertEqual(handlers.pop().__name__, "on_signal")


# ---------------------------------------------------------------------------
# H3 async-signal-safe handler
# ---------------------------------------------------------------------------

class SignalHandlerTests(JobTestBase):
    def test_h3_handler_takes_no_lock_and_does_no_io(self):
        worker = Worker(self.store, LOOP, worker_id="w1")
        # The main thread may hold the active-jobs lock when the signal arrives; the old
        # handler re-acquired that non-reentrant lock and deadlocked.
        with worker._active_lock, patch.object(worker_module._LOG, "info") as info, \
                patch.object(worker_module._LOG, "warning") as warning:
            thread, _ = run_in_thread(worker.on_signal)
            thread.join(2)
            self.assertFalse(thread.is_alive(), "the signal handler blocked on a lock")
            worker.on_signal()  # also re-entrant in the lock-holding thread itself
        info.assert_not_called()
        warning.assert_not_called()
        self.assertFalse(worker.stopping.is_set(), "the handler only sets a flag; the loop acts on it")
        self.assertTrue(worker._observe_stop())
        self.assertTrue(worker.stopping.is_set())

    def test_h3_no_new_claims_after_the_signal(self):
        job_id = self.start("unclaimed")["job_id"]
        worker = Worker(self.store, LOOP, worker_id="w1")
        worker.on_signal()
        with patch.object(self.store, "claim_research_job", side_effect=AssertionError("claimed")):
            loop, box = run_in_thread(worker.run_forever)
            loop.join(5)
        self.assertEqual(box["result"], EXIT_OK)
        self.assertEqual(self.job(job_id)["status"], "queued")

    def test_h3_stop_observed_while_idle_ends_the_loop_promptly(self):
        settings = JobSettings(**{**LOOP.__dict__, "poll_seconds": 30.0})
        worker = Worker(self.store, settings, worker_id="w1")
        loop, box = run_in_thread(worker.run_forever)
        time.sleep(0.2)
        signalled = time.monotonic()
        worker.on_signal()
        loop.join(5)
        self.assertFalse(loop.is_alive())
        self.assertLess(time.monotonic() - signalled, 1.0, "a 30s idle poll must not delay the stop")
        self.assertEqual(box["result"], EXIT_OK)


if __name__ == "__main__":
    logging.basicConfig()
    unittest.main()
