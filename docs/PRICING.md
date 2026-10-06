# Pricing model

Implemented in `lofgren_intelligence/billing/pricing.py`. All numbers are starting points to be checked against measured cost to serve during beta.

What the hosted service sells and grants is the versioned plan catalog
(`docs/PLAN_CATALOG.md`, `docs/PLAN_CATALOG.json`). The "Limit" column below
(entries) is a proposal that no code enforces; the catalog records it as
`unenforced_proposal` and marks every paid allowance `undecided`.

## Base unit and job classes

Base unit **u = $0.0312**. A job is metered in work units (WU) and falls into a class:

| Class | Up to | Price (pay as you go) | Typical work |
| --- | --- | --- | --- |
| Standard | 1 WU | 1u = $0.0312 | one light pass: research, verify, report |
| Verified | 4 WU | 4u = $0.1248 | several sources, cross-checked |
| Deep | 40 WU | 40u = $1.248 | multi-question investigation |
| Heavy | 400 WU | 400u = $12.48 | the full loop: research, verify, imagine, produce, fact-check, gaps, prior art, report |
| Project | > 400 WU | whole heavy units | estimated and approved before it runs |

## Plans

| Plan | Monthly fee | Standard rate | Included heavy | Extra heavy | Limit |
| --- | --- | --- | --- | --- | --- |
| Free | $0 | — | 1 trial | — | 25 entries/week; Research, Verify, Report only |
| Pay as you go | $0 | $0.0312 | 0 | $12.48 | 500/week |
| Researcher | $49.99 | $0.0156 | 4 | $9.36 (25% off) | 400/week |
| Good Idea | $79.99 | $0.0050 | 6 | $6.24 (50% off) | 20,000/month |

The Researcher fee converts into heavy credits: 4 × $12.48 = $49.92, so a user running four or more heavy jobs a month comes out ahead, and every standard unit is half price.

## Monthly bill

```
Bill = F + r·n + p·max(0, h − k) + data_cost·(1 + markup)
```

F = fee, r = rate, n = standard units, p = extra heavy price, h = heavy jobs, k = included heavy jobs, markup = 20% on licensed third-party data.

Which plan is cheapest:

- few heavy jobs → Pay as you go
- 5+ heavy jobs → Researcher
- about 10+ heavy jobs, or 6+ heavy jobs with about 1,100+ standard units → Good Idea

`lofgren pricing --standard-units N --heavy H` prints the comparison.

## Rules that protect users

1. Every job is estimated before it runs.
2. The contract's spend cap blocks any job whose estimate exceeds it.
3. The charge is never more than the estimate.
4. Licensed data is passed through at cost plus a stated markup.

## Rule that protects the business

For every price: `price × (1 − target_margin) ≥ cost to serve`. At a 50% margin a $0.005 Good Idea unit must cost under $0.0025 to run, which means small models, caching and fair-use limits for that tier. `billing.max_cost_to_serve()` computes the ceiling.
