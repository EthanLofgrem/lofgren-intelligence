# Discovery Intelligence (V2)

V1 establishes what is known, uncertain, contested and missing. V2 asks what could be done about it: it frames
the problem, checks prior art, finds gaps and connections, proposes hypotheses and counter-hypotheses, evaluates
candidate solutions with simulation, sensitivity analysis and optimization, verifies its own work, decides by an
explicit rule, and hands a typed specification to V3.

Everything V2 produces is labelled for what it is:

| V2 object | What it is | What it never is |
| --- | --- | --- |
| Known fact | A V1 verified claim, restated | Something V2 established |
| Hypothesis / counter-hypothesis | An idea with testable predictions | Verified (there is no such status) |
| Candidate | A possible solution with separate measures | A fact, or a single blended score |
| Simulation | A model's prediction | An observation |
| Optimization result | A solver's answer, re-checked; `optimal` only with a proof | Optimal because a grid search found it |
| Prior-art "no match" | A statement about the searched coverage | Evidence of novelty |

## Running it

```bash
# V1 research, then discovery over its validated knowledge-map/2
lofgren discover "Is industrial construction in the Phoenix metro increasing?" --files examples/sample-sources \
  --goal "Choose a warehouse size (fictional)" --design design.json \
  --out discovery.md --json summary.json --receipt discovery-receipt.json --handoff handoff.json
```

From Python:

```python
from lofgren_intelligence.discovery.pipeline import discover_from_run
result = discover_from_run(v1_run, "Choose a warehouse size (fictional)", design=design)
result.outcome, result.selected, result.receipt, result.handoff
```

From an AI client over MCP: `discover` (with a `run_id` from `investigate`), then `generate_hypotheses`,
`generate_candidates`, `verify_discovery`, `get_discovery_receipt`, `create_v3_handoff`, and so on. See the README.

## The design space

Discovery never invents a solution from prose. Candidates come from a structured design space (plain JSON):

| Field | Meaning |
| --- | --- |
| `model` | `{name, version, outcomes: {metric: expression}, units: {variable: unit}}`; expressions are structured trees (`{"op": "mul", "args": [...]}`), evaluated without `eval` |
| `value_metric` | The outcome the decision rule values (default: the first outcome) |
| `success` | A relation that defines success, e.g. `profit >= 0 usd` |
| `facts` | `{variable: CL-id}`: take a variable's value and unit from a **V1 known claim** (uncertain or contested claims are refused, and candidates cannot override them) |
| `assumptions` | `{statement, why_assumed, name, value, unit}`: every non-evidence number, stated and replaceable |
| `constraints` | `{name, kind, relation, variable_units, fact or assumption}`: each rests on one known fact or one named assumption |
| `distributions` | `{variable: {dist: uniform/normal/triangular/fixed, ...}}` for Monte Carlo |
| `candidates` | `{description, parameters, dependencies, test_requirements, ...}` |
| `optimization` | `{variables, objective, direction, constraints, solver}`; a verified solution becomes one more candidate |
| `decision_rule` | `{maximize, min_technical_feasibility, min_economic_feasibility, min_robustness}` |
| `simulation` | `{seed, iterations}` |

## Outcomes

`candidate_selected` (with a V3 handoff), `insufficient_evidence`, `contradicted`, `infeasible`,
`requires_research`, `unsupported` and `unknown`. All but the first are correct answers, not failures: discovery
says what V1 would need to find next (`requirements`) instead of guessing.

Selection is withheld when V1 knows nothing and its evidence conflicts (`contradicted`), whatever the candidates'
measures: a plan resting only on assumptions is not chosen against contested evidence.

## Worked example

The certification fixture (fictional): V1 researches *"Is industrial construction in the Phoenix metro
increasing?"* over three fictional documents. It verifies two claims about completed warehouse space in 2026
(14 and 14.2 million square feet) and records two contradictions. Discovery then chooses a warehouse size with
`fixtures.warehouse_design()`:

- model `profit = sqft × rent × occupancy − sqft × build_cost`
- assumptions rent = 12 USD/sqft, build cost = 8 USD/sqft, budget = 200000 sqft
- occupancy drawn from triangular(0.6, 0.85, 0.95), seed 11, 500 draws
- constraint `sqft <= max_sqft`, resting on the budget assumption
- candidates 100000, 150000 and 250000 sqft

Result (real output, `at = 2026-09-30T12:00:00+00:00`):

