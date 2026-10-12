"""Anonymous /mcp-trial: allowlisted noauth tools over the real LI pipeline, bounded and fail closed."""
import json
import os
import unittest
from pathlib import Path
from unittest.mock import patch

from starlette.testclient import TestClient

from lofgren_intelligence.hosted import ratelimit, trial, web_app
from lofgren_intelligence.hosted.store import StoreError
from lofgren_intelligence.kernel.receipt import verify_receipt

from .test_public_hardening import ENV
from .test_public_hosted import FakeStore
from .test_public_sources import fixture

ROOT = Path(__file__).resolve().parent.parent
TRIAL_ENV = {**ENV, "LI_DEPLOYMENT_SURFACE": "mcp", "LI_TRIAL_ENABLED": "true",
             "SUPABASE_SERVICE_ROLE_KEY": "service-test-key"}
HEADERS = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
QUESTION = "What does published literature say about zelvapril for hypertension?"
EUROPEPMC = "lofgren_intelligence.adapters.public_api.PublicHTTPClient.get_json"


class TrialOnlyStore:
    """Exposes only the keyed limiter. Any other store access (cases, runs, jobs, billing) fails the test."""

    def __init__(self):
        self.inner = FakeStore()
        self.takes = []
        self.error = None

    def take_keyed_rate_limit(self, bucket, key_hash, limit, window_seconds):
        if self.error is not None:
            raise self.error
        self.takes.append((bucket, key_hash, limit, window_seconds))
        return self.inner.take_keyed_rate_limit(bucket, key_hash, limit, window_seconds)

    def __getattr__(self, name):
        raise AssertionError(f"the trial must not touch store.{name}")


def rpc(method, params=None, id_=1):
    body = {"jsonrpc": "2.0", "id": id_, "method": method}
    if params is not None:
        body["params"] = params
    return body


INIT = rpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                          "clientInfo": {"name": "trial-test", "version": "1"}})


class TrialTestBase(unittest.TestCase):
    def setUp(self):
        self.store = TrialOnlyStore()
        self.env = patch.dict(os.environ, TRIAL_ENV)
        self.env.start()
        self.addCleanup(self.env.stop)
        p = patch.object(trial, "SupabaseStore", lambda: self.store)
        p.start()
        self.addCleanup(p.stop)
        self.client = TestClient(web_app.build_app(), base_url=ENV["LI_PUBLIC_BASE_URL"])
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)

    def post(self, body, headers=None, path=trial.TRIAL_PATH):
        return self.client.post(path, json=body, headers={**HEADERS, **(headers or {})})

    def call(self, name, arguments, headers=None):
        response = self.post(rpc("tools/call", {"name": name, "arguments": arguments}, 7), headers)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["result"]

    def research(self, tool="trial_research", **arguments):
        arguments = {k: v for k, v in {"question": QUESTION, "query": "zelvapril hypertension", **arguments}.items()
                     if v is not None}
        with patch(EUROPEPMC, return_value=json.loads(fixture("europepmc_page1.json"))) as fetch:
            result = self.call(tool, arguments)
        return result, fetch


