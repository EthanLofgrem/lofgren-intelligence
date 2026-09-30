"""The V1 -> V2 hand-off: a knowledge map built only from typed, verified state.

Discovery Intelligence (V2) reads this map and nothing else:

    known         claims that meet their evidence policy
    uncertain     claims with support that falls short of their policy
    contradicted  contested claims, with the contradiction that makes them so
    unknowns      open gaps with acquisition plans
    calculations  reproducible numbers V2 simulations may inherit

V2 may generate hypotheses from anything here, but a hypothesis re-enters the
evidence graph only as origin=HYPOTHESIS, which the verifier never promotes.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ..evidence.types import ClaimStatus, to_dict

if TYPE_CHECKING:
    from .pipeline import RunResult

STATE_SCHEMA = "lofgren.knowledge-map/1"


def export_state(r: "RunResult") -> dict:
    g = r.graph

    def claim_view(c) -> dict:
        return {"id": c.id, "statement": c.statement, "type": c.claim_type.value, "status": c.status.value,
                "confidence": c.confidence, "confidence_status": "calibrated" if r.calibrated else "provisional",
                "value": c.value, "unit": c.unit, "scope": to_dict(c.scope), "policy": c.sufficiency,
                "evidence": list(c.supporting), "issues": list(c.issues), "calculation_id": c.calculation_id}

    known = [claim_view(c) for c in g.claims.values() if c.status == ClaimStatus.VERIFIED]
    uncertain = [claim_view(c) for c in g.claims.values()
                 if c.status in (ClaimStatus.PARTIALLY_VERIFIED, ClaimStatus.SUPPORTED, ClaimStatus.INSUFFICIENT)]
    contradicted = [{**claim_view(c), "contradictions": [x.id for x in g.contradictions_for(c.id)]}
                    for c in g.claims.values() if c.status == ClaimStatus.CONTESTED]
    return {
        "schema": STATE_SCHEMA,
        "research_id": r.receipt.get("research_id"),
        "objective": r.contract.objective,
        "mode": r.contract.mode,
        "questions": [{"id": q.id, "text": q.text, "role": q.role, "stage": q.stage, "depends_on": q.depends_on}
                      for q in r.contract.questions],
        "known": known,
        "uncertain": uncertain,
        "contradicted": contradicted,
        "contradictions": [to_dict(c) for c in g.contradictions.values()],
        "unknowns": [to_dict(u) for u in r.unknowns if u.status == "open"],
        "calculations": [to_dict(c) for c in g.calculations.values()],
        "findings": [to_dict(f) for f in r.findings],
        "rule": "V2 may hypothesize from this map; it may never promote a hypothesis into a verified finding.",
    }
