"""Usage reservations are released, charged or reconciled on every path, never leaked.

Rules (hosted/service.py):
  * a refusal before any work releases the reservation;
  * a failure after work incurred cost charges what is known so far;
  * work that ran is charged even when li_finalize_usage refuses or fails: an
    'unsettled' marker is recorded and settled exactly once (reconcile_usage);
  * a retry never charges twice and a saved result is never lost.

Every store is the in-memory FakeStore; no network, Stripe or Supabase call is made.
"""

from __future__ import annotations

import copy
import os
import threading
import unittest
from unittest.mock import patch

from lofgren_intelligence.discovery.fixtures import warehouse_design
from lofgren_intelligence.hosted import service as service_module
from lofgren_intelligence.hosted.service import PublicService, PublicServiceError, QuotaExceeded
from lofgren_intelligence.hosted.store import StoreError

from .helpers import TEXTS
from .test_public_hosted import OBJECTIVE, FakeStore
from .test_public_ops_hardening import RUN_ENV, _discovered_store, _tle


def _statuses(store, operation=None):
    return sorted(r["status"] for r in store.reservations.values()
                  if operation is None or r["operation"] == operation)


class Base(unittest.TestCase):
    def setUp(self):
        env = patch.dict(os.environ, RUN_ENV, clear=False)
        env.start()
        self.addCleanup(env.stop)

    def assert_no_orphan(self, store):
        self.assertNotIn("reserved", _statuses(store))

    def research(self, store, **extra):
        return PublicService(store).investigate("u1", {"objective": OBJECTIVE, "texts": TEXTS, **extra})