class AnonymousSurfaceTests(TrialTestBase):
    def test_anonymous_initialize_and_tools_list_show_only_noauth_allowlist(self):
        init = self.post(INIT)
        self.assertEqual(init.status_code, 200)
        self.assertEqual(init.headers["cache-control"], "no-store")
        self.assertEqual(init.json()["result"]["serverInfo"]["name"], "Lofgren Intelligence Trial")
        listed = self.post(rpc("tools/list", {}, 2))
        self.assertEqual(listed.status_code, 200)
        tools = listed.json()["result"]["tools"]
        self.assertEqual(sorted(t["name"] for t in tools), sorted(trial.TRIAL_TOOLS))
        for tool in tools:
            with self.subTest(tool=tool["name"]):
                self.assertEqual(tool["securitySchemes"], [{"type": "noauth"}])
                self.assertEqual(tool["_meta"]["securitySchemes"], [{"type": "noauth"}])
                self.assertTrue(tool["_meta"]["li/trial_only"])
                self.assertTrue(tool["annotations"]["readOnlyHint"])
                self.assertFalse(tool["annotations"]["destructiveHint"])

    def test_authenticated_mcp_still_returns_401_with_discovery(self):
        denied = self.post(rpc("tools/list", {}), path="/mcp")
        self.assertEqual(denied.status_code, 401)
        self.assertIn("resource_metadata=", denied.headers["www-authenticate"])
        self.assertEqual(self.client.get("/.well-known/oauth-protected-resource").json()["resource"],
                         ENV["LI_PUBLIC_BASE_URL"] + "/mcp")
        root = self.client.get("/").json()
        self.assertEqual(root["resource"], ENV["LI_PUBLIC_BASE_URL"] + "/mcp")
        self.assertEqual(root["trial"]["tools"], list(trial.TRIAL_TOOLS))

    def test_no_account_discovery_advertises_enforced_input_limits(self):
        listed = self.post(rpc("tools/list", {}, 2))
        self.assertEqual(listed.status_code, 200)
        for item in listed.json()["result"]["tools"]:
            props = item["inputSchema"]["properties"]
            text = props.get("question", props.get("claim"))
            self.assertEqual(text["minLength"], 1)
            self.assertEqual(text["maxLength"], trial.BOUNDS.max_question_chars)
            self.assertEqual(props["source"]["enum"], list(trial.TRIAL_SOURCES))
            query = next(b for b in props["query"]["anyOf"] if b["type"] == "string")
            self.assertEqual(query["maxLength"], trial.BOUNDS.max_query_chars)
            if "options" in props:
                self.assertEqual(props["options"]["minItems"], 2)
                self.assertEqual(props["options"]["maxItems"], trial.BOUNDS.max_options)
                self.assertEqual(props["options"]["items"]["maxLength"], trial.BOUNDS.max_option_chars)

    def test_no_account_current_protocol_lists_only_restricted_tools(self):
        version = "2026-07-28"
        headers = {"MCP-Protocol-Version": version, "Mcp-Method": "tools/list"}
        params = {"_meta": {"io.modelcontextprotocol/protocolVersion": version,
                            "io.modelcontextprotocol/clientInfo": {"name": "audit", "version": "1"},
                            "io.modelcontextprotocol/clientCapabilities": {}}}
        listed = self.post(rpc("tools/list", params, 2), headers)
        self.assertEqual(listed.status_code, 200)
        self.assertEqual(sorted(t["name"] for t in listed.json()["result"]["tools"]), sorted(trial.TRIAL_TOOLS))
        self.assertNotIn("Mcp-Session-Id", listed.headers)
        self.assertNotIn("WWW-Authenticate", listed.headers)
        bad = self.post(rpc("tools/list", params, 3),
                        {"MCP-Protocol-Version": version, "Mcp-Method": "tools/call"})
        self.assertEqual(bad.status_code, 400)
        self.assertEqual(bad.json()["error"]["code"], -32020)
        self.assertIn("mcp-method header does not match", bad.json()["error"]["message"])

    def test_trial_is_disabled_by_default_and_only_on_mcp_surface(self):
        for env in ({"LI_TRIAL_ENABLED": ""}, {"LI_DEPLOYMENT_SURFACE": "full"}):
            with self.subTest(env=env), patch.dict(os.environ, env):
                with TestClient(web_app.build_app(), base_url=ENV["LI_PUBLIC_BASE_URL"]) as client:
                    response = client.post(trial.TRIAL_PATH, json=INIT, headers=HEADERS)
                    self.assertIn(response.status_code, (404, 405))
                    self.assertEqual(client.post("/mcp", json=INIT, headers=HEADERS).status_code, 401)
        with patch.dict(os.environ, {"LI_TRIAL_ENABLED": "yes"}):
            with self.assertRaisesRegex(RuntimeError, "LI_TRIAL_ENABLED"):
                web_app.build_app()

    def test_protected_tools_are_refused_with_account_limitation(self):
        for name in ("investigate", "start_research", "get_job_status", "case_status", "account_status",
                     "create_checkout", "execute_action", "propose_action", "discover", "unknown_tool"):
            with self.subTest(name=name):
                result = self.call(name, {"job_id": "job-of-another-user", "case_id": "case-of-another-user"})
                self.assertTrue(result["isError"])
                self.assertIn("ACCOUNT_REQUIRED", result["content"][0]["text"])
                self.assertIn("/mcp", result["content"][0]["text"])
                self.assertEqual(result["_meta"]["li/account_required"]["resource"],
                                 ENV["LI_PUBLIC_BASE_URL"] + "/mcp")


