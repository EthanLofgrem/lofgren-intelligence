import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import timedelta
from pathlib import Path

from lofgren_intelligence import build_registry, render_json, render_markdown
from lofgren_intelligence.adapters import (
    AdapterRegistry,
    DocumentAdapter,
    ImageryCatalogAdapter,
    OrbitalPassAdapter,
    SensorAdapter,
)
from lofgren_intelligence.adapters.base import Adapter, GatherResult
from lofgren_intelligence.cli import main as cli_main
from lofgren_intelligence.evidence import ClaimOrigin, ClaimStatus
from lofgren_intelligence.intent import compile_intent
from lofgren_intelligence.kernel import Stage, run_investigation
from lofgren_intelligence.mcp.server import Server, serve

from .helpers import EPOCH, PHOENIX, SUN_SYNC, TEXTS

OBJECTIVE = "Is industrial construction in the Phoenix metro increasing?"


def docs_registry():
    reg = AdapterRegistry()
    reg.register(DocumentAdapter(texts=TEXTS))
    return reg


class PipelineTests(unittest.TestCase):
    def test_end_to_end_with_contradiction(self):
        r = run_investigation(compile_intent(OBJECTIVE), docs_registry())
        self.assertTrue(r.completed)
        self.assertGreater(len(r.graph.claims), 3)
        self.assertGreaterEqual(len(r.graph.contradictions), 1)
        statuses = {c.status for c in r.graph.claims.values()}
        self.assertIn(ClaimStatus.VERIFIED, statuses)
        self.assertIn(ClaimStatus.CONTESTED, statuses)
        md = render_markdown(r)
        self.assertIn("## Contradictions", md)
        self.assertIn("arrives in V2", md)
        json.dumps(render_json(r))  # fully serializable

    def test_loop_is_complete_and_ordered(self):
        r = run_investigation(compile_intent(OBJECTIVE), docs_registry())
        stages = [s.stage for s in r.stages]
        self.assertEqual(stages[:6], [Stage.INTENT, Stage.PLAN, Stage.SENSE, Stage.RESEARCH, Stage.VERIFY,
                                      Stage.REPORT])
        self.assertEqual(len(stages), 18)

    def test_charge_never_exceeds_estimate(self):
        r = run_investigation(compile_intent(OBJECTIVE), docs_registry())
        self.assertLessEqual(r.charge.total_usd, r.estimate.total_usd)

    def test_spend_cap_stops_before_research(self):
        r = run_investigation(compile_intent(OBJECTIVE, max_spend_usd=0.01), docs_registry())
        self.assertFalse(r.completed)
        self.assertIn("cap", r.stopped_reason)
        self.assertEqual(len(r.graph.claims), 0)

    def test_free_plan_blocks_deep_jobs(self):
        r = run_investigation(compile_intent(OBJECTIVE), docs_registry(), plan_id="free")
        self.assertFalse(r.completed)
        self.assertIn("upgrade", r.stopped_reason)

    def test_missing_sources_become_gaps(self):
        r = run_investigation(compile_intent(OBJECTIVE), docs_registry())
        self.assertTrue(any("orbital_passes" in g for g in r.gaps))

    def test_venture_answers_by_lofgren_stage(self):
        c = compile_intent("Find a warehouse business opportunity in the Phoenix metro")
        r = run_investigation(c, docs_registry())
        md = render_markdown(r)
        for stage in ("Qualify", "Discover", "Diligence", "Blueprint", "Assemble", "Pilot", "Operate"):
            self.assertIn(f"### {stage}:", md)


