# Roadmap: V1 → V6

One kernel, extended version by version. Each version must be trustworthy before the next depends on it.

| Version | Name | Adds | Gate to start the next |
| --- | --- | --- | --- |
| **V1** ✅ | Evidence Intelligence | Intent compiler, research planner, adapters (documents, web, orbital, imagery, sensors), evidence graph, verification, confidence, report, MCP server, billing | Reports are cited, contradictions surface, gaps are explicit; tested on real user projects |
| **V1.1** ✅ (0.2.0) | V1 hardening | Typed findings/unknowns, question graph, scope, policies, lineage, contradiction graph, skeptic, calculation receipts, calibration log, cost ledger, research receipts, provider abstraction, web discovery, satellite/sensor normalization, structured MCP, knowledge map, schemas | `lofgren certify` → V1Ready = TRUE |
| **V2** ✅ (0.3.0) | Discovery Intelligence | Prior-art assessment, gap analysis, connections, hypotheses and counter-hypotheses, design-space candidates, simulation (seeded Monte Carlo), sensitivity, optimization (exhaustive, exact simplex, grid), discovery verifier, receipts, V3 handoff, MCP tools | `lofgren certify --v2` and `scripts/v2_gate.py` → V2ReadyForV3 = TRUE |
| V3 | Production Intelligence | Specification compiler, artifact planner, code / document / data / design generation, GitHub versioning, automated tests, independent verification of artifacts | Every artifact has acceptance tests that a separate verifier runs |
| V4 | Execution Intelligence | Authority engine for all actions, permissions, budgets, reversible-action classification, approval gates, tool / MCP adapters (GitHub, Vercel, Stripe…), rollback, monitoring | No action runs without passing *Authorized ∧ Validated ∧ WithinBudget ∧ WithinPolicy* |
| V5 | Outcome Intelligence | Predictions → expected vs. actual outcomes, error measurement by method / model / source, failure memory, confidence recalibration, procedural learning | Calibration error measured and falling |
| V6 | Meta-Intelligence | A router that picks models, algorithms, sources, tools, agents and stages for each objective | Routing beats fixed pipelines on measured outcomes per dollar |

## V2 construction plan (as built: see docs/DISCOVERY.md)

1. **Known / unknown / contradicted map** — consume `lofgren.knowledge-map/2` bound to its receipt (`/1` at degraded assurance).
2. **Prior-art search** — patents, papers, products, failed attempts as a new adapter class.
3. **Solution-space map and gap detection** — from unknowns and contradictions.
4. **Hypothesis generation** — every hypothesis names what it responds to and the test that would settle it; stored with `origin = hypothesis`.
5. **Candidates** — novelty, technical feasibility, economic feasibility and expected value kept as separate measures.
6. **Constraint checking, simulation, sensitivity, optimization** — deterministic engines inheriting V1 calculation receipts.
7. **Next evidence requirements** — ranked experiments and observations fed back into V1 as new unknowns.

## Still open in V1.x

- SQLite-backed project store so projects persist across runs and sessions
- Semantic question answering with a model provider (role-based fallback stays)
- Pixel-level change detection on open imagery
- Hosted service: accounts, Stripe metered billing, project view

## Build discipline

- Work happens on `build/vN` branches, merged by pull request with tests passing.
- Every new stage ships with tests and appears in the loop-status table of every report.
- A stage never claims more than it does: later stages stay marked "not available" until built.
