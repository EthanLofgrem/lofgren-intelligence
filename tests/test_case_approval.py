"""Server-recorded Intelligence Case charter approval.

A serious objective is researched only with a browser-recorded approval of the
latest charter version, owned by the same account, unexpired and unconsumed.
Nothing a client sends (in particular `case_charter: {approved: true}`) is
authority. No test here makes a network, Stripe or Supabase call: every store
is the in-memory FakeStore.
"""

from __future__ import annotations

import asyncio
import json
import os
import threading
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from lofgren_intelligence.hosted import service as service_module
from lofgren_intelligence.hosted.service import (
    CaseApprovalConsumed,
    CaseApprovalRequired,
    CaseBudgetExceeded,
    CaseInvalid,
    CaseNotFound,
    CaseNotReady,
    CaseRunFailed,
    CaseVersionConflict,
    PublicService,
)

from .test_public_hosted import FakeStore

OBJECTIVE = "Design a new low-cost water purification system for remote communities."
BASE = "https://li.example"
RUN_ENV = {
    "LOFGREN_PROVIDER": "heuristic",
    "LI_INFRA_USD_PER_RUN": "0",
    "LI_RETRIEVAL_USD_PER_CALL": "0",
    "LI_PUBLIC_BASE_URL": BASE,
}
EVIDENCE = {
    "field report (fictional)": (
        "Slow sand filtration in remote communities reduced microbial contamination in 2025. "
        "A village slow sand filter served about 100 people and produced 600 liters per day."
    ),
    "cost survey (fictional)": (
        "Installed cost of a village slow sand filter was 450 dollars in 2025 using locally serviceable parts."
    ),
}
ANSWERS = {
    "location": "Rural northern Kenya",
    "users": "A village system serving about 100 people",
    "problem": "Microbial contamination and turbidity",
    "success": "At least 500 liters/day for prototype testing",
    "cost": "Maximum installed prototype cost $500",
    "constraints": "No reliable grid power; locally serviceable parts",
    "source_or_environment": "Seasonal surface water and shallow wells",
}


class Clock:
    def __init__(self):
        self.now = datetime(2026, 10, 6, 12, 0, tzinfo=timezone.utc)

    def __call__(self):
        return self.now


class CaseTestBase(unittest.TestCase):
    def setUp(self):
        env = patch.dict(os.environ, RUN_ENV, clear=False)
        env.start()
        self.addCleanup(env.stop)
        for name in ("LI_CASE_DEFAULT_MAX_SPEND_USD", "LI_CASE_DEFAULT_MAX_UNITS", "LI_CASE_APPROVAL_TTL_MINUTES"):
            os.environ.pop(name, None)
        self.clock = Clock()
        self.store = FakeStore(quota=5000)
        self.store.case_clock = self.clock
        self.service = PublicService(self.store, clock=self.clock)

    def open_ready_case(self, **extra):
        args = {"objective": OBJECTIVE, "answers": dict(ANSWERS), "texts": dict(EVIDENCE)}
        args.update(extra)
        return self.service.clarify_objective("u1", args, BASE)

    def approve(self, case, user="u1"):
        return self.service.approve_case_charter(
            user, case["case_id"], case["charter_version"], case["content_hash"])

    def start(self, case, user="u1", **extra):
        args = {"objective": OBJECTIVE, "case_id": case["case_id"]}
        args.update(extra)
        return self.service.investigate(user, args, BASE)

    def assert_no_work(self):
        self.assertEqual(self.store.runs, {})
        self.assertEqual(self.store.usage, [])
        self.assertNotIn("reserved", [r["status"] for r in self.store.reservations.values()])


