"""Gap analysis: what the framed knowledge lacks, and on what basis each gap is claimed.

Sources of gaps, each with an explicit `basis`:

    V1 unknown                    -> evidence or data gap, basis "unknown"
    incompatible V1 contradiction -> knowledge gap, basis "contradiction"
    prior-art search, no match    -> market or technical gap, basis "search_absence"

Confidence here means confidence that the gap exists in the consumed knowledge. For V1 unknowns
and contradictions that is certain: V1 recorded them. For search absence it is capped (see
`SEARCH_ABSENCE_MAX_CONFIDENCE`) and always carries the coverage statement, because not finding
something does not show it is missing. Contradictions that only differ in scope are leads, not
conflicts, and do not become gaps. Incomplete prior-art searches produce no gap.
"""

from __future__ import annotations

from dataclasses import dataclass

from .context import DiscoveryContext
from .errors import UnknownReference
from .frame import FrameResult
from .types import (
    SEARCH_ABSENCE_MAX_CONFIDENCE,
    Gap,
    GapBasis,
    GapType,
    PriorArtAssessment,
    PriorArtConclusion,
    check_no_novelty_claim,
    utcnow,
)

# V1 capabilities whose absence is missing data rather than missing documentary evidence.
DATA_CAPABILITIES = frozenset({"sensor", "orbital_passes", "imagery_catalog"})
RECORDED_GAP_CONFIDENCE = 1.0  # V1 recorded the unknown or contradiction; the gap's existence is not in doubt


@dataclass(frozen=True)
class GapResult:
    gaps: tuple[Gap, ...]
    notes: tuple[str, ...]


def detect_gaps(context: DiscoveryContext, framed: FrameResult,
                assessments: list[PriorArtAssessment] | tuple[PriorArtAssessment, ...] = (),
                search_gap_type: GapType | str = GapType.MARKET, at: str | None = None) -> GapResult:
    at = at or utcnow()
    gaps, notes = [], []
    unknowns = {u["id"]: u for u in context.entities("unknowns")}
    for m in framed.missing_evidence:
        u = unknowns[m.unknown_id]
        question = f" for question {u['question_id']}" if u["question_id"] else ""
        gaps.append(Gap(
            GapType.DATA if m.capability in DATA_CAPABILITIES else GapType.EVIDENCE, m.description,
            f"V1 recorded this as an open unknown{question}; findings that depend on it stay incomplete.",
            GapBasis.UNKNOWN, [m.unknown_id], RECORDED_GAP_CONFIDENCE, list(m.sources), m.expected_gain,
            m.est_cost_usd, m.needs_approval, derived_from=[m.id], created_at=at))

    for cx in context.entities("contradictions"):
        if cx["kind"] != "incompatible":
            notes.append(f"{cx['id']} differs only in scope ({cx['scope_note'] or 'different period or place'}); "
                         "recorded as a lead, not a knowledge gap.")
            continue
        cited = [cx["id"]] + [c for c in (cx["claim_a"], cx["claim_b"]) if _resolves(context, c)]
        gaps.append(Gap(
            GapType.KNOWLEDGE, f"Which of {cx['claim_a']} and {cx['claim_b']} holds: {cx['reason'] or 'they disagree'}",
            "Independent V1 evidence disagrees; anything built on either claim inherits the conflict.",
            GapBasis.CONTRADICTION, cited, RECORDED_GAP_CONFIDENCE, [cx["resolution"]] if cx["resolution"] else [],
            cx["severity"], created_at=at))

    for a in assessments:
        if a.conclusion == PriorArtConclusion.INCOMPLETE:
            notes.append(f"{a.id}: the prior-art search was incomplete; no gap is inferred from it.")
            continue
        if a.conclusion != PriorArtConclusion.NO_MATCH_WITHIN_COVERAGE:
            continue
        missing = check_no_novelty_claim(
            f"No matching prior art recorded for {a.subject} within the searched coverage", f"{a.id}:gap")
        gaps.append(Gap(
            search_gap_type, missing,
            "May indicate an opening, or may only reflect what the searched sources cover.",
            GapBasis.SEARCH_ABSENCE, [a.id], SEARCH_ABSENCE_MAX_CONFIDENCE,
            ["Search further sources, domains or periods", "Test the opening directly through V1 research"],
            coverage_statement=a.statement, derived_from=[a.id], created_at=at))

    return GapResult(tuple(context.ensure(g) for g in sorted(gaps, key=lambda g: g.id)), tuple(notes))


def _resolves(context: DiscoveryContext, ref: str) -> bool:
    try:
        context.reference(ref)
        return True
    except UnknownReference:
        return False
