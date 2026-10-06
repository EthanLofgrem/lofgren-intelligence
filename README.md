# Lofgren Intelligence

Open outcome intelligence system for evidence-driven research, verification, discovery, building, execution, and learning.

**A Lofgren Enterprise project.**

> A governed intelligence layer designed to sense the physical and digital world, research problems, verify evidence, generate new possibilities, design solutions, build usable artifacts, execute authorized actions, measure real-world outcomes, and learn from the results.
>
> **Built today:** the governed V1–V6 stack: V1 research/verification, V2 discovery, V3 production, V4 authorized execution, V5 measured outcomes and V6 reviewed improvement. **Public readiness remains a separate hosted-release gate** covering OAuth, tenant isolation, persistence, deployment identity, real-client interoperability, billing, backup/restore and rollback.

Most AI tools stop at an answer. Lofgren Intelligence is built to carry an objective the whole way to an outcome:

```
Intent → Plan → Sense → Research → Verify → Imagine → Simulate → Optimize → Produce
→ Build → Test → Authorize → Execute → Operate → Measure → Learn → Improve → Research again
```

It is not another chatbot and not another model. Models (Claude, GPT and others) are replaceable suppliers of reasoning. This layer owns what makes results trustworthy: the plan, the evidence, the verification, the authority to act, and the memory of what actually worked.

## Status: governed V1–V6 intelligence stack

V1 runs the first six stages end to end:

| Stage | What V1 does |
| --- | --- |
| **Intent** | Compiles a free-text objective into an **Outcome Contract**: questions, constraints, evidence standard, spend cap, and which actions need approval. Business objectives are structured through the seven Lofgren Enterprise stages. |
| **Plan** | Builds a research plan from the sources connected, orders it by value per unit of cost, and turns anything no source can answer into an explicit gap. |
| **Sense** | Gathers evidence through adapters: your documents, public web pages, orbital passes of open-data imaging satellites, open Sentinel-2 / Landsat imagery catalogs, and your own IoT sensors (read-only, with your authorization). |
| **Research** | Extracts atomic claims, each linked to its evidence and source, into an **evidence graph**. |
| **Verify** | Cross-checks claims across *independent* sources, records contradictions instead of averaging them away, and scores confidence from source quality, independence, recency, directness and contradiction. |
| **Report** | A cited report: verified, partially verified, contested and single-source findings, contradictions, what is missing, the full loop status, and the cost. |

V2 (release 0.3.0) runs the next three over V1's verified state (see [docs/DISCOVERY.md](docs/DISCOVERY.md)):

| Stage | What V2 does |
| --- | --- |
| **Imagine** | Frames the problem from V1's knowledge map, checks prior art (a miss is never called novelty), finds gaps and connections, and proposes hypotheses with counter-hypotheses, each with the evidence that would test it. Candidate solutions come from a structured design space, never from prose. |
| **Simulate** | Runs structured models over each candidate (seeded Monte Carlo), then sensitivity analysis: elasticities, break-even points, failure thresholds, robustness. Results are predictions, never observations. |
| **Optimize** | Exhaustive integer search, an exact-rational simplex with an optimality certificate, or a grid search that can only report "feasible"; every answer is re-checked independently. |

A discovery verifier then lowers anything unsupported, an explicit recorded rule decides, and the result is a
tamper-evident discovery receipt plus, when a candidate is selected, a validated V3 handoff. V2 never writes to V1's
evidence, never turns an idea into a fact, and cannot execute anything. It passes its own gate, `lofgren certify --v2`
([docs/V2_CERTIFICATION.md](docs/V2_CERTIFICATION.md)).

V3 (release 0.4.0) runs the next three over V2's handoff (see [docs/PRODUCTION.md](docs/PRODUCTION.md)):

| Stage | What V3 does |
| --- | --- |
| **Produce** | Binds the handoff to its discovery value by value, compiles it into numbered requirements and plans which file satisfies each. |
| **Build** | Generates the artifact deterministically: a manifest, a plan, machine-readable checks, a README, a Python implementation with one function per acceptance criterion and constraint, and its tests. |
| **Test** | Runs the generated tests in a separate interpreter and has an independent verifier regenerate every file, re-evaluate every check with V2's evaluator, and reproduce reference probes that include expected failures. |