class RealPipelineTests(TrialTestBase):
    def test_research_runs_the_real_pipeline_with_inline_receipt(self):
        result, fetch = self.research()
        self.assertFalse(result.get("isError"), result)
        out = result["structuredContent"]
        self.assertEqual(fetch.call_count, 1)  # one source, one page
        self.assertTrue(out["trial"])
        self.assertEqual(out["identity"], "anonymous")
        self.assertFalse(out["persisted"])
        self.assertTrue(out["receipt_intact"])
        self.assertTrue(verify_receipt(out["receipt"]))
        self.assertTrue(out["result"]["findings"])
        self.assertIn("europepmc", json.dumps(out["provenance"]["sources"]).lower())
        self.assertTrue(out["limitations"])
        self.assertIn("report_markdown", out["artifacts"])
        self.assertLessEqual(out["estimate"]["total_usd"], trial.BOUNDS.max_estimated_usd)
        self.assertEqual(out["provenance"]["provider"]["name"], "heuristic")

    def test_verify_claim_and_compare_return_native_receipts(self):
        verified, _ = self.research("trial_verify_claim", question=None, claim="Zelvapril lowers blood pressure")
        self.assertFalse(verified.get("isError"), verified)
        self.assertEqual(verified["structuredContent"]["kind"], "claim_verification")
        self.assertTrue(verified["structuredContent"]["receipt_intact"])
        compared, _ = self.research("trial_compare", options=["zelvapril", "placebo"],
                                    question="Which lowers blood pressure more?")
        self.assertFalse(compared.get("isError"), compared)
        out = compared["structuredContent"]
        self.assertTrue(out["receipt_intact"])
        self.assertTrue(out["discovery"]["receipt_intact"])
        self.assertEqual(out["discovery"]["summary"]["research_id"], out["receipt"]["research_id"])

    def test_consequential_objective_returns_clarification_without_research(self):
        result, fetch = self.research(question="Should our hospital switch all hypertension patients to zelvapril?")
        out = result["structuredContent"]
        fetch.assert_not_called()
        self.assertTrue(out["approval_required"])
        self.assertFalse(out["researched"])
        self.assertTrue(out["clarification"]["questions"])
        self.assertNotIn("trial_spend", [t[0] for t in self.store.takes])

    def test_session_id_and_tokens_and_arguments_give_no_identity(self):
        with patch(EUROPEPMC, return_value=json.loads(fixture("europepmc_page1.json"))):
            result = self.call("trial_research", {"question": QUESTION, "query": "zelvapril hypertension",
                                                  "case_id": "case-of-u1", "user_id": "u1", "job_id": "job-of-u1"},
                               headers={"Mcp-Session-Id": "session-of-u1", "Authorization": "Bearer stolen"})
        out = result["structuredContent"]
        self.assertEqual(out["identity"], "anonymous")
        dumped = json.dumps(out)
        for value in ("case-of-u1", "job-of-u1", "session-of-u1", "stolen"):
            self.assertNotIn(value, dumped)
        # TrialOnlyStore raises on any access but the limiter: no Case, run, job, token or billing lookup happened.
        self.assertTrue(self.store.takes)