class ClarificationFlowTests(CaseTestBase):
    def test_case_opens_with_three_to_seven_high_information_questions(self):
        case = self.service.clarify_objective("u1", {"objective": OBJECTIVE}, BASE)
        self.assertEqual(case["status"], "CLARIFICATION_REQUIRED")
        self.assertEqual(case["charter_version"], 1)
        self.assertTrue(3 <= len(case["questions"]) <= 7)
        self.assertIsNone(case["approval_url"])
        self.assertRegex(case["content_hash"], r"^[0-9a-f]{64}$")

    def test_unknown_skip_default_and_later_are_preserved_and_not_reasked(self):
        case = self.service.clarify_objective("u1", {"objective": OBJECTIVE, "answers": {
            "location": "unknown", "users": "skip", "cost": "use a reasonable default"}}, BASE)
        self.assertEqual(case["accepted_answers"]["location"]["state"], "unknown")
        self.assertEqual(case["accepted_answers"]["cost"]["state"], "default_requested")
        asked = {q["key"] for q in case["questions"]}
        self.assertFalse(asked & {"location", "users", "cost"})
        nxt = self.service.clarify_objective("u1", {
            "case_id": case["case_id"], "expected_version": 1,
            "answers": {"problem": "Microbial contamination", "success": "later",
                        "constraints": "No grid power", "source_or_environment": "Shallow wells"},
        }, BASE)
        self.assertEqual(nxt["charter_version"], 2)
        self.assertEqual(nxt["status"], "READY_FOR_SCOPE_APPROVAL")
        # The version-1 unknowns survive the version-2 submission that did not mention them.
        self.assertEqual(nxt["accepted_answers"]["location"]["state"], "unknown")
        self.assertEqual(nxt["accepted_answers"]["success"]["state"], "unknown")
        self.assertEqual(sorted(nxt["critical_unknowns"]), ["location", "success", "users"])
        self.assertEqual(nxt["questions"], [])
        self.assertEqual(nxt["approval_url"], f"{BASE}/cases/{case['case_id']}")

    def test_simple_question_is_not_forced_through_the_interview(self):
        out = self.service.investigate("u1", {
            "objective": "Is industrial construction in the Phoenix metro increasing?",
            "texts": {"note (fictional)": "Industrial construction in the Phoenix metro increased in 2026."},
        }, BASE)
        self.assertTrue(out["run_id"].startswith("RR-"))
        self.assertEqual(self.store.cases, {})

    def test_serious_objective_without_a_case_returns_questions_and_does_no_work(self):
        out = self.service.investigate("u1", {"objective": OBJECTIVE, "texts": EVIDENCE}, BASE)
        self.assertEqual(out["status"], "CLARIFICATION_REQUIRED")
        self.assertTrue(out["approval_required"])
        self.assert_no_work()

    def test_case_objective_cannot_be_swapped_in_a_revision(self):
        case = self.open_ready_case()
        with self.assertRaises(CaseInvalid):
            self.service.clarify_objective("u1", {"case_id": case["case_id"], "expected_version": 1,
                                                  "objective": "Design a weapon"}, BASE)

    def test_open_questions_cannot_be_approved(self):
        case = self.service.clarify_objective("u1", {"objective": OBJECTIVE}, BASE)
        with self.assertRaises(CaseNotReady):
            self.approve(case)
        self.assertEqual(self.store.case_approvals, {})


