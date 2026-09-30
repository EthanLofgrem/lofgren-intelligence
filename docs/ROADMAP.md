# Roadmap: V1 → V6

One kernel, extended version by version. Each version must be trustworthy before the next depends on it.

| Version | Name | Adds | Gate to start the next |
| --- | --- | --- | --- |
| **V1** ✅ | Evidence Intelligence | Intent compiler, research planner, adapters (documents, web, orbital, imagery, sensors), evidence graph, verification, confidence, report, MCP server, billing | Reports are cited, contradictions surface, gaps are explicit; tested on real user projects |
| V2 | Discovery Intelligence | Prior-art search, gap analysis, cross-domain transfer, first-principles reconstruction, candidate generation, simulation (Monte Carlo, scenarios), optimization (LP/MIP, constraint solving) | Candidates are scored by deterministic models, not by how they sound |
| V3 | Production Intelligence | Specification compiler, artifact planner, code / document / data / design generation, GitHub versioning, automated tests, independent verification of artifacts | Every artifact has acceptance tests that a separate verifier runs |
| V4 | Execution Intelligence | Authority engine for all actions, permissions, budgets, reversible-action classification, approval gates, tool / MCP adapters (GitHub, Vercel, Stripe…), rollback, monitoring | No action runs without passing *Authorized ∧ Validated ∧ WithinBudget ∧ WithinPolicy* |
| V5 | Outcome Intelligence | Predictions → expected vs. actual outcomes, error measurement by method / model / source, failure memory, confidence recalibration, procedural learning | Calibration error measured and falling |
| V6 | Meta-Intelligence | A router that picks models, algorithms, sources, tools, agents and stages for each objective | Routing beats fixed pipelines on measured outcomes per dollar |

## Next up (V1.x)

- Search adapter (a licensed web-search API) behind the same registry
- SQLite-backed project store so projects persist across runs
- Semantic question answering with a model provider, keeping the role-based fallback
- Pixel-level change detection on open imagery (moves toward V2)
- Hosted service: accounts, Stripe metered billing, project view

## Build discipline

- Work happens on `build/vN` branches, merged by pull request with tests passing.
- Every new stage ships with tests and appears in the loop-status table of every report.
- A stage never claims more than it does: later stages stay marked "not available" until built.