class InvestigateSettlementTests(Base):
    def test_cost_ceiling_refusal_releases_the_reservation(self):
        store = FakeStore()
        with patch.dict(os.environ, {"LI_PUBLIC_MAX_ESTIMATED_USD_PER_RUN": "0"}), \
                patch.object(service_module, "run_investigation") as run:
            with self.assertRaises(QuotaExceeded):
                self.research(store)
        run.assert_not_called()
        self.assertEqual(_statuses(store), ["released"])
        self.assertEqual(store.usage, [])

    def test_provider_failure_before_any_cost_releases(self):
        store = FakeStore()
        with patch.object(service_module, "run_investigation", side_effect=RuntimeError("provider down")):
            with self.assertRaises(RuntimeError):
                self.research(store)
        self.assertEqual(_statuses(store), ["released"])
        self.assertEqual(store.usage, [])

    def test_provider_failure_after_partial_cost_charges_the_partial_work(self):
        store = FakeStore()

        def partial(contract, registry, provider, plan_id, approved=False, ledger=None):
            ledger.record("sense", "retrieval", "fixture", work_units=3.0)
            ledger.record("sense", "external_data", "licensed", work_units=1.0, external_usd=0.25)
            raise RuntimeError("provider failed mid-run")

        with patch.object(service_module, "run_investigation", partial), \
                patch.dict(os.environ, {"LI_RETRIEVAL_USD_PER_CALL": "0.01"}):
            with self.assertRaises(RuntimeError):
                self.research(store)
        self.assertEqual(_statuses(store), ["settled"])
        self.assertEqual(len(store.usage), 1)
        event = store.usage[0]
        self.assertEqual(event["units"], 4.0)
        self.assertIsNone(event["run_id"])
        self.assertIn("partial_run", event["unpriced_components"])
        self.assertGreaterEqual(event["known_cost_usd"], 0.25)
        self.assertEqual(store.runs, {})

    def test_persistence_failure_charges_the_work_without_an_orphan(self):
        store = FakeStore()
        with patch.object(store, "save_run", side_effect=StoreError("database is unreachable")):
            with self.assertRaises(StoreError):
                self.research(store)
        self.assertEqual(_statuses(store), ["settled"])
        self.assertEqual(len(store.usage), 1)
        self.assertGreater(store.usage[0]["units"], 0)
        self.assertIsNone(store.usage[0]["run_id"])  # no run row exists to reference
        self.assertEqual(store.runs, {})

    def test_settlement_failure_after_persistence_keeps_the_result_and_reconciles_once(self):
        store = FakeStore()
        real_settle = store.settle_usage
        with patch.object(store, "finalize_usage", side_effect=StoreError("database is unreachable")), \
                patch.object(store, "settle_usage", side_effect=StoreError("database is unreachable")):
            out = self.research(store)
        self.assertIn(("u1", out["run_id"]), store.runs)  # the saved result is kept
        self.assertEqual(_statuses(store), ["unsettled"])
        self.assertEqual(store.usage, [])
        marker = next(iter(store.reservations.values()))
        self.assertEqual(marker["run_id"], out["run_id"])
        self.assertGreater(marker["pending_units"], 0)
        rid = next(iter(store.reservations))
        store.settle_usage = real_settle
        service = PublicService(store)
        self.assertEqual(service.reconcile_unsettled_usage(), {"reconciled": {rid: "settled"}})
        self.assertEqual(service.reconcile_usage(rid), "already_settled")
        self.assertEqual(service.reconcile_unsettled_usage(), {"reconciled": {}})
        self.assertEqual(len(store.usage), 1)
        self.assertEqual(store.usage[0]["id"], rid)
        self.assertEqual(store.usage[0]["run_id"], out["run_id"])
        self.assertEqual(store.usage[0]["units"], marker["pending_units"])

    def test_finalize_refusal_after_the_work_ran_still_charges_actual_usage(self):
        # Reserved 5 units, the run used 7; 6 units of quota were left. Formerly the
        # reservation was released and nothing was charged (an undercount).
        store = FakeStore(quota=6.0)
        out = self.research(store)
        self.assertEqual(_statuses(store), ["settled"])
        self.assertEqual(len(store.usage), 1)
        self.assertGreater(store.usage[0]["units"], 5.0)
        self.assertIn(("u1", out["run_id"]), store.runs)

    def test_finalize_answer_lost_after_commit_never_double_charges(self):
        store = FakeStore()
        real = store.finalize_usage

        def committed_then_lost(*a, **k):
            real(*a, **k)
            raise StoreError("database is unreachable")

        with patch.object(store, "finalize_usage", committed_then_lost):
            self.research(store)
        self.assertEqual(_statuses(store), ["settled"])
        self.assertEqual(len(store.usage), 1)
        rid = next(iter(store.reservations))
        self.assertEqual(PublicService(store).reconcile_usage(rid), "already_settled")
        self.assertEqual(len(store.usage), 1)

    def test_retry_of_a_failed_request_is_a_new_reservation_and_never_double_charges(self):
        store = FakeStore()
        with patch.object(store, "save_run", side_effect=StoreError("database is unreachable")):
            with self.assertRaises(StoreError):
                self.research(store)
        out = self.research(store)
        self.assertEqual(_statuses(store), ["settled", "settled"])
        self.assertEqual(len(store.usage), 2)
        self.assertEqual(len({e["id"] for e in store.usage}), 2)
        for rid in list(store.reservations):
            self.assertEqual(PublicService(store).reconcile_usage(rid), "already_settled")
        self.assertEqual(len(store.usage), 2)
        self.assertIn(("u1", out["run_id"]), store.runs)

    def test_concurrent_requests_respect_the_quota(self):
        store = FakeStore(quota=12.0)  # each run reserves 5 units: at most two may hold reservations
        lock = threading.Lock()
        for name in ("reserve_usage", "finalize_usage", "release_usage", "mark_usage_unsettled", "settle_usage"):
            original = getattr(store, name)

            def locked(*a, _f=original, **k):
                with lock:
                    return _f(*a, **k)
            setattr(store, name, locked)
        go = threading.Event()
        attempts = []
        real = service_module.run_investigation

        def slow(*a, **k):
            go.wait(10)
            return real(*a, **k)

        outcomes = []

        def call():
            attempts.append(1)
            try:
                self.research(store)
                outcomes.append("ok")
            except QuotaExceeded:
                outcomes.append("quota")

        with patch.object(service_module, "run_investigation", slow):
            threads = [threading.Thread(target=call) for _ in range(5)]
            for t in threads:
                t.start()
            for _ in range(1000):
                if len(outcomes) >= 3:
                    break
                threading.Event().wait(0.01)
            go.set()
            for t in threads:
                t.join(60)
        self.assertEqual(sorted(outcomes), ["ok", "ok", "quota", "quota", "quota"])
        self.assertEqual(_statuses(store), ["settled", "settled"])
        self.assertEqual(len(store.usage), 2)
        self.assert_no_orphan(store)