The result is a tamper-evident production receipt and a V4 handoff with `authority.granted = false`. V3 never
deploys, publishes, purchases, sends or writes to an external repository. It passes its own gate,
`lofgren certify --v3` ([docs/V3_CERTIFICATION.md](docs/V3_CERTIFICATION.md)).

V4–V6 continue the governed chain:

| Version | Role | Boundary |
| --- | --- | --- |
| **V4 — Act** | Executes only identity-bound, explicitly approved, bounded actions with preflight, verification, idempotency and rollback where supported. | Client booleans never grant authority. |
| **V5 — Measure** | Compares expected outcomes with actual observations, records missing/failed measurements, and keeps descriptive evidence separate from causal claims. | Unmeasured outcomes cannot drive improvement. |
| **V6 — Improve** | Evaluates candidate improvements against held-out evidence, minimum gain and safety constraints, then produces a review proposal. | V6 cannot silently rewrite policy, history, authority or the running system. |

Each version has its own executable certification gate. The combined lifecycle may be certified while the hosted service still remains **not public-ready** until the public release gate passes.

### Release 0.2.0: V1 hardened for V2

V1 is now a typed, provable evidence base, and it passes the **V1 certification gate** (`lofgren certify`) that V2 construction depends on:

- **Typed findings and unknowns.** Every question is answered by a `Finding` built only from graph state. Every gap is an `Unknown` with an acquisition plan (sources, expected gain, cost, approval).
- **Scope on every claim.** Each claim records the period and place it covers. Disagreements across different scopes are leads, not conflicts.
- **Lineage.** Syndicated copies and quoted sources count as one confirmation.
- **Contradiction graph and skeptic pass.** Every conflict states what would resolve it. An adversarial review flags unsupported, circular, stale and unscoped claims.
- **Receipts.** Every run gets a tamper-evident research receipt with inputs, state and report hashes, plus a per-operation cost ledger and calculation receipts for every derived number.
- **Model-independent.** Heuristic, Anthropic, or any OpenAI-compatible endpoint, including local models.
- **Web discovery.** Search results are deduplicated and robots.txt is respected.
- **Clean physical data.** Normalized satellite metadata, and unit-checked sensor data.
- **Structured MCP tools.** A V2 knowledge map (`export_state`) and JSON Schemas for the evidence protocol.

See [CHANGELOG.md](CHANGELOG.md), [docs/V1_CERTIFICATION.md](docs/V1_CERTIFICATION.md) and [docs/EVIDENCE_PROTOCOL.md](docs/EVIDENCE_PROTOCOL.md).

V1 runs on the Python standard library alone (Python 3.10+). A model is optional: with `ANTHROPIC_API_KEY` set it uses Claude for claim extraction; without one it uses a deterministic offline extractor.

## Quick start

