"""The V1 -> V2 boundary certification itself (boundary certification, phase B).

These tests pin what the certification reports, so it cannot drift silently: every scenario passes, every code
term is TRUE and the command exits 0. V1-FUTURE-DATED-EVIDENCE, pinned here as a defect until LI-V1-HARDEN-07A, is
now a passing adversarial scenario (tests/test_v1_future_evidence.py covers the rule in detail).
"""

from __future__ import annotations

import io
import json
import re
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from lofgren_intelligence import boundary_certification as bc
from lofgren_intelligence.cli import main


class BoundaryCertification(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cert = bc.run_boundary_certification()
        cls.by_name = {s["scenario"]: s for s in cls.cert["scenarios"]}

    def test_every_code_term_has_scenarios(self):
        terms = {s["term"] for s in self.cert["scenarios"]}
        self.assertEqual(terms, set(bc.CODE_TERMS))
        self.assertEqual(set(self.cert["terms"]), set(bc.CODE_TERMS))
        self.assertEqual(set(bc.CODE_TERMS) | set(bc.PROCESS_TERMS), set(bc.GATE_ORDER))
        self.assertEqual(len(bc.GATE_ORDER), 16)

    def test_required_adversarial_cases_are_covered(self):
        required = {"fabricated source", "copied evidence", "outdated evidence", "future-dated evidence",
                    "wrong geography", "conflicting evidence", "unsupported claim", "hypothesis as fact",
                    "cross-question leak", "recomputed-map tampering", "receipt tampering", "malformed lineage",
                    "source-quality tampering"}
        adversarial = {s["scenario"] for s in self.cert["scenarios"] if s["term"] == "AdversarialSuitePassing"}
        self.assertTrue(required <= adversarial, required - adversarial)

    def test_all_scenarios_pass(self):
        failed = sorted(s["scenario"] for s in self.cert["scenarios"] if not s["passed"])
        self.assertEqual(failed, [], [self.by_name[n]["detail"] for n in failed])
        adversarial = [s for s in self.cert["scenarios"] if s["term"] == "AdversarialSuitePassing"]
        self.assertEqual(len(adversarial), 14)

    def test_future_dated_evidence_passes(self):
        # Fixed by LI-V1-HARDEN-07A (was the pinned defect V1-FUTURE-DATED-EVIDENCE).
        check = self.by_name["future-dated evidence"]
        self.assertTrue(check["passed"], check["detail"])
        self.assertNotIn("KNOWN DEFECT", check["detail"])
        self.assertTrue(all(self.cert["terms"].values()), self.cert["terms"])
        self.assertTrue(self.cert["code_terms_certified"])

    def test_a_crash_is_a_failure(self):
        def boom() -> str:
            raise RuntimeError("crash")
        check = bc._run_one("V1Certified", "crash", boom)
        self.assertFalse(check.passed)
        self.assertIn("RuntimeError", check.detail)

    def test_render_and_cli(self):
        text = bc.render_boundary(self.cert)
        self.assertIn("BoundaryCodeTerms = TRUE", text)
        self.assertNotIn("V1ReadyForV2Step4", text)  # only the gate script may print the verdict
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "boundary.json"
            with redirect_stdout(io.StringIO()):
                code = main(["certify-boundary", "--out", str(out)])
            self.assertEqual(code, 0)
            self.assertTrue(json.loads(out.read_text(encoding="utf-8"))["code_terms_certified"])


class GateScript(unittest.TestCase):
    def test_gate_requires_steps_the_workflow_has(self):
        import importlib.util

        root = Path(__file__).resolve().parent.parent
        spec = importlib.util.spec_from_file_location("boundary_gate", root / "scripts" / "boundary_gate.py")
        gate = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(gate)
        workflow = (root / ".github" / "workflows" / "tests.yml").read_text(encoding="utf-8")
        steps = set(re.findall(r"^\s*- name: (.+?)\s*$", workflow, re.M))
        self.assertTrue(set(gate.REQUIRED_STEPS) <= steps, set(gate.REQUIRED_STEPS) - steps)
        for py in gate.MATRIX:
            self.assertIn(f'"{py}"', workflow)
        self.assertEqual(gate.GATE_ORDER, bc.GATE_ORDER)
        self.assertIn("certify-boundary", workflow)


if __name__ == "__main__":
    unittest.main()
