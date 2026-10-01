import unittest

from lofgren_intelligence.authority import Action, decide
from lofgren_intelligence.billing import (
    BASE_UNIT_USD,
    PLANS,
    cheapest_plan,
    classify,
    estimate,
    max_cost_to_serve,
    monthly_bill,
)
from lofgren_intelligence.intent import LOFGREN_VENTURE_STAGES, compile_intent


class IntentTests(unittest.TestCase):
    def test_venture_uses_the_seven_lofgren_stages(self):
        c = compile_intent("Find a profitable business opportunity in Phoenix under $5,000")
        self.assertEqual(c.mode, "venture")
        self.assertEqual([q.stage for q in c.questions], [s for s, _ in LOFGREN_VENTURE_STAGES])
        self.assertEqual(c.project_budget_usd, 5000)
        self.assertEqual(c.location["name"], "Phoenix")

    def test_verify_mode_and_money_scales(self):
        c = compile_intent("Is it true that the plant cost $2.5 million?")
        self.assertEqual(c.mode, "verify")
        self.assertEqual(c.project_budget_usd, 2_500_000)
        self.assertEqual({q.role for q in c.questions}, {"claim", "support", "contradict", "gap"})

    def test_coordinates_make_it_physical(self):
        c = compile_intent("How has land use changed at 33.4484, -112.0740?")
        self.assertTrue(c.physical)
        self.assertEqual(c.location["lat"], 33.4484)
        self.assertIn("orbital_passes", c.questions[0].needs)

    def test_contract_defaults_require_approval_for_actions(self):
        c = compile_intent("Research solar panel recycling")
        for kind in ("spend_money", "deploy", "publish", "send_message", "control_device"):
            self.assertIn(kind, c.approval_required)

    def test_empty_objective_rejected(self):
        with self.assertRaises(ValueError):
            compile_intent("   ")


class BillingTests(unittest.TestCase):
    def test_heavy_equals_400_base_units(self):
        self.assertAlmostEqual(PLANS["payg"].heavy_price, 400 * BASE_UNIT_USD)
        self.assertAlmostEqual(PLANS["researcher"].rate, BASE_UNIT_USD / 2)

    def test_classes(self):
        self.assertEqual(classify(1), "standard")
        self.assertEqual(classify(3), "verified")
        self.assertEqual(classify(30), "deep")
        self.assertEqual(classify(300), "heavy")
        self.assertEqual(classify(1000), "project")

    def test_class_prices_payg(self):
        self.assertAlmostEqual(estimate("payg", 1).platform_usd, 0.0312)
        self.assertAlmostEqual(estimate("payg", 4).platform_usd, 0.1248)
        self.assertAlmostEqual(estimate("payg", 40).platform_usd, 1.248)
        self.assertAlmostEqual(estimate("payg", 400).platform_usd, 12.48)
        self.assertAlmostEqual(estimate("payg", 1000).platform_usd, 3 * 12.48)

    def test_included_heavy_credits(self):
        self.assertEqual(estimate("researcher", 400).platform_usd, 0.0)
        self.assertAlmostEqual(estimate("researcher", 400, heavy_credits_left=0).platform_usd, 9.36)

    def test_data_passthrough_markup(self):
        self.assertAlmostEqual(estimate("payg", 1, data_cost_usd=10).data_usd, 12.0)

    def test_free_plan_limits(self):
        self.assertFalse(estimate("free", 30).allowed)  # deep not in free
        self.assertTrue(estimate("free", 3).allowed)
        self.assertTrue(estimate("free", 300).allowed)  # one heavy trial

    def test_bill_formula(self):
        # F + r*n + p*max(0, h-k)
        self.assertAlmostEqual(monthly_bill("researcher", 1000, 6), 49.99 + 15.60 + 2 * 9.36, places=2)
        self.assertAlmostEqual(monthly_bill("good_idea", 20_000, 0), 179.99, places=2)

    def test_every_paid_plan_wins_somewhere(self):
        self.assertEqual(cheapest_plan(500, 0), "payg")
        self.assertEqual(cheapest_plan(0, 5), "researcher")
        self.assertEqual(cheapest_plan(0, 10), "good_idea")

    def test_margin_guard(self):
        self.assertAlmostEqual(max_cost_to_serve(0.005, 0.5), 0.0025)


class AuthorityTests(unittest.TestCase):
    def setUp(self):
        self.c = compile_intent("Research drone delivery", max_spend_usd=10)

    def test_research_within_cap_allowed(self):
        self.assertTrue(decide(Action("research", "run", cost_usd=2), self.c).allowed)

    def test_over_cap_blocked(self):
        d = decide(Action("research", "run", cost_usd=11), self.c)
        self.assertFalse(d.allowed)
        self.assertIn("cap", d.reasons[0])

    def test_deploy_needs_approval(self):
        d = decide(Action("deploy", "ship site"), self.c)
        self.assertFalse(d.allowed)
        self.assertTrue(d.needs_approval)
        self.assertTrue(decide(Action("deploy", "ship site"), self.c, approved=True).allowed)

    def test_forbidden_even_if_approved(self):
        d = decide(Action("control_third_party_satellite", "x"), self.c, approved=True)
        self.assertFalse(d.allowed)

    def test_unvalidated_blocked(self):
        self.assertFalse(decide(Action("research", "x", validated=False), self.c).allowed)


if __name__ == "__main__":
    unittest.main()
