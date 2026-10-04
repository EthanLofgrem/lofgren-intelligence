"""MCP contract lofgren.mcp/2: every V1 tool unchanged; every discovery tool's success, schema and error paths, in
process and over a real stdio session. All data is fictional."""

from __future__ import annotations

import json
import subprocess
import sys
import unittest
from pathlib import Path

import lofgren_intelligence.certification as C
from lofgren_intelligence.discovery import fixtures as F
from lofgren_intelligence.mcp.server import CONTRACT, DISCOVERY_TOOLS, TOOLS, Server

ROOT = Path(__file__).resolve().parent.parent
V1_TOOLS = ["compile_objective", "plan_research", "investigate", "verify_claim", "get_finding", "find_contradictions",
            "find_gaps", "trace_claim", "get_receipt", "export_state", "render_report", "satellite_passes", "pricing"]


def required(tool_name: str) -> list[str]:
    tool = next(t for t in TOOLS if t["name"] == tool_name)
    return tool.get("outputSchema", {}).get("required", [])


class InProcess(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = Server()
        cls.run_id = cls.call("investigate", {"objective": C.OBJECTIVE, "texts": C.AGREE_AND_CONFLICT})["structuredContent"]["run_id"]
        out = cls.call("discover", {"run_id": cls.run_id, "objective": "Choose a warehouse size (fictional)",
                                    "design": F.warehouse_design()})
        cls.discovery = out["structuredContent"]

    @classmethod
    def call(cls, name: str, args: dict) -> dict:
        reply = cls.srv.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                "params": {"name": name, "arguments": args}})
        return reply.get("result", reply)

    def assertConforms(self, name: str, out: dict) -> None:
        self.assertFalse(out.get("isError"), out)
        for key in required(name):
            self.assertIn(key, out["structuredContent"], f"{name} misses {key}")

    def test_contract(self):
        self.assertEqual([t["name"] for t in TOOLS[:len(V1_TOOLS)]], V1_TOOLS)
        self.assertEqual(CONTRACT, "lofgren.mcp/2")
        names = [t["name"] for t in TOOLS]
        self.assertEqual(len(names), len(set(names)))
        for t in DISCOVERY_TOOLS:
            self.assertTrue(t["description"] and t["inputSchema"]["type"] == "object", t["name"])
        listed = self.srv.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})["result"]["tools"]
        self.assertEqual(len(listed), len(V1_TOOLS) + len(DISCOVERY_TOOLS))
        init = self.srv.handle({"jsonrpc": "2.0", "id": 3, "method": "initialize", "params": {}})["result"]
        self.assertIn(CONTRACT, init["instructions"])

    def test_discover(self):
        self.assertEqual(self.discovery["kind"], "discovery")
        self.assertEqual(self.discovery["outcome"], "candidate_selected")
        self.assertTrue(self.discovery["handoff_ready"])
        for c in self.discovery["candidates"]:
            self.assertEqual(c["expected_value_kind"], "simulated")
        for h in self.discovery["hypotheses"]:
            self.assertEqual(h["confidence_kind"], "hypothesis")

    def test_discovery_views(self):
        did = {"discovery_id": self.discovery["discovery_id"]}
        for name in ("find_discovery_gaps", "find_connections", "generate_hypotheses", "generate_candidates",
                     "verify_discovery", "get_discovery_receipt", "create_v3_handoff"):
            with self.subTest(tool=name):
                self.assertConforms(name, self.call(name, did))
        v = self.call("verify_discovery", did)["structuredContent"]
        self.assertTrue(v["receipt_intact"])
        self.assertEqual((v["receipt_object_problems"], v["handoff_problems"]), ([], []))
        self.assertTrue(self.call("get_discovery_receipt", did)["structuredContent"]["intact"])
        self.assertTrue(self.call("create_v3_handoff", did)["structuredContent"]["ready"])
        report = self.call("render_discovery_report", did)
        self.assertIn("# Discovery", report["content"][0]["text"])

    def test_ad_hoc_tools(self):
        params = {"sqft": [1000, "sqft"], "occupancy": [0.85, ""], "rent": [12, "usd/sqft"], "build_cost": [8, "usd/sqft"]}
        sim = self.call("simulate_candidate", {"run_id": self.run_id, "model": F.PROFIT_MODEL, "parameters": params})
        self.assertConforms("simulate_candidate", sim)
        self.assertEqual(sim["structuredContent"]["simulation"]["kind"], "simulated")
        self.assertAlmostEqual(sim["structuredContent"]["simulation"]["outcomes"]["profit"]["mean"], 2200.0)
        sens = self.call("analyze_sensitivity", {"run_id": self.run_id, "model": F.PROFIT_MODEL, "parameters": params,
                                                 "success": F.rel(F.V("profit"), ">=", F.K(0, "usd"))})
        self.assertConforms("analyze_sensitivity", sens)
        self.assertAlmostEqual(sens["structuredContent"]["sensitivity"]["break_even"]["rent"]["value"], 8 / 0.85, 3)
        opt = self.call("optimize_solution", {"run_id": self.run_id, "problem": F.resource_design()["optimization"]})
        self.assertConforms("optimize_solution", opt)
        self.assertEqual((opt["structuredContent"]["status"], opt["structuredContent"]["solution"]),
                         ("optimal", {"x": 2.0, "y": 6.0}))
        pa = self.call("find_prior_art", {"run_id": self.run_id, "subject": F.PRIOR_ART_MATCHING["subject"],
                                          "queries": F.PRIOR_ART_MATCHING["queries"],
                                          "records": F.PRIOR_ART_MATCHING["records"],
                                          "coverage": F.PRIOR_ART_MATCHING["coverage"]})
        self.assertConforms("find_prior_art", pa)
        self.assertEqual(pa["structuredContent"]["conclusion"], "match_found")
        km = self.call("export_knowledge_map", {"run_id": self.run_id})
        self.assertConforms("export_knowledge_map", km)
        self.assertEqual(km["structuredContent"]["schema"], "lofgren.knowledge-map/2")

    def test_error_paths(self):
        cases = {
            "get_discovery_receipt": {"discovery_id": "DR-none"},
            "discover": {"run_id": "RR-00000000000000000000", "objective": "x"},
            "simulate_candidate": {"run_id": self.run_id, "model": {"name": "m"}, "parameters": {}},
            "optimize_solution": {"run_id": self.run_id, "problem": {"variables": []}},
            "find_prior_art": {"run_id": self.run_id, "subject": "a first-ever idea", "queries": ["x"], "records": [],
                               "coverage": F.PRIOR_ART_COVERAGE},
        }
        for name, args in cases.items():
            with self.subTest(tool=name):
                out = self.call(name, args)
                self.assertTrue(out.get("isError"), out)
        bad = self.call("discover", {"run_id": self.run_id, "objective": "x",
                                     "design": {"candidates": [{"description": "first-ever (fictional)"}]}})
        self.assertTrue(bad["isError"])

    def test_no_selection_has_no_handoff(self):
        out = self.call("discover", {"run_id": self.run_id, "objective": "Choose a warehouse size (fictional)",
                                     "design": F.infeasible_design()})["structuredContent"]
        self.assertEqual(out["outcome"], "infeasible")
        h = self.call("create_v3_handoff", {"discovery_id": out["discovery_id"]})["structuredContent"]
        self.assertFalse(h["ready"])


