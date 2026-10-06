import importlib.util
from pathlib import Path
import unittest

spec = importlib.util.spec_from_file_location('economics', Path(__file__).resolve().parents[1] / 'scripts/li_customer_economics.py')
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


class EconomicsTests(unittest.TestCase):
    def scenario(self):
        return dict(target_mrr=75000, monthly_price=100, visit_to_activation=.1,
                    activation_to_paid=.2, first_renewal_rate=.8, cost_per_visit=1,
                    variable_cost_per_customer=30, fixed_monthly_cost=1000)

    def test_funnel_and_renewal_are_distinct(self):
        r = m.calculate(self.scenario())
        self.assertEqual((r['new_paid_customers_required'], r['activations_required'], r['qualified_visits_required']), (750, 3750, 37500))
        self.assertEqual(r['expected_first_renewal_mrr_without_new_sales'], 60000)
        self.assertEqual(r['replacement_paid_customers_for_original_target'], 150)
        self.assertEqual(r['modeled_cac'], 50)

    def test_nonpositive_contribution_has_no_payback(self):
        s = self.scenario()
        s['variable_cost_per_customer'] = 101
        self.assertIsNone(m.calculate(s)['simple_cac_payback_months'])

    def test_invalid_inputs_fail(self):
        for key, value in [('monthly_price', 0), ('first_renewal_rate', 0),
                           ('activation_to_paid', 1.1), ('cost_per_visit', -1),
                           ('target_mrr', float('nan')), ('monthly_price', True)]:
            s = self.scenario()
            s[key] = value
            with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                m.calculate(s)

    def row(self, identity, outcome):
        return dict(customer_id=identity, activated=True, repeat_case=True,
                    paid=True, renewal_eligible=True, renewal_outcome=outcome)

    def test_pending_renewals_prevent_final_rate(self):
        r = m.cohort_metrics([self.row('a', 'renewed'), self.row('b', 'pending')])
        self.assertEqual(r['renewal_pending'], 1)
        self.assertIsNone(r['renewal_rate_all_eligible'])
        self.assertEqual(r['renewal_rate_among_resolved'], 1)

    def test_resolved_cohort_and_empty_cohort(self):
        r = m.cohort_metrics([self.row('a', 'renewed'), self.row('b', 'not_renewed')])
        self.assertEqual(r['renewal_rate_all_eligible'], .5)
        self.assertIsNone(m.cohort_metrics([])['renewal_rate_among_resolved'])

    def test_duplicate_and_impossible_cohorts_fail(self):
        a = self.row('a', 'renewed')
        with self.assertRaises(ValueError):
            m.cohort_metrics([a, a])
        a['paid'] = False
        with self.assertRaises(ValueError):
            m.cohort_metrics([a])


if __name__ == '__main__':
    unittest.main()
