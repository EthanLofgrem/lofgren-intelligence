"""Q36 trial hardening: runtime request, time, body and redirect bounds proven against fake upstream servers on
127.0.0.1 (never the public internet); global admission and concurrency; keyed IP hashing and trusted proxies;
receipts, cost honesty and source-failure reporting; separation from private resources.

The SSRF guard refuses loopback in production. These tests allow 127.0.0.1 only by patching the trial fetcher's
resolver (and the manifest's address check) inside `allow_loopback()`; one test proves the guard still refuses it
without that patch.
"""
import ast
import contextlib
import hashlib
import hmac
import http.client
import http.server
import json
import os
import socket
import threading
import time
import unittest
import urllib.request
from pathlib import Path
from unittest.mock import patch

from starlette.testclient import TestClient

from lofgren_intelligence.adapters import manifest as manifest_module
from lofgren_intelligence.adapters.europepmc import EuropePMCAdapter
from lofgren_intelligence.adapters.public_api import ProviderUnavailable, PublicHTTPClient
from lofgren_intelligence.hosted import trial, trial_fetch, web_app
from lofgren_intelligence.hosted.store import StoreError, SupabaseStore
from lofgren_intelligence.hosted.trial_fetch import TrialFetcher, TrialFetchRefused
from lofgren_intelligence.kernel.receipt import verify_receipt

from .test_mcp_trial import ENV, HEADERS, INIT, PEER, QUESTION, TEST_IP_HMAC_KEY, TRIAL_ENV, TrialOnlyStore, rpc
from .test_public_sources import fixture

ROOT = Path(__file__).resolve().parent.parent
EPMC = fixture("europepmc_page1.json")
PAGE = (b"<html><body><h1>Zelvapril and hypertension</h1><p>Zelvapril lowered systolic blood pressure in a "
        b"fictional trial of adults with hypertension.</p></body></html>")
LIMIT = trial.BOUNDS.max_body_bytes


# ---- fake upstream servers on 127.0.0.1 --------------------------------------------------------------------