class PhysicalAdapterTests(unittest.TestCase):
    def test_orbital_passes_become_observed_claims(self):
        reg = AdapterRegistry()
        reg.register(OrbitalPassAdapter(tles=[SUN_SYNC], start=EPOCH, hours=24))
        c = compile_intent(f"Which imaging satellites pass over {PHOENIX[0]}, {PHOENIX[1]}?")
        r = run_investigation(c, reg)
        obs = [cl for cl in r.graph.claims.values() if cl.origin == ClaimOrigin.OBSERVED]
        self.assertEqual(len(obs), 1)
        self.assertEqual(obs[0].status, ClaimStatus.VERIFIED)
        ev = next(iter(r.graph.evidence.values()))
        self.assertTrue(ev.data["passes"])

    def test_different_satellites_do_not_confirm_each_other(self):
        other = SUN_SYNC.__class__(**{**SUN_SYNC.__dict__, "name": "LANDSAT 9", "norad_id": 49260,
                                      "raan_deg": 15.0, "mean_motion_rev_per_day": 14.57109})
        reg = AdapterRegistry()
        reg.register(OrbitalPassAdapter(tles=[SUN_SYNC, other], start=EPOCH, hours=24))
        r = run_investigation(compile_intent(f"Which imaging satellites pass over {PHOENIX[0]}, {PHOENIX[1]}?"), reg)
        for c in r.graph.claims.values():
            self.assertEqual(len(r.graph.sources_for_claim(c.id)), 1)

    def test_orbital_without_coordinates_is_a_gap(self):
        reg = AdapterRegistry()
        reg.register(OrbitalPassAdapter(tles=[SUN_SYNC], start=EPOCH))
        r = run_investigation(compile_intent("Which satellites image farmland in Iowa?"), reg)
        self.assertTrue(any("no coordinates" in g for g in r.gaps))

    def test_imagery_catalog_with_stubbed_stac(self):
        calls = []

        def fake(url, body):
            calls.append(body)
            return {"features": [{"id": "S2A_TEST_1", "properties": {"datetime": "2026-09-20T18:00:00Z",
                                                                      "eo:cloud_cover": 3.2, "platform": "sentinel-2a"}}]}

        reg = AdapterRegistry()
        reg.register(ImageryCatalogAdapter(collections=("sentinel-2-l2a",), fetch_json=fake, now=EPOCH))
        r = run_investigation(compile_intent(f"What changed on the land at {PHOENIX[0]}, {PHOENIX[1]}?"), reg)
        self.assertEqual(calls[0]["intersects"]["coordinates"], [PHOENIX[1], PHOENIX[0]])
        ev = next(iter(r.graph.evidence.values()))
        self.assertEqual(ev.data["scenes"][0]["id"], "S2A_TEST_1")

    def test_imagery_network_failure_is_a_gap(self):
        def boom(url, body):
            raise OSError("offline")

        reg = AdapterRegistry()
        reg.register(ImageryCatalogAdapter(collections=("sentinel-2-l2a",), fetch_json=boom, now=EPOCH))
        r = run_investigation(compile_intent(f"What changed on the land at {PHOENIX[0]}, {PHOENIX[1]}?"), reg)
        self.assertTrue(any("unavailable" in g for g in r.gaps))

    def _sensor_csv(self, d):
        p = Path(d) / "soil.csv"
        p.write_text("timestamp,sensor_id,metric,value,unit\n"
                     "2026-09-01T00:00:00Z,soil-1,soil_moisture,31.5,%\n"
                     "2026-09-29T00:00:00Z,soil-1,soil_moisture,19.4,%\n")
        return p

    def test_sensor_requires_authorization(self):
        with tempfile.TemporaryDirectory() as d:
            reg = AdapterRegistry()
            reg.register(SensorAdapter([self._sensor_csv(d)], authorized=False))
            r = run_investigation(compile_intent("Is soil moisture falling on my farm field?"), reg)
        self.assertEqual(len(r.graph.evidence), 0)
        self.assertTrue(any("authorized" in g for g in r.gaps))

    def test_sensor_readings_summarized(self):
        with tempfile.TemporaryDirectory() as d:
            reg = AdapterRegistry()
            reg.register(SensorAdapter([self._sensor_csv(d)], authorized=True))
            r = run_investigation(compile_intent("Is soil moisture falling on my farm field?"), reg)
        ev = next(iter(r.graph.evidence.values()))
        self.assertAlmostEqual(ev.data["mean"], 25.45)
        self.assertEqual(ev.data["last"], 19.4)

    def test_registry_refuses_write_access(self):
        class Controller(Adapter):
            id = "valve"
            capabilities = frozenset({"sensor"})
            authorized_operations = frozenset({"read", "control"})

            def gather(self, question, contract):
                return GatherResult()

        with self.assertRaises(PermissionError):
            AdapterRegistry().register(Controller())


