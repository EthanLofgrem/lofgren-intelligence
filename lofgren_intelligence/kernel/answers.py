"""Answer each contract question from the verified evidence graph.

A question's role decides which claims answer it: the leading verified
state, the claims that support it, the contested ones, the gaps, prior work,
or (for Lofgren Enterprise ventures) the findings relevant to each stage.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from ..evidence.types import Claim, ClaimStatus
from ..intent.compiler import PRIOR_WORDS, STAGE_KEYWORDS, Question

if TYPE_CHECKING:
    from .pipeline import RunResult

CONFIRMED = (ClaimStatus.VERIFIED, ClaimStatus.PARTIALLY_VERIFIED)
MAX_ITEMS = 5


@dataclass
class Answer:
    question: Question
    claims: list[Claim] = field(default_factory=list)
    gaps: list[str] = field(default_factory=list)

    @property
    def answered(self) -> bool:
        if self.question.role == "gap":
            return True
        if self.question.role == "contradict":
            return True  # "none found" is itself an answer once research ran
        return any(c.status in CONFIRMED for c in self.claims)


def _ranked(claims: list[Claim]) -> list[Claim]:
    order = {ClaimStatus.VERIFIED: 0, ClaimStatus.PARTIALLY_VERIFIED: 1, ClaimStatus.CONTESTED: 2,
             ClaimStatus.SUPPORTED: 3, ClaimStatus.INSUFFICIENT: 4, ClaimStatus.UNVERIFIED: 5}
    return sorted(claims, key=lambda c: (order[c.status], -c.confidence, c.statement))


def declared_dependencies(q: Question, r: "RunResult") -> list[str]:
    """The questions `q` explicitly depends on in the research contract. Direct only: if q depends on B and B on C,
    q may use B's claims but not C's. A cycle cannot loop, because nothing is followed transitively."""
    in_contract = {x.id for x in r.contract.questions}
    return sorted({d for d in q.depends_on if d in in_contract and d != q.id})


def claim_scope(q: Question, r: "RunResult") -> dict[str, list[str]]:
    """claim id -> the question(s) through which `q` may use that claim.

    A claim gathered for `q` is used directly: [q.id]. A claim gathered only for questions `q` declares it
    depends on is used through those: [dependency ids]. Any other claim is absent: no declared path, no access.
    Claim.question_ids is read, never changed; using a dependency's claim does not re-associate it.
    """
    deps = set(declared_dependencies(q, r))
    scope: dict[str, list[str]] = {}
    for c in r.graph.claims.values():
        if q.id in c.question_ids:
            scope[c.id] = [q.id]
        else:
            via = sorted(deps & set(c.question_ids))
            if via:
                scope[c.id] = via
    return scope


def permitted_claims(q: Question, r: "RunResult") -> list[Claim]:
    """The claims an answer to `q` may use: its own, plus those of its declared direct dependencies."""
    scope = claim_scope(q, r)
    return [c for c in r.graph.claims.values() if c.id in scope]


def select_claims(role: str, stage: str, claims: list[Claim]) -> list[Claim]:
    """Rank and select, by question role, from an already-permitted pool of claims."""
    if role in ("state", "claim"):
        return _ranked(claims)[:MAX_ITEMS]
    if role == "support":
        return [c for c in _ranked(claims) if c.status in CONFIRMED][:MAX_ITEMS]
    if role == "contradict":
        return [c for c in _ranked(claims) if c.status == ClaimStatus.CONTESTED][:MAX_ITEMS]
    if role == "prior":
        return [c for c in _ranked(claims) if set(c.topic) & PRIOR_WORDS][:MAX_ITEMS]
    if role == "stage":
        words = STAGE_KEYWORDS.get(stage, frozenset())
        return [c for c in _ranked(claims) if set(c.topic) & words][:MAX_ITEMS]
    return []


def answer(q: Question, r: "RunResult") -> Answer:
    a = Answer(q)
    if q.role == "gap":
        a.gaps = [g for g in r.gaps if not g.endswith("no verified finding yet")]
    else:
        a.claims = select_claims(q.role, q.stage, permitted_claims(q, r))  # filter first, then rank
    return a


def answer_all(r: "RunResult") -> list[Answer]:
    return [answer(q, r) for q in r.contract.questions]
