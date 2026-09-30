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

## Research receipt (`lofgren.research-receipt/1`)

Contract and hash; plan; every operation from the ledger; sources; evidence hashes; rejected evidence; claims with policy, scope and issues; contradictions; calculations; lineage; unknowns; findings; provider name, version and usage; extraction template; verifier settings; cost; timestamps; `inputs_hash`, `state_hash`, `report_hash`; and `research_id`, the hash of all of it.
