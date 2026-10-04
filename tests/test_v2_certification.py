"""The V2 certification itself (`lofgren certify --v2`) and the V2 gate script, pinned so they cannot drift."""

from __future__ import annotations

import importlib.util
import io
import json
import re
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from lofgren_intelligence.cli import main
from lofgren_intelligence.discovery import certification as v2

ROOT = Path(__file__).resolve().parent.parent
E2E = [f"{i} " for i in range(1, 13)]


class V2Certification(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cert = v2.run_v2_certification()

    def test_every_scenario_passes(self):
        failed = [(s["scenario"], s["detail"]) for s in self.cert["scenarios"] if not s["passed"]]
        self.assertEqual(failed, [])
        self.assertTrue(all(self.cert["terms"].values()), self.cert["terms"])
        self.assertTrue(self.cert["code_terms_certified"])

    def test_the_twelve_end_to_end_scenarios(self):
        e2e = [s["scenario"] for s in self.cert["scenarios"] if s["term"] == "E2ECertificationPassing"]
        self.assertEqual(len(e2e), 12)
        for prefix in E2E:
            self.assertEqual(sum(1 for name in e2e if name.startswith(prefix)), 1, prefix)

    def test_terms(self):
        self.assertEqual(set(self.cert["terms"]), set(v2.CODE_TERMS))
        self.assertEqual(set(v2.GATE_ORDER), set(v2.CODE_TERMS) | set(v2.PROCESS_TERMS))
        self.assertEqual({s["term"] for s in self.cert["scenarios"]}, set(v2.CODE_TERMS))

    def test_a_crash_is_a_failure(self):
        def boom() -> str:
            raise RuntimeError("crash")

        self.assertFalse(v2._run_one("V1Certified", "crash", boom).passed)

    def test_cli_and_rendering(self):
        text = v2.render_v2_certification(self.cert)
        self.assertIn("V2CodeTerms = TRUE", text)
        self.assertNotIn("V2ReadyForV3", text)  # only the gate script may print the verdict
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "v2.json"
            with redirect_stdout(io.StringIO()):
                self.assertEqual(main(["certify", "--v2", "--out", str(out)]), 0)
            self.assertTrue(json.loads(out.read_text(encoding="utf-8"))["code_terms_certified"])


class GateScript(unittest.TestCase):
    def test_gate_requires_steps_the_workflow_has(self):
        spec = importlib.util.spec_from_file_location("v2_gate", ROOT / "scripts" / "v2_gate.py")
        gate = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(gate)
        workflow = (ROOT / ".github" / "workflows" / "tests.yml").read_text(encoding="utf-8")
        steps = set(re.findall(r"^\s*- name: (.+?)\s*$", workflow, re.M))
        self.assertTrue(set(gate.REQUIRED_STEPS) <= steps, set(gate.REQUIRED_STEPS) - steps)
        self.assertIn("certify --v2", workflow)
        self.assertEqual(gate.GATE_ORDER, v2.GATE_ORDER)


if __name__ == "__main__":
    unittest.main()
