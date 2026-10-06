# Q22 customer acquisition and retention validation

This harness supports issue #22. It does not invoke LI, prove demand, or authorize a launch. No changes to Claude's clarification, account deletion, source adapters, PostgreSQL workflow or shared integration branch are required.

## Reproduce

Run `python -m unittest discover -s tests -p test_li_customer_economics.py`.
Run `python scripts/li_customer_economics.py scenario.json` with these explicit scenario assumptions:

```json
{"target_mrr":75000,"monthly_price":100,"visit_to_activation":0.1,"activation_to_paid":0.2,"first_renewal_rate":0.8,"cost_per_visit":1,"variable_cost_per_customer":30,"fixed_monthly_cost":1000}
```

The $100 price is hypothetical, not an approved LI plan. This example requires 750 paid customers, 3,750 activations and 37,500 qualified visits. Its modeled first renewal falls to $60,000 MRR without new sales. None of these values is observed customer evidence.

## Experiment contract

Before recruitment, record buyer segment, recurring task, acquisition channel, offer, pricing status, sample size, follow-up window, budget, pass/fail thresholds, primary outcome, exclusions and stopping rule. Thresholds require a dated decision before results; leave them UNDECIDED rather than invent benchmarks. Recruitment, paid experiments and publication remain pending explicit authorization.

Compare LI against the participant's existing workflow on equivalent tasks. Counterbalance order, retain unsuccessful runs and score anonymized evidence packages independently where practical. Measure time to a usable answer, material claim errors, evidence coverage, unknowns and participant task completion. Do not infer buyer willingness to pay from LI's own assessment.

Activation: an approved research case completes and the customer inspects evidence or its receipt. Repeat usage: a subsequent distinct case, excluding automated retries and staff tests. Paid conversion: a genuine successful customer payment, excluding synthetic accounts. Renewal: a successful scheduled renewal after the customer becomes eligible, using an explicit grace period and final observation cutoff. Revenue reporting must distinguish subscription MRR from annual cash receipts, refunds and one-time purchases.

## Minimum event contract

Each event needs event_id, UTC occurred_at, pseudonymous customer_id, experiment/cohort ID, source, environment (test/staging/production), event type and applicable case/job/payment identifier. Deduplicate event IDs and reconcile payments against the billing source of truth. Do not capture raw research queries or unnecessary personal data. Synthetic and production cohorts must never be combined.

Events: offer_viewed, signup, case_approved, research_completed, evidence_viewed, repeat_case_completed, payment_succeeded, cancellation_requested, renewal_eligible, renewal_succeeded, renewal_not_paid_after_grace. Keep pending follow-up separate from failed renewal. Record real usage cost and support minutes without treating work units as dollars.

## Cohort arithmetic

Run `python scripts/li_customer_economics.py cohort.json --cohort`. Rows contain exactly customer_id, activated, repeat_case, paid, renewal_eligible and renewal_outcome. Flags are booleans. Outcomes are renewed, not_renewed, pending or not_applicable. The calculator rejects duplicate customers and inconsistent eligibility. A final all-eligible renewal rate is withheld while outcomes are pending; the resolved subset rate is explicitly labeled and may be biased by incomplete follow-up. These rows are a reporting extract, not a substitute for an event ledger or independently verified payments.

## Production handoff

Claude owns the unpublished LI offer prototype. Integrate this calculator and event contract only after review. Attach actual LI stage receipts, exact SHA, source manifest, budget, artifact hashes and test outputs. Report technical, market and renewal evidence separately. For each unmeasured outcome name the next evidence-producing action. No commercial conclusion can be certified from this harness alone.