class BoundsTests(TrialTestBase):
    def test_input_and_source_bounds(self):
        cases = (
            {"question": "x" * (trial.BOUNDS.max_question_chars + 1)},
            {"query": "q" * (trial.BOUNDS.max_query_chars + 1)},
            {"source": "brave"},
            {"source": "operator"},  # not enabled by the operator
        )
        for extra in cases:
            with self.subTest(extra=list(extra)):
                result, fetch = self.research(**extra)
                self.assertTrue(result["isError"])
                self.assertIn("TRIAL_INVALID_INPUT", result["content"][0]["text"])
                fetch.assert_not_called()
        result, _ = self.research("trial_compare", options=["a"])
        self.assertTrue(result["isError"])

    def test_record_and_page_bounds_reach_the_adapter(self):
        registry = trial._registry("clinicaltrials", "zelvapril")
        adapter = registry.get("clinicaltrials")
        self.assertEqual((adapter.max_records, adapter.max_pages), (5, 1))
        self.assertEqual((adapter.client.timeout, adapter.client.max_retries), (10.0, 0))

    def test_estimated_cost_bound_refuses_before_any_work(self):
        with patch.object(trial, "BOUNDS", trial.TrialBounds(max_estimated_usd=0.01)):
            result, fetch = self.research()
        self.assertIn("TRIAL_BOUND_EXCEEDED", result["content"][0]["text"])
        fetch.assert_not_called()
        self.assertNotIn("trial_spend", [t[0] for t in self.store.takes])

    def test_time_bound(self):
        with patch.object(trial, "BOUNDS", trial.TrialBounds(max_seconds=-1)):
            result, fetch = self.research()
        self.assertIn("TRIAL_TIME_BOUND", result["content"][0]["text"])
        fetch.assert_not_called()

    def test_output_bound_sheds_report_then_refuses(self):
        with patch.object(trial, "BOUNDS", trial.TrialBounds(max_output_bytes=40_000)):
            result, _ = self.research()
        out = result.get("structuredContent")
        if out is not None:  # fits after shedding: the receipt is never dropped
            self.assertIn("receipt", out)
            self.assertIn("report_markdown", out["output_shed"])
            self.assertLessEqual(len(json.dumps(out).encode()), 40_000 + 200)
        with patch.object(trial, "BOUNDS", trial.TrialBounds(max_output_bytes=1_000)):
            result, _ = self.research()
        self.assertIn("TRIAL_OUTPUT_BOUND", result["content"][0]["text"])


class AbuseControlTests(TrialTestBase):
    def test_per_ip_request_limit_is_a_429(self):
        with patch.dict(os.environ, {"LI_TRIAL_IP_REQUESTS_PER_MINUTE": "2"}):
            self.assertEqual(self.post(INIT).status_code, 200)
            self.assertEqual(self.post(INIT).status_code, 200)
            limited = self.post(INIT)
        self.assertEqual(limited.status_code, 429)
        self.assertEqual(limited.json()["error"], "rate_limited")
        self.assertIn("Retry-After", limited.headers)

    def test_global_request_limit_is_a_429(self):
        with patch.dict(os.environ, {"LI_TRIAL_GLOBAL_REQUESTS_PER_MINUTE": "1"}):
            self.assertEqual(self.post(INIT).status_code, 200)
            self.assertEqual(self.post(INIT, headers={"X-Forwarded-For": "203.0.113.9"}).status_code, 429)

    def test_per_ip_tool_call_allowance(self):
        with patch.dict(os.environ, {"LI_TRIAL_IP_CALLS_PER_HOUR": "1"}):
            first, _ = self.research()
            second, fetch = self.research()
        self.assertFalse(first.get("isError"))
        self.assertIn("TRIAL_RATE_LIMITED", second["content"][0]["text"])
        fetch.assert_not_called()

    def test_global_spend_ceiling_exhaustion(self):
        with patch.dict(os.environ, {"LI_TRIAL_SPEND_CEILING_USD": "2.0"}):  # one worst-case call
            first, _ = self.research()
            second, fetch = self.research()
        self.assertFalse(first.get("isError"))
        self.assertIn("TRIAL_CEILING_REACHED", second["content"][0]["text"])
        fetch.assert_not_called()
        with patch.dict(os.environ, {"LI_TRIAL_SPEND_CEILING_USD": "0"}):
            third, _ = self.research()
        self.assertIn("TRIAL_CEILING_REACHED", third["content"][0]["text"])

    def test_invalid_ceiling_configuration_fails_closed(self):
        for value in ("lots", "-1", "nan"):
            with self.subTest(value=value), patch.dict(os.environ, {"LI_TRIAL_SPEND_CEILING_USD": value}):
                result, fetch = self.research()
                self.assertIn("TRIAL_UNAVAILABLE", result["content"][0]["text"])
                fetch.assert_not_called()

    def test_store_outage_fails_closed(self):
        self.store.error = StoreError("database is unreachable")
        response = self.post(INIT)
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["error"], "temporarily_unavailable")

    def test_store_outage_during_a_call_fails_closed(self):
        real = self.store.take_keyed_rate_limit

        def flaky(bucket, *args):
            if bucket == "trial_spend":
                raise StoreError("database is unreachable")
            return real(bucket, *args)

        self.store.take_keyed_rate_limit = flaky
        result, fetch = self.research()
        self.assertIn("TRIAL_UNAVAILABLE", result["content"][0]["text"])
        fetch.assert_not_called()

    def test_unconfigured_store_fails_closed_without_memory_fallback(self):
        def missing():
            raise StoreError("SUPABASE_URL is required")

        with patch.object(trial, "SupabaseStore", missing), \
                patch.dict(os.environ, {"LI_RATE_LIMIT_BACKEND": "memory"}):
            self.assertEqual(self.post(INIT).status_code, 503)

    def test_stored_keys_are_hashed_and_minimal(self):
        self.research()
        self.assertTrue(self.store.takes)
        for bucket, key_hash, _, _ in self.store.takes:
            self.assertTrue(bucket.startswith("trial_"))
            self.assertRegex(key_hash, r"^[0-9a-f]{64}$")
        self.assertNotIn("testclient", json.dumps(self.store.takes))
        self.assertEqual(ratelimit.rate_key("trial_spend", "operator"),
                         next(k for b, k, _, _ in self.store.takes if b == "trial_spend"))
        self.assertTrue({"trial_ip", "trial_global", "trial_call_ip", "trial_spend"} <= ratelimit.FAIL_CLOSED_BUCKETS)


