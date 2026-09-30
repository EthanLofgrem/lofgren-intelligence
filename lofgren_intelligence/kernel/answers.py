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


def answer(q: Question, r: "RunResult") -> Answer:
    claims = list(r.graph.claims.values())
    a = Answer(q)
    if q.role in ("state", "claim"):
        a.claims = _ranked(claims)[:MAX_ITEMS]
    elif q.role == "support":
        a.claims = [c for c in _ranked(claims) if c.status in CONFIRMED][:MAX_ITEMS]
    elif q.role == "contradict":
        a.claims = [c for c in _ranked(claims) if c.status == ClaimStatus.CONTESTED][:MAX_ITEMS]
    elif q.role == "gap":
        a.gaps = [g for g in r.gaps if not g.endswith("no verified finding yet")]
    elif q.role == "prior":
        a.claims = [c for c in _ranked(claims) if set(c.topic) & PRIOR_WORDS][:MAX_ITEMS]
    elif q.role == "stage":
        words = STAGE_KEYWORDS.get(q.stage, frozenset())
        a.claims = [c for c in _ranked(claims) if set(c.topic) & words][:MAX_ITEMS]
    return a


def answer_all(r: "RunResult") -> list[Answer]:
    return [answer(q, r) for q in r.contract.questions]
