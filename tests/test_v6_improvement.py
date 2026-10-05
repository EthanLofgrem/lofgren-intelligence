from __future__ import annotations

import copy
import unittest

from lofgren_intelligence.improvement import (
    EvaluationDataset,
    ImprovementError,
    ImprovementProposal,
    MetricObservation,
    SafetyConstraint,
    evaluate_improvement,
    verify_improvement_receipt,
)
from lofgren_intelligence.improvement.certification import CODE_TERMS, _proposal, _v6_handoff, run_v6_certification


class ImprovementTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.handoff = _v6_handoff()

    def test_good_candidate_recommends_review_but_does_not_apply(self):
        out = evaluate_improvement(self.handoff, _proposal())
        self.assertEqual(out.decision, "recommend_review")
        self.assertFalse(out.receipt["mutation_performed"])
        self.assertFalse(out.next_cycle["apply_change"])
        self.assertTrue(out.next_cycle["review_required"])

    def test_low_gain_rejected(self):
        out = evaluate_improvement(self.handoff, _proposal(candidate=0.76, baseline=0.75))
        self.assertEqual(out.decision, "reject_candidate")

    def test_safety_regression_rejected(self):
        p = _proposal()
        unsafe = ImprovementProposal(
            p.proposal_id, p.baseline_id, p.candidate_id, p.change_summary,
            p.evaluation_dataset, p.primary_metric, p.min_gain,
            (SafetyConstraint("integrity", 0.99, 0.8, 0.01),),
            True,
        )
        out = evaluate_improvement(self.handoff, unsafe)
        self.assertEqual(out.decision, "reject_candidate")
        self.assertIn("integrity", out.receipt["failed_safety_metrics"])

    def test_non_held_out_dataset_refused(self):
        with self.assertRaises(ImprovementError):
            EvaluationDataset("DS", "hash", 100, False)

    def test_too_few_samples_refused(self):
        with self.assertRaises(ImprovementError):
            evaluate_improvement(self.handoff, _proposal(samples=2), minimum_samples=30)

    def test_receipt_tamper_fails(self):
        out = evaluate_improvement(self.handoff, _proposal())
        self.assertTrue(verify_improvement_receipt(out.receipt))
        bad = copy.deepcopy(out.receipt)
        bad["candidate_id"] = "forged"
        self.assertFalse(verify_improvement_receipt(bad))


class V6CertificationTests(unittest.TestCase):
    def test_all_terms_pass(self):
        cert = run_v6_certification()
        self.assertEqual(set(cert["terms"]), set(CODE_TERMS))
        self.assertTrue(cert["code_terms_certified"], cert)
        self.assertTrue(all(cert["terms"].values()))


if __name__ == "__main__":
    unittest.main()
