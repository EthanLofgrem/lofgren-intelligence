# V2 construction status and certification blockers

This file records where V2 construction stands against `docs/V2_CONSTRUCTION_DIRECTIVE.md`, and what blocks
certification. It states what is implemented and tested, not what is certified: nothing in V2 is certified until
`V2ReadyForV3` passes on one pushed SHA (`scripts/v2_gate.py`, see docs/V2_CERTIFICATION.md).

## Implemented (tested, not certified)

| Step | Scope | Modules |
| --- | --- | --- |
| 1 | Typed V2 objects, typed errors, structured unit-aware expressions, hardening | `discovery/types.py`, `errors.py`, `expr.py` |
| 2 | `DiscoveryContext` over one immutable knowledge map (`/2` validated and receipt-bound, `/1` degraded), problem framing, prior-art assessment, gaps | `discovery/context.py`, `frame.py`, `prior_art.py`, `gaps.py` |
| 2-3 | Constraints and assumptions, evidence requirements, JSON Schemas | `discovery/principles.py`, `requirements.py`, `schemas.py`, `schemas/discovery/` |
| 4-15 | Connections, hypotheses and counter-hypotheses, candidates and decision rule, simulation, sensitivity, optimization, discovery verifier, ledger and receipt, V3 handoff, pipeline and reports, MCP tools (`lofgren.mcp/2`), adversarial suite, V2 certification, docs and CI | `connections.py`, `hypotheses.py`, `candidates.py`, `simulate.py`, `sensitivity.py`, `optimize.py`, `verifier.py`, `receipt.py`, `handoff.py`, `pipeline.py`, `report.py`, `certification.py`, `mcp/server.py` |

## The knowledge-map fingerprint

For a `knowledge-map/2` context, `knowledge_map_fingerprint` is the map's own `KM2-` fingerprint and
`content_fingerprint` its `KM2C-` fingerprint (`sha256/canonical-json-2`, defined in `kernel/knowledge_map.py`),
both recomputed on load; `receipt` exposes the research id and the receipt's contract, inputs and state hashes.
None of these is interchangeable with another, and no KMF is computed for `/2`.

The receipt (`lofgren.research-receipt/2`) also carries `knowledge_state_hash`, its commitment to the complete
state a `/2` map exports. Editing any field of a map and recomputing `KM2-`, `KM2C-` and the map's copy of the
commitment still fails against the intact receipt. `lofgren.research-receipt/1` receipts still verify but are
refused as a binding for `/2`. The research id is an unkeyed hash: the binding is as trustworthy as the receipt the
consumer already holds.

For a `knowledge-map/1` context, `knowledge_map_fingerprint` is `KMF-` followed by the SHA-256 of the canonical JSON of the
exported map (`sha256/canonical-json-1`):

- object keys sorted; separators `,` and `:`; UTF-8; no ASCII escaping
- the entity collections (`questions`, `known`, `uncertain`, `contradicted`, `contradictions`, `unknowns`,
  `calculations`, `findings`) sorted by `id`; every other array keeps its order
- NaN and Infinity rejected; a float with an integral value within ±2^53 written as an integer; larger
  integers rejected

It identifies the map V2 consumed. It is **not** V1's receipt `state_hash` and proves nothing about V1's internal
evidence state, because `knowledge-map/1` does not carry that hash.

## Knowledge-map compatibility rule

- `lofgren.knowledge-map/2` is accepted only with the research receipt it was exported from
  (`DiscoveryContext(map, receipt)`), and only if V1's own validator (`validate_knowledge_map`) accepts the pair:
  assurance `validated_v2`. Its records are kept as exported; CL, EV, SRC, CX, UNK, CALC, F and Q ids resolve;
  evidence, sources and lineage are inspectable. Unverified claims resolve but cannot be cited by V2 objects.
- `lofgren.knowledge-map/1` is accepted without a receipt (a receipt with `/1` is refused): assurance
  `degraded_v1`, unchanged behaviour, EV ids attachment-only, no sources. Any other schema string is refused.
- Within `/1` an exporter may add fields (additive compatibility). V2 requires the fields it reads, type-checks
  every field it recognizes, ignores the rest and lists them in `DiscoveryContext.limitations`. Ignored fields are
  still covered by the fingerprint.
