# V2 construction status and certification blockers

This file records where V2 construction stands against `docs/V2_CONSTRUCTION_DIRECTIVE.md`, and what blocks
certification. It states what is implemented and tested, not what is certified: nothing in V2 is certified until
`V2ReadyForV3` passes on one pushed SHA.

## Implemented (tested, not certified)

| Step | Scope | Modules |
| --- | --- | --- |
| 1 | Typed V2 objects, typed errors, structured unit-aware expressions, hardening | `discovery/types.py`, `errors.py`, `expr.py` |
| 2 | `DiscoveryContext` over one immutable `knowledge-map/1`, problem framing, prior-art assessment, gaps | `discovery/context.py`, `frame.py`, `prior_art.py`, `gaps.py` |
| 2-3 | Constraints and assumptions, evidence requirements, JSON Schemas | `discovery/principles.py`, `requirements.py`, `schemas.py`, `schemas/discovery/` |

Not started: connections, hypotheses and counter-hypotheses, candidates, simulation, sensitivity, optimization,
the discovery verifier, receipts, the V3 handoff, MCP tools, V2 certification.

## The knowledge-map fingerprint

`DiscoveryContext.knowledge_map_fingerprint` is `KMF-` followed by the SHA-256 of the canonical JSON of the
exported map (`sha256/canonical-json-1`):

- object keys sorted; separators `,` and `:`; UTF-8; no ASCII escaping
- the entity collections (`questions`, `known`, `uncertain`, `contradicted`, `contradictions`, `unknowns`,
  `calculations`, `findings`) sorted by `id`; every other array keeps its order
- NaN and Infinity rejected; a float with an integral value within ±2^53 written as an integer; larger
  integers rejected

It identifies the map V2 consumed. It is **not** V1's receipt `state_hash` and proves nothing about V1's internal
evidence state, because `knowledge-map/1` does not carry that hash.

## Knowledge-map compatibility rule

- Only `lofgren.knowledge-map/1` is accepted. Any other schema string is refused until V2 implements it.
- Within `/1` an exporter may add fields (additive compatibility). V2 requires the fields it reads, type-checks
  every field it recognizes, ignores the rest and lists them in `DiscoveryContext.limitations`. Ignored fields are
  still covered by the fingerprint.
- A recognized optional field that is absent takes a documented default (`context.OPTIONAL_DEFAULTS`), and every
  default used is listed in `limitations`. An unexported contradiction `kind` is treated as `incompatible`, so a
  possible conflict is surfaced rather than hidden.
- Malformed values (wrong types, NaN or Infinity, impossible dates, ids of the wrong kind, statuses in the wrong
  section) fail closed.

## Blockers

### V1 certification blocker: `knowledge-map/2`

Status: V1 exports `lofgren.knowledge-map/2` (`kernel/knowledge_map.py`, `lofgren investigate --state2`,
`schemas/knowledge-map-2.schema.json`; hardening branch, commit 5) and `/1` is unchanged. `DiscoveryContext` still
accepts `/1` only; consuming `/2` is the next hardening step. `/2` carries the five items below, plus claim
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
