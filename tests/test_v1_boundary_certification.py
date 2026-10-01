"""The V1 -> V2 boundary certification itself (boundary certification, phase B).

These tests pin what the certification reports, so it cannot drift silently: every scenario passes except the
pinned defect V1-FUTURE-DATED-EVIDENCE, which keeps AdversarialSuitePassing (and the gate) FALSE. When that defect is
fixed, test_future_dated_evidence_is_a_pinned_defect fails and must be updated with the fix.
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

    def test_all_scenarios_pass_except_the_pinned_defect(self):
        failed = sorted(s["scenario"] for s in self.cert["scenarios"] if not s["passed"])
        self.assertEqual(failed, ["future-dated evidence"], [self.by_name[n]["detail"] for n in failed])

    def test_future_dated_evidence_is_a_pinned_defect(self):
        detail = self.by_name["future-dated evidence"]["detail"]
        self.assertIn("KNOWN DEFECT V1-FUTURE-DATED-EVIDENCE", detail)
        self.assertFalse(self.cert["terms"]["AdversarialSuitePassing"])
        self.assertFalse(self.cert["code_terms_certified"])
        others = {t: v for t, v in self.cert["terms"].items() if t != "AdversarialSuitePassing"}
        self.assertTrue(all(others.values()), others)

    def test_a_crash_is_a_failure(self):
        def boom() -> str:
            raise RuntimeError("crash")
        check = bc._run_one("V1Certified", "crash", boom)
        self.assertFalse(check.passed)
        self.assertIn("RuntimeError", check.detail)

    def test_render_and_cli(self):
        text = bc.render_boundary(self.cert)
        self.assertIn("BoundaryCodeTerms = FALSE", text)
        self.assertNotIn("V1ReadyForV2Step4", text)  # only the gate script may print the verdict
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "boundary.json"
            with redirect_stdout(io.StringIO()):
                code = main(["certify-boundary", "--out", str(out)])
            self.assertEqual(code, 1)
            self.assertFalse(json.loads(out.read_text(encoding="utf-8"))["code_terms_certified"])


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
