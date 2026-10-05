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
from .answers import CONFIRMED, answer, claim_scope, declared_dependencies, select_claims

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


class FindingIntegrityError(ValueError):
    """A finding uses a claim, contradiction or question outside its authorized question scope."""


def _derivation(question_id: str, claims: list, scopes: dict[str, list[str]]) -> tuple[str, dict[str, list[str]]]:
    """How a non-synthesis finding came by its claims, and the scope recorded for each claim."""
    used = {c.id: scopes[c.id] for c in claims}
    own = [cid for cid, via in used.items() if via == [question_id]]
    through = [cid for cid, via in used.items() if via != [question_id]]
    if not through:
        return "direct", used
    return ("dependency" if not own else "mixed"), used


def _finding(r: "RunResult", question_id: str, question_text: str, role: str, stage: str, claims: list,
             scopes: dict[str, list[str]], derivation: str | None, unknowns: list,
             finding_id: str = "", named: list[str] | None = None) -> Finding:
    statuses: dict[str, int] = {}
    for c in claims:
        statuses[c.status.value] = statuses.get(c.status.value, 0) + 1
    confirmed = [c for c in claims if c.status in CONFIRMED]
    top = confirmed[0] if confirmed else None
    claim_ids = {c.id for c in claims}
    contradictions = [cx for cx in r.graph.contradictions.values()
                      if cx.claim_a in claim_ids or cx.claim_b in claim_ids]
    if role == "contradict":
        # Every contradiction touching a claim this finding may use, never one between claims outside its scope.
        contradictions = [cx for cx in r.graph.contradictions.values()
                          if cx.claim_a in scopes or cx.claim_b in scopes]
    if derivation is None:
        derivation, used = _derivation(question_id, claims, scopes)
    else:
        used = {c.id: scopes[c.id] for c in claims}
    question_ids = sorted({question_id} | {q for via in used.values() for q in via} | set(named or ()))
    if derivation == "direct":
        used = {}  # implied: every claim is the question's own
    evidence_ids = sorted({e for c in claims for e in c.supporting})
    affects = [f"venture.{stage.lower()}"] if stage else [role]
    best = max(unknowns, key=lambda u: u.expected_gain, default=None)
    return Finding(
        question_id=question_id,
        question=question_text,
        answer=_answer_text(role, statuses, top.statement if top else None, len(contradictions), len(unknowns)),
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
        question_ids=question_ids,
        derivation=derivation,
        claim_scopes=used,
        id=finding_id,
    )


def build_findings(r: "RunResult") -> list[Finding]:
    """One finding per question, from that question's own claims and those of its declared direct dependencies.

    Unknowns: a question lists the unknowns raised for it; the gap question lists every open unknown of the run,
    because its question is what the whole research is still missing.
    """
    findings: list[Finding] = []
    for q in r.contract.questions:
        unknowns = [u for u in r.unknowns if u.question_id == q.id or (q.role == "gap" and u.status == "open")]
        claims = answer(q, r).claims
        findings.append(_finding(r, q.id, q.text, q.role, q.stage, claims, claim_scope(q, r), None, unknowns))
    return findings


def synthesis_finding(r: "RunResult", question_ids: list[str], role: str = "state") -> Finding:
    """An explicit finding across several named questions. It draws only on claims gathered for those questions
    (not their dependencies) and records which named question each claim came through."""
    by_id = {q.id: q for q in r.contract.questions}
    ids = sorted(set(question_ids))
    unknown = [q for q in ids if q not in by_id]
    if unknown:
        raise FindingIntegrityError(f"synthesis names questions that are not in the contract: {unknown}")
    if len(ids) < 2:
        raise FindingIntegrityError("a synthesis combines at least two questions")
    scopes = {c.id: sorted(set(c.question_ids) & set(ids)) for c in r.graph.claims.values()
              if set(c.question_ids) & set(ids)}
    pool = [c for c in r.graph.claims.values() if c.id in scopes]
    unknowns = [u for u in r.unknowns if u.question_id in ids]
    text = " + ".join(by_id[q].text for q in ids)
    # Every named question is recorded, even one that contributed no claim.
    return _finding(r, ids[0], text, role, "", select_claims(role, "", pool), scopes, "synthesis", unknowns,
                    named=ids)


def authorized_questions(f: Finding, r: "RunResult") -> set[str]:
    """The questions whose claims a finding may use: a synthesis's named questions; otherwise its own question and
    that question's declared direct dependencies."""
    if f.derivation == "synthesis":
        return set(f.question_ids)
    q = next((x for x in r.contract.questions if x.id == f.question_id), None)
    return {f.question_id} | (set(declared_dependencies(q, r)) if q else set())


def finding_problems(findings: list[Finding], r: "RunResult") -> list[str]:
    """Every claim and contradiction a finding uses must lie inside its authorized question scope, and every
    recorded scope must be true of the claim it describes."""
    questions = {q.id for q in r.contract.questions}
    out = []
    for f in findings:
        allowed = authorized_questions(f, r)
        for q in f.question_ids:
            if q not in questions:
                out.append(f"finding {f.id} names question {q}, which is not in the contract")
            elif q not in allowed:
                out.append(f"finding {f.id} names question {q} without a declared dependency on it")
        for cid in f.claim_ids:
            claim = r.graph.claims.get(cid)
            if claim is None:
                out.append(f"finding {f.id} cites missing claim {cid}")
                continue
            if not set(claim.question_ids) & allowed:
                out.append(f"finding {f.id} cites claim {cid}, gathered for {claim.question_ids}, outside its "
                           f"authorized questions {sorted(allowed)}")
            for via in f.claim_scopes.get(cid, [f.question_id]):
                if via not in claim.question_ids:
                    out.append(f"finding {f.id} records claim {cid} as coming through {via}, but the claim was "
                               f"gathered for {claim.question_ids}")
        for xid in f.contradiction_ids:
            cx = r.graph.contradictions.get(xid)
            if cx is None:
                out.append(f"finding {f.id} cites missing contradiction {xid}")
                continue
            ends = [r.graph.claims.get(cx.claim_a), r.graph.claims.get(cx.claim_b)]
            if not any(c is not None and set(c.question_ids) & allowed for c in ends):
                out.append(f"finding {f.id} cites contradiction {xid} between claims outside its authorized "
                           "questions")
    return out


def validate_findings(r: "RunResult") -> None:
    found = finding_problems(r.findings, r)
    if found:
        raise FindingIntegrityError("; ".join(found))
