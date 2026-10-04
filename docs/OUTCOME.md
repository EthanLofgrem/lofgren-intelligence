# V5 Outcome Intelligence

V5 measures what happened after a committed V4 action.

It converts V4 expected outcomes into typed measurement contracts, accepts
explicit observations, compares expected to actual, records missing metrics,
and produces a tamper-evident outcome receipt plus a V6 handoff.

V5 deliberately distinguishes observation from causality:

- normal measurements are **descriptive only**;
- correlation or temporal sequence is not promoted into causal proof;
- causal standing requires a separately validated randomized or controlled
  quasi-experimental design and remains bounded to that design's scope.

Missing measurements produce `insufficient_measurement` and block improvement.
Unit mismatches fail closed.

Certification:

- `lofgren certify --v5` — V5 code terms;
- `scripts/v5_gate.py` — exact-SHA `V5ReadyForV6` process gate.

A V5 pass proves the bounded measurement contract. It does not establish that
every real-world outcome is measurable or causally attributable.
