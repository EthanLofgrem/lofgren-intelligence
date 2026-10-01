# Changelog

Releases use semantic versions. "V1 … V6" name capability generations, not release numbers.

## 0.2.0 — V1 hardening (the V2 gate)

V1 becomes a typed, provable evidence base that Discovery Intelligence (V2) can build on.

- **Typed findings and unknowns.** Every research question gets a `Finding` built only from graph state (claims, evidence ids, contradictions, unknowns, scope, confidence, next best evidence). Gaps are `Unknown` objects with acquisition plans: possible sources, expected gain, estimated cost, approval needed. The report is a view of this state.
- **Research question graph.** Questions carry dependencies and stop conditions; research runs prerequisites first; cycles are rejected.
- **Temporal and geographic scope** on every claim (`valid_from`, `valid_to`, place). Disagreements across different periods or places are recorded as scope differences, not conflicts.
- **Evidence sufficiency policies** by claim type: attribution, quantitative, trend, physical (direct observation), general.
- **Source lineage.** Declared derivation, near-duplicate syndication and "according to" quoting merge sources into one independence group, so copies never count as confirmation.
- **Contradiction graph** with kind, scope note, severity, the evidence that would resolve it, and a calculation receipt for numeric differences.
- **Skeptic pass.** An adversarial review over claims and evidence only: unsupported statements, circular sourcing, stale periods, unscoped numbers, model inferences. Serious issues downgrade status; nothing is ever upgraded.
- **Calculation receipts** for every derived number (pass counts, scene counts, sensor means, relative differences).
- **Calibration infrastructure.** Prediction log (JSONL) of every stated confidence, outcome recording, Brier score, reliability table; the calibrator fits on raw scores. Scores stay labelled provisional until calibrated.
- **Cost ledger** per operation (retrieval, model, compute, report) with evidence yield per source.
- **Research receipts** with inputs hash, state hash, report hash and a tamper-evident research ID.
- **ReasoningProvider abstraction.** Heuristic, Anthropic and any OpenAI-compatible endpoint (including local models such as Ollama).
- **Web discovery.** Search-provider interface (Brave included), canonical-URL dedupe, robots.txt respected, source-type classification, query history, page caps.
- **Satellite metadata normalization** (acquisition time, cloud, resolution, footprint, license, provider). Pass predictions are labelled as predictions, not acquisition schedules.
- **Sensor ingestion hardening.** Unit normalization to canonical units, rejection of unknown or mixed units, sampling-health and calibration-age checks, owner and authorization recorded.
- **Structured MCP contracts.** `compile_objective`, `plan_research`, `investigate`, `verify_claim`, `get_finding`, `find_contradictions`, `find_gaps`, `trace_claim`, `get_receipt`, `export_state`, `render_report`, `satellite_passes`, `pricing`.
- **V2 entry contract.** `export_state` knowledge map; `discovery` package with `Hypothesis` and `Candidate` (novelty, feasibility and value kept separate); hypotheses can never be verified or promoted.
- **JSON Schemas** for the evidence protocol, generated from the types (`schemas/`).
- **V1 certification** (`lofgren certify`): 15 end-to-end scenarios and the `V1Ready` gate.

## 0.1.0 — V1 Evidence Intelligence

First release: intent compiler, research planner, adapters, evidence graph, verification, reports, pricing, CLI and MCP server.
