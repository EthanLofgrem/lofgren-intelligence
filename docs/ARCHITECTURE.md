# Architecture

Lofgren Intelligence is one kernel that every version extends. V1 through V6 are one evolving codebase, not six products.

```
                         OBJECTIVE
                             │
                     INTENT COMPILER ──────────► Outcome Contract (the project's constitution)
                             │
                     RESEARCH PLANNER ─────────► gather tasks, gaps, stopping rule
                             │
      ┌──────────┬───────────┼────────────┬─────────────┐
   documents   web pages   orbital     imagery       sensors      ◄── adapter registry
      └──────────┴───────────┼────────────┴─────────────┘             (capabilities, cost,
                             │                                          license, read-only)
                      EVIDENCE GRAPH ──────────► source → evidence → claim, typed edges
                             │
                     VERIFICATION ENGINE ──────► cross-check, contradictions, confidence,
                             │                   calibration, status vs. the contract
                          REPORT
                             │
        V2 Imagine · Simulate · Optimize   (built: lofgren_intelligence/discovery, see docs/DISCOVERY.md)
        V3 Produce · Build · Test          (built: lofgren_intelligence/production, see docs/PRODUCTION.md)
        V4 Authorize · Execute             (built: lofgren_intelligence/execution, see docs/EXECUTION.md)
        V5 Measure outcomes                (built: lofgren_intelligence/outcome, see docs/OUTCOME.md)
        V6 Evaluate improvements           (built: lofgren_intelligence/improvement, see docs/IMPROVEMENT.md)
```

## Hosted capability surface

The hosted MCP service (`lofgren_intelligence/hosted`, endpoint `/mcp`)
serves V1 through V6 plus account tools. The single source of truth for that
surface is the generated capability manifest,
[`docs/CAPABILITIES.json`](CAPABILITIES.json), produced by
`python -m lofgren_intelligence.hosted.capabilities` from the tool registry
the service actually builds. A test fails if the registry, the manifest or
this document, `README.md` and `docs/PUBLIC_MCP.md` disagree.

| Level | Hosted tools |
| --- | --- |
| V1 | 13: `compile_objective`, `export_knowledge_map2`, `export_state`, `find_contradictions`, `find_gaps`, `get_finding`, `get_receipt`, `investigate`, `plan_research`, `render_report`, `satellite_passes`, `trace_claim`, `verify_claim` |
| V2 | 13: `analyze_sensitivity`, `create_v3_handoff`, `discover`, `find_connections`, `find_discovery_gaps`, `find_prior_art`, `generate_candidates`, `generate_hypotheses`, `get_discovery_receipt`, `optimize_solution`, `render_discovery_report`, `simulate_candidate`, `verify_discovery` |
| V3 | 2: `build_artifact`, `get_artifact` |
| V4 | 3: `action_status`, `execute_action`, `propose_action` |
| V5 | 2: `get_outcome`, `measure_outcome` |
| V6 | 2: `evaluate_improvement`, `get_improvement` |
| account | 5: `account_status`, `billing_portal`, `create_checkout`, `pricing`, `usage_status` |

"Hosted" means registered on the authenticated endpoint for activated
accounts. It does not mean deployed or launched. Not proven by this
repository: deployment, real clients (no real third-party MCP client session
is verified), backup/restore, Supabase owner settings, and PublicMCPReady (the
public release gate has not passed).

## Principles

1. **Models are replaceable suppliers.** Every model sits behind `models.ModelProvider`. The offline `HeuristicProvider` keeps the system working with no model at all. Model output is stored as a claim to be verified, never as truth.
2. **The evidence graph is the source of truth.** Every claim links to the evidence and source it came from, with time, place, license and transformation history. A sensor reading, a satellite calculation, a document statement and a model inference are different types.
3. **Contradictions are recorded, not averaged.** Disagreement between independent sources is a lead worth investigating.
4. **Independence matters.** Sources in the same independence group (one publisher, one press release) do not confirm each other.
5. **Thinking and acting are separate.** Nothing acts in the world without passing the authority engine.
6. **Deterministic computation beats model intuition where it applies.** Orbital mechanics, pricing, confidence scoring and (in V2) optimization and simulation are code, not prompts.
7. **Confidence must be calibrated.** The calibrator records (stated confidence, was it right) and pulls overconfident scores down.

