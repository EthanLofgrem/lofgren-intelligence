"""Findings: one typed answer per research question, built only from verified state.

The report is a view of these objects; it never adds facts of its own.
Unknowns are gaps as executable work: each says what is missing, where it
might come from, what it would resolve and whether it needs approval.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ..evidence.scope import merge_scopes
from ..evidence.types import ClaimStatus, Finding, Unknown
from ..research.planner import CAPABILITY_SOURCES
from .answers import CONFIRMED, answer

if TYPE_CHECKING:
    from .pipeline import RunResult


TEXT_LOOKUP_UNITS = 1.0


def post_verification_unknowns(r: "RunResult") -> list[Unknown]:
    """Unknowns revealed by verification: unconfirmed questions, contradictions, thin support."""
    rate = r.ledger.rate_usd_per_unit if r.ledger else 0.0
    cost = round(TEXT_LOOKUP_UNITS * rate, 4)
    out: list[Unknown] = []
    for q in r.contract.questions:
        if q.role in ("gap", "contradict"):
            continue
        a = answer(q, r)
        if not any(c.status in CONFIRMED for c in a.claims):
            out.append(Unknown(
                description=f"No finding meets the evidence standard for: {q.text}",
                question_id=q.id, capability=q.needs[0] if q.needs else "text",
                source_types=CAPABILITY_SOURCES.get(q.needs[0] if q.needs else "text", []),
                expected_gain=round(min(1.0, q.weight / 2), 3), est_cost_usd=cost))
    for cx in r.graph.contradictions.values():
        if cx.kind != "incompatible":
            continue
        out.append(Unknown(
            description=f"Resolve contradiction {cx.id}: {cx.reason}",
            question_id=r.graph.claims[cx.claim_a].question_id, capability="text",
            source_types=[cx.resolution] if cx.resolution else CAPABILITY_SOURCES["text"],
            expected_gain=round(cx.severity * 0.8, 3), est_cost_usd=cost))
    for c in r.graph.claims.values():
        if c.status == ClaimStatus.PARTIALLY_VERIFIED and c.sufficiency and not c.sufficiency.startswith(("attribution", "direct")):
            out.append(Unknown(
                description=f"A second independent source for: {c.statement[:140]}",
                question_id=c.question_id, capability="text", source_types=CAPABILITY_SOURCES["text"],
                expected_gain=0.3, est_cost_usd=cost))
    return out


def _answer_text(role: str, statuses: dict[str, int], top: str | None, n_contra: int, n_unknown: int) -> str:
    if role == "gap":
        return (f"{n_unknown} open unknown(s) remain; each has an acquisition plan below."
                if n_unknown else "No open unknowns for the sources connected.")
    if role == "contradict":
        return (f"{n_contra} contradiction(s) between independent sources; see below."
                if n_contra else "No contradiction found among the collected evidence.")
    if top:
        confirmed = statuses.get("verified", 0) + statuses.get("partially_verified", 0)
        return f"Evidence supports: {top.rstrip('.')} ({confirmed} confirmed claim(s))."
    return "No finding meets the evidence standard yet."


def build_findings(r: "RunResult") -> list[Finding]:
    findings: list[Finding] = []
    for q in r.contract.questions:
        a = answer(q, r)
        claims = a.claims
        statuses: dict[str, int] = {}
        for c in claims:
            statuses[c.status.value] = statuses.get(c.status.value, 0) + 1
        confirmed = [c for c in claims if c.status in CONFIRMED]
        top = confirmed[0] if confirmed else None
        claim_ids = {c.id for c in claims}
        contradictions = [cx for cx in r.graph.contradictions.values()
                          if cx.claim_a in claim_ids or cx.claim_b in claim_ids]
        if q.role == "contradict":
            contradictions = list(r.graph.contradictions.values())
        unknowns = [u for u in r.unknowns if u.question_id == q.id or (q.role == "gap" and u.status == "open")]
        evidence_ids = sorted({e for c in claims for e in c.supporting})
        affects = [f"venture.{q.stage.lower()}"] if q.stage else [q.role]
        best = max(unknowns, key=lambda u: u.expected_gain, default=None)
        findings.append(Finding(
            question_id=q.id,
            question=q.text,
            answer=_answer_text(q.role, statuses, top.statement if top else None, len(contradictions), len(unknowns)),
            claim_ids=[c.id for c in claims],
            evidence_ids=evidence_ids,
            contradiction_ids=[cx.id for cx in contradictions],
            unknown_ids=[u.id for u in unknowns],
            scope=merge_scopes([c.scope for c in confirmed or claims]),
            confidence=round(top.confidence, 3) if top else 0.0,
            confidence_status="calibrated" if r.calibrated else "provisional",
            confidence_method=top.confidence_method if top else "v1-heuristic",
            affects=affects,
            next_best_evidence=(f"{best.description} — from: {', '.join(best.source_types) or 'unspecified'}"
                                if best else ""),
            issues=sorted({i for c in claims for i in c.issues}),
        ))
    return findings
