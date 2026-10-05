# V6 Improvement Intelligence

V6 closes the intelligence loop without granting itself permission to mutate
the running system.

A V6 proposal is evaluated only when it is bound to a measured V5 outcome and
uses a distinct baseline and candidate on an identified held-out evaluation
dataset.

## Required controls

- held-out evaluation data;
- minimum sample size;
- explicit primary metric and direction;
- declared minimum gain;
- independent safety metrics with maximum permitted regression;
- immutable outcome provenance;
- distinct baseline and candidate identities;
- human review required;
- no automatic policy/model/prompt/runtime mutation.

## Decisions

A candidate can produce only:

- `recommend_review` — the measured gain and safety constraints passed, but a
  human must still review and authorize any change;
- `reject_candidate` — gain or safety requirements failed.

The receipt always records `mutation_performed = false`. The next-cycle
handoff always records `apply_change = false` until a separately authorized
future action applies a reviewed change.

## Certification

`lofgren certify --v6` proves the V6 code terms. The exact-SHA process gate is
`scripts/v6_gate.py`, which independently verifies the GitHub Actions run,
the exact SHA, all Python 3.10/3.11/3.12 jobs, the required certification steps,
the clean-wheel package step, regression suites and a clean checkout.

V6 completion closes the V1→V6 governed intelligence architecture. It does not
by itself prove hosted public readiness; OAuth, tenancy, persistence, billing,
deployment, backup/restore, rollback and client interoperability remain
separate public-release gates.
