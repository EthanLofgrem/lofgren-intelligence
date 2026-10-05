from __future__ import annotations

import unittest

from lofgren_intelligence.hosted.service import PublicService
from lofgren_intelligence.intent.clarification import (
    clarify_objective,
    requires_clarification,
)


OBJECTIVE = "Design a new low-cost water purification system for remote communities."


class ClarificationEngineTests(unittest.TestCase):
    def test_large_design_objective_requires_clarification(self):
        self.assertTrue(requires_clarification(OBJECTIVE))
        result = clarify_objective(OBJECTIVE)
        self.assertEqual(result.status, "CLARIFICATION_REQUIRED")
        self.assertGreaterEqual(len(result.questions), 3)
        self.assertLessEqual(len(result.questions), 7)
        keys = [q.key for q in result.questions]
        self.assertIn("location", keys)
        self.assertIn("problem", keys)
        self.assertIn("success", keys)

    def test_unknown_is_valid_and_preserved(self):
        result = clarify_objective(OBJECTIVE, {"location": "unknown"})
        self.assertEqual(result.status, "CLARIFICATION_REQUIRED")
        self.assertEqual(result.accepted_answers["location"]["state"], "unknown")
        self.assertIn("location", result.critical_unknowns)
        self.assertNotIn("location", [q.key for q in result.questions])

    def test_complete_answers_create_case_charter_but_do_not_self_approve(self):
        answers = {
            "location": "Rural northern Kenya",
            "users": "A village system serving about 100 people",
            "problem": "Microbial contamination and turbidity",
            "success": "At least 500 liters/day for prototype testing",
            "cost": "Maximum installed prototype cost $500",
            "constraints": "No reliable grid power; locally serviceable parts",
            "source_or_environment": "Seasonal surface water and shallow wells",
        }
        result = clarify_objective(OBJECTIVE, answers)
        self.assertEqual(result.status, "READY_FOR_SCOPE_APPROVAL")
        self.assertIsNotNone(result.case_charter)
        self.assertTrue(result.case_charter["approval_required_before_research"])
        self.assertNotIn("approved", result.case_charter)

    def test_non_design_research_is_not_forced_through_design_interview(self):
        result = clarify_objective("Research the history of desalination membranes.")
        self.assertEqual(result.status, "READY_FOR_SCOPE_APPROVAL")
        self.assertEqual(result.round, 0)


class HostedClarificationGateTests(unittest.TestCase):
    def setUp(self):
        self.service = PublicService(store=object())

    def test_plan_returns_questions_before_expensive_work(self):
        out = self.service.plan_research({"objective": OBJECTIVE})
        self.assertEqual(out["status"], "CLARIFICATION_REQUIRED")
        self.assertIn("questions", out)
        self.assertIn("message", out)

    def test_investigate_returns_questions_before_touching_store(self):
        out = self.service.investigate("user-1", {"objective": OBJECTIVE})
        self.assertEqual(out["status"], "CLARIFICATION_REQUIRED")

    def test_approved_matching_charter_allows_planning(self):
        charter = {"objective": OBJECTIVE, "approved": True}
        out = self.service.plan_research({
            "objective": OBJECTIVE,
            "case_charter": charter,
            "texts": {"fixture": "Water treatment evidence."},
        })
        self.assertIn("estimate", out)

    def test_wrong_objective_charter_does_not_bypass_gate(self):
        out = self.service.plan_research({
            "objective": OBJECTIVE,
            "case_charter": {"objective": "Something else", "approved": True},
        })
        self.assertEqual(out["status"], "CLARIFICATION_REQUIRED")


if __name__ == "__main__":
    unittest.main()