| Candidate | Status | Technical feasibility | Economic feasibility | Expected profit (simulated) | Robustness |
| --- | --- | --- | --- | --- | --- |
| Build 100000 sqft | dominated (by the 150000 option) | 1.0 | 0.95 | 163 143 USD | moderate |
| **Build 150000 sqft** | **selected** | 1.0 | 0.95 | 244 715 USD | moderate |
| Build 250000 sqft | infeasible: violates the budget constraint | 0.0 | 0.95 | 407 858 USD | unknown |

Rule: *maximize expected_value among viable candidates subject to technical_feasibility >= 0.5 and robustness >=
moderate; ties go to the smallest candidate id.* The 250000 option has the highest expected profit and is still
rejected: it breaks a constraint.

Sensitivity of the selected candidate (hand-checkable, since profit = sqft·(rent·occupancy − build_cost)):

| Input | Elasticity | Break-even |
| --- | --- | --- |
| rent | 4.636 (= 10.2 / 2.2) | 9.41 USD/sqft (= 8 / 0.85) |
| occupancy | 4.636 | 0.667 (= 8 / 12) |
| build_cost | −3.636 (= −8 / 2.2) | 10.2 USD/sqft (= 12 × 0.85) |
| sqft | 1.0 | none (profit stays positive) |

Seven hypotheses and three counter-hypotheses come from the V1 contradictions and uncertain claims; the
discovery verifier lowers the four with no supporting V1 claim to `requires_research`.

The V3 handoff (`lofgren.v3-handoff/1`) carries, among others:

```json
"specifications": [
  {"name": "build_cost", "value": 8.0, "unit": "usd/sqft", "source_id": "ASM-6a3eda776c"},
  {"name": "max_sqft", "value": 200000.0, "unit": "sqft", "source_id": "ASM-853c201528"},
  {"name": "occupancy", "value": 0.85, "unit": "", "source_id": "SCN-16a7a314b5"},
  {"name": "rent", "value": 12.0, "unit": "usd/sqft", "source_id": "ASM-999040bce9"},
  {"name": "sqft", "value": 150000.0, "unit": "sqft", "source_id": "SCN-16a7a314b5"}
],
"expected_outcomes": [
  {"metric": "profit", "mean": 244714.978265, "p5": -4638.748415, "p50": 262480.114856, "p95": 440060.739705,
   "unit": "usd", "kind": "simulated", "simulation_id": "SIM-e868ebde3b"}
],
"acceptance_criteria": [
  {"name": "success", "relation": {"lhs": {"op": "var", "name": "profit"}, "op": ">=",
                                   "rhs": {"op": "const", "value": 0, "unit": "usd"}}},
  {"name": "size within budget", "relation": {"lhs": {"op": "var", "name": "sqft"}, "op": "<=",
                                              "rhs": {"op": "var", "name": "max_sqft"}}}
]
```

Every number in the handoff has a typed source: a V1 claim, a named assumption, or the candidate's scenario.
`validate_handoff` refuses a handoff whose free text states a number with no typed counterpart, a hypothesis under
`verified_evidence`, an acceptance criterion that is not machine-evaluable, a fingerprint that does not match the
discovery receipt, or a candidate that was not selected.

The discovery receipt (`lofgren.discovery-receipt/1`) pins the V1 evidence (research id, knowledge-map
fingerprints, and the V1 receipt's state, inputs and knowledge-state hashes; assurance `validated_v2`), the
config and its hash, algorithm versions, seeds, the decision rule, a digest of every object and the ledger
(7 operations here). Its `discovery_fingerprint` is identical for identical inputs at any time; its
`discovery_id` changes if any field changes.

## The boundary with V1, and with V4

- V2 reads V1 only through a validated knowledge map (knowledge-map/2 bound to its receipt; /1 at degraded
  assurance). It never writes to an evidence graph and never mutates a V1 run.
- The only way back to V1 is `reverify(requirement, registry)`: a new, separate V1 investigation whose claims the
  V1 verifier judges like any others.
- A provider may propose hypothesis text; everything else it returns (claimed evidence, ids, confidence) is
  ignored, and its ideas stay `requires_research` until tied to V1 claims.
- V2 outputs specifications only. It imports no adapter that writes, no network client and nothing from the
  authority engine: it cannot deploy, purchase, pay, message, change infrastructure or control devices.

See [V2 certification](V2_CERTIFICATION.md) for how each of these is proven.
