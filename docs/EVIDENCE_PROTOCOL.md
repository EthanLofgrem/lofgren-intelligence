# Evidence protocol

Lofgren Intelligence's open data model. JSON Schemas are generated from the code (`lofgren schemas --out schemas/`) and committed in `schemas/`.

```
Source ──provided_by── Evidence ──supports / contradicts── Claim ──answers── Finding
   │                        │                                 │
 derived_from           derived_from                     conflicts (Contradiction)
   │                        │
 Source                 Calculation                       Unknown (acquisition plan)
```

| Type | What it is | Key fields |
| --- | --- | --- |
| `Source` | Where evidence came from | kind, uri, publisher, license, quality prior, independence group, derived_from |
| `Evidence` | One observation, passage, dataset or calculation result | kind, content, content_hash, observed_at, valid_from/to, location, transformations |
| `Claim` | One checkable statement | origin (extracted, observed, inferred, user, hypothesis), type, value/unit, scope, status, confidence, policy, issues |
| `Contradiction` | Two claims that disagree | kind (incompatible, scope_mismatch), reason, scope note, resolution, severity |
| `Calculation` | A derived number with its receipt | formula, inputs (value, unit, evidence id), result |
| `Unknown` | A gap as executable work | capability, possible sources, expected gain, estimated cost, approval needed |
| `Finding` | The answer to one research question | claim ids, evidence ids, contradictions, unknowns, scope, confidence (+ status and method), next best evidence |
| `Scope` | Where and when a claim applies | valid_from, valid_to, geography, lat, lon |

## Rules

1. **Observation ≠ interpretation ≠ claim.** A satellite pass or sensor mean is an observation (origin `observed`) with a calculation receipt. A statement in a document is `extracted`. A model's own conclusion is `inferred`. A V2 idea is a `hypothesis`.
2. **Independence is by lineage, not by URL.** Copies, syndication and quoting merge sources.
3. **Scope is part of the fact.** Claims about different periods or places are not treated as the same fact.
4. **Every number has a receipt.**
5. **Confidence carries its status and method** (`provisional` / `calibrated`, `v1-heuristic`).
6. **Reports add no facts.** They render findings.

## Research receipt (`lofgren.research-receipt/2`)

Contract and hash; plan; every operation from the ledger; sources; evidence hashes; rejected evidence; claims with policy, scope and issues; contradictions; calculations; lineage; unknowns; findings; provider name, version and usage; extraction template; verifier settings; cost; timestamps; `inputs_hash`, `state_hash`, `knowledge_state_hash` (the commitment to everything knowledge-map/2 exports), `report_hash`; and `research_id`, the hash of all of it. `/1` receipts still verify but cannot vouch for a knowledge-map/2.

## From evidence to a V3 handoff

```
Evidence → Claim (V1 verified) → KnownFact ─┐
                                            ├→ Hypothesis / CounterHypothesis (never verified)
Contradiction / Unknown / Gap ──────────────┘          │ EvidenceRequirement → reverify() → new V1 run
                                                       ↓
Assumption + Constraint (rests on a known fact or a named assumption)
                                                       ↓
Candidate → Scenario → Simulation (kind: simulated) → SensitivityResult
          → OptimizationResult (optimal only with a proof; re-checked)
                                                       ↓
Discovery verifier (lowers only) → DiscoveryDecision (explicit rule) → discovery receipt → V3 handoff
```

| Format | What it is |
| --- | --- |
| `lofgren.knowledge-map/2` | V1's state for V2: claims, evidence, sources, lineage, findings with derivation, fingerprints, receipt reference |
| `lofgren.discovery-receipt/1` | What determined a discovery: evidence fingerprints, config, algorithms, seeds, rule, object digests, ledger; `discovery_fingerprint` and `discovery_id` |
| `lofgren.v3-handoff/1` | The selected candidate, typed: specifications with sources, assumptions, constraints, simulated expected outcomes, acceptance criteria, open questions, fingerprints |

V2 rules: a V2 idea is never a fact; a simulation is never an observation; a prior-art miss is never novelty; a
speculative connection only feeds hypotheses; every number in a handoff has a typed source.
