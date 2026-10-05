"""Round-2 public operations hardening (build/public-ops-hardening).

No test here makes a network, Stripe or Supabase call. Every store is the
in-memory FakeStore from test_public_hosted (or a subclass of it).
"""

import copy
import json
import os
import unittest
from unittest.mock import patch

from mcp import Client
from mcp.server.auth.provider import AccessToken

from lofgren_intelligence.discovery.fixtures import warehouse_design
from lofgren_intelligence.hosted.mcp_sdk import build_mcp
from lofgren_intelligence.hosted.service import (
    DiscoveryStateInvalid,
    PublicService,
    PublicServiceError,
)

from .helpers import TEXTS
from .test_public_hosted import OBJECTIVE, FakeStore

RUN_ENV = {
    "LOFGREN_PROVIDER": "heuristic",
    "LI_INFRA_USD_PER_RUN": "0",
    "LI_RETRIEVAL_USD_PER_CALL": "0",
    "LI_PUBLIC_MAX_DISCOVERY_UNITS": "100",
    "LI_MCP_REQUESTS_PER_MINUTE": "1000",
}

TOKEN = AccessToken(
    token="test", client_id="client", scopes=["mcp"],
    resource="https://li.example/mcp", subject="u1",
)

# Words that only appear in internal restore/verification detail. None may reach a caller.
INTERNAL_MARKERS = ("Traceback", "context_objects", "does not match its receipt", "unresolved references",
                    "unsupported type", "digest", "File \"", "KeyError", "ValueError", "TypeError")


def _discovered_store():
    """One research run and one discovery in a fresh FakeStore, as JSON (what a database returns)."""
    store = FakeStore(quota=5000)
    with patch.dict(os.environ, RUN_ENV, clear=False):
        run = PublicService(store).investigate("u1", {"objective": OBJECTIVE, "texts": TEXTS})
        discovery = PublicService(store).discover("u1", {
            "run_id": run["run_id"],
            "objective": "Choose a fictional warehouse size",
            "design": warehouse_design(),
        })
    store.runs = {k: json.loads(json.dumps(v)) for k, v in store.runs.items()}
    store.discoveries = {k: json.loads(json.dumps(v)) for k, v in store.discoveries.items()}
    return store, run["run_id"], discovery["discovery_id"]