class DiscoveryAndArtifactSettlementTests(Base):
    @classmethod
    def setUpClass(cls):
        with patch.dict(os.environ, RUN_ENV, clear=False):
            cls.base_store, cls.run_id, cls.discovery_id = _discovered_store()

    def _store(self):
        store = copy.deepcopy(self.base_store)
        store.reservations = {}
        store.usage = []
        return store

    def _discover(self, store):
        return PublicService(store).discover("u1", {
            "run_id": self.run_id, "objective": "Choose a fictional warehouse size", "design": warehouse_design(),
        })

    def test_discover_refused_for_an_unknown_run_is_released(self):
        store = self._store()
        with self.assertRaises(PublicServiceError):
            PublicService(store).discover("u1", {"run_id": "RR-unknown", "objective": "x"})
        self.assertEqual(_statuses(store), ["released"])
        self.assertEqual(store.usage, [])

    def test_discover_over_the_ceiling_charges_the_work_it_did(self):
        store = self._store()
        with patch.dict(os.environ, {"LI_PUBLIC_MAX_DISCOVERY_UNITS": "0.0001"}):
            with self.assertRaises(QuotaExceeded):
                self._discover(store)
        self.assertEqual(_statuses(store), ["settled"])
        self.assertGreater(store.usage[0]["units"], 0)
        self.assertEqual(store.discoveries, self.base_store.discoveries)

    def test_discover_persistence_failure_is_charged_and_not_orphaned(self):
        store = self._store()
        with patch.object(store, "save_discovery", side_effect=StoreError("database is unreachable")):
            with self.assertRaises(StoreError):
                self._discover(store)
        self.assertEqual(_statuses(store), ["settled"])
        self.assertEqual(store.usage[0]["run_id"], self.run_id)

    def test_discover_settlement_failure_leaves_a_marker_that_reconciles_once(self):
        store = self._store()
        with patch.object(store, "finalize_usage", side_effect=StoreError("x")), \
                patch.object(store, "settle_usage", side_effect=StoreError("x")):
            out = self._discover(store)
        self.assertIn(("u1", out["discovery_id"]), store.discoveries)
        self.assertEqual(_statuses(store), ["unsettled"])
        rid = next(iter(store.reservations))
        self.assertEqual(PublicService(store).reconcile_usage(rid), "settled")
        self.assertEqual(PublicService(store).reconcile_usage(rid), "already_settled")
        self.assertEqual(len(store.usage), 1)

    def test_build_artifact_persistence_failure_is_charged(self):
        store = self._store()
        with patch.object(store, "save_artifact", side_effect=StoreError("database is unreachable")):
            with self.assertRaises(StoreError):
                PublicService(store).build_artifact("u1", {"discovery_id": self.discovery_id})
        self.assertEqual(_statuses(store), ["settled"])
        self.assertEqual([(e["operation"], e["units"]) for e in store.usage], [("build_artifact", 10.0)])
        self.assertEqual(store.artifacts, {})

    def test_build_artifact_settlement_failure_keeps_the_artifact(self):
        store = self._store()
        with patch.object(store, "finalize_usage", side_effect=StoreError("x")), \
                patch.object(store, "settle_usage", side_effect=StoreError("x")):
            built = PublicService(store).build_artifact("u1", {"discovery_id": self.discovery_id})
        self.assertIsNotNone(store.get_artifact("u1", built["artifact_id"]))
        self.assertEqual(_statuses(store), ["unsettled"])
        self.assertEqual(PublicService(store).reconcile_unsettled_usage()["reconciled"],
                         {next(iter(store.reservations)): "settled"})
        self.assertEqual(len(store.usage), 1)

    def test_adhoc_settlement_failure_is_reconciled_once(self):
        store = self._store()
        with patch.object(store, "finalize_usage", side_effect=StoreError("x")):
            out = PublicService(store).satellite_passes(
                "u1", {"lat": 33.4, "lon": -112.0, "hours": 6, "tle_text": _tle()})
        self.assertIn("passes", out)
        # The marker was recorded and settled in the same call.
        self.assertEqual(_statuses(store), ["settled"])
        self.assertEqual([(e["operation"], e["units"]) for e in store.usage], [("satellite_passes", 1.0)])
        rid = next(iter(store.reservations))
        self.assertEqual(PublicService(store).reconcile_usage(rid), "already_settled")
        self.assertEqual(len(store.usage), 1)