class MCPTests(unittest.TestCase):
    def call(self, server, mid, method, params=None):
        return server.handle({"jsonrpc": "2.0", "id": mid, "method": method, "params": params or {}})

    def test_initialize_and_list(self):
        s = Server()
        init = self.call(s, 1, "initialize", {"protocolVersion": "2025-06-18"})
        self.assertEqual(init["result"]["protocolVersion"], "2025-06-18")
        self.assertIsNone(s.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}))
        names = {t["name"] for t in self.call(s, 2, "tools/list")["result"]["tools"]}
        self.assertIn("investigate", names)
        self.assertIn("satellite_passes", names)

    def test_investigate_then_trace(self):
        s = Server()
        out = self.call(s, 3, "tools/call", {"name": "investigate",
                                             "arguments": {"objective": OBJECTIVE, "texts": TEXTS}})
        res = out["result"]
        self.assertFalse(res["isError"])
        data = res["structuredContent"]
        trace = self.call(s, 4, "tools/call", {"name": "trace_claim",
                                               "arguments": {"run_id": data["run_id"],
                                                             "claim_id": data["claims"][0]["id"]}})
        self.assertTrue(trace["result"]["structuredContent"]["supporting"])

    def test_satellite_passes_tool(self):
        from lofgren_intelligence.orbital import format_tle

        l1, l2 = format_tle(SUN_SYNC.__class__(**{**SUN_SYNC.__dict__, "epoch": EPOCH}))
        s = Server()
        out = self.call(s, 5, "tools/call", {"name": "satellite_passes", "arguments": {
            "lat": PHOENIX[0], "lon": PHOENIX[1], "tle_text": f"S2A\n{l1}\n{l2}", "hours": 24}})
        self.assertFalse(out["result"]["isError"])

    def test_errors(self):
        s = Server()
        self.assertEqual(self.call(s, 6, "nope")["error"]["code"], -32601)
        self.assertEqual(self.call(s, 7, "tools/call", {"name": "nope"})["error"]["code"], -32602)
        bad = self.call(s, 8, "tools/call", {"name": "satellite_passes", "arguments": {"lat": 1, "lon": 2}})
        self.assertTrue(bad["result"]["isError"])

    def test_stdio_transport(self):
        lines = io.StringIO(json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping"}) + "\nnot json\n")
        out = io.StringIO()
        serve(lines, out)
        replies = [json.loads(x) for x in out.getvalue().splitlines()]
        self.assertEqual(replies[0]["result"], {})
        self.assertEqual(replies[1]["error"]["code"], -32700)


class CLITests(unittest.TestCase):
    def test_pricing_and_estimate(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.assertEqual(cli_main(["pricing", "--standard-units", "500", "--heavy", "5"]), 0)
            self.assertEqual(cli_main(["estimate", OBJECTIVE]), 0)
        self.assertIn("cheapest: Researcher", buf.getvalue())

    def test_investigate_writes_report(self):
        with tempfile.TemporaryDirectory() as d:
            for name, text in TEXTS.items():
                (Path(d) / f"{name.split()[0]}.md").write_text(text)
            out = Path(d) / "report.md"
            with redirect_stdout(io.StringIO()):
                code = cli_main(["investigate", OBJECTIVE, "--files", d, "--out", str(out)])
            self.assertEqual(code, 0)
            self.assertIn("# Lofgren Intelligence report", out.read_text())

    def test_build_registry(self):
        reg = build_registry(texts=TEXTS, imagery=True)
        self.assertEqual({a.id for a in reg.all()}, {"documents", "imagery_catalog"})


if __name__ == "__main__":
    unittest.main()
