"""Evidence requirements: what V1 would have to establish. V2 emits these; only V1 can satisfy them.

A requirement carries a V1 capability, a place and period taken from the frame (never widened
silently), and optionally a machine-evaluable threshold with declared units.
"""

from __future__ import annotations

from .context import DiscoveryContext
from .frame import FrameResult
from .types import EvidenceRequirement, Gap, GapBasis, utcnow


def requirement_for_gap(context: DiscoveryContext, gap: Gap, framed: FrameResult,
                        at: str | None = None) -> EvidenceRequirement:
    """The V1 evidence that would close `gap`, scoped to the frame."""
    context.reference(gap.id, (Gap,), "requirement_for_gap.gap")
    capability = "text"
    if gap.basis == GapBasis.UNKNOWN:
        unknown = context.reference(gap.evidence_ids[0], ("unknown",), "requirement_for_gap.gap.evidence_ids[0]")
        capability = unknown["capability"] or "text"
    scope = framed.frame.scope if framed.frame is not None else None
    place = scope.geography if scope is not None else None
    when = (scope.valid_from, scope.valid_to) if scope is not None else (None, None)
    return context.ensure(EvidenceRequirement(
        f"Evidence that would close: {gap.missing}", capability, place, when, derived_from=[gap.id],
        created_at=at or utcnow()))


def evidence_requirement(context: DiscoveryContext, description: str, capability: str = "text",
                         place: str | None = None, period: tuple[str | None, str | None] = (None, None),
                         threshold: dict | None = None, variable_units: dict[str, str] | None = None,
                         derived_from: list[str] | tuple[str, ...] = (), at: str | None = None) -> EvidenceRequirement:
    return context.ensure(EvidenceRequirement(description, capability, place, period, threshold, variable_units or {},
                                              derived_from=list(derived_from), created_at=at or utcnow()))