- A recognized optional field that is absent takes a documented default (`context.OPTIONAL_DEFAULTS`), and every
  default used is listed in `limitations`. An unexported contradiction `kind` is treated as `incompatible`, so a
  possible conflict is surfaced rather than hidden.
- Malformed values (wrong types, NaN or Infinity, impossible dates, ids of the wrong kind, statuses in the wrong
  section) fail closed.

## V1 -> V2 boundary certification

`lofgren certify-boundary` runs executable scenarios for the eleven code terms of the step-4 gate (identity, graph
integrity, question isolation, synthesis, knowledge-map/2, receipt commitment, provenance, DiscoveryContext/2,
adversarial cases, V1 certification). `scripts/boundary_gate.py --sha <commit>`, run in a fresh clone of that commit
after CI, adds the process terms (full suite in both encodings, package gate and CI from GitHub Actions, clean tree,
exact SHA) and is the only place that prints `V1ReadyForV2Step4`. Unknown is false.

Future-dated evidence (LI-V1-HARDEN-07A, formerly the pinned defect V1-FUTURE-DATED-EVIDENCE): evidence dated more
than 5 minutes (`FUTURE_SKEW`) after the verifier's clock stays in the graph and in provenance but supports,
contradicts and adds independence or freshness to nothing, and its claim carries a `future-dated:` issue. Up to 5
minutes ahead is treated as clock skew. The run's verification time (`Verifier.now`) is the clock.

## Blockers

### V1 certification blocker: `knowledge-map/2`

Status: V1 exports `lofgren.knowledge-map/2` (`kernel/knowledge_map.py`, `lofgren investigate --state2`,
`schemas/knowledge-map-2.schema.json`; hardening branch, commit 5) and `/1` is unchanged. `DiscoveryContext`
consumes `/2` (commit 6, see the compatibility rule above); boundary certification is next. `/2` carries the five items below, plus claim
question associations and finding derivation, a map fingerprint (`KM2-`, the exact export) and a content
fingerprint (`KM2C-`, reproducible across reruns of identical inputs). Item 4 is carried as the verifier's
recorded factors and the policy thresholds, not as a re-derived reason.

`knowledge-map/1` is too thin for V2 to audit what it consumes. Before V2 certification, V1 needs a
backwards-compatible `knowledge-map/2` export (keeping `/1`) that adds:

1. V1's receipt `state_hash` (and `inputs_hash`), so a V2 run can pin the V1 evidence state it rests on, not only
   the exported map.
2. Evidence and source summaries for every evidence id the map cites: source kind, publisher, retrieval time,
   licence, quality, content hash.
3. Lineage: independence groups and declared derivations, so V2 can check that copies were not counted as
   independent confirmation.
4. The reason a claim fell short of its policy (for example single-source versus stale), not only its status.
5. Claims referenced by contradictions and findings but not exported today (V2 records these as unresolvable
   limitations).

### V2 certification blockers inherited from V1

A perfect `DiscoveryContext` can only guarantee that V2 consumed V1's state faithfully. These V1 defects must be
fixed before hypothesis generation (step 4) and before V2 certification. Both are fixed on the hardening branch
(claim identity v2, commits 2 and 2A; question isolation, commit 4) and await certification:

1. **Scope-insensitive claim identity.** `Claim.id` hashes only the normalized statement, so the same sentence
   about different places, periods or questions collapses into one claim, and `EvidenceGraph.add_claim` keeps
   whichever arrived first.
2. **Question-answer leakage.** `kernel/answers.py` ranks all claims globally for ordinary questions instead of
   first restricting to the claims gathered for that question, so a strong claim from one question can answer
   another.

## Branch policy

Each active coding session works on its own branch (for example `build/v2-step2-reconcile`). `build/v2-discovery`
is the integration branch: work reaches it by pull request, never by pushing a shared working branch.

## Next steps (agreed order)

Steps 2-3 (this work) → V1 hardening: `knowledge-map/2` and the two defects above → step 4 hypotheses and
counter-hypotheses → candidates → simulation and sensitivity → optimization → discovery verifier → receipts and
V3 handoff → V2 certification → V3.