class OutcomeAndImprovementSettlementTests(Base):
    def test_v5_refusal_before_work_is_released(self):
        store = FakeStore()
        with self.assertRaises(PublicServiceError):
            PublicService(store).measure_outcome("u1", {"action_id": "ACT-unknown", "measurements": []})
        self.assertEqual(_statuses(store), ["released"])
        self.assertEqual(store.usage, [])

    def test_v6_refusal_before_work_is_released(self):
        store = FakeStore()
        with self.assertRaises(PublicServiceError):
            PublicService(store).evaluate_improvement("u1", {"outcome_id": "OUT-unknown", "proposal": {}})
        self.assertEqual(_statuses(store), ["released"])
        self.assertEqual(store.usage, [])

    def test_v5_and_v6_persistence_failures_are_charged(self):
        from lofgren_intelligence.execution.certification import NOW, _base as v4_base
        from lofgren_intelligence.execution.core import InMemoryAdapter, execute_authorized

        store = FakeStore(quota=5000)
        product, request, grant, approval = v4_base()
        executed = execute_authorized(product.v4_handoff, request, grant, approval, InMemoryAdapter(),
                                      subject="user-cert", now=NOW)
        store.save_action({
            "user_id": "u1", "action_id": request.action_id, "artifact_id": request.artifact_id,
            "request": {}, "status": "executed", "grant_record": None, "approval_record": None,
            "receipt": executed.receipt, "v5_handoff": executed.v5_handoff,
        })
        measurements = [
            {"metric": item["metric"], "value": item["mean"], "unit": item.get("unit", ""),
             "observed_at": "2026-11-04T12:00:00+00:00", "source": "hosted-test", "observation_id": f"OBS-{i}"}
            for i, item in enumerate(executed.v5_handoff["expected_outcomes"], 1)
        ]
        args = {"action_id": request.action_id, "measurements": measurements}
        with patch.object(store, "save_outcome", side_effect=StoreError("database is unreachable")):
            with self.assertRaises(StoreError):
                PublicService(store).measure_outcome("u1", args)
        self.assertEqual(_statuses(store), ["settled"])
        self.assertEqual([(e["operation"], e["units"]) for e in store.usage], [("measure_outcome", 5.0)])
        measured = PublicService(store).measure_outcome("u1", args)
        proposal = {
            "proposal_id": "IMP-hosted", "baseline_id": "BASE-1", "candidate_id": "CAND-2",
            "change_summary": "Improve routing threshold.",
            "evaluation_dataset": {"dataset_id": "DS-heldout", "content_hash": "sha256:" + "a" * 64,
                                   "sample_count": 100, "held_out": True},
            "primary_metric": {"metric": "task_success", "baseline": 0.75, "candidate": 0.82,
                               "direction": "higher_is_better"},
            "min_gain": 0.03, "safety_constraints": [],
        }
        with patch.object(store, "save_improvement", side_effect=StoreError("database is unreachable")):
            with self.assertRaises(StoreError):
                PublicService(store).evaluate_improvement("u1", {"outcome_id": measured["outcome_id"],
                                                                 "proposal": proposal})
        self.assertEqual([(e["operation"], e["units"]) for e in store.usage],
                         [("measure_outcome", 5.0), ("measure_outcome", 5.0), ("evaluate_improvement", 5.0)])
        self.assert_no_orphan(store)


