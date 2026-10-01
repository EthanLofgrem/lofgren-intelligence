"""Problem framing: what the V1 knowledge map says is known, uncertain, contested and missing.

`frame_problem` turns the map held by a `DiscoveryContext` into typed V2 objects and one
`ProblemFrame`. It never adds knowledge:

- each known V1 claim becomes a `KnownFact` with its statement, value, policy, evidence
  confidence and scope copied unchanged
- each uncertain or contested claim becomes an `Uncertainty`
- each open V1 unknown becomes `MissingEvidence`
- the frame's scope is copied from V1 findings; if they disagree, the widening is recorded

If the map has no known and no uncertain claims, there is nothing to frame: the result is
`INSUFFICIENT_EVIDENCE` with the missing evidence, and no frame.
"""

from __future__ import annotations

from dataclasses import dataclass, fields

from ..evidence.scope import merge_scopes
from ..evidence.types import Scope
from .context import DiscoveryContext, thaw
from .types import (
    DiscoveryObjective,
    DiscoveryOutcome,
    KnownFact,
    MissingEvidence,
    ProblemFrame,
    Uncertainty,
    UncertaintyReason,
    texts,
    utcnow,
)


@dataclass(frozen=True)
class FrameResult:
    objective: DiscoveryObjective
    frame: ProblemFrame | None  # None when the outcome is INSUFFICIENT_EVIDENCE
    outcome: DiscoveryOutcome | None  # None when a frame was built
    known_facts: tuple[KnownFact, ...]
    uncertainties: tuple[Uncertainty, ...]
    missing_evidence: tuple[MissingEvidence, ...]
    notes: tuple[str, ...]


def uncertainty_reason(record) -> UncertaintyReason:
    """Why a V1 claim is uncertain, from its status and issues only (knowledge-map/1 has nothing more)."""
    if record["status"] == "contested":
        return UncertaintyReason.CONTESTED
    if any(str(i).lower().startswith("stale") for i in record["issues"]):
        return UncertaintyReason.STALE
    if record["status"] == "insufficient_evidence":
        return UncertaintyReason.INSUFFICIENT
    return UncertaintyReason.POLICY_UNMET


def _has_scope(s: Scope) -> bool:
    return any(getattr(s, f.name) is not None for f in fields(Scope))


def frame_scope(context: DiscoveryContext) -> tuple[Scope, str]:
    """The scope of the V1 findings, unchanged, or their recorded widening."""
    scopes = [context.scope_of(f) for f in context.entities("findings")]
    scopes = [s for s in scopes if s is not None and _has_scope(s)]
    if not scopes:
        return Scope(), "V1 established no time or place for this objective; the frame is unscoped."
    distinct = {tuple(getattr(s, f.name) for f in fields(Scope)) for s in scopes}
    if len(distinct) == 1:
        return scopes[0], "Copied unchanged from the V1 findings."
    merged = merge_scopes(scopes)
    listed = "; ".join(sorted(f"{s.valid_from or '?'}..{s.valid_to or '?'} {s.geography or '?'}" for s in scopes))
    return merged, (f"Widened to cover {len(distinct)} different V1 finding scopes ({listed}). "
                    "Later objects must keep this widening visible.")


def frame_problem(context: DiscoveryContext, objective: DiscoveryObjective,
                  success_metrics: list[str] | tuple[str, ...] = (), at: str | None = None) -> FrameResult:
    """Build the V2 view of the map. Registers every object in `context`; calling it twice is a no-op."""
    at = at or utcnow()
    objective = context.ensure(objective)
    metrics = texts(list(success_metrics), "frame_problem.success_metrics")
    notes = []

    facts = tuple(context.ensure(KnownFact(
        c["id"], c["statement"], thaw(c["scope"]), c["value"], c["unit"], c["policy"], c["confidence"],
        created_at=at)) for c in context.claims("known"))
    uncertain = [c for cat in ("uncertain", "contradicted") for c in context.claims(cat)]
    uncertainties = tuple(context.ensure(Uncertainty(
        c["id"], c["statement"], uncertainty_reason(c), thaw(c["scope"]), c["confidence"], created_at=at))
        for c in sorted(uncertain, key=lambda c: c["id"]))
    missing = tuple(context.ensure(MissingEvidence(
        u["id"], u["description"], u["capability"], list(u["source_types"]), u["expected_gain"],
        u["est_cost_usd"], u["needs_approval"], created_at=at)) for u in context.entities("unknowns"))

    if not facts and not uncertainties:
        notes.append("The knowledge map has no known and no uncertain claims, so there is nothing to frame. "
                     "Close the missing evidence through V1 first.")
        return FrameResult(objective, None, DiscoveryOutcome.INSUFFICIENT_EVIDENCE, facts, uncertainties, missing,
                           tuple(notes))

    scope, scope_note = frame_scope(context)
    frame = context.ensure(ProblemFrame(
        objective.id, [f.id for f in facts], [u.id for u in uncertainties],
        [cx["id"] for cx in context.entities("contradictions")], [m.id for m in missing], scope, metrics,
        scope_note, created_at=at))
    if not facts:
        notes.append("No V1 claim met its evidence policy; everything framed is uncertain.")
    return FrameResult(objective, frame, None, facts, uncertainties, missing, tuple(notes))