class Upstream:
    """A local HTTP server with slow, redirecting, oversized and failing routes. Counts hits and in-flight calls."""

    def __init__(self, ok_delay=0.0):
        self.hits: dict[str, int] = {}
        self.lock = threading.Lock()
        self.stop = threading.Event()
        self.in_flight = 0
        self.max_in_flight = 0
        self.ok_delay = ok_delay
        self.closed_early = 0
        upstream = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):  # noqa: N802 - http.server API
                path = self.path.split("?", 1)[0]
                with upstream.lock:
                    upstream.hits[path] = upstream.hits.get(path, 0) + 1
                    upstream.in_flight += 1
                    upstream.max_in_flight = max(upstream.max_in_flight, upstream.in_flight)
                try:
                    getattr(self, "route_" + path.strip("/").replace("-", "_"), self.route_missing)()
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
                    with upstream.lock:
                        upstream.closed_early += 1
                finally:
                    with upstream.lock:
                        upstream.in_flight -= 1

            def send(self, status, body=b"", ctype="application/json", headers=()):
                self.send_response(status)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                for k, v in headers:
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(body)

            def route_ok(self):
                if upstream.ok_delay:
                    upstream.stop.wait(upstream.ok_delay)
                self.send(200, EPMC)

            def route_page(self):
                self.send(200, PAGE, "text/html; charset=utf-8")

            def route_unavailable(self):
                self.send(503, b"{}", headers=(("Retry-After", "0"),))

            def route_redirect(self):
                self.send(302, b"", headers=(("Location", "/ok"),))

            def route_slow_headers(self):
                upstream.stop.wait(30)

            def route_drip(self):
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", "100000")
                self.end_headers()
                while not upstream.stop.is_set():
                    self.wfile.write(b" ")
                    self.wfile.flush()
                    upstream.stop.wait(0.05)

            def route_big_declared(self):
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(LIMIT + 1))
                self.end_headers()
                self.wfile.write(b" " * (LIMIT + 1))

            def route_big_unsized(self):
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()  # HTTP/1.0, no length: the body ends when the server closes
                chunk = b" " * 65536
                for _ in range(LIMIT // len(chunk) + 4):
                    self.wfile.write(chunk)

            def route_short(self):
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", "1000")
                self.end_headers()
                self.wfile.write(b'{"partial": ')  # then the server closes: a truncated body

            def route_missing(self):
                self.send(404, b"{}")

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def url(self, path):
        return f"http://127.0.0.1:{self.port}{path}"

    def total_hits(self):
        with self.lock:
            return sum(self.hits.values())

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.stop.set()
        self.server.shutdown()
        self.server.server_close()


def _loopback_only(host, port):
    if host == "127.0.0.1":
        return ["127.0.0.1"]
    raise AssertionError(f"test tried to reach a non-loopback host {host!r}")  # never the public internet


@contextlib.contextmanager
def allow_loopback():
    real_public_ip = manifest_module._public_ip
    with patch.object(trial_fetch, "resolve_public", _loopback_only), \
            patch.object(manifest_module, "_public_ip", lambda v: v == "127.0.0.1" or real_public_ip(v)):
        yield


def fetcher(deadline_s=10.0, **kw):
    return TrialFetcher(deadline_at=time.monotonic() + deadline_s, **kw)


def get(f, url):
    return f(urllib.request.Request(url, headers={"Accept": "application/json"}))


# ---- 2 and 3: the fetcher's runtime bounds -------------------------------------------------------------------

class FetcherBoundTests(unittest.TestCase):
    def setUp(self):
        self.up = Upstream()
        self.up.__enter__()
        self.addCleanup(self.up.__exit__, None, None, None)
        cm = allow_loopback()
        cm.__enter__()
        self.addCleanup(cm.__exit__, None, None, None)

    def test_one_request_per_call_is_enforced_and_counted(self):
        f = fetcher()
        with get(f, self.up.url("/ok")) as resp:
            self.assertEqual(resp.status, 200)
            self.assertEqual(resp.read(), EPMC)
        with self.assertRaisesRegex(TrialFetchRefused, "budget"):
            get(f, self.up.url("/ok"))
        self.assertEqual((f.attempts, f.refused), (1, 1))
        self.assertEqual(self.up.hits, {"/ok": 1})

    def test_redirect_is_refused_not_followed(self):
        f = fetcher()
        with self.assertRaisesRegex(TrialFetchRefused, "redirect bound is 0"):
            get(f, self.up.url("/redirect"))
        self.assertEqual(self.up.hits, {"/redirect": 1})  # the target was never requested
        self.assertEqual(f.attempts, 1)

    def test_redirect_limit_counts_against_the_request_budget(self):
        # Even with a redirect allowance, the 1-request budget stops the second hop before it is sent.
        f = fetcher(max_redirects=3)
        with self.assertRaisesRegex(TrialFetchRefused, "budget"):
            get(f, self.up.url("/redirect"))
        self.assertEqual(self.up.hits, {"/redirect": 1})

    def test_body_limit_declared_and_unsized(self):
        for path in ("/big-declared", "/big-unsized"):
            with self.subTest(path=path):
                f = fetcher()
                with self.assertRaisesRegex(ValueError, f"trial limit of {LIMIT} bytes"):
                    get(f, self.up.url(path))
                self.assertLessEqual(f.bytes_read, LIMIT + trial_fetch.READ_CHUNK)
        self.assertEqual(trial.BOUNDS.max_body_bytes, 1_000_000)

    def test_truncated_body_is_a_failure_not_a_short_success(self):
        f = fetcher()
        with self.assertRaises(http.client.IncompleteRead):
            get(f, self.up.url("/short"))
        self.assertEqual(self.up.hits, {"/short": 1})

    def test_deadline_cuts_a_server_that_never_sends_headers(self):
        f = fetcher(deadline_s=1.0, op_timeout_s=10.0)  # the socket timeout alone would wait 10 s
        start = time.monotonic()
        with self.assertRaises(TimeoutError):
            get(f, self.up.url("/slow-headers"))
        self.assertLess(time.monotonic() - start, 1.75)  # min(op timeout, time left) bounds the wait

    def test_deadline_cuts_a_drip_feeding_body(self):
        f = fetcher(deadline_s=1.5, op_timeout_s=10.0)  # every recv succeeds well inside the socket timeout
        start = time.monotonic()
        with self.assertRaises(TimeoutError):
            get(f, self.up.url("/drip"))
        elapsed = time.monotonic() - start
        self.assertGreaterEqual(elapsed, 1.4)
        self.assertLess(elapsed, 2.25)

    def test_per_operation_socket_timeout(self):
        f = fetcher(deadline_s=20.0, op_timeout_s=0.5)
        start = time.monotonic()
        with self.assertRaises(OSError):
            get(f, self.up.url("/slow-headers"))
        self.assertLess(time.monotonic() - start, 1.5)

    def test_no_retry_through_the_trial_client(self):
        client = PublicHTTPClient("t", timeout=10.0, max_retries=trial.BOUNDS.max_retries, min_interval_s=0.0,
                                  opener=fetcher())
        with self.assertRaises(ProviderUnavailable) as caught:
            client.get(self.up.url("/unavailable"))
        self.assertIn("HTTP 503 after 1 attempt", caught.exception.reason)
        self.assertEqual(self.up.hits, {"/unavailable": 1})
        self.assertEqual(trial.BOUNDS.max_retries, 0)

    def test_ssrf_guard_still_refuses_loopback_without_the_test_patch(self):
        f = fetcher()
        with patch.object(trial_fetch, "resolve_public", trial_fetch.net._resolve_public):
            with self.assertRaises(trial_fetch.net.UnsafeURL):
                get(f, self.up.url("/ok"))
            for url in ("http://localhost/x", "ftp://example.org/x", "http://user:pw@example.org/x"):
                with self.assertRaises(trial_fetch.net.UnsafeURL):
                    get(fetcher(), url)
        self.assertEqual(self.up.total_hits(), 0)


# ---- end-to-end through /mcp-trial ---------------------------------------------------------------------------

class TrialHarness(unittest.TestCase):
    env_extra: dict = {}

    def setUp(self):
        self.store = TrialOnlyStore()
        env = patch.dict(os.environ, {**TRIAL_ENV, **self.env_extra})
        env.start()
        self.addCleanup(env.stop)
        p = patch.object(trial, "SupabaseStore", lambda **kw: self.store)
        p.start()
        self.addCleanup(p.stop)
        self.up = Upstream()
        self.up.__enter__()
        self.addCleanup(self.up.__exit__, None, None, None)
        cm = allow_loopback()
        cm.__enter__()
        self.addCleanup(cm.__exit__, None, None, None)
        self.client = self.new_client()

    def new_client(self, peer=PEER):
        client = TestClient(web_app.build_app(), base_url=ENV["LI_PUBLIC_BASE_URL"], client=peer)
        client.__enter__()
        self.addCleanup(client.__exit__, None, None, None)
        return client

    def call(self, name, arguments, client=None, headers=None):
        client = client or self.client
        response = client.post(trial.TRIAL_PATH, json=rpc("tools/call", {"name": name, "arguments": arguments}, 7),
                                headers={**HEADERS, **(headers or {})})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["result"]

    def research(self, path="/ok", tool="trial_research", client=None, **arguments):
        arguments = {"question": QUESTION, "query": "zelvapril hypertension", **arguments}
        arguments = {k: v for k, v in arguments.items() if v is not None}
        if path is None:  # the caller already pointed the adapter at the fake upstream (parallel tests)
            return self.call(tool, arguments, client=client)
        with patch.object(EuropePMCAdapter, "endpoint", self.up.url(path)):
            return self.call(tool, arguments, client=client)

    def error_text(self, result):
        self.assertTrue(result.get("isError"), result)
        return result["content"][0]["text"]

    def wait_slots_released(self, limit=5.0):
        end = time.monotonic() + limit
        while time.monotonic() < end:
            with self.store.lock:
                if not self.store.slots:
                    return
            time.sleep(0.02)
        self.fail(f"trial slots were not released: {self.store.slots}")


class RequestBoundEndToEndTests(TrialHarness):
    def test_research_sends_exactly_one_upstream_request(self):
        for tool, extra in (("trial_research", {}), ("trial_verify_claim", {"question": None, "claim": QUESTION}),
                            ("trial_compare", {"options": ["zelvapril", "placebo"]})):
            with self.subTest(tool=tool):
                before = self.up.total_hits()
                result = self.research(tool=tool, **extra)
                self.assertFalse(result.get("isError"), result)
                out = result["structuredContent"]
                self.assertEqual(self.up.total_hits() - before, 1)
                self.assertEqual(out["retrieval"]["upstream_requests"], 1)
                self.assertEqual(out["retrieval"]["outcome"], "complete")
                self.assertGreater(out["retrieval"]["records"], 0)
        self.wait_slots_released()

    def test_operator_manifest_of_three_urls_fetches_exactly_one(self):
        doc = {"schema": "lofgren.sources-manifest/1", "sources": [
            {"url": self.up.url("/page") + "?a", "title": "Unrelated budget memo"},
            {"url": self.up.url("/page") + "?b", "title": "Zelvapril hypertension trial summary"},
            {"url": self.up.url("/page") + "?c", "title": "Another unrelated page"},
        ]}
        with patch.dict(os.environ, {"LI_TRIAL_SOURCES_MANIFEST": json.dumps(doc)}):
            result = self.research(source="operator", query=None)
        self.assertFalse(result.get("isError"), result)
        self.assertEqual(self.up.hits, {"/page": 1})
        out = result["structuredContent"]
        self.assertEqual(out["retrieval"]["upstream_requests"], 1)
        self.assertEqual([s["uri"] for s in out["provenance"]["sources"]], [self.up.url("/page") + "?b"])

    def test_upstream_failure_is_reported_failed_and_never_retried(self):
        result = self.research("/unavailable")
        text = self.error_text(result)
        self.assertIn("TRIAL_SOURCE_FAILED", text)
        self.assertIn("HTTP 503", text)
        self.assertNotIn("structuredContent", result)  # no empty-success envelope
        self.assertEqual(self.up.hits, {"/unavailable": 1})
        self.wait_slots_released()

    def test_redirect_and_oversized_bodies_fail_the_call(self):
        for path, needle in (("/redirect", "redirect bound is 0"), ("/big-declared", "trial limit"),
                             ("/big-unsized", "trial limit")):
            with self.subTest(path=path):
                text = self.error_text(self.research(path))
                self.assertIn("TRIAL_SOURCE_FAILED", text)
                self.assertIn(needle, text)
        self.assertNotIn("/ok", self.up.hits)  # the redirect target was never requested


class TimeBoundEndToEndTests(TrialHarness):
    def test_worst_case_wall_time_with_a_stalled_upstream(self):
        bounds = trial.TrialBounds(max_seconds=1.5)
        for path in ("/slow-headers", "/drip"):
            with self.subTest(path=path), patch.object(trial, "BOUNDS", bounds):
                start = time.monotonic()
                result = self.research(path)
                elapsed = time.monotonic() - start
                text = self.error_text(result)
                self.assertTrue("TRIAL_TIME_BOUND" in text or "TRIAL_SOURCE_FAILED" in text, text)
                self.assertLess(elapsed, 1.5 + 1.5)  # deadline + request handling slack, never the 10 s op timeout
        self.wait_slots_released()

    def test_call_answers_at_the_deadline_even_when_the_pipeline_itself_stalls(self):
        real = trial.TrialBounds(max_seconds=1.0)

        def stalled(*args, **kwargs):
            time.sleep(4.0)  # non-network work that no socket timeout can interrupt
            raise AssertionError("unreachable in time")

        with patch.object(trial, "BOUNDS", real), \
                patch("lofgren_intelligence.kernel.pipeline.run_investigation", stalled):
            start = time.monotonic()
            result = self.research()
            elapsed = time.monotonic() - start
        self.assertIn("TRIAL_TIME_BOUND", self.error_text(result))
        self.assertLess(elapsed, 2.5)

    def test_documented_worst_case_fits_the_host_limit(self):
        worst = trial.BOUNDS.max_seconds + trial.HTTP_ADMISSION_STORE_CALLS * trial.STORE_TIMEOUT_S
        self.assertEqual(worst, 50.0)
        self.assertLessEqual(worst, trial.HOST_LIMIT_S)
        self.assertEqual(json.loads((ROOT / "vercel.json").read_text())["functions"]["api/index.py"]["maxDuration"],
                         trial.HOST_LIMIT_S)
        self.assertGreater(trial.SLOT_LEASE_S, trial.BOUNDS.max_seconds)
        self.assertEqual(trial.trial_manifest()["worst_case_wall_seconds"], worst)
        doc = (ROOT / "docs/MCP_TRIAL.md").read_text(encoding="utf-8")
        self.assertIn("50 s", doc)


# ---- 4: admission and concurrency ---------------------------------------------------------------------------

class AdmissionConcurrencyTests(TrialHarness):
    env_extra = {"LI_TRIAL_IP_CALLS_PER_HOUR": "1000", "LI_TRIAL_IP_REQUESTS_PER_MINUTE": "1000"}

    def parallel(self, n, fn):
        clients = [self.new_client() for _ in range(n)]  # separate app instances sharing one store
        endpoint = patch.object(EuropePMCAdapter, "endpoint", self.up.url("/ok"))  # once, not per thread
        endpoint.start()
        self.addCleanup(endpoint.stop)
        barrier = threading.Barrier(n)
        results = [None] * n

        def run(i):
            barrier.wait(10)
            results[i] = fn(clients[i])

        threads = [threading.Thread(target=run, args=(i,)) for i in range(n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(60)
        self.assertFalse(any(t.is_alive() for t in threads))
        return results

    def codes(self, results):
        import re
        out = []
        for r in results:
            if not r.get("isError"):
                out.append("OK")
            else:
                m = re.search(r"(TRIAL_[A-Z_]+)", r["content"][0]["text"])
                out.append(m.group(1) if m else r["content"][0]["text"])
        return out

    def test_concurrent_execution_is_bounded_across_instances(self):
        self.up.ok_delay = 1.0
        with patch.dict(os.environ, {"LI_TRIAL_MAX_CONCURRENT": "2", "LI_TRIAL_SPEND_CEILING_USD": "100"}):
            results = self.parallel(6, lambda c: self.research(None, client=c))
        codes = self.codes(results)
        self.assertLessEqual(self.up.max_in_flight, 2)
        self.assertGreaterEqual(codes.count("OK"), 1)
        self.assertLessEqual(codes.count("OK"), 6)
        self.assertEqual(set(codes) - {"OK", "TRIAL_BUSY"}, set(), codes)
        self.assertGreater(codes.count("TRIAL_BUSY"), 0)
        self.assertEqual(self.up.hits.get("/ok", 0), codes.count("OK"))  # refused calls never reached upstream
        acquires = [c for c in self.store.slot_calls if c[0] == "acquire"]
        self.assertTrue(all(c[2] == 2 and c[3] == trial.SLOT_LEASE_S for c in acquires))
        self.wait_slots_released()

    def test_global_ceiling_admits_exactly_its_calls_under_parallel_load(self):
        with patch.dict(os.environ, {"LI_TRIAL_MAX_CONCURRENT": "10", "LI_TRIAL_SPEND_CEILING_USD": "4"}):
            results = self.parallel(6, lambda c: self.research(None, client=c))
        codes = self.codes(results)
        self.assertEqual(codes.count("OK"), 2, codes)  # floor(4 / 2)
        self.assertEqual(codes.count("TRIAL_CEILING_REACHED"), 4)
        self.assertEqual(self.up.hits.get("/ok", 0), 2)
        self.wait_slots_released()

    def test_default_ceiling_is_five_research_calls_per_day(self):
        self.assertEqual(trial.spend_ceiling(), (5, 86_400))
        self.assertEqual(trial.research_calls_per_day(), 5)
        with patch.dict(os.environ, {"LI_TRIAL_SPEND_CEILING_USD": "10", "LI_TRIAL_SPEND_WINDOW_SECONDS": "3600"}):
            self.assertEqual(trial.research_calls_per_day(), 120)
        results = [self.research() for _ in range(6)]
        self.assertEqual(sum(1 for r in results if not r.get("isError")), 5)
        self.assertIn("TRIAL_CEILING_REACHED", self.error_text(results[-1]))
        self.assertEqual(self.up.hits.get("/ok"), 5)

    def test_storage_outage_at_the_slot_fails_closed_before_any_work(self):
        real = self.store.acquire_trial_slot

        def outage(*args):
            raise StoreError("database is unreachable")

        self.store.acquire_trial_slot = outage
        result = self.research()
        self.assertIn("TRIAL_UNAVAILABLE", self.error_text(result))
        self.assertEqual(self.up.total_hits(), 0)
        self.assertNotIn("trial_spend", [t[0] for t in self.store.takes])
        self.store.acquire_trial_slot = real

    def test_malformed_storage_answers_fail_closed(self):
        for bad in ("0", True, -1, None, 1.5, [0, 0], {"a": 0, "b": 0}, float("nan")):
            with self.subTest(slot_answer=bad):
                self.store.acquire_trial_slot = lambda *a, bad=bad: bad
                self.assertIn("TRIAL_UNAVAILABLE", self.error_text(self.research()))
        self.assertEqual(self.up.total_hits(), 0)
        del self.store.acquire_trial_slot
        for bad in ("0", True, -1, None, float("nan"), float("inf"), {"a": 0, "b": 0}):
            with self.subTest(limiter_answer=bad):
                self.store.take_keyed_rate_limit = lambda *a, bad=bad: bad
                response = self.client.post(trial.TRIAL_PATH, json=INIT, headers=HEADERS)
                self.assertEqual(response.status_code, 503)
        self.assertEqual(self.up.total_hits(), 0)

    def test_storage_outage_at_spend_releases_the_slot(self):
        real = self.store.take_keyed_rate_limit

        def flaky(bucket, *args):
            if bucket == "trial_spend":
                raise StoreError("database is unreachable")
            return real(bucket, *args)

        self.store.take_keyed_rate_limit = flaky
        self.assertIn("TRIAL_UNAVAILABLE", self.error_text(self.research()))
        self.assertEqual(self.up.total_hits(), 0)
        self.wait_slots_released()
        self.assertEqual([c[0] for c in self.store.slot_calls], ["acquire", "release"])

    def test_release_failure_does_not_fail_the_call_and_the_lease_bounds_it(self):
        def broken(slot_id):
            raise StoreError("database is unreachable")

        self.store.release_trial_slot = broken
        result = self.research()
        self.assertFalse(result.get("isError"), result)

    def test_invalid_concurrency_configuration_fails_closed(self):
        for value in ("0", "11", "-1", "lots"):
            with self.subTest(value=value), patch.dict(os.environ, {"LI_TRIAL_MAX_CONCURRENT": value}):
                self.assertIn("TRIAL_UNAVAILABLE", self.error_text(self.research()))
        self.assertEqual(self.up.total_hits(), 0)


class SlotStoreContractTests(unittest.TestCase):
    """The real SupabaseStore methods (only the HTTP transport is replaced): malformed answers are StoreErrors."""

    def store(self, answer):
        store = SupabaseStore("https://db.example", "synthetic-service")
        store.rpc = lambda name, body: answer
        return store

    def test_acquire_accepts_only_a_non_negative_integer(self):
        self.assertEqual(self.store(0).acquire_trial_slot("s", 2, 60), 0)
        self.assertEqual(self.store([{"li_acquire_trial_slot": 7}]).acquire_trial_slot("s", 2, 60), 7)
        for bad in (True, -1, "0", None, 1.5, [], [0, 1], {"a": 1, "b": 2}):
            with self.subTest(answer=bad), self.assertRaises(StoreError):
                self.store(bad).acquire_trial_slot("s", 2, 60)

    def test_release_accepts_only_a_boolean(self):
        self.assertTrue(self.store(True).release_trial_slot("s"))
        self.assertFalse(self.store([False]).release_trial_slot("s"))
        for bad in (1, 0, "true", None, [], {"a": True, "b": False}):
            with self.subTest(answer=bad), self.assertRaises(StoreError):
                self.store(bad).release_trial_slot("s")

    def test_rpc_names_and_arguments_match_the_migration(self):
        calls = []
        store = SupabaseStore("https://db.example", "synthetic-service")
        store.rpc = lambda name, body: calls.append((name, body)) or (0 if "acquire" in name else True)
        store.acquire_trial_slot("slot", 2, 60)
        store.release_trial_slot("slot")
        self.assertEqual(calls, [
            ("li_acquire_trial_slot", {"p_slot_id": "slot", "p_max_concurrent": 2, "p_lease_seconds": 60}),
            ("li_release_trial_slot", {"p_slot_id": "slot"}),
        ])
        sql = (ROOT / "supabase/migrations/20261011120000_li_trial_slots.sql").read_text(encoding="utf-8")
        self.assertIn("li_acquire_trial_slot(\n  p_slot_id uuid,\n  p_max_concurrent integer,\n  p_lease_seconds integer",
                      sql)
        self.assertIn("pg_advisory_xact_lock", sql)

    def test_trial_store_uses_a_short_timeout(self):
        with patch.dict(os.environ, {"SUPABASE_URL": "https://db.example", "SUPABASE_SERVICE_ROLE_KEY": "x"}):
            self.assertEqual(trial._store().timeout, trial.STORE_TIMEOUT_S)


# ---- 5: client address and keyed hashing --------------------------------------------------------------------

class ClientAddressTests(TrialHarness):
    def init(self, client=None, headers=None):
        return (client or self.client).post(trial.TRIAL_PATH, json=INIT, headers={**HEADERS, **(headers or {})})

    def test_missing_or_short_hmac_key_fails_closed(self):
        for value in ("", "too-short"):
            with self.subTest(key=value), patch.dict(os.environ, {"LI_TRIAL_IP_HMAC_KEY": value}):
                response = self.init()
                self.assertEqual(response.status_code, 503)
        self.assertFalse(self.store.takes)

    def test_per_ip_keys_are_hmac_under_the_secret(self):
        self.assertEqual(self.init().status_code, 200)
        ip_hash = next(k for b, k, _, _ in self.store.takes if b == "trial_ip")
        expected = hmac.new(TEST_IP_HMAC_KEY.encode(), b"trial_ip:198.51.100.7", hashlib.sha256).hexdigest()
        self.assertEqual(ip_hash, expected)
        self.assertNotEqual(ip_hash, hashlib.sha256(b"trial_ip:198.51.100.7").hexdigest())
        with patch.dict(os.environ, {"LI_TRIAL_IP_HMAC_KEY": "another-test-only-key-0123456789abcdef"}):
            self.assertNotEqual(trial.trial_key_hash("trial_ip", "198.51.100.7"), ip_hash)
        self.assertNotIn("198.51.100.7", json.dumps(self.store.takes))

    def test_global_keys_never_depend_on_the_client(self):
        self.assertEqual(trial.trial_key_hash("trial_global", "operator"),
                         hashlib.sha256(b"trial_global:operator").hexdigest())
        with self.assertRaises(ValueError):
            trial.trial_key_hash("trial_spend", "198.51.100.7")
        with patch.dict(os.environ, {"LI_TRIAL_GLOBAL_REQUESTS_PER_MINUTE": "1", "VERCEL": "1"}):
            self.assertEqual(self.init(headers={"x-vercel-forwarded-for": "203.0.113.1"}).status_code, 200)
            self.assertEqual(self.init(headers={"x-vercel-forwarded-for": "203.0.113.2"}).status_code, 429)

    def test_spend_ceiling_holds_across_client_addresses(self):
        with patch.dict(os.environ, {"VERCEL": "1", "LI_TRIAL_SPEND_CEILING_USD": "2"}),                 patch.object(EuropePMCAdapter, "endpoint", self.up.url("/ok")):
            a = self.call("trial_research", {"question": QUESTION, "query": "zelvapril"},
                          headers={"x-vercel-forwarded-for": "203.0.113.1"})
            b = self.call("trial_research", {"question": QUESTION, "query": "zelvapril"},
                          headers={"x-vercel-forwarded-for": "203.0.113.99"})
        self.assertFalse(a.get("isError"), a)
        self.assertIn("TRIAL_CEILING_REACHED", self.error_text(b))

    def test_forwarding_headers_are_ignored_off_vercel(self):
        spoof = {"x-forwarded-for": "203.0.113.50", "x-vercel-forwarded-for": "203.0.113.51",
                 "x-real-ip": "203.0.113.52"}
        with patch.dict(os.environ, {"LI_TRIAL_IP_REQUESTS_PER_MINUTE": "1", "LI_CLIENT_IP_HEADER": "x-real-ip"}):
            self.assertEqual(self.init(headers=spoof).status_code, 200)
            rotated = {k: v.replace(".5", ".7") for k, v in spoof.items()}
            self.assertEqual(self.init(headers=rotated).status_code, 429)  # same socket peer, same bucket
        ip_hashes = {k for b, k, _, _ in self.store.takes if b == "trial_ip"}
        self.assertEqual(ip_hashes, {trial.trial_key_hash("trial_ip", "198.51.100.7")})

    def test_vercel_headers_are_used_on_vercel_in_documented_order(self):
        with patch.dict(os.environ, {"VERCEL": "1", "LI_TRIAL_IP_REQUESTS_PER_MINUTE": "1"}):
            self.assertEqual(self.init(headers={"x-vercel-forwarded-for": "203.0.113.1",
                                                "x-forwarded-for": "203.0.113.9"}).status_code, 200)
            self.assertEqual(self.init(headers={"x-vercel-forwarded-for": "203.0.113.2"}).status_code, 200)
            self.assertEqual(self.init(headers={"x-vercel-forwarded-for": "203.0.113.1"}).status_code, 429)
            self.assertEqual(self.init(headers={"x-forwarded-for": "203.0.113.3, 10.0.0.1"}).status_code, 200)
            for headers in ({}, {"x-vercel-forwarded-for": "not-an-ip"}):
                with self.subTest(headers=headers):
                    self.assertEqual(self.init(headers=headers).status_code, 503)
        hashes = [k for b, k, _, _ in self.store.takes if b == "trial_ip"]
        for ip in ("203.0.113.1", "203.0.113.2", "203.0.113.3"):
            self.assertIn(trial.trial_key_hash("trial_ip", ip), hashes)
        self.assertNotIn(trial.trial_key_hash("trial_ip", "203.0.113.9"), hashes)

    def test_trusting_vercel_headers_outside_vercel_is_refused(self):
        with patch.dict(os.environ, {"LI_TRIAL_TRUSTED_PROXY": "vercel", "VERCEL": ""}):
            self.assertEqual(self.init(headers={"x-vercel-forwarded-for": "203.0.113.1"}).status_code, 503)
        with patch.dict(os.environ, {"LI_TRIAL_TRUSTED_PROXY": "cloudflare"}):
            self.assertEqual(self.init().status_code, 503)
        with patch.dict(os.environ, {"LI_TRIAL_TRUSTED_PROXY": "none", "VERCEL": "1",
                                     "LI_TRIAL_IP_REQUESTS_PER_MINUTE": "1"}):
            self.assertEqual(self.init(headers={"x-vercel-forwarded-for": "203.0.113.1"}).status_code, 200)
            self.assertEqual(self.init(headers={"x-vercel-forwarded-for": "203.0.113.2"}).status_code, 429)

    def test_ipv6_clients_are_keyed_by_their_slash_64(self):
        self.assertEqual(trial._normalise_ip("2001:db8:1:2:aaaa::1"), "2001:db8:1:2::/64")
        self.assertEqual(trial._normalise_ip("2001:db8:1:2:ffff::9"), "2001:db8:1:2::/64")
        self.assertEqual(trial._normalise_ip("::ffff:203.0.113.4"), "203.0.113.4")
        self.assertIsNone(trial._normalise_ip("testclient"))
        client = self.new_client(peer=("testclient", 1))
        self.assertEqual(self.init(client=client).status_code, 503)  # no usable address: fail closed

    def test_docs_explain_shared_egress_and_trusted_proxies(self):
        doc = (ROOT / "docs/MCP_TRIAL.md").read_text(encoding="utf-8")
        for needle in ("LI_TRIAL_IP_HMAC_KEY", "x-vercel-forwarded-for", "shared egress", "IP is not a user",
                       "never trusted"):
            self.assertIn(needle, doc)


# ---- 6: receipts, output and cost honesty --------------------------------------------------------------------

class ReceiptOutputCostTests(TrialHarness):
    def test_receipts_verify_and_survive_output_shedding(self):
        compared = self.research(tool="trial_compare", options=["zelvapril", "placebo"])
        out = compared["structuredContent"]
        self.assertTrue(verify_receipt(out["receipt"]))
        from lofgren_intelligence.discovery.receipt import verify_discovery_receipt
        self.assertTrue(verify_discovery_receipt(out["discovery"]["receipt"]))
        self.assertLessEqual(len(json.dumps(out).encode()), trial.BOUNDS.max_output_bytes + 2_000)
        with patch.object(trial, "BOUNDS", trial.TrialBounds(max_output_bytes=60_000)):
            tight = self.research(tool="trial_compare", options=["zelvapril", "placebo"])
        if not tight.get("isError"):
            t = tight["structuredContent"]
            self.assertTrue(verify_receipt(t["receipt"]))
            self.assertIn("receipt", t["discovery"])
            self.assertTrue(t["output_shed"])

    def test_an_unverifiable_receipt_is_refused(self):
        with patch("lofgren_intelligence.kernel.receipt.verify_receipt", lambda r: False):
            text = self.error_text(self.research())
        self.assertIn("TRIAL_RECEIPT_INVALID", text)

    def test_partial_retrieval_is_reported_as_partial(self):
        doc = json.loads(EPMC)
        doc["resultList"]["result"].append({"title": None})  # one malformed record
        with patch(__name__ + ".EPMC", json.dumps(doc).encode()):
            result = self.research()
        self.assertFalse(result.get("isError"), result)
        out = result["structuredContent"]
        self.assertEqual(out["retrieval"]["outcome"], "partial")
        self.assertIn("malformed", out["warnings"][0])

    def test_cost_separates_platform_price_from_infrastructure_cost_and_never_claims_measurement(self):
        out = self.research()["structuredContent"]
        cost = out["cost"]
        price, infra = cost["platform_price_estimate"], cost["infrastructure_cost_estimate"]
        self.assertEqual((price["kind"], price["measured"]), ("estimate", False))
        self.assertEqual((infra["kind"], infra["measured"]), ("estimate", False))
        self.assertLessEqual(price["usd"], trial.BOUNDS.max_estimated_usd)
        self.assertFalse(infra["fully_priced"])  # no operator rates configured: unpriced, never counted as zero
        self.assertIn("infra_per_run", infra["unpriced_components"])
        self.assertNotIn("estimate", out)
        with patch.dict(os.environ, {"LI_RETRIEVAL_USD_PER_CALL": "0.0001", "LI_INFRA_USD_PER_RUN": "0.002"}):
            priced = self.research()["structuredContent"]["cost"]["infrastructure_cost_estimate"]
        self.assertTrue(priced["fully_priced"])
        self.assertGreater(priced["known_usd"], 0)
        self.assertLess(priced["known_usd"], price["usd"])

    def test_docs_state_calls_per_day_and_estimates(self):
        doc = (ROOT / "docs/MCP_TRIAL.md").read_text(encoding="utf-8")
        for needle in ("5 research calls per rolling 24 hours", "platform-price estimate",
                       "infrastructure-cost estimate", "never a measured cost", "TRIAL_SOURCE_FAILED",
                       "TRIAL_BUSY", "LI_TRIAL_MAX_CONCURRENT"):
            self.assertIn(needle, doc)


# ---- 7: separation from private resources --------------------------------------------------------------------

FORBIDDEN_IMPORTS = ("service", "jobs", "worker", "stripe", "cases", "auth", "entitlements", "console", "economics",
                     "journey", "site", "billing.catalog", "billing.stripe", "mcp_sdk")


class SeparationTests(TrialHarness):
    def test_trial_modules_import_nothing_private(self):
        for name in ("trial.py", "trial_fetch.py"):
            tree = ast.parse((ROOT / "lofgren_intelligence/hosted" / name).read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom):
                    mod = (node.module or "")
                    targets = [mod] + [f"{mod}.{a.name}" for a in node.names]
                    for t in targets:
                        stripped = t.lstrip(".")
                        for bad in FORBIDDEN_IMPORTS:
                            with self.subTest(module=name, target=t):
                                self.assertFalse(stripped == bad or stripped.endswith("." + bad)
                                                 or stripped.startswith(bad + "."), t)

    def test_tools_never_reach_service_jobs_billing_or_tokens(self):
        from lofgren_intelligence.hosted import mcp_sdk, service

        def forbidden(*args, **kwargs):
            raise AssertionError("the trial reached a private surface")

        with patch.object(service.PublicService, "__init__", forbidden), \
                patch.object(mcp_sdk.OpaqueTokenVerifier, "verify_token", forbidden):
            for tool, extra in (("trial_research", {}), ("trial_verify_claim", {"question": None, "claim": QUESTION}),
                                ("trial_compare", {"options": ["zelvapril", "placebo"]})):
                with self.subTest(tool=tool):
                    result = self.research(tool=tool, case_id="case-of-u1", job_id="job-of-u1",
                                           user_id="u1", **extra)
                    self.assertFalse(result.get("isError"), result)
                    dumped = json.dumps(result)
                    self.assertNotIn("case-of-u1", dumped)
                    self.assertNotIn("job-of-u1", dumped)
        # TrialOnlyStore raises on anything but the limiter and the slot table: no Case, run, job or billing row.
        self.assertTrue(self.store.takes)

    def test_private_tools_resources_and_prompts_are_unreachable(self):
        headers = {**HEADERS, "Authorization": "Bearer a-real-looking-token"}
        for name in ("list_cases", "get_case", "approve_case", "get_research_job", "cancel_research_job",
                     "create_checkout", "billing_portal", "propose_action", "execute_action", "delete_account"):
            with self.subTest(tool=name):
                response = self.client.post(trial.TRIAL_PATH, headers=headers, json=rpc(
                    "tools/call", {"name": name, "arguments": {"case_id": "c1", "job_id": "j1"}}))
                result = response.json()["result"]
                self.assertTrue(result["isError"])
                self.assertIn("ACCOUNT_REQUIRED", result["content"][0]["text"])
        listed = self.client.post(trial.TRIAL_PATH, headers=headers, json=rpc("resources/list", {})).json()
        self.assertEqual(listed.get("result", {}).get("resources", []), [])
        read = self.client.post(trial.TRIAL_PATH, headers=headers,
                                json=rpc("resources/read", {"uri": "li://cases/case-of-u1"})).json()
        self.assertIn("error", read)
        prompts = self.client.post(trial.TRIAL_PATH, headers=headers, json=rpc("prompts/list", {})).json()
        self.assertEqual(prompts.get("result", {}).get("prompts", []), [])
        self.assertEqual(self.client.post("/mcp", headers=HEADERS, json=rpc("tools/list", {})).status_code, 401)
        self.assertEqual(self.up.total_hits(), 0)


if __name__ == "__main__":
    unittest.main()
