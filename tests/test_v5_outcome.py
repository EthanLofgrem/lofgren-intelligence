from __future__ import annotations

import copy
import unittest
from datetime import timedelta

from lofgren_intelligence.execution.certification import NOW, _base as v4_base
from lofgren_intelligence.execution.core import InMemoryAdapter, execute_authorized
from lofgren_intelligence.outcome import (
    Measurement,
    OutcomeError,
    build_measurement_contract,
    evaluate_outcome,
    verify_outcome_receipt,
)
from lofgren_intelligence.outcome.certification import CODE_TERMS, run_v5_certification


class V5Fixture(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        product, req, grant, approval = v4_base()
        cls.executed = execute_authorized(
            product.v4_handoff, req, grant, approval, InMemoryAdapter(),
            subject="user-cert", now=NOW,
        )
        assert cls.executed.v5_handoff is not None
        cls.contracts = build_measurement_contract(cls.executed.v5_handoff)

    def measurements(self):
        return [
            Measurement(
                c.metric, c.expected, c.unit,
                (NOW + timedelta(days=30)).isoformat(),
                "test-observation", f"OBS-{i}",
            )
            for i, c in enumerate(self.contracts, 1)
        ]


class OutcomeTests(V5Fixture):
    def test_successful_measurement_receipt(self):
        out = evaluate_outcome(self.executed.v5_handoff, self.measurements(), contracts=self.contracts)
        self.assertEqual(out.receipt["status"], "successful")
        self.assertTrue(verify_outcome_receipt(out.receipt))
        self.assertTrue(out.v6_handoff["improvement_allowed"])

    def test_missing_metric_blocks_improvement(self):
        ms = self.measurements()
        out = evaluate_outcome(self.executed.v5_handoff, ms[:-1], contracts=self.contracts)
        self.assertEqual(out.receipt["status"], "insufficient_measurement")
        self.assertFalse(out.v6_handoff["improvement_allowed"])

    def test_unit_mismatch_fails(self):
        ms = self.measurements()
        ms[0] = Measurement(ms[0].metric, ms[0].value, "bad-unit", ms[0].observed_at, ms[0].source)
        with self.assertRaises(OutcomeError):
            evaluate_outcome(self.executed.v5_handoff, ms, contracts=self.contracts)

    def test_default_is_descriptive_not_causal(self):
        out = evaluate_outcome(self.executed.v5_handoff, self.measurements(), contracts=self.contracts)
        self.assertEqual(out.receipt["causal"]["standing"], "descriptive_only")

    def test_invalid_causal_design_fails(self):
        with self.assertRaises(OutcomeError):
            evaluate_outcome(
                self.executed.v5_handoff, self.measurements(), contracts=self.contracts,
                causal_design={"kind": "observational", "validated": True},
            )

    def test_receipt_tampering_fails(self):
        out = evaluate_outcome(self.executed.v5_handoff, self.measurements(), contracts=self.contracts)
        bad = copy.deepcopy(out.receipt)
        bad["measurements"][0]["actual"] += 1
        self.assertFalse(verify_outcome_receipt(bad))


class V5CertificationTests(unittest.TestCase):
    def test_all_terms_pass(self):
        cert = run_v5_certification()
        self.assertEqual(set(cert["terms"]), set(CODE_TERMS))
        self.assertTrue(cert["code_terms_certified"], cert)
        self.assertTrue(all(cert["terms"].values()))


if __name__ == "__main__":
    unittest.main()