class AdditionalBoundTests(TrialTestBase):
    def test_oversize_request_refused_before_accounting_or_pipeline(self):
        response = self.client.post(trial.TRIAL_PATH, content=b"x" * (trial.BOUNDS.max_request_bytes + 1),
                                    headers=HEADERS)
        self.assertEqual(response.status_code, 413)
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertEqual(self.store.takes, [])

    def test_capacity_refused_before_research_and_released_after_failure(self):
        import threading
        slots = threading.BoundedSemaphore(1)
        slots.acquire()
        with patch.object(trial, "_call_slots", slots):
            result, fetch = self.research()
            self.assertIn("TRIAL_BUSY", result["content"][0]["text"])
            fetch.assert_not_called()
            slots.release()
            with patch.object(trial, "_research", side_effect=trial.TrialRefused("TEST", "failure")):
                failed, _ = self.research()
                self.assertTrue(failed["isError"])
            self.assertTrue(slots.acquire(blocking=False))
            slots.release()

    def test_late_result_is_not_delivered_as_success(self):
        token = trial._client_ip.set("test-client")
        self.addCleanup(trial._client_ip.reset, token)
        deadline = trial._Deadline(1, clock=lambda: 0)
        def run(_):
            deadline.clock = lambda: 2
            return {}
        with patch.object(trial, "_Deadline", return_value=deadline), patch.object(trial, "_clarification", return_value=None):
            with self.assertRaisesRegex(trial.TrialRefused, "TRIAL_TIME_BOUND"):
                trial._trial_call("trial_research", "bounded question", run, "https://li.invalid")

    def test_parallel_call_is_refused_while_first_pipeline_is_active(self):
        import concurrent.futures
        import threading
        started, finish = threading.Event(), threading.Event()
        slots = threading.BoundedSemaphore(1)
        def run(_):
            started.set()
            if not finish.wait(2):
                raise AssertionError("test barrier timed out")
            return {}
        def invoke():
            token = trial._client_ip.set("isolated-test")
            try:
                return trial._trial_call("trial_research", "question", run, "https://li.invalid")
            finally:
                trial._client_ip.reset(token)
        with patch.object(trial, "_call_slots", slots), patch.object(trial, "_take_call_allowance"), \
             patch.object(trial, "_clarification", return_value=None), patch.object(trial, "_bounded_output", side_effect=lambda x:x):
            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
                first = pool.submit(invoke)
                self.assertTrue(started.wait(1))
                try:
                    with self.assertRaisesRegex(trial.TrialRefused, "TRIAL_BUSY"):
                        pool.submit(invoke).result(timeout=1)
                finally:
                    finish.set()
                self.assertEqual(first.result(timeout=1), {})

    def test_multiple_operator_urls_fail_before_fetch(self):
        manifest = {"schema": "lofgren.sources-manifest/1", "sources": [{"url": "https://example.org/a"}, {"url": "https://example.org/b"}]}
        with patch.dict(os.environ, {"LI_TRIAL_SOURCES_MANIFEST": json.dumps(manifest)}):
            with patch("lofgren_intelligence.adapters.public_api.PublicHTTPClient.get") as fetch:
                result = self.call("trial_research", {"question": "What is reported?", "source": "operator"})
                self.assertTrue(result["isError"])
                self.assertIn("TRIAL_UNAVAILABLE", result["content"][0]["text"])
                fetch.assert_not_called()