class StoreSettlementContractTests(unittest.TestCase):
    """The SupabaseStore calls the settlement RPCs with the reservation id as the key."""

    def test_rpc_payloads_and_answer_parsing(self):
        from lofgren_intelligence.hosted import store as li_store
        from lofgren_intelligence.hosted.http import JSONResponse
        calls = []

        def fake(url, method, **kw):
            calls.append((url.rsplit("/", 1)[-1], kw.get("body")))
            name = url.rsplit("/", 1)[-1]
            return JSONResponse(200, {}, {"li_mark_usage_unsettled": True, "li_settle_usage": "settled"}[name])

        s = li_store.SupabaseStore("https://db.example", "service-role-test", "publishable-test")
        with patch.object(li_store, "json_request", fake):
            self.assertTrue(s.mark_usage_unsettled("r1", "RR-1", 7.0, 0.5, ["x"]))
            self.assertEqual(s.settle_usage("r1"), "settled")
        self.assertEqual(calls[0], ("li_mark_usage_unsettled", {
            "p_id": "r1", "p_run_id": "RR-1", "p_actual_units": 7.0, "p_known_cost_usd": 0.5,
            "p_unpriced_components": ["x"]}))
        self.assertEqual(calls[1], ("li_settle_usage", {"p_id": "r1"}))
        with patch.object(li_store, "json_request", lambda *a, **k: JSONResponse(200, {}, "maybe")):
            with self.assertRaises(li_store.StoreError):
                s.settle_usage("r1")


class SettlementMigrationTests(unittest.TestCase):
    def _dir(self):
        from pathlib import Path
        return Path(__file__).resolve().parent.parent / "supabase" / "migrations"

    def test_every_earlier_migration_is_unchanged(self):
        import hashlib
        applied = {
            "20261004201650_public_mcp.sql": "b6594b609bff9f837e4fd7e37b3333ab800979d998b18e685190e0b9d176ea2e",
            "20261004202354_public_mcp_indexes.sql": "4767630f82016246b640eb344916d5942d8ccdbf2b620bcff8f0d1b5e48de53b",
            "20261005052012_public_lifecycle.sql": "9a67ddc2d0b58319cbc8a0fc32055227503351006bc7052e386051e43d9ee3f2",
            "20261005052550_usage_reservations.sql": "f109f62077843099a90b7868f794c5d0a652579ccb9d3f0547c3ae1ab95272f6",
            "20261005191819_keyed_rate_limits.sql": "d58b9cd5b381dc04a48c37fe12cbcc440a23abe1362cc84593126eb746875a01",
        }
        for name, digest in applied.items():
            raw = (self._dir() / name).read_bytes().replace(b"\r\n", b"\n")
            with self.subTest(migration=name):
                self.assertEqual(hashlib.sha256(raw).hexdigest(), digest)

    def test_settlement_migration_is_new_idempotent_and_locked_down(self):
        import re
        files = sorted(p.name for p in self._dir().glob("*_usage_settlement.sql"))
        self.assertEqual(len(files), 1)
        self.assertGreater(files[0], "20261005191819_keyed_rate_limits.sql")
        sql = (self._dir() / files[0]).read_text(encoding="utf-8").lower()
        self.assertIn("'unsettled'", sql)
        self.assertIn("on conflict (id) do nothing", sql)
        for fn in ("li_reserve_usage", "li_finalize_usage", "li_mark_usage_unsettled", "li_settle_usage"):
            self.assertIn(f"create or replace function public.{fn}(", sql)
        self.assertNotIn("create policy", sql)
        for grant in re.findall(r"\bgrant\b[^;]*;", sql):
            self.assertRegex(grant, r"\bto\s+service_role\s*;\s*$")
            self.assertNotRegex(grant, r"\b(anon|authenticated)\b")


if __name__ == "__main__":
    unittest.main()
