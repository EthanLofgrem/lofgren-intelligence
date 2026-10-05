# V2 Discovery Intelligence: construction directive

**Goal.** V2 is not finished because its features exist or its unit tests pass. It is finished when one exact GitHub SHA, reproduced from a fresh clone, passes `V2ReadyForV3`. Until then, V3 Production Intelligence does not start.

This directive is written against the real code at `V2_BASE_SHA`. Every step names the modules it touches, the tests it must add and the evidence it must produce. Work through the phases in order, and do not skip a gate.

---

## 0. Ground truth at the start of V2

These are recorded facts confirmed by GitHub. Do not restate them without re-checking.

| Item | Value |
| --- | --- |
| Repository | `EthanLofgrem/lofgren-intelligence` |
| `main` | `0e3c67523faca5121f95823c41924a130a08d548` |
| `build/v1` | `27577121a978b3a7c51e8ec79ac50f14e02dd88c` (PR #1 → main) |
| `build/v1.1-hardening` | `8453d03e3e1cab4445927f017701816ea096c302` (PR #2 → build/v1) |
| **V2_BASE_SHA** | **`8453d03e3e1cab4445927f017701816ea096c302`** |
| V1 tests at base | 91 run / 91 passed / 0 failed / 0 skipped (fresh GitHub clone, Python 3.11.15) |
| V1 certification at base | 15/15 scenarios, `V1Ready = TRUE` |
| CI at base | Actions run 36808385275: success on Python 3.10, 3.11, 3.12 |
| Version at base | `0.2.0` |

Topology (real, not idealized):

```
5fe553e (GitHub initial commit) ─┐
790a915 (local init) ────────────┴─ 0e3c675 main
                                     └─ 2757712 build/v1      (merge of main into V1 history)
                                          └─ 8453d03 build/v1.1-hardening
                                               └─ build/v2-discovery
```

**Rule 0.** At the start of every work session, run `git fetch origin` and confirm that `build/v2-discovery` still descends from `V2_BASE_SHA`. Any SHA you report must be one GitHub has confirmed (`git ls-remote origin`).

---

## 1. What V1 hands to V2 (the only input V2 may read)

V2 reads V1 state **only** through `kernel.state.export_state(run)`, which uses the schema `lofgren.knowledge-map/1`:

| Key | Meaning | V2 may |
| --- | --- | --- |
| `known` | claims that meet their sufficiency policy | cite as facts |
| `uncertain` | partially verified, single-source, or insufficient | cite as uncertain only |
| `contradicted` | contested claims and their contradiction ids | treat as open conflicts |
| `contradictions` | contradiction graph with `kind`, `resolution` | turn into gaps and hypotheses |
| `unknowns` | open gaps with acquisition plans | turn into gaps and evidence requirements |
| `calculations` | reproducible numbers | use as simulation inputs (by id) |
| `findings` | one per research question | frame problems |
| `research_id` | the V1 receipt id | pin as the evidence fingerprint |

**Rule 1.** V2 never imports `EvidenceGraph` in order to write to it. V2 never mutates the `RunResult`. Enforce this with a test that deep-copies the run, runs V2, and asserts that the V1 receipt still verifies and that `state_hash` is unchanged.

---

## 2. The non-negotiable invariant

```
V2-generated idea ≠ verified fact
```

There is no transition `HYPOTHESIS → VERIFIED` anywhere. The only path is:

```
Hypothesis → EvidenceRequirement → (new V1 run) → Evidence → separate Claim → V1 Verifier → Verified Claim
```

This is already enforced at `V2_BASE_SHA`:

- `ClaimOrigin.HYPOTHESIS`: `Verifier.status()` returns `UNVERIFIED`.
- `discovery.promote()` always raises `PromotionRefused`.
- The certification scenario "hypothesis guard" covers it.

V2 must add the following:

1. A V2 hypothesis, once confirmed, produces an `EvidenceRequirement`. V2 only *emits* these requirements. A separate, explicit V1 call (`reverify(requirement, registry)`) runs a **new** V1 investigation whose objective is the hypothesis's test, and its claims are verified by the V1 verifier like any others.
2. A `Candidate` or `Hypothesis` object cannot be passed to any V1 function that adds claims. Add a type guard in `EvidenceGraph.add_claim` that rejects anything that isn't a `Claim`, and a test for it.
3. Free text produced by a provider in V2 can never become an `Evidence` object. Only adapters create `Evidence`. Test: run V2 with a provider that returns fabricated "evidence" text, and assert that no new `Evidence` id appears anywhere.

---

## 3. Package layout to build

All of V2 lives under `lofgren_intelligence/discovery/`. Keep it standard-library only, so offline certification needs no dependencies.

```
discovery/
  __init__.py        public API; keeps Hypothesis, Candidate, PromotionRefused, promote (extend, don't break)
  types.py           every V2 object (section 4)
  errors.py          typed errors (section 18)
  frame.py           Problem framing from the knowledge map
  prior_art.py       PriorArt records, PriorArtProvider interface, coverage
  gaps.py            Gap detection and typing
  connections.py     Connection discovery (observed / derived / speculative)
  principles.py      First-principles decomposition: constraints, variables, assumptions
  hypotheses.py      Hypothesis + counter-hypothesis generation
  candidates.py      Candidate generation and constraint checking
  simulate.py        Deterministic simulation interface + Monte Carlo (seeded)
  sensitivity.py     One-at-a-time and range sensitivity, break-even, robust ranges
  optimize.py        Explicit optimization problems, solvers, independent verification
  verifier.py        Discovery verifier (lowers standing only)
  ledger.py          (or reuse kernel.ledger.CostLedger with new operation kinds)
  receipt.py         Discovery receipt + fingerprint
  handoff.py         V3Handoff + validation
  pipeline.py        run_discovery(knowledge_map, objective, config, provider) -> DiscoveryResult
  report.py          Markdown/JSON views of DiscoveryResult (views only)
  certification.py   V2 scenarios + V2ReadyForV3 gate
```

The kernel's `Stage` enum already declares `IMAGINE`, `SIMULATE` and `OPTIMIZE` as V2 stages. When V2 lands, set `CURRENT_VERSION` behaviour so that these stages report `done` only when the discovery pipeline actually ran them.

---

## 4. Object model (`discovery/types.py`)

Every object is a dataclass. Each has:

- a stable `id` built with `evidence.types.make_id` from its defining content, so identical inputs give identical ids
- `created_at`
- `derived_from` (parent ids)
- `version`

Include the fields below at minimum. Do not merge two fields into one.

| Type | Required fields |
| --- | --- |
| `DiscoveryObjective` | objective text, `research_id` (V1), mode, constraints (typed), config hash |
| `ProblemFrame` | objective id, `known_ids`, `uncertain_ids`, `contradiction_ids`, `unknown_ids`, scope (V1 `Scope`), success metrics |
| `KnownFact` | V1 claim id, statement, value/unit, scope, policy, confidence + `confidence_kind="evidence"` |
| `Uncertainty` | V1 claim id, reason (policy unmet / single source / insufficient / stale) |
| `MissingEvidence` | V1 unknown id, capability, sources, expected gain, cost, approval needed |
| `PriorArt` | query, sources searched, time range, jurisdictions/domains, results, `coverage`, `limitations`, cost |
| `Gap` | type (enum below), what is missing, why it matters, evidence ids, `basis` (`observed_absence` / `search_absence` / `contradiction` / `unknown`), confidence, ways to close it, information gain, cost, authorization needed |
| `Constraint` | name, kind (physical / logical / economic / legal / resource), expression (structured, see section 12), units, source (V1 claim id **or** assumption id) |
| `Assumption` | statement, value/unit (optional), why assumed, sensitivity (filled later), replaceable=True |
| `Connection` | a, b (ids), relation type, `strength_kind` (`observed` / `derived` / `speculative`), evidence ids |
| `Hypothesis` | statement, origin, supporting/contradicting V1 claim ids, assumptions, scope, mechanism, predicted observations, falsification criteria, evidence required, status, provisional score, parent ids, derived candidate ids |
| `CounterHypothesis` | the same fields plus `counters: hypothesis_id` |
| `Candidate` | extend the existing class: problem addressed, hypotheses used, constraints satisfied/violated, benefits, costs, risks, unknowns, dependencies, test requirements, reversibility, expected outcome; **separate** `novelty`, `technical_feasibility`, `economic_feasibility`, `expected_value`, `robustness` |
| `Scenario` | candidate id, parameter values, assumption ids |
| `Simulation` | id, candidate id, model, model version, parameters, initial conditions, assumptions, seed, iterations, outcomes (distribution summary), uncertainty, failure states, runtime, cost, `kind="simulated"` |
| `SensitivityResult` | candidate id, per-variable elasticity, high/low variables, fragile assumptions, break-even points, failure thresholds, robust ranges |
| `OptimizationProblem` | decision variables (name, unit, bounds, integer?), objective (structured), direction, constraints, solver |
| `OptimizationResult` | status (`optimal` / `feasible` / `infeasible` / `unbounded` / `unknown` / `timeout` / `unsupported`), solution, objective value, `proof` (what establishes optimality), independent verification result |
| `DiscoveryFinding` | statement, kind (`verified_fact` / `hypothesis` / `simulation_result` / `optimization_result` / `gap`), ids it rests on, confidence kind |
| `DiscoveryDecision` | selected candidate id, alternatives with reasons, decision rule, tie-breaks |
| `DiscoveryReceipt` | section 15 |
| `V3Handoff` | section 16 |

**Status enums:**

- Gaps: `knowledge`, `evidence`, `capability`, `market`, `technical`, `scientific`, `operational`, `data`, `measurement`, `integration`, `constraint`.
- Hypotheses: `proposed`, `challenged`, `survives`, `refuted_by_analysis`, `requires_research`. There is no `verified` value.
- Candidates: `proposed`, `infeasible`, `dominated`, `viable`, `selected`.
- Discovery outcomes that must be first-class (not errors): `INSUFFICIENT_EVIDENCE`, `CONTRADICTED`, `INFEASIBLE`, `UNSUPPORTED`, `UNKNOWN`, `REQUIRES_RESEARCH`.

Generate JSON Schemas for every V2 type by extending `schemas.TYPES`, and commit them under `schemas/discovery/`.

---

## 5. Problem framing (`frame.py`)

- Input: the knowledge map plus a `DiscoveryObjective`.
- Output: a `ProblemFrame`.
- Copy scope **unchanged** from V1 findings. Temporal and geographic scope must flow through every later object. Test this by building a frame from a 2024-Phoenix map and asserting that every hypothesis and candidate carries the 2024 / Phoenix scope or an explicit, recorded widening.
- If `known` is empty and `uncertain` is empty, return `INSUFFICIENT_EVIDENCE` with the V1 unknowns as `MissingEvidence`. Do not continue to hypothesis generation as if the evidence were there.

## 6. Prior art (`prior_art.py`)

- `PriorArtProvider` interface, with the same pattern as `adapters.search.SearchProvider`: `search(query, domains, time_range) -> list[PriorArtHit]`.
- Ship an offline `FixturePriorArtProvider` for tests and certification. Add live providers later behind the interface (for example a patents API or a papers API). Each one records its own coverage.
- Every `PriorArt` record stores queries, sources searched, the time range, domains or jurisdictions, cost and `limitations`.
- **Novelty discipline (enforced in code):** `novelty_statement(records)` may only return:
  - "Matching prior art found: …", or
  - "No matching prior art was found within the searched sources (…) for … ; this does not establish novelty."

  The words `novel`, `new`, `first`, `never attempted` and `unprecedented` must not appear in any V2 output when the prior-art status is "none found". Add a test that scans every rendered string.

## 7. Gaps (`gaps.py`)

Sources of gaps:

- V1 unknowns become `evidence`/`data` gaps.
- Contradictions become `knowledge` gaps.
- Unmet constraints become `constraint`/`technical` gaps.
- Prior-art misses become `market`/`technical` gaps, with `basis="search_absence"`.

An absence of retrieved evidence is **never** proof that a real gap exists. When `basis="search_absence"`, the gap's confidence is capped (for example ≤ 0.4) and a coverage statement is required.

## 8. Connections (`connections.py`)

Find connections across claims, gaps and prior art:

- shared entities or places (from scope)
- shared mechanisms (from hypothesis mechanisms)
- similar constraints
- temporal ordering

Each connection gets `strength_kind`:

- `observed`: both endpoints are V1 known facts, and the relation itself is stated in evidence
- `derived`: computed deterministically, for example two facts in the same place and period
- `speculative`: anything else

A speculative connection can only produce a `Hypothesis`. It is never a finding. Test this.

## 9. First principles (`principles.py`)

- Decompose into an objective, variables (controllable or uncontrollable, with units), constraints (typed) and assumptions.
- A constraint whose source is not a V1 known fact **must** reference an `Assumption`. Assumptions are separate, inspectable objects. Replacing one, for example `replace_assumption(id, new_value)`, re-runs everything downstream and produces a new fingerprint.

## 10. Hypotheses (`hypotheses.py`)

Generation strategies, each named and recorded in `origin`:

- **gap-closing:** one hypothesis per high-value gap
- **contradiction-explaining:** for each contradiction, at least two competing explanations, for example a data error, a time lag, or different definitions
- **cross-domain transfer:** from prior-art mechanisms
- **constraint relaxation:** what becomes possible if a constraint is lifted (this produces a hypothesis about value, not a fact)

Counter-hypotheses: every hypothesis with a provisional score above a threshold gets at least one `CounterHypothesis`. The engine must not report a single explanation when the evidence admits more than one.

Each hypothesis states `predicted_observations` and `falsification_criteria` as structured `EvidenceRequirement`s (capability, place, period, threshold). V1 can act on these directly.

Providers: a `ReasoningProvider` may *propose* hypothesis text. The deterministic engine then attaches structure and evidence ids. Provider text is stored with `origin="provider:<name>"`, and the verifier treats it as unsupported until it is tied to V1 ids.

## 11. Candidates (`candidates.py`)

- Generate **several** candidates whenever the frame permits.
- Candidate order is not a ranking. "Candidate #1" has no special meaning.
- Check constraints: satisfied, violated or unknown, each with the reason.
- Dominance: a candidate is `dominated` only if another is at least as good on every measured dimension and strictly better on one. Report the dominating candidate.
- The four measures (novelty, technical feasibility, economic feasibility, expected value) plus robustness are **never** combined into one score. Selection uses an explicit, recorded `DiscoveryDecision` rule, for example "maximize expected value subject to feasibility ≥ 0.5 and robustness ≥ 'moderate'". The rule itself appears in the receipt.

## 12. Structured expressions (shared by constraints, objectives and simulations)

Do not optimize or simulate prose. Use a small, safe expression tree:

```
Expr = Const(value, unit) | Var(name) | Add | Sub | Mul | Div | Min | Max | Pow(int) | Neg
Constraint = (lhs: Expr, op: "<=" | ">=" | "==", rhs: Expr)
```

- Evaluate it with an interpreter. **Never** use `eval`/`exec`.
- Units are checked on construction (reuse the canonical units from `adapters.physical.UNIT_ALIASES`). A unit mismatch raises `UnitMismatch`.
- NaN, Infinity and division by zero raise typed errors (section 18).

## 13. Simulation (`simulate.py`)

- Interface: `simulate(candidate, scenario, model, seed, iterations) -> Simulation`.
- Models are registered objects with a name and version. Ship a deterministic `ExpressionModel`, which evaluates an `Expr` over parameters, and a `MonteCarloModel`, which samples parameters from declared distributions using `random.Random(seed)`. Never use the global random state.
- Output is a distribution summary (mean, p5, p50, p95), failure states (the share of draws violating constraints), runtime and cost.
- Every `Simulation` carries `kind="simulated"`. A `DiscoveryFinding` of kind `simulation_result` cannot be rendered with the words "observed", "measured" or "verified". Test this.
- Determinism test: the same candidate, scenario, model, seed and iterations must give a byte-identical summary across two runs.

## 14. Sensitivity and optimization

**Sensitivity** (`sensitivity.py`):

- One-at-a-time ±10% and ±50% swings on every variable and assumption.
- Elasticity ranking.
- Break-even points, found by bisection on the outcome threshold.
- Failure thresholds and robust ranges.
- A candidate that succeeds only within a narrow band (for example ±5% on any input) is marked `robustness="fragile"`.

**Optimization** (`optimize.py`):

- Solvers, in the standard library only:
  - exhaustive search for small integer domains, which is provably optimal and records `proof="exhaustive over N points"`
  - a dense simplex method for linear programs, whose optimality comes from the reduced-cost certificate
  - bounded grid search for nonlinear problems, which is **feasible only** and must never be labelled `optimal`
- An independent verifier re-evaluates every returned solution against every constraint with the expression interpreter. Any violation makes the status `infeasible`.
- An infeasible problem returns `infeasible` with the minimal violated set when it can be found. A timeout returns `timeout`, and an unsupported form returns `unsupported`.
- Tests: an LP with a known optimum, an infeasible LP, an unbounded LP, a degenerate LP, an integer problem with ties, and a nonlinear problem labelled `feasible` and never `optimal`.

## 15. Discovery verifier, confidence, cost, receipt

**Verifier** (`verifier.py`) receives only the structured objects, never generator reasoning. It checks for:

- unsupported assumptions
- missing evidence
- false novelty
- duplicate candidates (same structure, different wording)
- constraint violations
- scope, time, place and unit mismatches
- invalid calculations
- simulation results presented as fact
- optimization status that doesn't match its proof
- circular reasoning (hypothesis A supports B, which supports A, detected in the `derived_from` graph)
- evidence laundering (a claim id cited that isn't in the knowledge map)
- hypotheses promoted to fact
- citation gaps

The verifier can only **lower** standing (`survives` → `challenged`, `viable` → `requires_research`) and records each reason. It never adds evidence.

**Confidence kinds** are kept separate and labelled everywhere:

- `evidence` (from V1)
- `claim` (from V1)
- `hypothesis` (provisional, V2)
- `simulation_uncertainty`
- `candidate_robustness`
- `outcome` (empty until V5)

No function returns a blended percentage. All V2 confidence is `provisional`.

**Cost:** extend `kernel.ledger.CostLedger` with these kinds: `search`, `retrieval`, `verification`, `model`, `hypothesis_generation`, `simulation`, `optimization`, `tool`, `external_api`, `licensed_data`, `compute`. Every operation records its kind, actor, work units and result count. The receipt total must equal the sum of the entries, and a test asserts this.

**Receipt** (`receipt.py`, `lofgren.discovery-receipt/1`) contains:

- the objective
- the V1 `research_id` and `state_hash` (the evidence fingerprint)
- the config and its hash
- the algorithms and models with their versions
- the provider name and version
- parameters, constraints and seeds
- every object id and hash
- the ledger
- the decision rule
- timestamps

It also contains:

- `discovery_fingerprint`: a hash of everything that determines the result, excluding timestamps
- `discovery_id`: a hash of the whole receipt

Tamper test: changing any field makes `verify_discovery_receipt` fail. Reproducibility test: two runs on identical inputs give an identical `discovery_fingerprint`.

## 16. V3 handoff (`handoff.py`, `lofgren.v3-handoff/1`)

```
V3Handoff {
  objective, selected_candidate, alternatives (with rejection reasons),
  verified_evidence (V1 claim ids + statements, kind=verified_fact only),
  hypotheses (kind=hypothesis, status), assumptions (typed), constraints (structured Expr),
  specifications (structured: name, value, unit, tolerance, source id),
  expected_outcomes (from simulation, kind=simulated), acceptance_criteria (testable Expr thresholds),
  test_requirements, unresolved_questions (MissingEvidence + open hypotheses), risks, dependencies,
  resource_requirements, cost_estimates, evidence_fingerprint, discovery_fingerprint, discovery_receipt_id
}
```

`validate_handoff()` fails closed if any of the following is true:

- a free-text field contains a number, unit or assumption that has no typed counterpart (the "no hidden untyped assumptions" rule)
- a hypothesis appears under `verified_evidence`
- an acceptance criterion is not machine-evaluable
- a fingerprint does not match the receipt
- the selected candidate is `infeasible` or `dominated`

If no candidate is viable, the handoff is **not produced**. Instead the outcome is `INFEASIBLE` or `REQUIRES_RESEARCH`, with the evidence requirements V1 would need. This is a correct result, not a failure.

## 17. MCP contracts (`mcp/server.py`, contract `lofgren.mcp/2`)

Add structured tools:

- `discover`: the full pipeline. It returns a `discovery_id`, typed summary and outcome.
- `find_prior_art`, `find_gaps`, `find_connections`, `generate_hypotheses`, `generate_candidates`, `simulate_candidate`, `analyze_sensitivity`, `optimize_solution`, `verify_discovery`, `get_discovery_receipt`, `create_v3_handoff`, `render_discovery_report`.

Each tool:

- takes a `run_id` (a V1 run already in the session) or a knowledge-map JSON
- returns `structuredContent` with machine-readable `kind`, `confidence_kind` and provenance ids
- includes an `outputSchema`

Keep every V1 tool unchanged. Add a version field to `serverInfo`. Bump the MCP contract version and record the change in `CHANGELOG.md`.

## 18. Failure behaviour (`errors.py`): fail closed

Typed errors include: `MalformedInput`, `UnknownReference`, `DependencyCycle` (reuse it), `UnitMismatch`, `NonFiniteValue`, `NegativeCost`, `ImpossibleTimestamp`, `InvalidScope`, `UnsupportedAlgorithm`, `UnknownStatus`, `DuplicateId`, `InputTooLarge`, `UnsafeName`, `ReceiptTampered`, `PromotionRefused` (exists).

Never silently repair scientifically meaningful input. Each error says what was wrong and where. Add a negative test per error type, and a hypothesis-style test that feeds randomized malformed maps (seeded) and asserts that every outcome is either a typed error or a valid result, never an uncaught exception.

## 19. Authority boundary

V2 imports nothing from `authority` except `FORBIDDEN`, and it calls no adapter or tool that writes. Add a test that walks `discovery/` imports and asserts that the network-writing and execution modules are absent. Also run the full V2 pipeline with a registry whose adapters raise on any call other than reads, and assert that it succeeds.

V2 may output specifications. It may not deploy, purchase, pay, message, change infrastructure, or control devices.

## 20. Provider independence

Hypothesis text generation uses `ReasoningProvider`. Add a `propose_hypotheses(frame) -> list[dict]` method with a deterministic default in `HeuristicProvider`. Test with three providers (heuristic, a canned fake, and a failing fake) and assert that the domain schema of the output is identical. Only provider metadata, which lives in the receipt, may differ.

---

## 21. Tests to add (organized by risk, not by count)

| Category | Must include |
| --- | --- |
| Unit | each module's main function, with hand-computed expected values |
| Invariants (the 20 below) | one named test each, named `test_invariant_NN_<name>` |
| Negative | every typed error |
| Adversarial | section 22 inputs |
| Determinism | fingerprints, simulations, optimization, ids |
| Serialization | every V2 type round-trips through JSON and validates against its schema |
| Boundaries | V1 → V2 read-only; V2 → V1 re-verification through `reverify` only; V2 → V3 handoff validation |
| MCP | every new tool: success, schema, and error paths |

**Required invariants:**

1. A hypothesis cannot become verified directly.
2. A candidate cannot become a verified claim.
3. Generated text cannot create evidence.
4. Missing evidence cannot become negative evidence.
5. Copied sources cannot create independence (the V1 lineage test is re-run through V2).
6. Temporal scope is preserved.
7. Geographic scope is preserved.
8. Simulated outcomes cannot become observed outcomes.
9. An infeasible optimization cannot be labelled feasible.
10. An unproven solution cannot be labelled optimal.
11. A speculative connection stays speculative.
12. A prior-art miss cannot prove novelty.
13. Every discovery conclusion traces to structured inputs.
14. Every cost traces to an operation.
15. Every receipt detects material tampering.
16. Switching providers does not change the domain schema.
17. Unsupported input fails closed.
18. V2 cannot invoke execution authority.
19. The V3 handoff contains no hidden untyped assumptions.
20. V1 evidence state is immutable from V2.

## 22. Adversarial suite (`tests/test_v2_adversarial.py`)

Feed the pipeline each of these:

- conflicting sources
- copied sources
- outdated evidence
- future-dated evidence (`ImpossibleTimestamp`, or flagged)
- wrong locations
- wrong units
- ambiguous units
- fabricated citations (ids not in the map)
- unsupported hypotheses
- circular hypotheses
- false novelty claims
- impossible constraints
- degenerate LPs
- simulation results labelled "observed"
- an empty map
- very weak evidence
- high-confidence false inputs (a V1 claim marked contested but given high raw confidence)
- malformed receipts
- tampered receipts

Assert the **correct outcome**, which is often `INSUFFICIENT_EVIDENCE`, `CONTRADICTED`, `INFEASIBLE`, `UNSUPPORTED`, `UNKNOWN` or `REQUIRES_RESEARCH`, rather than any answer at all.

## 23. V2 certification (`discovery/certification.py`, command `lofgren certify --v2`)

Twelve end-to-end scenarios on fixed, fictional fixtures, fully offline. Each scenario builds a real V1 run first, using the V1 pipeline and document fixtures, and then runs the full discovery pipeline:

1. scientific reasoning (competing mechanisms for an observation)
2. technical engineering (structured constraints, simulation)
3. business/economics (break-even, sensitivity)
4. geospatial/physical (scope preserved from orbital/sensor evidence)
5. software/system design (dependencies, handoff specs)
6. resource optimization (LP, provably optimal)
7. contradictory evidence (outcome `CONTRADICTED` or competing hypotheses)
8. insufficient evidence (outcome `INSUFFICIENT_EVIDENCE`, no handoff)
9. no feasible solution (outcome `INFEASIBLE`, minimal violated set)
10. prior-art-heavy (novelty statement cites matches)
11. apparently novel (novelty statement is the coverage-limited form)
12. sensitivity-fragile (robustness `fragile`, not selected under the default rule)

Each scenario **inspects the artifacts**: object counts and kinds, statuses, fingerprints, receipt verification, handoff validation and forbidden words. Checking the exit code alone is not enough.

**Gate:**

```
V2ReadyForV3 =
    V1Certified (V1 suite + V1 certification pass at the same SHA)
  ∧ EvidenceBoundaryPreserved (invariants 1–5, 20)
  ∧ PriorArtImplemented ∧ GapAnalysisImplemented
  ∧ HypothesisLifecycleSafe ∧ CandidateLifecycleSafe
  ∧ SimulationReproducible ∧ SensitivityImplemented ∧ OptimizationVerified
  ∧ DiscoveryVerifierPassing ∧ CostLedgerComplete ∧ DiscoveryReceiptsReproducible
  ∧ MCPContractsStable ∧ ProviderAbstractionStable
  ∧ AdversarialSuitePassing ∧ E2ECertificationPassing ∧ V3HandoffValidated
  ∧ WorkingTreeClean (git status --porcelain is empty)
  ∧ ExactSHAPinned (HEAD equals the pushed remote branch SHA)
```

`lofgren certify --v2` computes every term from real checks, prints which evidence satisfied each one, and exits non-zero if any term is false. The last two terms read git itself; they are never hard-coded.

## 24. CI (`.github/workflows/tests.yml`)

- Keep the V1 jobs.
- Add steps for the V2 tests, the V2 adversarial suite and `certify --v2`.
- Add a `python -m compileall -q lofgren_intelligence` static check.
- Add a packaging check: `python -m pip wheel --no-deps --no-build-isolation .`, with a fallback if setuptools is unavailable in the runner.
- Live-provider tests go in a separate, manually triggered workflow that uses repository secrets. Offline certification never needs keys.

## 25. Fresh-clone reproduction (before declaring V2 done)

```
git clone https://github.com/EthanLofgrem/lofgren-intelligence fresh && cd fresh
git checkout <V2_CANDIDATE_SHA>
python -m unittest discover -s tests -t . -v
python -m lofgren_intelligence certify
python -m lofgren_intelligence certify --v2
python -m compileall -q lofgren_intelligence
```

No untracked file, environment variable, cache or network access may be required.

## 26. Documentation to update

- `README` (what V2 does and does not do)
- `docs/ARCHITECTURE.md`
- `docs/EVIDENCE_PROTOCOL.md` (add the evidence → claim → hypothesis → candidate → simulation → handoff chain)
- a new `docs/DISCOVERY.md` with one complete worked example (input knowledge map → handoff JSON)
- `docs/V2_CERTIFICATION.md`
- `CHANGELOG.md`
- `SECURITY.md` (the execution boundary)

## 27. Versioning

- Release as `0.3.0`.
- Record the package version, commit SHA, `lofgren.knowledge-map/1`, `lofgren.discovery-receipt/1`, `lofgren.v3-handoff/1` and `lofgren.mcp/2`.
- Any incompatible change to a V1 schema is a breaking change. Avoid it, and if it can't be avoided, document it.

## 28. Work order and commits

Use one branch, `build/v2-discovery`, with small commits that each pass the full suite:

1. types + errors + expression interpreter (+ tests)
2. frame + prior art + gaps (+ tests)
3. connections + principles (+ tests)
4. hypotheses + counter-hypotheses (+ tests)
5. candidates + decision rule (+ tests)
6. simulation + sensitivity (+ tests)
7. optimization + independent verification (+ tests)
8. discovery verifier (+ tests)
9. ledger kinds + receipt + fingerprint (+ tests)
10. V3 handoff + validation (+ tests)
11. pipeline + report views (+ tests)
12. MCP tools (+ tests)
13. adversarial suite
14. V2 certification + gate
15. docs + CI + version bump

After each commit: run the V1 suite and `lofgren certify`. **V1 must stay green throughout V2 construction.**

## 29. Final V2 report (one, at the end)

```
Repository / Branch / V2_BASE_SHA / V2_CERTIFIED_SHA / Version
V1 tests (run/pass/fail/skip) / V1 certification / V2 tests / V2 adversarial / V2 certification
Static checks / Package check / Fresh-clone reproduction
Changed files / Modules / MCP tools / Schemas / Docs / CI
Known limitations / Deferred work / External dependencies / Network-dependent capabilities / Security boundaries
Working tree / Remote status
V2ReadyForV3: PASS | FAIL — and for each term, the evidence (or the next concrete repair)
```

---

## Rules that apply to every step

- Never fabricate evidence, citations, test results or SHAs. Report only what ran and what GitHub confirms.
- Never describe a simulation as an observation, a hypothesis as a fact, or a prior-art miss as novelty.
- Never hide contradictory evidence or discard inconvenient evidence.
- Never let copied sources create independence, or model confidence stand in for evidence.
- Never let V2 bypass V1 verification or acquire V4 execution authority.
- Never start V3 because V2 compiles. Start it when `V2ReadyForV3 = PASS` on a pinned, pushed SHA.