class AbuseBoundaryRegressionTests(TrialTestBase):
    def test_forged_forwarding_headers_do_not_rotate_client_allowance(self):
        with patch.dict(os.environ, {"LI_CLIENT_IP_HEADER": "", "LI_TRIAL_IP_REQUESTS_PER_MINUTE": "1"}):
            first = self.post(INIT, {"X-Forwarded-For": "8.8.8.8", "X-Real-IP": "8.8.8.8"})
            second = self.post(INIT, {"X-Forwarded-For": "1.1.1.1", "X-Real-IP": "1.1.1.1"})
        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 429)

    def test_invalid_store_answers_fail_closed_before_research(self):
        for value in (float("nan"), float("inf"), -1, "invalid", None):
            with self.subTest(value=value), patch.object(self.store, "take_keyed_rate_limit", return_value=value), \
                 patch(EUROPEPMC) as fetch:
                self.assertEqual(self.post(INIT).status_code, 503)
                fetch.assert_not_called()

    def test_operator_private_urls_refused_before_fetch_or_spend(self):
        for url in ("http://127.0.0.1/private", "http://169.254.169.254/latest/meta-data/",
                    "http://[::1]/private", "https://localhost/private", "https://user:pass@example.org/"):
            manifest = {"schema": "lofgren.sources-manifest/1", "sources": [{"url": url}]}
            self.store.takes.clear()
            with self.subTest(url=url), patch.dict(os.environ, {"LI_TRIAL_SOURCES_MANIFEST": json.dumps(manifest)}), \
                 patch("lofgren_intelligence.adapters.public_api.PublicHTTPClient.get") as fetch:
                result = self.call("trial_research", {"question": "What is reported?", "source": "operator"})
                self.assertTrue(result["isError"])
                self.assertIn("TRIAL_UNAVAILABLE", result["content"][0]["text"])
                fetch.assert_not_called()
                self.assertNotIn("trial_spend", [take[0] for take in self.store.takes])


class BodyDeadlineTests(unittest.IsolatedAsyncioTestCase):
    async def test_stalled_body_is_refused_without_starting_mcp(self):
        import asyncio
        from dataclasses import replace
        from unittest.mock import AsyncMock
        inner, send = AsyncMock(), AsyncMock()
        async def receive():
            await asyncio.Event().wait()
        scope = {"type": "http", "method": "POST", "path": "/mcp-trial", "headers": []}
        with patch.object(trial, "BOUNDS", replace(trial.BOUNDS, body_timeout_s=0.01)):
            await trial.TrialEndpoint(inner)(scope, receive, send)
        inner.assert_not_called()
        self.assertEqual(send.call_args_list[0].args[0]["status"], 408)


class ManifestAndDocsTests(unittest.TestCase):
    def test_capability_manifest_lists_trial_tools_as_trial_only(self):
        from lofgren_intelligence.hosted import capabilities
        checked_in = json.loads((ROOT / "docs/CAPABILITIES.json").read_text(encoding="utf-8"))
        section = checked_in["trial"]
        self.assertEqual(section, trial.trial_manifest())
        self.assertEqual([t["name"] for t in section["tools"]], sorted(trial.TRIAL_TOOLS))
        for tool in section["tools"]:
            self.assertTrue(tool["trial_only"])
            self.assertEqual(tool["security_schemes"], [{"type": "noauth"}])
        self.assertFalse(set(trial.TRIAL_TOOLS) & set(capabilities.TOOL_LEVELS))
        self.assertFalse(set(trial.TRIAL_TOOLS) & set(capabilities.registered_tools()))

    def test_docs_describe_trial_only_tools_and_unverified_chatgpt(self):
        doc = (ROOT / "docs/MCP_TRIAL.md").read_text(encoding="utf-8")
        for name in trial.TRIAL_TOOLS:
            self.assertRegex(doc, rf"`{name}`[^\n]*trial-only")
        self.assertIn("has NOT been done", doc)
        self.assertIn("/mcp-trial", doc)


if __name__ == "__main__":
    unittest.main()