class _Corruptions:
    """Each corruption takes a stored discovery row (JSON) and damages it in place."""

    @staticmethod
    def tampered_value(row):
        obj = next(x for x in row["snapshot"]["context_objects"] if x["type"] == "Assumption")
        obj["data"]["value"] = float(obj["data"]["value"]) * 2 + 1

    @staticmethod
    def tampered_type(row):
        row["snapshot"]["context_objects"][0]["type"] = "NotADiscoveryType"

    @staticmethod
    def tampered_fields(row):
        row["snapshot"]["context_objects"][0]["data"]["unexpected_field"] = 1

    @staticmethod
    def tampered_bare_value_error(row):
        # A row whose data is not an object makes restore raise a bare ValueError.
        row["snapshot"]["context_objects"][0]["data"] = "not-an-object"

    @staticmethod
    def truncated_objects(row):
        objs = row["snapshot"]["context_objects"]
        row["snapshot"]["context_objects"] = objs[: len(objs) // 2]

    @staticmethod
    def truncated_dependency(row):
        row["snapshot"]["context_objects"] = [
            x for x in row["snapshot"]["context_objects"] if x["type"] != "Simulation"
        ]

    @staticmethod
    def truncated_snapshot_keys(row):
        del row["snapshot"]["receipt_intact"]
        del row["snapshot"]["candidates"]

    @staticmethod
    def missing_context_objects(row):
        del row["snapshot"]["context_objects"]

    @staticmethod
    def empty_context_objects(row):
        row["snapshot"]["context_objects"] = []

    @staticmethod
    def missing_snapshot(row):
        row["snapshot"] = None

    @classmethod
    def all(cls):
        return {name: getattr(cls, name) for name in vars(cls)
                if not name.startswith("_") and name != "all"}


class CorruptDiscoveryStateServiceTests(unittest.TestCase):
    """Item 1: corrupt stored discovery state is a typed refusal, never a crash and never an artifact."""

    @classmethod
    def setUpClass(cls):
        cls.base_store, cls.run_id, cls.discovery_id = _discovered_store()

    def _store(self, corruption=None):
        store = copy.deepcopy(self.base_store)
        if corruption is not None:
            corruption(store.discoveries[("u1", self.discovery_id)])
        return store

    def test_every_corruption_is_discovery_state_invalid_with_no_internal_detail(self):
        for name, corruption in _Corruptions.all().items():
            store = self._store(corruption)
            usage_before = list(store.usage)
            with self.subTest(corruption=name):
                with self.assertRaises(DiscoveryStateInvalid) as ctx:
                    PublicService(store).build_artifact("u1", {"discovery_id": self.discovery_id})
                exc = ctx.exception
                self.assertIsInstance(exc, PublicServiceError)
                self.assertEqual(exc.code, "DISCOVERY_STATE_INVALID")
                # No chained internal exception travels with the refusal.
                self.assertIsNone(exc.__cause__)
                self.assertTrue(exc.__context__ is None or exc.__suppress_context__)
                for marker in INTERNAL_MARKERS:
                    self.assertNotIn(marker, str(exc))
                for obj in (self.base_store.discoveries[("u1", self.discovery_id)]["snapshot"]["context_objects"]):
                    self.assertNotIn(obj["data"]["id"], str(exc))
                # No artifact, no charge, and the held allowance was returned.
                self.assertEqual(store.artifacts, {})
                self.assertEqual(store.usage, usage_before)
                held = [r for r in store.reservations.values() if r["operation"] == "build_artifact"]
                self.assertEqual([r["status"] for r in held], ["released"])

    def test_read_tools_refuse_a_truncated_snapshot_instead_of_crashing(self):
        store = self._store(_Corruptions.truncated_snapshot_keys)
        service = PublicService(store)
        args = {"discovery_id": self.discovery_id}
        for name in ("find_discovery_gaps", "find_connections", "generate_hypotheses", "generate_candidates",
                     "verify_discovery", "get_discovery_receipt", "create_v3_handoff",
                     "render_discovery_report"):
            with self.subTest(tool=name), self.assertRaises(DiscoveryStateInvalid):
                getattr(service, name)("u1", dict(args))

    def test_intact_state_still_builds_a_verified_artifact(self):
        store = self._store()
        built = PublicService(store).build_artifact("u1", {"discovery_id": self.discovery_id})
        self.assertTrue(built["verified"])
        settled = [r for r in store.reservations.values() if r["operation"] == "build_artifact"]
        self.assertEqual([r["status"] for r in settled], ["settled"])

    def test_tampered_handoff_is_still_refused_by_the_v3_upstream_binding(self):
        from lofgren_intelligence.production.errors import ProductionError
        store = self._store()
        snap = store.discoveries[("u1", self.discovery_id)]["snapshot"]
        spec = snap["handoff"]["specifications"][0]
        spec["value"] = float(spec["value"]) * 2 + 1
        with self.assertRaises((ProductionError, PublicServiceError)):
            PublicService(store).build_artifact("u1", {"discovery_id": self.discovery_id})
        self.assertEqual(store.artifacts, {})

    def test_unknown_discovery_is_not_reported_as_corrupt(self):
        store = self._store()
        with self.assertRaises(PublicServiceError) as ctx:
            PublicService(store).build_artifact("u1", {"discovery_id": "DR-missing"})
        self.assertNotIsInstance(ctx.exception, DiscoveryStateInvalid)
        self.assertIn("unknown discovery_id", str(ctx.exception))


class CorruptDiscoveryStateMCPTests(unittest.IsolatedAsyncioTestCase):
    """Item 1 through the official MCP SDK tool path."""

    @classmethod
    def setUpClass(cls):
        cls.base_store, cls.run_id, cls.discovery_id = _discovered_store()

    async def _call(self, store, name, args):
        with (
            patch("lofgren_intelligence.hosted.mcp_sdk.get_access_token", return_value=TOKEN),
            patch("lofgren_intelligence.hosted.mcp_sdk.SupabaseStore", return_value=store),
            patch.dict(os.environ, RUN_ENV, clear=False),
        ):
            async with Client(build_mcp("https://li.example")) as client:
                return await client.call_tool(name, args)

    def _text(self, result):
        return "\n".join(getattr(c, "text", "") for c in result.content)

    async def test_tampered_truncated_and_missing_state_is_a_typed_tool_error(self):
        for name in ("tampered_value", "tampered_bare_value_error", "truncated_objects",
                     "truncated_snapshot_keys", "missing_context_objects", "missing_snapshot"):
            store = copy.deepcopy(self.base_store)
            getattr(_Corruptions, name)(store.discoveries[("u1", self.discovery_id)])
            with self.subTest(corruption=name):
                result = await self._call(store, "build_artifact", {"discovery_id": self.discovery_id})
                self.assertTrue(result.is_error)
                text = self._text(result)
                self.assertIn("DISCOVERY_STATE_INVALID", text)
                self.assertIn("run discover again", text)
                for marker in INTERNAL_MARKERS:
                    self.assertNotIn(marker, text)
                self.assertEqual(store.artifacts, {})

    async def test_read_tool_on_truncated_state_is_a_typed_tool_error(self):
        store = copy.deepcopy(self.base_store)
        _Corruptions.truncated_snapshot_keys(store.discoveries[("u1", self.discovery_id)])
        result = await self._call(store, "generate_candidates", {"discovery_id": self.discovery_id})
        self.assertTrue(result.is_error)
        self.assertIn("DISCOVERY_STATE_INVALID", self._text(result))


def _tle():
    from pathlib import Path
    return (Path(__file__).resolve().parent.parent / "examples" / "sample.tle").read_text(encoding="utf-8")


class LockedFakeStore(FakeStore):
    """FakeStore whose reserve/finalize/release are serialized, as the li_* SQL functions take an advisory lock."""

    def __init__(self, *a, **k):
        import threading
        super().__init__(*a, **k)
        self._lock = threading.Lock()
        self.reserve_attempts = 0

    def reserve_usage(self, *a, **k):
        with self._lock:
            self.reserve_attempts += 1
            return super().reserve_usage(*a, **k)

    def finalize_usage(self, *a, **k):
        with self._lock:
            return super().finalize_usage(*a, **k)

    def release_usage(self, *a, **k):
        with self._lock:
            return super().release_usage(*a, **k)


class AdHocChargingTests(unittest.TestCase):
    """Item 2: ad hoc tools are charged through reserve -> settle -> release."""

    @classmethod
    def setUpClass(cls):
        from lofgren_intelligence.discovery import fixtures as F
        cls.base_store, cls.run_id, _ = _discovered_store()
        params = {"sqft": [1000, "sqft"], "occupancy": [0.85, ""], "rent": [12, "usd/sqft"],
                  "build_cost": [8, "usd/sqft"]}
        cls.calls = {
            "simulate_candidate": {"run_id": cls.run_id, "model": F.PROFIT_MODEL, "parameters": params},
            "analyze_sensitivity": {"run_id": cls.run_id, "model": F.PROFIT_MODEL, "parameters": params,
                                    "success": F.rel(F.V("profit"), ">=", F.K(0, "usd"))},
            "optimize_solution": {"run_id": cls.run_id, "problem": F.resource_design()["optimization"]},
            "find_prior_art": {"run_id": cls.run_id, "subject": F.PRIOR_ART_MATCHING["subject"],
                               "queries": F.PRIOR_ART_MATCHING["queries"],
                               "records": F.PRIOR_ART_MATCHING["records"],
                               "coverage": F.PRIOR_ART_MATCHING["coverage"]},
            "satellite_passes": {"lat": 33.4, "lon": -112.0, "hours": 6, "tle_text": _tle()},
        }

    def _store(self, quota=None):
        store = copy.deepcopy(self.base_store)
        store.reservations = {}
        store.usage = []
        if quota is not None:
            store.entitlement["quota_units_per_week"] = quota
        return store

    def test_documented_unit_costs_cover_every_ad_hoc_tool(self):
        from pathlib import Path
        from lofgren_intelligence.hosted.service import ADHOC_UNIT_COSTS
        self.assertEqual(set(ADHOC_UNIT_COSTS), set(self.calls))
        doc = (Path(__file__).resolve().parent.parent / "docs" / "PUBLIC_MCP.md").read_text(encoding="utf-8")
        for tool, units in ADHOC_UNIT_COSTS.items():
            self.assertIn(f"| `{tool}` | {units:g} |", doc)

    def test_successful_call_settles_exactly_its_documented_units(self):
        from lofgren_intelligence.hosted.service import ADHOC_UNIT_COSTS
        for tool, args in self.calls.items():
            store = self._store()
            with self.subTest(tool=tool):
                getattr(PublicService(store), tool)("u1", copy.deepcopy(args))
                self.assertEqual([r["status"] for r in store.reservations.values()], ["settled"])
                self.assertEqual(len(store.usage), 1)
                event = store.usage[0]
                self.assertEqual(event["operation"], tool)
                self.assertEqual(event["units"], ADHOC_UNIT_COSTS[tool])
                self.assertEqual(event["unpriced_components"], [f"{tool}_compute"])
                self.assertEqual(event["run_id"], None if tool == "satellite_passes" else self.run_id)

    def test_failed_call_releases_its_reservation_and_is_not_charged(self):
        bad = {
            "simulate_candidate": {"run_id": self.run_id, "model": {"outcomes": "nonsense"}, "parameters": {}},
            "analyze_sensitivity": {"run_id": self.run_id, "model": {}, "parameters": {}},
            "optimize_solution": {"run_id": self.run_id, "problem": {"variables": []}},
            "find_prior_art": {"run_id": "RR-unknown", "subject": "x", "queries": ["x"], "records": [],
                               "coverage": {}},
            "satellite_passes": {"lat": 91, "lon": 0, "tle_text": _tle()},
        }
        for tool, args in bad.items():
            store = self._store()
            with self.subTest(tool=tool):
                with self.assertRaises(Exception):
                    getattr(PublicService(store), tool)("u1", args)
                self.assertEqual([r["status"] for r in store.reservations.values()], ["released"])
                self.assertEqual(store.usage, [])

    def test_crash_inside_the_work_releases_the_reservation(self):
        store = self._store()
        with patch("lofgren_intelligence.hosted.service.find_passes", side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                PublicService(store).satellite_passes("u1", self.calls["satellite_passes"])
        self.assertEqual([r["status"] for r in store.reservations.values()], ["released"])
        self.assertEqual(store.usage, [])

    def test_call_that_would_overrun_quota_is_refused_before_compute(self):
        from lofgren_intelligence.hosted.service import QuotaExceeded
        store = self._store(quota=4.0)
        with patch("lofgren_intelligence.hosted.service.evaluate_candidates") as compute:
            with self.assertRaises(QuotaExceeded):
                PublicService(store).simulate_candidate("u1", copy.deepcopy(self.calls["simulate_candidate"]))
        compute.assert_not_called()
        self.assertEqual(store.usage, [])

    def test_repeated_calls_drain_the_weekly_quota(self):
        from lofgren_intelligence.hosted.service import QuotaExceeded
        store = self._store(quota=10.0)
        service = PublicService(store)
        service.simulate_candidate("u1", copy.deepcopy(self.calls["simulate_candidate"]))
        service.optimize_solution("u1", copy.deepcopy(self.calls["optimize_solution"]))
        with self.assertRaises(QuotaExceeded):
            service.satellite_passes("u1", self.calls["satellite_passes"])
        self.assertEqual(sum(e["units"] for e in store.usage), 10.0)

    def test_operator_can_override_a_unit_cost(self):
        store = self._store()
        with patch.dict(os.environ, {"LI_UNITS_SATELLITE_PASSES": "3"}):
            PublicService(store).satellite_passes("u1", self.calls["satellite_passes"])
        self.assertEqual(store.usage[0]["units"], 3.0)
        for junk in ("-1", "nan", "inf", "lots"):
            store = self._store()
            with self.subTest(value=junk), patch.dict(os.environ, {"LI_UNITS_SATELLITE_PASSES": junk}):
                PublicService(store).satellite_passes("u1", self.calls["satellite_passes"])
                self.assertEqual(store.usage[0]["units"], 1.0)

    def test_concurrent_calls_never_oversubscribe_the_quota(self):
        import threading
        from lofgren_intelligence.hosted.service import QuotaExceeded
        from lofgren_intelligence.orbital.propagate import find_passes as real_find_passes

        store = LockedFakeStore(quota=2.0)
        go = threading.Event()

        def slow_find_passes(*a, **k):
            # Hold every in-flight reservation open until all callers have tried to reserve.
            go.wait(10)
            return real_find_passes(*a, **k)

        outcomes = []

        def call():
            try:
                PublicService(store).satellite_passes("u1", self.calls["satellite_passes"])
                outcomes.append("ok")
            except QuotaExceeded:
                outcomes.append("quota")

        with patch("lofgren_intelligence.hosted.service.find_passes", slow_find_passes):
            threads = [threading.Thread(target=call) for _ in range(5)]
            for t in threads:
                t.start()
            for _ in range(1000):
                if store.reserve_attempts >= 5:
                    break
                threading.Event().wait(0.01)
            go.set()
            for t in threads:
                t.join(20)
        self.assertEqual(store.reserve_attempts, 5)
        self.assertEqual(sorted(outcomes), ["ok", "ok", "quota", "quota", "quota"])
        self.assertEqual(sum(e["units"] for e in store.usage), 2.0)
        self.assertEqual(sorted(r["status"] for r in store.reservations.values()), ["settled", "settled"])

    def test_mcp_tool_call_is_charged(self):
        import asyncio
        store = self._store()

        async def run():
            with (
                patch("lofgren_intelligence.hosted.mcp_sdk.get_access_token", return_value=TOKEN),
                patch("lofgren_intelligence.hosted.mcp_sdk.SupabaseStore", return_value=store),
                patch.dict(os.environ, RUN_ENV, clear=False),
            ):
                async with Client(build_mcp("https://li.example")) as client:
                    return await client.call_tool("satellite_passes", self.calls["satellite_passes"])

        result = asyncio.run(run())
        self.assertFalse(result.is_error)
        self.assertEqual([(e["operation"], e["units"]) for e in store.usage], [("satellite_passes", 1.0)])


if __name__ == "__main__":
    unittest.main()