class OverStdio(unittest.TestCase):
    def test_a_real_session(self):
        p = subprocess.Popen([sys.executable, "-m", "lofgren_intelligence", "mcp"], cwd=ROOT, stdin=subprocess.PIPE,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8", bufsize=1)
        n = [0]

        def call(method: str, params: dict | None = None) -> dict:
            n[0] += 1
            p.stdin.write(json.dumps({"jsonrpc": "2.0", "id": n[0], "method": method, "params": params or {}}) + "\n")
            p.stdin.flush()
            return json.loads(p.stdout.readline())

        try:
            self.assertEqual(call("initialize", {"protocolVersion": "2025-06-18"})["result"]["protocolVersion"],
                             "2025-06-18")
            names = [t["name"] for t in call("tools/list")["result"]["tools"]]
            self.assertIn("discover", names)
            run = call("tools/call", {"name": "investigate", "arguments": {
                "objective": C.OBJECTIVE, "texts": C.AGREE_AND_CONFLICT}})["result"]["structuredContent"]["run_id"]
            d = call("tools/call", {"name": "discover", "arguments": {
                "run_id": run, "objective": "Choose a warehouse size (fictional)", "design": F.warehouse_design()}})
            self.assertEqual(d["result"]["structuredContent"]["outcome"], "candidate_selected")
            h = call("tools/call", {"name": "create_v3_handoff",
                                    "arguments": {"discovery_id": d["result"]["structuredContent"]["discovery_id"]}})
            self.assertTrue(h["result"]["structuredContent"]["ready"])
            p.stdin.write("{not json\n")
            p.stdin.flush()
            self.assertEqual(json.loads(p.stdout.readline())["error"]["code"], -32700)
            self.assertEqual(call("ping")["result"], {})
        finally:
            p.stdin.close()
            err = p.stderr.read()
            p.wait(timeout=30)
            p.stdout.close()
            p.stderr.close()
        self.assertEqual(err, "", "the server wrote to stderr")


if __name__ == "__main__":
    unittest.main()