```bash
git clone https://github.com/EthanLofgrem/lofgren-intelligence.git
cd lofgren-intelligence
pip install -e .            # or run without installing: python -m lofgren_intelligence ...

# Investigate with your own documents (sample data is fictional and labeled)
lofgren investigate "Is industrial construction in the Phoenix metro increasing?" \
  --files examples/sample-sources --out report.md

# See the contract, plan and price before running anything
lofgren estimate "Find a warehouse business opportunity near 33.4484, -112.0740"

# Which open-data imaging satellites pass over a place (live elements from CelesTrak)
lofgren passes --lat 33.4484 --lon -112.0740 --fetch

# Physical-world investigation: orbits + open imagery catalogs + your sensors
lofgren investigate "How is soil moisture changing on my field at 33.4484, -112.0740?" \
  --fetch-orbits --imagery --sensors examples/sample_sensors.csv --sensors-authorized

# Plans and bills
lofgren pricing --standard-units 1000 --heavy 6

# Receipts, the V2 knowledge map, and calibration
lofgren investigate "..." --files notes/ --receipt receipt.json --state knowledge-map.json --log predictions.jsonl
# knowledge-map/2 adds provenance, question associations, finding derivation and fingerprints
lofgren investigate "..." --files notes/ --state2 knowledge-map-2.json
lofgren calibration --log predictions.jsonl --claim CL-... --correct yes

# Web discovery (Brave Search API key) and model choice
BRAVE_API_KEY=... lofgren investigate "..." --search brave
LOFGREN_PROVIDER=openai-compatible LOFGREN_BASE_URL=http://localhost:11434/v1 LOFGREN_MODEL=llama3.1 lofgren investigate "..."

# Discovery (V2): research, then frame, hypothesize, simulate, optimize and decide
lofgren discover "Is industrial construction in the Phoenix metro increasing?" --files examples/sample-sources \
  --goal "Choose a warehouse size (fictional)" --design design.json --handoff handoff.json

# Version gates
lofgren certify
lofgren certify-boundary
lofgren certify --v2
lofgren certify --v3
lofgren certify --v4
lofgren certify --v5
lofgren certify --v6

# Production (V3): research, discover, then build, test and verify an artifact
lofgren produce "Is industrial construction in the Phoenix metro increasing?" --files examples/sample-sources \
  --goal "Choose a warehouse size (fictional)" --design examples/designs/warehouse.json --out-dir build/warehouse
lofgren verify-artifact build/warehouse
```

## Use it inside Claude Code, Codex and other AI tools (MCP)

Lofgren Intelligence has both a local stdio MCP server and a hosted MCP service under release certification. The core V1–V6 lifecycle is implemented and certified on the release line. The hosted integration exposes governed lifecycle capabilities through authenticated, tenant-scoped, durable service code, but it is **not public-ready** until OAuth, tenant isolation, restart persistence, real-client, Stripe sandbox, backup/restore, rollback, deployment and exact-SHA release-evidence gates pass.

```bash
# Claude Code
claude mcp add lofgren -- lofgren mcp
```

```toml
# Codex: ~/.codex/config.toml
[mcp_servers.lofgren]
command = "lofgren"
args = ["mcp"]
```

Tools return structured data, not narrative. The hosted service (`/mcp`) registers exactly these tools; the list is generated in [docs/CAPABILITIES.json](docs/CAPABILITIES.json) and a test keeps it equal to the server's registry:

- **Research (V1):** `case_status`, `clarify_objective`, `compile_objective`, `export_knowledge_map2`, `export_state`, `find_contradictions`, `find_gaps`, `get_finding`, `get_receipt`, `investigate`, `plan_research`, `render_report`, `satellite_passes`, `trace_claim`, `verify_claim`.
- **Discovery (V2):** `analyze_sensitivity`, `create_v3_handoff`, `discover`, `find_connections`, `find_discovery_gaps`, `find_prior_art`, `generate_candidates`, `generate_hypotheses`, `get_discovery_receipt`, `optimize_solution`, `render_discovery_report`, `simulate_candidate`, `verify_discovery`.
- **Production (V3):** `build_artifact`, `get_artifact`.
- **Execution (V4):** `action_status`, `execute_action`, `propose_action`. Execution remains server-authorized and browser-reviewed; the model cannot self-authorize.
- **Outcome (V5):** `get_outcome`, `measure_outcome`.
- **Improvement (V6):** `evaluate_improvement`, `get_improvement`.
- **Account / billing:** `account_status`, `billing_portal`, `create_checkout`, `pricing`, `usage_status`. `create_checkout` refuses until billing is enabled and economically certified.

The local stdio server (`lofgren mcp`, contract `lofgren.mcp/3`) covers V1–V3 for developer use: it has `export_knowledge_map` instead of `export_knowledge_map2`, adds `create_v4_handoff`, `verify_artifact`, `get_artifact_file` and `get_production_receipt`, and has no V4–V6, account or billing tools.

Not proven by this repository: deployment, real clients, backup/restore, the Supabase owner settings, and PublicMCPReady.

Every output says what kind of thing it is (`kind`, `confidence_kind`): a verified fact, a hypothesis, a simulated value or a candidate. The local stdio server is intended for developer use. The hosted service uses durable tenant-scoped storage for research runs, discoveries, artifacts, actions, outcomes and improvement records and is being certified separately before public release.

