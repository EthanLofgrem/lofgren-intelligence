# Lofgren Intelligence

**Research and Verification. Discovery. Production. Authorized Execution. Outcome Measurement. Improvement Evaluation.**

Lofgren Intelligence (LI) is an open-source, governed research engine that compatible AI assistants can use through MCP. It connects a question to inspectable claims, source records, contradictions, bounded artifacts and versioned receipts. Ethan Lofgren is its founder and release authority.

**Release status: pre-public hosted verification.** Local developer use is available. A dedicated authenticated MCP endpoint is reachable, but hosted-worker execution and an ordinary end-to-end client journey remain unverified. There is **no committed public launch date**. See [current public status](docs/PUBLIC_STATUS.md) before connecting or relying on a capability.

## What makes LI useful

A useful answer should preserve how it was reached. LI provides mechanisms to inspect a claim, trace its sources, surface disagreements and missing evidence, and retain a case when a conversation disconnects. Discovery and generated artifacts remain linked to upstream evidence and requirements. External action requires separate authority; a model's request does not grant it permission.

For example, a researcher evaluating a technical option can inspect the evidence behind a conclusion, compare bounded candidate designs, produce an artifact where supported, and record later observations. Medical, legal and financial research still needs appropriate expert review. Receipts document provenance and execution; they do not prove that a source is truthful.

## Capabilities and boundaries

The V1–V6 implementations are available software capabilities, not six mandatory stages for every question.

| Layer | Implemented purpose | Boundary |
| --- | --- | --- |
| V1 — Research and Verification | Extract scoped claims, trace evidence, record contradictions and unknowns, produce reports and receipts | Unsupported statements must retain their limitations |
| V2 — Discovery | Frame problems, assess prior art, generate bounded candidates, simulate and optimize supported models | Hypotheses and predictions are not observations |
| V3 — Production | Build and verify supported specifications and deterministic artifacts | Not a general guarantee that any requested product can be built |
| V4 — Authorized Execution | Propose actions and enforce reviewed authority | Hosted external execution is disabled; duplicate-effect fencing remains unfinished |
| V5 — Outcome Measurement | Record observations and compare them with expectations | Observation does not establish causation |
| V6 — Improvement Evaluation | Evaluate improvement candidates and produce review proposals | No automatic production mutation or authority expansion |

**Interfaces differ.** The local stdio server supports V1–V3 developer workflows and handoff utilities. The hosted registry includes broader case and lifecycle tools behind OAuth and tenant checks. A tool's presence does not establish deployed acceptance. Inspect the [generated capability manifest](docs/CAPABILITIES.json), [request assessment schema](docs/li_assess_request.schema.json) and [MCP integration guide](docs/MCP_SELECTION_AND_VERIFICATION.md).


<details>
<summary>Inspect the authenticated hosted tool inventory</summary>

- **Research (V1):** `cancel_research`, `case_status`, `clarify_objective`, `compile_objective`, `export_knowledge_map2`, `export_state`, `find_contradictions`, `find_gaps`, `get_finding`, `get_job_status`, `get_receipt`, `investigate`, `li_assess_request`, `plan_research`, `render_report`, `satellite_passes`, `start_research`, `trace_claim`, `verify_claim`.
- **Discovery (V2):** `analyze_sensitivity`, `create_v3_handoff`, `discover`, `find_connections`, `find_discovery_gaps`, `find_prior_art`, `generate_candidates`, `generate_hypotheses`, `get_discovery_receipt`, `optimize_solution`, `render_discovery_report`, `simulate_candidate`, `verify_discovery`.
- **Production (V3):** `build_artifact`, `get_artifact`.
- **Execution (V4):** `action_status`, `execute_action`, `propose_action`.
- **Outcome (V5):** `get_outcome`, `measure_outcome`.
- **Improvement (V6):** `evaluate_improvement`, `get_improvement`.
- **Account / billing:** `account_status`, `billing_portal`, `create_checkout`, `pricing`, `usage_status`.

Trial tools, when enabled, are separate from this authenticated inventory.
</details>

## Sources and reasoning

- Local documents and protected public-URL intake are supported. Supplied URL manifests are labelled `operator_selected`.
- Europe PMC supplies **abstracts only**. Retractions, corrections and unavailable results have explicit handling.
- ClinicalTrials.gov supplies registry records. A registered trial is **not efficacy evidence**; trial results are not fetched.
- Configured search and model providers can extend research. They are not automatically included or funded by connecting LI.
- Without a configured reasoning provider, LI uses rule-based extraction. Do not mistake that mode for model-backed analysis.

Hosted public-source investigations require the durable worker. See [source behavior and limits](docs/HOSTED_PUBLIC_SOURCES.md).

## Try the local developer version

