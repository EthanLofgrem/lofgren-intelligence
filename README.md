# Lofgren Intelligence

Open outcome intelligence system for evidence-driven research, verification, discovery, building, execution, and learning.

**A Lofgren Enterprise project.**

> Lofgren Intelligence is designed as a governed intelligence layer that can grow from sensing and verified research into discovery, artifact creation, authorized execution, outcome measurement, and governed learning. **Today, the certified implementation is V1 Evidence Intelligence; V2 is partial and V3–V6 are not yet certified public capabilities.**

Most AI tools stop at an answer. Lofgren Intelligence is being built version by version to carry an objective the whole way to an outcome. The sequence below is the architecture, not a claim that every stage is currently available:

```
Intent → Plan → Sense → Research → Verify → Imagine → Simulate → Optimize → Produce
→ Build → Test → Authorize → Execute → Operate → Measure → Learn → Improve → Research again
```

It is not another chatbot and not another model. Models (Claude, GPT and others) are replaceable suppliers of reasoning. This layer owns what makes results trustworthy: the plan, the evidence, the verification, the authority to act, and the memory of what actually worked.

## Status: V1 — Evidence Intelligence (certified); V2 partial; V3–V6 planned

This release runs the first six stages end to end:

| Stage | What V1 does |
| --- | --- |
| **Intent** | Compiles a free-text objective into an **Outcome Contract**: questions, constraints, evidence standard, spend cap, and which actions need approval. Business objectives are structured through the seven Lofgren Enterprise stages. |
| **Plan** | Builds a research plan from the sources connected, orders it by value per unit of cost, and turns anything no source can answer into an explicit gap. |
| **Sense** | Gathers evidence through adapters: your documents, public web pages, orbital passes of open-data imaging satellites, open Sentinel-2 / Landsat imagery catalogs, and your own IoT sensors (read-only, with your authorization). |
| **Research** | Extracts atomic claims, each linked to its evidence and source, into an **evidence graph**. |
| **Verify** | Cross-checks claims across *independent* sources, records contradictions instead of averaging them away, and scores confidence from source quality, independence, recency, directness and contradiction. |
| **Report** | A cited report: verified, partially verified, contested and single-source findings, contradictions, what is missing, the full loop status, and the cost. |

Later stages are declared in the kernel already and show as "arrives in V2…V5" in every report. See [docs/ROADMAP.md](docs/ROADMAP.md).

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

# The gate before V2
lofgren certify
```

## Use it inside Claude Code, Codex and other AI tools (MCP)

Lofgren Intelligence has a certified-core local stdio MCP surface and a separate hosted/public MCP build. The local developer server below is usable now. The hosted service is not public-ready until its database, OAuth, tenancy, quota, economic, Stripe-sandbox, deployment and cross-client certification gates pass.

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

Tools return structured data, not narrative: `compile_objective`, `plan_research`, `investigate`, `verify_claim`, `get_finding`, `find_contradictions`, `find_gaps`, `trace_claim`, `get_receipt`, `export_state`, `render_report`, `satellite_passes`, `pricing`.

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
  discovery/     V2 entry contracts: Hypothesis, Candidate, promotion guard
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
- [Changelog](CHANGELOG.md) · [Security](SECURITY.md)

## License

Apache License 2.0. Copyright 2026 Lofgren Enterprise. See [LICENSE](LICENSE) and [NOTICE](NOTICE).

The open-source core (framework, schemas, adapters, verification) is free. The hosted Lofgren Intelligence service — orchestration, commercial data connectors, outcome memory, heavy compute and billing — is the paid layer.
