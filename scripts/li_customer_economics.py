"""Q22 scenario arithmetic. Outputs are assumptions, never observed demand."""
import argparse
import json
import math


def calculate(s):
    required = ('target_mrr', 'monthly_price', 'visit_to_activation',
                'activation_to_paid', 'first_renewal_rate', 'cost_per_visit',
                'variable_cost_per_customer', 'fixed_monthly_cost')
    if set(s) != set(required):
        raise ValueError('Supply exactly: ' + ', '.join(required))
    for key, value in s.items():
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise ValueError(key + ' must be a finite number')
        if value < 0:
            raise ValueError(key + ' must be nonnegative')
    if s['target_mrr'] <= 0 or s['monthly_price'] <= 0:
        raise ValueError('Target and price must be positive')
    for key in ('visit_to_activation', 'activation_to_paid', 'first_renewal_rate'):
        if not 0 < s[key] <= 1:
            raise ValueError(key + ' must be in (0, 1]')
    customers = math.ceil(s['target_mrr'] / s['monthly_price'])
    activations = math.ceil(customers / s['activation_to_paid'])
    visits = math.ceil(activations / s['visit_to_activation'])
    spend = visits * s['cost_per_visit']
    contribution = s['monthly_price'] - s['variable_cost_per_customer']
    retained = customers * s['first_renewal_rate']
    return {
        'evidence_status': 'SCENARIO_ONLY_NOT_OBSERVED',
        'new_paid_customers_required': customers,
        'activations_required': activations,
        'qualified_visits_required': visits,
        'modeled_launch_mrr': customers * s['monthly_price'],
        'acquisition_spend': spend,
        'modeled_cac': spend / customers,
        'contribution_per_customer_month': contribution,
        'contribution_margin_fraction': contribution / s['monthly_price'],
        'simple_cac_payback_months': (spend / customers / contribution) if contribution > 0 else None,
        'launch_month_contribution_after_acquisition_and_fixed_cost': customers * contribution - spend - s['fixed_monthly_cost'],
        'expected_first_renewal_customers': retained,
        'expected_first_renewal_mrr_without_new_sales': retained * s['monthly_price'],
        'replacement_paid_customers_for_original_target': max(0, math.ceil(customers - retained)),
        'limitations': ['No observed demand or renewal evidence.',
                       'Payback assumes constant contribution and ignores churn; not an LTV estimate.',
                       'All customers are assumed renewal-eligible together; real reporting needs cohorts.',
                       'Variable cost must include payment fees, research, refunds and support.',
                       'Cost per visit excludes founder labor unless explicitly included.'],
    }


def cohort_metrics(rows):
    """Explicit denominators; unknown renewal outcomes are excluded, not counted as successes."""
    allowed = {'customer_id', 'activated', 'repeat_case', 'paid', 'renewal_eligible', 'renewal_outcome'}
    seen = set()
    for r in rows:
        if set(r) != allowed or not isinstance(r['customer_id'], str) or not r['customer_id']:
            raise ValueError('Malformed cohort row')
        if r['customer_id'] in seen:
            raise ValueError('Duplicate customer')
        seen.add(r['customer_id'])
        if any(type(r[k]) is not bool for k in ('activated', 'repeat_case', 'paid', 'renewal_eligible')):
            raise ValueError('Flags must be booleans')
        if r['renewal_outcome'] not in ('renewed', 'not_renewed', 'pending', 'not_applicable'):
            raise ValueError('Invalid renewal outcome')
        if r['repeat_case'] and not r['activated']:
            raise ValueError('Repeat case requires activation')
        if r['renewal_eligible'] and not r['paid']:
            raise ValueError('Renewal eligibility requires payment')
        if not r['renewal_eligible'] and r['renewal_outcome'] != 'not_applicable':
            raise ValueError('Ineligible customer cannot have renewal outcome')
        if r['renewal_eligible'] and r['renewal_outcome'] == 'not_applicable':
            raise ValueError('Eligible customer needs outcome or pending')
    activated = sum(r['activated'] for r in rows)
    paid = sum(r['paid'] for r in rows)
    repeat = sum(r['repeat_case'] for r in rows)
    eligible = sum(r['renewal_eligible'] for r in rows)
    renewed = sum(r['renewal_outcome'] == 'renewed' for r in rows)
    resolved = sum(r['renewal_outcome'] in ('renewed', 'not_renewed') for r in rows)
    return {'customers': len(rows), 'activated': activated, 'paid': paid,
            'repeat_case': repeat, 'repeat_rate_among_activated': repeat / activated if activated else None,
            'renewal_eligible': eligible, 'renewal_resolved': resolved,
            'renewal_pending': eligible - resolved,
            'renewed': renewed, 'renewal_rate_among_resolved': renewed / resolved if resolved else None,
            'renewal_rate_all_eligible': renewed / eligible if eligible and resolved == eligible else None}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('input', help='JSON scenario or cohort rows')
    parser.add_argument('--cohort', action='store_true')
    args = parser.parse_args()
    with open(args.input, encoding='utf-8') as stream:
        data = json.load(stream)
    print(json.dumps(cohort_metrics(data) if args.cohort else calculate(data), indent=2, allow_nan=False))