## How a finding earns its status

| Status | Meaning |
| --- | --- |
| Verified | Meets the contract's standard (default: 2 independent sources, confidence ≥ 70%), or is a direct measurement with no contradiction |
| Verified attribution | One primary source establishes what it *said*, not that it is true |
| Partially verified | Supported, confidence ≥ 50%, but short of the standard |
| Contested | Independent evidence disagrees; both sides are shown |
| Single-source | One source, low confidence |
| Insufficient evidence | Nothing supports it yet |

Confidence scores are **uncalibrated starting estimates** until real outcomes are recorded; reports say so. The calibrator (V1) and outcome memory (V5) exist to hold "80% confident" to account.

## Boundaries (by design, not by policy alone)

- **Satellites:** public orbital elements and open or licensed imagery only. Nothing in this codebase contacts, commands or accesses a spacecraft or a provider without authorization.
- **Sensors / IoT:** only devices the user owns or is authorized to read; adapters are read-only and the registry refuses adapters that request write or control access.
- **People:** no tracking of individual people, homes or vehicles.
- **Actions:** every action passes the authority engine: *Execute = Authorized ∧ Validated ∧ WithinBudget ∧ WithinPolicy*. Spending, deploying, publishing, messaging and device control always need explicit approval.
- **Money:** a job is estimated before it runs, capped by the contract, and never charged above the estimate.

## Repository layout

```
lofgren_intelligence/
  kernel/        the loop, pipeline, findings, ledger, receipts, knowledge map
  intent/        objective → Outcome Contract (incl. Lofgren Enterprise venture stages)
  research/      research planner, value-per-cost ordering, stopping rule
  adapters/      evidence adapters + source registry (documents, web search, web pages, orbital, imagery, sensors)
  evidence/      typed evidence, evidence graph, scope, lineage
  verification/  cross-checking, policies, contradictions, skeptic, confidence, calibration
  orbital/       TLE parsing, orbit propagation, pass prediction, imaging-satellite catalog
  models/        replaceable model providers (offline heuristic, Anthropic)
  authority/     the authority engine (V4 foundation)
  billing/       work units, job classes, plans, bill formula
  report/        Markdown and JSON reports
  mcp/           MCP server (stdio), structured tools
  production/    V3 Production: specification compiler, planner, code generation, verifier, sandboxed tests,
                 receipt, V4 handoff, disk store, certification
  discovery/     V2 Discovery: context, framing, prior art, gaps, connections, hypotheses, candidates,
                 simulation, sensitivity, optimization, verifier, receipt, V3 handoff, pipeline, certification
  certification.py  the V1Ready gate (lofgren certify)
  schemas.py     JSON Schemas for the evidence protocol
  cli.py         the `lofgren` command
schemas/         generated JSON Schemas
docs/            architecture, roadmap, pricing, Lofgren Enterprise integration
tests/           standard-library unittest suite
```

## Tests

```bash
python -m unittest discover -s tests -t .
```

## Documentation

- [Architecture](docs/ARCHITECTURE.md)
- [Roadmap: V1 → V6](docs/ROADMAP.md)
- [Pricing model](docs/PRICING.md)
- [Lofgren Enterprise integration](docs/LOFGREN_ENTERPRISE.md)
- [Evidence protocol](docs/EVIDENCE_PROTOCOL.md)
- [V1 certification: the gate before V2](docs/V1_CERTIFICATION.md)
- [Discovery Intelligence (V2)](docs/DISCOVERY.md) · [V2 certification: the gate before V3](docs/V2_CERTIFICATION.md)
- [Production Intelligence (V3)](docs/PRODUCTION.md) · [V3 certification: the gate before V4](docs/V3_CERTIFICATION.md)
- [Changelog](CHANGELOG.md) · [Security](SECURITY.md)

## License

Apache License 2.0. Copyright 2026 Lofgren Enterprise. See [LICENSE](LICENSE) and [NOTICE](NOTICE).

The open-source core (framework, schemas, adapters, verification) is free. The hosted Lofgren Intelligence service — orchestration, commercial data connectors, outcome memory, heavy compute and billing — is the paid layer.