Requires Python 3.10 or later. The default branch may lag the current candidate; the following pins the application revision documented in the status page. Review updates before changing that pin.

```bash
git clone https://github.com/EthanLofgrem/lofgren-intelligence.git
cd lofgren-intelligence
git checkout e232ced165a31ab66124decb1f0fe9ea4b9057ac
python -m venv .venv
# Linux/macOS:
source .venv/bin/activate
# Windows PowerShell instead: .venv\Scripts\Activate.ps1
python -m pip install -e .
lofgren --help
lofgren investigate "What does the supplied sample evidence establish?" \
  --files examples/sample-sources --out report.md --receipt receipt.json
```

Sample sources are fictional demonstration data. This exercise does not establish real-world findings. Keep generated research files private when they contain sensitive inputs.

If you already use `uv`, an alternative is `uv sync --frozen` followed by `uv run lofgren --help`.

Configure a compatible local MCP client to start the installed executable:

```json
{
  "mcpServers": {
    "lofgren": {
      "command": "lofgren",
      "args": ["mcp"]
    }
  }
}
```

Client configuration formats differ. The executable must be available to the client; an absolute virtual-environment executable path may be necessary. This is local stdio configuration, not proof of hosted ChatGPT compatibility.

The local stdio server (`lofgren mcp`, contract `lofgren.mcp/3`) covers V1–V3 for developer use. It checks every tool
call against the tool's `inputSchema` and the hosted size limits (objective/claim 8000 characters, at most 20
documents, 250000 characters of inline text, 12 URLs) and answers a bad call with an `INPUT_INVALID: ...` tool error.
It is a trusted local tool: `files`, `tle_path` and a `sources_manifest` path read the caller's own disk. Only regular
files of a supported document type (txt, md, csv, json, html, rst) within the size limits are read; set
`LOFGREN_MCP_FILES_ROOT=/some/dir` to confine every local path to that directory.

## Hosted MCP and release work

The dedicated integration endpoint is `https://lofgren-intelligence-mcp.vercel.app/mcp`. It requires LI authorization. An anonymous **401 is expected**; health or discovery success alone does not establish usable research. Do not distribute administrative credentials or hosting bypass secrets to clients.

Current release work focuses on hosted-worker delivery, real OAuth/client acceptance, recovery and settlement, bounded anonymous-trial review, and exact-revision release evidence. [PUBLIC_STATUS.md](docs/PUBLIC_STATUS.md) separates implemented features from demonstrated delivery.

Hosted refusals reach the client as `<CODE>: reason` with a stable code (`UNSAFE_URL`, `INPUT_INVALID`,
`MANIFEST_INVALID`, `EXECUTION_REFUSED`, `IMPROVEMENT_REFUSED`, or a service code such as `QUOTA_EXCEEDED`);
unexpected failures stay a generic error. See [docs/PUBLIC_MCP.md](docs/PUBLIC_MCP.md#typed-refusals).

**Not proven:** complete customer deployment, real clients, hosted recovery, backup/restore and `PublicMCPReady`. These require retained operational evidence; code certification alone is insufficient.

## New evidence and updates

LI is designed to reassess findings when sources, scope or assumptions change while preserving earlier records. It does not promise continuous monitoring of every law, rule or publication. Legal changes require jurisdiction, effective date, authoritative sources and reviewed interpretation. Code and policy releases should pass review and testing before deployment; LI does not silently update its own production code. The [change-aware update guide](docs/CHANGE_AWARE_UPDATES.md) explains the existing offline receipt comparison and the requirements for future monitored reassessment.

## Learn, contribute and support

Start with [architecture](docs/ARCHITECTURE.md), [evidence protocol](docs/EVIDENCE_PROTOCOL.md), [discovery](docs/DISCOVERY.md), [production](docs/PRODUCTION.md), [worker runtime](docs/WORKER_RUNTIME.md) and [public privacy](docs/PUBLIC_PRIVACY.md). See [CHANGELOG.md](CHANGELOG.md) for release history and the [roadmap](docs/ROADMAP.md) for current construction priorities. To understand a finding, inspect its claim, supporting and contradicting evidence, assumptions, evidence cut-off and receipt; treat an unanswered dependency as an unknown rather than a confirmed conclusion.

Use [GitHub issues](https://github.com/EthanLofgrem/lofgren-intelligence/issues) for reproducible defects and proposals. Include the revision, command, expected behavior and sanitized evidence. Do not post credentials, private research or personal data. Contributions that improve evidence quality, safety boundaries and verified interoperability are especially useful.

**Donations:** no verified donation destination is published here. A founder-approved support link can be added when available; do not send funds to unverified links. Live paid service and payment activation are not established by this repository.

Licensed under [Apache 2.0](LICENSE).