## The Outcome Contract

Compiled by `intent.compile_intent`. Fields that govern every later stage:

| Field | Purpose |
| --- | --- |
| `mode` | `investigate`, `verify` or `venture` (Lofgren Enterprise stages) |
| `questions` | what must be known, each with a role and the capabilities that could answer it |
| `constraints` | budget, place, scope parsed from the objective |
| `evidence_standard` | minimum independent sources and confidence for "Verified" |
| `max_spend_usd` | research spend cap; the run stops before research if the estimate exceeds it |
| `allowed_actions` / `approval_required` | what may happen without asking, and what always needs a yes |

## Adapters and the registry

An adapter declares `capabilities`, `license`, `cost_per_call_usd` and `authorized_operations`. The planner asks the registry which available adapter can satisfy each capability a question needs; a capability with no adapter becomes a gap in the report.

| Adapter | Capability | Source |
| --- | --- | --- |
| `DocumentAdapter` | `text` | user files and inline text |
| `WebPageAdapter` | `text` | public pages by URL (no crawling, no logins) |
| `OrbitalPassAdapter` | `orbital_passes` | public TLEs (file or CelesTrak) → pass predictions |
| `ImageryCatalogAdapter` | `imagery_catalog` | Earth Search STAC: Sentinel-2 L2A, Landsat C2 L2 (metadata only) |
| `SensorAdapter` | `sensor` | user-owned sensor CSVs, only with explicit authorization |

The registry refuses any adapter that requests `write` or `control`. Control of devices is a V4 action behind the authority engine.

## Orbital mechanics

`orbital/propagate.py` implements two-body motion with secular J2 perturbations (nodal regression, apsidal precession, mean-motion correction), GMST rotation to Earth-fixed coordinates, WGS84 geodesy, look angles and pass search with bisection-refined rise and set times. Tests check the altitude of a Sentinel-2-like orbit, a 90° elevation from the sub-satellite point, and the sun-synchronous node drift (≈ 0.9856°/day). Accuracy is tens of kilometres within a day of fresh elements — enough to know when to request imagery, not for precision tasking; swap in SGP4 for that.

## Verification

For each pair of claims from different independence groups:

- **Same subject?** The subject is the words before the claim's first change verb ("Industrial *vacancy* in Phoenix rose" is not about construction). Match requires overlap ≥ 0.8 and Jaccard ≥ 0.5 on subject words.
- **Agree?** Opposite polarity, opposite direction of change, or values more than 15% apart (same unit) → contradiction. Otherwise → mutual support.

Confidence:

```
z = -1.6 + 2.0·Q + 0.9·(min(D,4) − 1) + 0.8·T + 0.6·R − 1.4·K
confidence = calibrate(1 / (1 + e^−z))
```

Q = mean source quality, D = independent supporting sources, T = directness (observed 1.0, extracted 0.8, user 0.5, model-inferred 0.4), R = recency (e^(−age/730 days)), K = independent contradicting sources. These weights are a documented starting point to be recalibrated against measured outcomes.

## Cost control

The planner estimates work units; the billing module maps them to a job class and price for the user's plan. The authority engine blocks the run if the estimate exceeds the contract's cap. During the run, the research budget is capped at the estimated class, and the final charge is never more than the estimate.

## V1 data path (0.2.0)

```
Objective → Outcome Contract → research question graph → evidence requirements
→ source selection / search → acquisition → normalization (scope, units, calculations)
→ evidence graph → claim extraction → lineage → independent verification (policies)
→ contradiction graph → skeptic → unknowns → findings → report + research receipt
```

Report text is a view of validated state, never the state itself. See [EVIDENCE_PROTOCOL.md](EVIDENCE_PROTOCOL.md) and [V1_CERTIFICATION.md](V1_CERTIFICATION.md).

## Known limits

- The offline extractor finds factual-looking sentences; it does not understand them. Configure a model provider for better extraction; the skeptic still checks every model claim against its evidence.
- Question answering is role-based (state, support, contradict, gap, prior art, venture stage keywords), not semantic.
- Web discovery needs a search-provider key (Brave included); without one, V1 reads documents and pages you name.
- Imagery adapters return normalized scene metadata, not pixel analysis. Pixel change detection is next.
- Confidence is provisional until outcomes are recorded in the prediction log and the calibrator is fitted.
