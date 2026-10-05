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


if __name__ == "__main__":
    unittest.main()