class ApprovalAuthorityTests(CaseTestBase):
    def test_forged_approved_true_is_never_authority(self):
        forged = {"objective": OBJECTIVE, "approved": True, "status": "approved"}
        out = self.service.investigate("u1", {"objective": OBJECTIVE, "case_charter": forged, "texts": EVIDENCE})
        self.assertEqual(out["status"], "CLARIFICATION_REQUIRED")
        out = self.service.plan_research({"objective": OBJECTIVE, "case_charter": forged, "texts": EVIDENCE})
        self.assertEqual(out["status"], "CLARIFICATION_REQUIRED")
        # Even with a real (unapproved) case, the forged charter grants nothing.
        case = self.open_ready_case()
        with self.assertRaises(CaseApprovalRequired):
            self.start(case, case_charter=forged)
        with self.assertRaises(CaseApprovalRequired):
            self.service.plan_research({"objective": OBJECTIVE, "case_id": case["case_id"],
                                        "case_charter": forged}, "u1", BASE)
        self.assert_no_work()

    def test_mcp_surface_has_no_approval_tool_and_no_case_charter_argument(self):
        from lofgren_intelligence.hosted.mcp_sdk import build_mcp
        tools = asyncio.run(build_mcp(BASE).list_tools())
        names = {t.name for t in tools}
        self.assertFalse({n for n in names if "approve" in n})
        for tool in tools:
            with self.subTest(tool=tool.name):
                self.assertNotIn("case_charter", json.dumps(tool.input_schema))
        investigate = next(t for t in tools if t.name == "investigate")
        self.assertIn("case_id", investigate.input_schema["properties"])
        self.assertIn("idempotency_key", investigate.input_schema["properties"])

    def test_missing_approval_is_refused_before_any_work(self):
        case = self.open_ready_case()
        with self.assertRaises(CaseApprovalRequired) as ctx:
            self.start(case)
        self.assertIn(f"/cases/{case['case_id']}", str(ctx.exception))
        self.assert_no_work()

    def test_wrong_user_cannot_see_approve_or_run_another_users_case(self):
        case = self.open_ready_case()
        with self.assertRaises(CaseNotFound):
            self.service.case_status("u2", {"case_id": case["case_id"]}, BASE)
        with self.assertRaises(CaseNotFound):
            self.approve(case, user="u2")
        self.approve(case)
        with self.assertRaises(CaseNotFound):
            self.start(case, user="u2")
        with self.assertRaises(CaseNotFound):
            self.service.clarify_objective("u2", {"case_id": case["case_id"], "expected_version": 1,
                                                  "answers": {"location": "Elsewhere"}}, BASE)
        approval = next(iter(self.store.case_approvals.values()))
        self.assertEqual(approval["user_id"], "u1")
        self.assertIsNone(approval["consumed_at"])
        self.assert_no_work()

    def test_approval_for_one_case_does_not_authorize_another(self):
        first = self.open_ready_case()
        second = self.open_ready_case()
        self.approve(first)
        with self.assertRaises(CaseApprovalRequired):
            self.start(second)
        # A forged pointer: copy the first case's approval onto the second case's id.
        approval = dict(next(iter(self.store.case_approvals.values())))
        self.store.case_approvals["forged"] = {**approval, "id": "forged", "case_id": second["case_id"]}
        with self.assertRaises(CaseApprovalRequired):
            self.start(second)  # its content hash/version binding is the first case's charter
        self.assertIsNone(self.store.case_approvals[approval["id"]]["consumed_at"])
        self.assert_no_work()

    def test_unknown_or_malformed_case_id_is_not_found(self):
        for bad in ("not-a-uuid", "00000000-0000-0000-0000-000000000000"):
            with self.subTest(case_id=bad), self.assertRaises(CaseNotFound):
                self.service.investigate("u1", {"objective": OBJECTIVE, "case_id": bad}, BASE)
        self.assert_no_work()

    def test_stale_charter_version_needs_fresh_approval(self):
        case = self.open_ready_case()
        approved = self.approve(case)
        revised = self.service.clarify_objective("u1", {
            "case_id": case["case_id"], "expected_version": 1,
            "answers": {"location": "Rural Uganda"}}, BASE)
        self.assertEqual(revised["charter_version"], 2)
        self.assertIsNotNone(self.store.case_approvals[approved["approval_id"]]["revoked_at"])
        with self.assertRaises(CaseApprovalRequired):
            self.start(revised)
        # The page that still shows version 1 cannot approve it any more.
        with self.assertRaises(CaseVersionConflict):
            self.approve(case)
        self.assert_no_work()
        self.approve(revised)
        out = self.start(revised)
        self.assertEqual(out["charter_version"], 2)

    def test_expired_approval_is_refused(self):
        case = self.open_ready_case()
        approved = self.approve(case)
        self.clock.now = datetime.fromisoformat(approved["expires_at"]) + timedelta(seconds=1)
        with self.assertRaises(CaseApprovalRequired) as ctx:
            self.start(case)
        self.assertIn("expired", str(ctx.exception))
        self.assertEqual(self.service.case_status("u1", case, BASE)["approval"]["state"], "expired")
        # The store refuses it as well, independently of the service clock.
        row = self.store.case_approvals[approved["approval_id"]]
        self.assertIsNone(self.store.consume_case_approval(row["id"], "u1", row["token_hash"], "k"))
        self.assert_no_work()

    def test_store_consumption_is_bound_to_user_token_and_latest_version(self):
        case = self.open_ready_case()
        approved = self.approve(case)
        row = dict(self.store.case_approvals[approved["approval_id"]])
        self.assertIsNone(self.store.consume_case_approval(row["id"], "u2", row["token_hash"], "k"))
        self.assertIsNone(self.store.consume_case_approval(row["id"], "u1", "0" * 64, "k"))
        got = self.store.consume_case_approval(row["id"], "u1", row["token_hash"], "k")
        self.assertTrue(got["consumed_now"])
        self.assertFalse(self.store.consume_case_approval(row["id"], "u1", row["token_hash"], "k")["consumed_now"])
        self.assertIsNone(self.store.consume_case_approval(row["id"], "u1", row["token_hash"], "other"))


