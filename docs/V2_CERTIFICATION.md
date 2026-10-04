# V2 certification: the gate before V3

V3 (Production Intelligence) does not start because V2 compiles. It starts when one exact, pushed commit passes
`V2ReadyForV3`, reproduced from a fresh clone.

```
V2ReadyForV3 = V1Certified ∧ EvidenceBoundaryPreserved ∧ PriorArtImplemented ∧ GapAnalysisImplemented
             ∧ HypothesisLifecycleSafe ∧ CandidateLifecycleSafe ∧ SimulationReproducible ∧ SensitivityImplemented
             ∧ OptimizationVerified ∧ DiscoveryVerifierPassing ∧ CostLedgerComplete ∧ DiscoveryReceiptsReproducible
             ∧ MCPContractsStable ∧ ProviderAbstractionStable ∧ AdversarialSuitePassing ∧ E2ECertificationPassing
             ∧ V3HandoffValidated
             ∧ V2RegressionPassing ∧ V1BoundaryCertified ∧ PackageGatePassing ∧ GitHubCIPassing
             ∧ WorkingTreeClean ∧ ExactSHAPinned
```

Unknown is false.

## Code terms: `lofgren certify --v2`

`lofgren_intelligence/discovery/certification.py` runs executable scenarios (fictional fixtures, heuristic
provider, no network, no model) and prints each term with the scenarios behind it. It exits non-zero unless every
term is TRUE. A scenario passes only by returning; an assertion or any crash fails it.

The twelve end-to-end scenarios each build a real V1 run first, then run the whole discovery pipeline, and inspect
the artifacts (statuses, outcomes, fingerprints, receipt verification, handoff validation, wording):

| # | Scenario | What must hold |
| --- | --- | --- |
| 1 | Scientific reasoning | every contradiction gets at least two competing explanations, plus counter-hypotheses |
| 2 | Technical engineering | a candidate breaking a physical constraint is infeasible; a design variable takes the verified sensor reading (42 °C) and the handoff cites that claim |
| 3 | Business and economics | break-even rent = 8 / 0.85 and occupancy = 8 / 12, computed by bisection |
| 4 | Geospatial and physical | every hypothesis keeps the frame's period and place |
| 5 | Software and system design | dependencies, specifications and acceptance criteria reach the handoff |
| 6 | Resource optimization | the LP optimum (x = 2, y = 6, 36) with a reduced-cost certificate, re-checked and selected |
| 7 | Contradictory evidence | outcome `contradicted`, no selection, no handoff |
| 8 | Insufficient evidence | outcome `insufficient_evidence`, no hypotheses, no candidates |
| 9 | No feasible solution | outcome `infeasible`, with the minimal infeasible constraint set |
| 10 | Prior-art heavy | "Matching prior art found" cites the matches; transfer hypotheses follow |
| 11 | Apparently novel | the coverage-limited "no match" statement; no novelty wording anywhere in the output |
| 12 | Sensitivity-fragile | a higher-value but fragile candidate is not selected under the default rule |

The other code terms each have their own scenarios: V1 certification (15/15), the read-only V1 boundary, the
promotion guards, prior-art conclusions, gap bases, hypothesis lifecycle and counters, candidate statuses and the
recorded rule, simulation determinism, sensitivity, every optimization status, the discovery verifier,
the ledger, receipt reproducibility and tampering, the MCP contract (V1 tools unchanged, V2 tools typed, discover end
to end), provider independence (heuristic, canned and failing providers give one domain schema), the handoff's
refusals, and an adversarial set (conflicting, copied, stale and future-dated evidence; wrong and ambiguous units;
fabricated citations; unsupported, circular and falsely novel ideas; an unsupported distribution; empty and
falsely confident evidence; malformed and tampered receipts; non-finite input).

## Process terms: `scripts/v2_gate.py`

Run from a fresh clone at the exact commit, after CI for that commit has finished:

```bash
git clone -c core.longpaths=true https://github.com/EthanLofgrem/lofgren-intelligence.git fresh
cd fresh
git checkout <sha>
python scripts/v2_gate.py --sha <sha>
```

It runs `certify --v2` and `certify-boundary` in that checkout, the whole test suite in normal and UTF-8 mode
(nothing may be skipped), reads GitHub Actions for the exact commit (every required step on Python 3.10, 3.11
and 3.12, including the clean-wheel package steps and both certifications), and checks that the tree is clean
before and after and that HEAD is the requested commit. It is the only place that prints `V2ReadyForV3`. The
`v2-release-gate` workflow runs it automatically after the test matrix succeeds on the build branch.

## Tests

| File | Covers |
| --- | --- |
| `tests/test_v2_engines.py` | each engine, with hand-computed values |
| `tests/test_v2_invariants.py` | directive invariants 03–07 and 12–20 (01, 02 and 08–11 are in `test_v2_foundations.py`) |
| `tests/test_v2_adversarial.py` | every adversarial input, plus 60 seeded random corruptions of a design space |
| `tests/test_v2_mcp_discovery.py` | the MCP contract in process and over a real stdio session |
| `tests/test_v2_certification.py` | the certification itself, the CLI, and gate/workflow consistency |