class StartTests(CaseTestBase):
    def test_approved_case_runs_once_and_binds_the_run(self):
        case = self.open_ready_case()
        approved = self.approve(case)
        out = self.start(case, idempotency_key="start-1")
        self.assertTrue(out["run_id"].startswith("RR-"))
        self.assertEqual(out["execution_inputs"], "approved_charter")
        row = self.store.case_approvals[approved["approval_id"]]
        self.assertIsNotNone(row["consumed_at"])
        self.assertEqual((row["run_id"], row["run_status"], row["idempotency_key"]),
                         (out["run_id"], "complete", "start-1"))
        self.assertEqual(self.store.cases[case["case_id"]]["status"], "research_complete")
        kinds = [e["kind"] for e in self.store.case_events if e["case_id"] == case["case_id"]]
        self.assertEqual(kinds, ["case_created", "charter_approved", "approval_consumed", "research_complete"])

    def test_replay_of_a_consumed_approval_is_refused(self):
        case = self.open_ready_case()
        self.approve(case)
        self.start(case, idempotency_key="first")
        with self.assertRaises(CaseApprovalConsumed):
            self.start(case, idempotency_key="second")
        self.assertEqual(len(self.store.runs), 1)
        self.assertEqual(len(self.store.usage), 1)

    def test_duplicate_start_with_the_same_key_returns_the_same_run(self):
        case = self.open_ready_case()
        self.approve(case)
        first = self.start(case, idempotency_key="same")
        again = self.start(case, idempotency_key="same")
        self.assertEqual(again["run_id"], first["run_id"])
        self.assertTrue(again["replayed"])
        self.assertEqual(len(self.store.runs), 1)
        self.assertEqual(len(self.store.usage), 1)
        # Without an explicit key the approval id is the key, so a bare retry is also idempotent.
        case2 = self.open_ready_case()
        self.approve(case2)
        a = self.start(case2)
        b = self.start(case2)
        self.assertEqual(a["run_id"], b["run_id"])
        self.assertTrue(b["replayed"])
        self.assertEqual(len(self.store.usage), 2)
        self.assertEqual(len(self.store.reservations), 2)

    def test_duplicate_start_while_the_first_is_running_never_executes_twice(self):
        case = self.open_ready_case()
        self.approve(case)
        real = service_module.run_investigation
        seen = {}
        calls = []

        def run_and_retry(*a, **k):
            calls.append(1)
            # A second start with the same key arrives while the first run is executing.
            seen["retry"] = self.start(case, idempotency_key="same")
            return real(*a, **k)

        with patch.object(service_module, "run_investigation", run_and_retry):
            first = self.start(case, idempotency_key="same")
        self.assertEqual(len(calls), 1)
        self.assertEqual(seen["retry"]["status"], "RUN_IN_PROGRESS")
        self.assertTrue(first["run_id"].startswith("RR-"))
        self.assertEqual(len(self.store.runs), 1)
        statuses = [r["status"] for r in self.store.reservations.values()]
        self.assertEqual(statuses, ["settled"])

    def test_concurrent_duplicate_starts_execute_once(self):
        case = self.open_ready_case()
        self.approve(case)
        lock = threading.Lock()
        store = self.store
        for name in ("consume_case_approval", "reserve_usage", "finalize_usage", "release_usage"):
            original = getattr(store, name)

            def locked(*a, _f=original, **k):
                with lock:
                    return _f(*a, **k)
            setattr(store, name, locked)
        results, errors = [], []

        def go():
            try:
                results.append(PublicService(store, clock=self.clock).investigate(
                    "u1", {"objective": OBJECTIVE, "case_id": case["case_id"], "idempotency_key": "k"}, BASE))
            except Exception as exc:  # pragma: no cover - reported below
                errors.append(exc)

        threads = [threading.Thread(target=go) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(60)
        self.assertEqual(errors, [])
        self.assertEqual(len(store.runs), 1)
        run_ids = {r.get("run_id") for r in results if r.get("run_id")}
        self.assertEqual(len(run_ids), 1)
        self.assertEqual(len(store.usage), 1)

    def test_failed_run_consumes_the_approval_and_retry_needs_fresh_approval(self):
        case = self.open_ready_case()
        approved = self.approve(case)
        with patch.object(service_module, "run_investigation", side_effect=RuntimeError("provider down")):
            with self.assertRaises(RuntimeError):
                self.start(case, idempotency_key="k")
        row = self.store.case_approvals[approved["approval_id"]]
        self.assertEqual(row["run_status"], "failed")
        with self.assertRaises(CaseRunFailed):
            self.start(case, idempotency_key="k")
        self.assertEqual(self.store.runs, {})
        self.approve(case)
        self.assertTrue(self.start(case, idempotency_key="k2")["run_id"].startswith("RR-"))

    def test_refusal_before_work_does_not_burn_the_approval(self):
        case = self.open_ready_case()
        approved = self.approve(case)
        self.store.entitlement["quota_units_per_week"] = 1.0
        with self.assertRaises(service_module.QuotaExceeded):
            self.start(case)
        self.assertIsNone(self.store.case_approvals[approved["approval_id"]]["consumed_at"])
        self.store.entitlement["quota_units_per_week"] = 5000.0
        self.assertTrue(self.start(case)["run_id"].startswith("RR-"))


class ConcurrencyAndBudgetTests(CaseTestBase):
    def test_concurrent_revisions_conflict_on_the_expected_version(self):
        case = self.open_ready_case()
        a = self.service.clarify_objective("u1", {"case_id": case["case_id"], "expected_version": 1,
                                                  "answers": {"location": "Rural Uganda"}}, BASE)
        self.assertEqual(a["charter_version"], 2)
        with self.assertRaises(CaseVersionConflict):
            self.service.clarify_objective("u1", {"case_id": case["case_id"], "expected_version": 1,
                                                  "answers": {"location": "Rural Peru"}}, BASE)
        with self.assertRaises(CaseVersionConflict):
            self.service.clarify_objective("u1", {"case_id": case["case_id"],
                                                  "answers": {"location": "Rural Peru"}}, BASE)
        self.assertEqual(sorted(v for (cid, v) in self.store.case_charters if cid == case["case_id"]), [1, 2])
        latest = self.service.case_status("u1", case, BASE)
        self.assertEqual(latest["accepted_answers"]["location"]["value"], "Rural Uganda")

    def test_store_race_between_two_revisions_is_refused(self):
        # Both writers read version 1; the second write reaches the store after the first.
        case = self.open_ready_case()
        real = self.store.revise_case
        raced = {}

        def revise_after_a_competitor(*a, **k):
            if not raced:
                raced["done"] = True
                real(case["case_id"], "u1", 1, "ready_for_approval", "f" * 64,
                     {"objective": OBJECTIVE}, {"location": "competitor"})
            return real(*a, **k)

        with patch.object(self.store, "revise_case", revise_after_a_competitor):
            with self.assertRaises(CaseVersionConflict):
                self.service.clarify_objective("u1", {"case_id": case["case_id"], "expected_version": 1,
                                                      "answers": {"location": "Rural Peru"}}, BASE)

    def test_budget_increase_requires_fresh_approval(self):
        case = self.open_ready_case()
        first = self.approve(case)
        self.assertEqual(first["budget"], {"max_spend_usd": 5.0, "max_units": 100.0})
        bigger = self.service.clarify_objective("u1", {"case_id": case["case_id"], "expected_version": 1,
                                                       "budget": {"max_units": 500, "max_spend_usd": 9}}, BASE)
        self.assertEqual(bigger["charter_version"], 2)
        with self.assertRaises(CaseApprovalRequired):
            self.start(bigger)
        self.assert_no_work()
        second = self.approve(bigger)
        self.assertEqual(second["budget"], {"max_spend_usd": 9.0, "max_units": 500.0})
        row = self.store.case_approvals[second["approval_id"]]
        self.assertEqual((row["budget_usd"], row["budget_units"]), (9.0, 500.0))
        self.assertTrue(self.start(bigger)["run_id"].startswith("RR-"))

    def test_tampered_stored_approval_budget_is_refused(self):
        case = self.open_ready_case()
        approved = self.approve(case)
        self.store.case_approvals[approved["approval_id"]]["budget_units"] = 1e9
        with self.assertRaises(CaseApprovalRequired):
            self.start(case)
        self.assert_no_work()

    def test_tampered_stored_charter_fails_its_integrity_check(self):
        case = self.open_ready_case()
        self.approve(case)
        self.store.case_charters[(case["case_id"], 1)]["charter"]["budget"]["max_units"] = 1e9
        with self.assertRaises(CaseInvalid):
            self.start(case)
        self.assert_no_work()

    @patch("lofgren_intelligence.adapters.net.socket.getaddrinfo", return_value=[(2, 1, 6, "", ("93.184.216.34", 443))])
    def test_approved_charter_values_drive_execution_inputs(self, _dns):
        case = self.open_ready_case(budget={"max_spend_usd": 2.5, "max_units": 80})
        self.approve(case)
        captured = {}
        real_compile = service_module.compile_intent
        real_registry = PublicService._registry

        def compile_spy(objective, **k):
            captured["objective"] = objective
            captured["max_spend_usd"] = k.get("max_spend_usd")
            return real_compile(objective, **k)

        def registry_spy(a):
            captured["texts"] = a.get("texts")
            captured["urls"] = a.get("urls")
            return real_registry(a)

        with patch.object(service_module, "compile_intent", compile_spy), \
                patch.object(PublicService, "_registry", staticmethod(registry_spy)):
            out = self.start(case, max_spend_usd=999.0, texts={"injected": "Unapproved evidence."},
                             urls=["https://example.com/unapproved"])
        self.assertEqual(captured["objective"], OBJECTIVE)
        self.assertEqual(captured["max_spend_usd"], 2.5)
        self.assertEqual(captured["texts"], EVIDENCE)
        self.assertIsNone(captured["urls"])
        self.assertEqual(out["approved_budget"], {"max_spend_usd": 2.5, "max_units": 80.0})
        plan = self.service.plan_research({"objective": OBJECTIVE, "case_id": case["case_id"],
                                           "max_spend_usd": 999.0}, "u1", BASE)
        self.assertEqual(plan["spend_cap_usd"], 2.5)
        self.assertEqual(plan["execution_inputs"], "approved_charter")

    def test_plan_that_exceeds_the_approved_units_is_refused(self):
        case = self.open_ready_case(budget={"max_units": 1})
        self.approve(case)
        with self.assertRaises(CaseBudgetExceeded):
            self.start(case)
        self.assert_no_work()
        self.assertIsNone(next(iter(self.store.case_approvals.values()))["consumed_at"])


class ApprovalPageTests(CaseTestBase):
    def setUp(self):
        super().setUp()
        from lofgren_intelligence.hosted import ratelimit
        ratelimit.LIMITER.reset()
        self.addCleanup(ratelimit.LIMITER.reset)

        from lofgren_intelligence.hosted.store import StoreError

        class Store(FakeStore):
            TOKENS = {"tok-u1": {"id": "u1"}, "tok-u2": {"id": "u2"}}

            def verify_supabase_user(self, token):
                if token not in self.TOKENS:
                    raise StoreError("invalid Supabase user session")
                return self.TOKENS[token]

        store = Store(quota=5000)
        store.__dict__.update({k: v for k, v in self.store.__dict__.items()})
        self.store = store
        # The page handlers build their own PublicService on the real clock.
        self.clock.now = datetime.now(timezone.utc)
        self.service = PublicService(self.store, clock=self.clock)

    def _call(self, handler, case_id, token=None, method="GET", body=None):
        from starlette.requests import Request
        from lofgren_intelligence.hosted import web_app
        headers = [(b"content-type", b"application/json")]
        if token:
            headers.append((b"authorization", f"Bearer {token}".encode()))
        raw = json.dumps(body or {}).encode()
        scope = {"type": "http", "method": method, "path": f"/cases/{case_id}", "raw_path": b"/cases/x",
                 "query_string": b"", "headers": headers, "scheme": "https", "server": ("li.example", 443),
                 "client": ("203.0.113.9", 1), "root_path": "", "path_params": {"case_id": case_id}}

        async def receive():
            return {"type": "http.request", "body": raw, "more_body": False}

        env = {"SUPABASE_URL": "https://project.supabase.example", "SUPABASE_PUBLISHABLE_KEY": "publishable-test"}
        with patch.object(web_app, "SupabaseStore", return_value=self.store), patch.dict(os.environ, env):
            return asyncio.run(handler(Request(scope, receive)))

    def test_workspace_requires_session_and_uses_server_owner(self):
        from lofgren_intelligence.hosted import web_app
        for token in (None, "forged-token"):
            self.assertEqual(self._call(web_app.workspace_cases, "unused", token).status_code, 401)
        response = self._call(web_app.workspace_cases, "unused", "tok-u1", "POST",
                              {"objective": OBJECTIVE, "answers": dict(ANSWERS), "user_id": "u2"})
        self.assertEqual(response.status_code, 200)
        saved = json.loads(response.body)
        self.assertEqual(self.store.cases[saved["case_id"]]["user_id"], "u1")
        self.assertEqual(self.store.runs, {})
        with patch.object(self.store, "list_cases", return_value=[
            {"id": "own", "user_id": "u1", "status": "clarifying", "secret": "do-not-return"},
            {"id": "foreign", "user_id": "u2", "status": "approved"},
        ], create=True) as listing:
            response = self._call(web_app.workspace_cases, "unused", "tok-u1")
        listing.assert_called_once_with("u1", limit=101)
        self.assertEqual(json.loads(response.body)["cases"], [{"id": "own", "status": "clarifying"}])
        self.assertEqual(response.headers["cache-control"], "no-store")

    def test_workspace_page_does_not_embed_private_records(self):
        from lofgren_intelligence.hosted import web_app
        response = self._call(web_app.workspace_page, "unused")
        text = response.body.decode()
        self.assertEqual(response.status_code, 200)
        self.assertIn("Real persisted cases", text)
        self.assertIn("textContent", text)
        self.assertNotIn("service-role", text)
        self.assertIn("frame-ancestors 'none'", response.headers["content-security-policy"])

    def test_unauthenticated_and_wrong_user_requests_are_refused(self):
        from lofgren_intelligence.hosted import web_app
        case = self.open_ready_case()
        body = {"charter_version": case["charter_version"], "content_hash": case["content_hash"]}
        for token in (None, "forged-token"):
            with self.subTest(token=token):
                self.assertEqual(self._call(web_app.case_details, case["case_id"], token).status_code, 401)
                self.assertEqual(self._call(web_app.case_approve, case["case_id"], token, "POST", body).status_code,
                                 401)
        self.assertEqual(self._call(web_app.case_details, case["case_id"], "tok-u2").status_code, 404)
        refused = self._call(web_app.case_approve, case["case_id"], "tok-u2", "POST", body)
        self.assertEqual(refused.status_code, 404)
        self.assertEqual(json.loads(refused.body)["error"], "case_not_found")
        self.assertEqual(self.store.case_approvals, {})

    def test_owner_reviews_and_approves_the_exact_version(self):
        from lofgren_intelligence.hosted import web_app
        case = self.open_ready_case()
        details = self._call(web_app.case_details, case["case_id"], "tok-u1")
        self.assertEqual(details.status_code, 200)
        shown = json.loads(details.body)
        self.assertEqual(shown["history"]["events"][0]["kind"], "case_created")
        self.assertEqual(details.headers["cache-control"], "no-store")
        self.assertEqual(shown["content_hash"], case["content_hash"])
        self.assertEqual(shown["charter"]["objective"], OBJECTIVE)
        stale = self._call(web_app.case_approve, case["case_id"], "tok-u1", "POST",
                           {"charter_version": 1, "content_hash": "0" * 64})
        self.assertEqual(stale.status_code, 409)
        ok = self._call(web_app.case_approve, case["case_id"], "tok-u1", "POST",
                        {"charter_version": shown["charter_version"], "content_hash": shown["content_hash"]})
        self.assertEqual(ok.status_code, 200)
        self.assertEqual(json.loads(ok.body)["status"], "approved")
        approval = next(iter(self.store.case_approvals.values()))
        self.assertEqual((approval["user_id"], approval["case_id"], approval["charter_version"]),
                         ("u1", case["case_id"], 1))
        # Case approval is separate from V4 action approval: no action exists or was approved.
        self.assertEqual(self.store.actions, {})

    def test_page_is_served_without_data_and_with_a_strict_policy(self):
        from lofgren_intelligence.hosted import web_app
        case = self.open_ready_case()
        page = self._call(web_app.case_page, case["case_id"])
        self.assertEqual(page.status_code, 200)
        text = page.body.decode()
        self.assertNotIn(ANSWERS["location"], text)
        self.assertIn("frame-ancestors 'none'", page.headers["content-security-policy"])
        self.assertEqual(page.headers["cache-control"], "no-store")

    def test_routes_are_registered_separately_from_v4_actions(self):
        from starlette.testclient import TestClient
        from lofgren_intelligence.hosted import web_app
        env = {"LI_PUBLIC_BASE_URL": BASE}
        with patch.dict(os.environ, env):
            app = web_app.build_app()
        client = TestClient(app, base_url=BASE)
        self.assertEqual(client.get("/cases/abc/approve").status_code, 405)
        self.assertEqual(client.post("/cases/abc/approve", json={}).status_code, 401)


class MCPCaseFlowTests(CaseTestBase):
    """The case flow through the real MCP tool surface (typed refusals, approval URL)."""

    def setUp(self):
        super().setUp()
        # The MCP tools build their own PublicService on the real clock, so the approval
        # recorded here must use real time too; a fixed date makes it expire once that
        # date's TTL has passed in wall-clock time.
        self.clock.now = datetime.now(timezone.utc)
        self.service = PublicService(self.store, clock=self.clock)

    def _call(self, name, args):
        from mcp import Client
        from mcp.server.auth.provider import AccessToken
        from lofgren_intelligence.hosted.mcp_sdk import build_mcp
        token = AccessToken(token="t", client_id="c", scopes=["mcp"], resource=f"{BASE}/mcp", subject="u1")

        async def run():
            with patch("lofgren_intelligence.hosted.mcp_sdk.get_access_token", return_value=token),                     patch("lofgren_intelligence.hosted.mcp_sdk.SupabaseStore", return_value=self.store),                     patch.dict(os.environ, {"LI_MCP_REQUESTS_PER_MINUTE": "1000"}):
                async with Client(build_mcp(BASE)) as client:
                    return await client.call_tool(name, args)

        return asyncio.run(run())

    def test_clarify_then_typed_refusal_until_browser_approval(self):
        opened = self._call("clarify_objective", {"objective": OBJECTIVE, "answers": ANSWERS, "texts": EVIDENCE,
                                                  "max_units": 90})
        self.assertFalse(opened.is_error)
        case = opened.structured_content
        self.assertEqual(case["status"], "READY_FOR_SCOPE_APPROVAL")
        self.assertEqual(case["approval_url"], f"{BASE}/cases/{case['case_id']}")
        self.assertEqual(case["budget"]["max_units"], 90.0)
        refused = self._call("investigate", {"objective": OBJECTIVE, "case_id": case["case_id"]})
        self.assertTrue(refused.is_error)
        self.assertIn("CASE_APPROVAL_REQUIRED", refused.content[0].text)
        self.approve(case)
        ran = self._call("investigate", {"objective": OBJECTIVE, "case_id": case["case_id"],
                                         "idempotency_key": "mcp-1"})
        self.assertFalse(ran.is_error)
        self.assertEqual(ran.structured_content["execution_inputs"], "approved_charter")
        status = self._call("case_status", {"case_id": case["case_id"]}).structured_content
        self.assertEqual(status["approval"]["state"], "consumed")
        self.assertEqual(status["case_status"], "research_complete")


class MigrationTests(unittest.TestCase):
    def _sql(self):
        from pathlib import Path
        root = Path(__file__).resolve().parent.parent / "supabase" / "migrations"
        files = sorted(p.name for p in root.glob("*_intelligence_cases.sql"))
        self.assertEqual(len(files), 1)
        self.assertGreater(files[0], "20261005191819_keyed_rate_limits.sql")
        return (root / files[0]).read_text(encoding="utf-8").lower()

    def test_cases_migration_is_locked_down(self):
        import re
        sql = self._sql()
        for table in ("li_cases", "li_case_charters", "li_case_approvals", "li_case_events"):
            with self.subTest(table=table):
                self.assertIn(f"create table if not exists public.{table}", sql)
                self.assertIn(f"alter table public.{table} enable row level security", sql)
                self.assertIn(f"revoke all on table public.{table} from anon, authenticated", sql)
        self.assertIn("primary key (case_id, version)", sql)
        self.assertNotIn("create policy", sql)
        for grant in re.findall(r"\bgrant\b[^;]*;", sql):
            self.assertRegex(grant, r"\bto\s+service_role\s*;\s*$")
            self.assertNotRegex(grant, r"\b(anon|authenticated)\b")
        for fn in ("li_create_case", "li_revise_case", "li_approve_case_charter", "li_consume_case_approval",
                   "li_record_case_run"):
            self.assertIn(f"create or replace function public.{fn}(", sql)
        self.assertEqual(sql.count("security definer"), 5)
        self.assertEqual(sql.count("set search_path = public"), 5)
        for column in ("user_id uuid not null", "content_hash", "budget_usd", "budget_units", "expires_at",
                       "consumed_at", "token_hash", "charter_version"):
            self.assertIn(column, sql)


class PersistedHistoryTests(CaseTestBase):
    def test_history_reopens_saved_events_without_internal_payloads(self):
        case = self.open_ready_case()
        self.approve(case)
        self.store.case_events[0]["token_hash"] = "private-test-token"
        self.store.case_events[0]["payload"] = {"internal": "private-test-data"}
        reopened = PublicService(self.store, clock=self.clock).case_charter_details("u1", case["case_id"])
        self.assertEqual([e["kind"] for e in reopened["history"]["events"]],
                         ["case_created", "charter_approved"])
        self.assertNotIn("private-test", json.dumps(reopened["history"]))
        self.assertFalse(reopened["history"]["truncated"])
        with self.assertRaises(CaseNotFound):
            self.service.case_charter_details("u2", case["case_id"])

    def test_history_defends_against_cross_tenant_rows_and_reports_bounds(self):
        case = self.open_ready_case()
        rows = [{"case_id": case["case_id"], "user_id": "u1", "kind": f"event-{i}"}
                for i in range(205)]
        rows.append({"case_id": case["case_id"], "user_id": "u2", "kind": "other-user"})
        rows.append({"case_id": "other-case", "user_id": "u1", "kind": "other-case"})
        with patch.object(self.store, "list_case_events", return_value=rows):
            history = self.service.case_charter_details("u1", case["case_id"])["history"]
        self.assertTrue(history["truncated"])
        self.assertEqual(len(history["events"]), 200)
        self.assertEqual(history["events"][0]["kind"], "event-5")
        self.assertEqual(history["events"][-1]["kind"], "event-204")


if __name__ == "__main__":
    unittest.main()
