"""Verification: try to break every claim before anyone relies on it.

1. Cross-check every claim against claims from *independent* sources.
2. Record contradictions explicitly; never average them away.
3. Score confidence from source quality (Q), independent diversity (D),
   recency (R), directness (T) and contradiction penalty (K).
4. Pass the raw score through a calibrator so "80%" can be held to account
   against real outcomes (see calibration.py).
5. Assign a status against the contract's evidence standard.

The weights below are a documented starting point, not truth. They exist to
be recalibrated against measured outcomes (Outcome Intelligence, V5).
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import datetime, timezone

from ..evidence.graph import EvidenceGraph
from ..evidence.types import Calculation, Claim, ClaimOrigin, ClaimStatus, Contradiction, topic_tokens
from ..models.provider import DOWN_WORDS, UP_WORDS, trend
from .calibration import Calibrator
from .policies import classify_claim, policy_for

DIRECTNESS = {
    ClaimOrigin.OBSERVED: 1.0,
    ClaimOrigin.EXTRACTED: 0.8,
    ClaimOrigin.USER: 0.5,
    ClaimOrigin.INFERRED: 0.4,
    ClaimOrigin.HYPOTHESIS: 0.2,
}

# Logistic weights: z = B0 + wQ*Q + wD*(D-1) + wT*T + wR*R - wK*K
WEIGHTS = {"b0": -1.6, "q": 2.0, "d": 0.9, "t": 0.8, "r": 0.6, "k": 1.4}

OVERLAP_THRESHOLD = 0.8  # share of the smaller subject found in the larger
JACCARD_THRESHOLD = 0.5
VALUE_TOLERANCE = 0.15  # relative difference beyond which two values disagree

_CHANGE_WORDS = UP_WORDS | DOWN_WORDS


def jaccard(a: list[str] | set[str], b: list[str] | set[str]) -> float:
    sa, sb = set(a), set(b)
    return len(sa & sb) / len(sa | sb) if sa and sb else 0.0


def subject_tokens(statement: str) -> set[str]:
    """What a claim is about: the words before its first change verb.

    "Industrial vacancy in Phoenix rose" is about {industrial, vacancy, phoenix};
    it must not be matched against "Industrial construction in Phoenix rose".
    """
    words = re.findall(r"[a-zA-Z][a-zA-Z\-]+", statement.lower())
    for i, w in enumerate(words):
        if w in _CHANGE_WORDS and i >= 2:
            return set(topic_tokens(" ".join(words[:i])))
    return set(topic_tokens(statement)) - _CHANGE_WORDS


def similarity(a: Claim, b: Claim) -> float:
    sa, sb = subject_tokens(a.statement), subject_tokens(b.statement)
    if len(sa) < 2 or len(sb) < 2:
        return 0.0
    overlap = len(sa & sb) / min(len(sa), len(sb))
    return overlap if overlap >= OVERLAP_THRESHOLD and jaccard(sa, sb) >= JACCARD_THRESHOLD else 0.0


def relation(a: Claim, b: Claim) -> tuple[str | None, str]:
    """Do two claims talk about the same thing, and do they agree?"""
    if a.subject or b.subject:
        sim = 1.0 if a.subject == b.subject else 0.0
    else:
        sim = similarity(a, b)
    if not sim:
        return None, ""
    if a.polarity != b.polarity:
        return "contradicts", "one statement negates the other"
    ta, tb = trend(set(a.topic)), trend(set(b.topic))
    if ta and tb and ta != tb:
        return "contradicts", "opposite direction of change"
    if a.value is not None and b.value is not None and a.unit == b.unit:
        scale = max(abs(a.value), abs(b.value), 1e-9)
        if abs(a.value - b.value) / scale > VALUE_TOLERANCE:
            return "contradicts", f"values differ: {a.value:g} vs {b.value:g} {a.unit}".strip()
    return "supports", f"same subject (similarity {sim:.2f}) and consistent"


def _age_days(stamp: str | None, now: datetime) -> float | None:
    if not stamp:
        return None
    try:
        t = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except ValueError:
        return None
    if t.tzinfo is None:
        t = t.replace(tzinfo=timezone.utc)
    return max((now - t).total_seconds() / 86400, 0.0)


@dataclass
class ConfidenceFactors:
    quality: float
    independent_sources: int
    recency: float
    directness: float
    contradicting_sources: int
    raw: float
    calibrated: float

    def as_dict(self) -> dict:
        return {k: round(v, 3) if isinstance(v, float) else v for k, v in self.__dict__.items()}


class Verifier:
    def __init__(self, calibrator: Calibrator | None = None, now: datetime | None = None) -> None:
        self.calibrator = calibrator or Calibrator()
        self.now = now or datetime.now(timezone.utc)
        self.factors: dict[str, ConfidenceFactors] = {}

    def _groups(self, graph: EvidenceGraph, evidence_ids: list[str]) -> set[str]:
        return {graph.sources[graph.evidence[e].source_id].independence_group for e in evidence_ids}

    def cross_check(self, graph: EvidenceGraph) -> None:
        claims = list(graph.claims.values())
        for i, a in enumerate(claims):
            groups_a = self._groups(graph, a.supporting)
            for b in claims[i + 1:]:
                groups_b = self._groups(graph, b.supporting)
                if groups_a and groups_a == groups_b:
                    continue  # same source family: not an independent check
                rel, reason = relation(a, b)
                if rel is None:
                    continue
                mismatch = scope_mismatch(a, b)
                if rel == "supports":
                    if mismatch:
                        continue  # same subject, different period or place: not a confirmation
                    for e in b.supporting:
                        graph.link(e, a.id, "supports")
                    for e in a.supporting:
                        graph.link(e, b.id, "supports")
                    continue
                cx = Contradiction(a.id, b.id, reason, resolution=resolution_for(a, b, reason))
                if a.value is not None and b.value is not None and a.unit == b.unit:
                    calc = graph.add_calculation(Calculation(
                        name="relative difference", formula="|a - b| / max(|a|, |b|)",
                        inputs=[{"name": "a", "value": a.value, "unit": a.unit,
                                 "evidence_id": a.supporting[0] if a.supporting else None},
                                {"name": "b", "value": b.value, "unit": b.unit,
                                 "evidence_id": b.supporting[0] if b.supporting else None}],
                        result=round(abs(a.value - b.value) / max(abs(a.value), abs(b.value), 1e-9), 4)))
                    cx.reason += f" (relative difference {calc.result:.1%}, {calc.id})"
                if mismatch:
                    # Disagreement across different scopes is a lead, not a conflict.
                    cx.kind, cx.scope_note, cx.severity = "scope_mismatch", mismatch, 0.3
                    graph.add_contradiction(cx)
                    continue
                graph.add_contradiction(cx)
                for e in b.supporting:
                    graph.link(e, a.id, "contradicts")
                for e in a.supporting:
                    graph.link(e, b.id, "contradicts")

    def score(self, graph: EvidenceGraph, claim: Claim) -> ConfidenceFactors:
        sources = graph.sources_for_claim(claim.id, "supports")
        q = sum(s.quality for s in sources) / len(sources) if sources else 0.0
        d = len(self._groups(graph, claim.supporting))
        k = len(self._groups(graph, claim.contradicting) - self._groups(graph, claim.supporting))
        ages = []
        for e in claim.supporting:
            ev = graph.evidence[e]
            src = graph.sources[ev.source_id]
            ages.append(_age_days(ev.observed_at or src.published_at, self.now))
        rec = [math.exp(-a / 730) for a in ages if a is not None]
        r = sum(rec) / len(rec) if rec else 0.7
        t = DIRECTNESS[claim.origin]
        w = WEIGHTS
        z = w["b0"] + w["q"] * q + w["d"] * (min(d, 4) - 1) + w["t"] * t + w["r"] * r - w["k"] * k
        raw = 1 / (1 + math.exp(-z)) if d else 0.0
        return ConfidenceFactors(q, d, r, t, k, raw, self.calibrator.calibrate(raw))

    def status(self, claim: Claim, f: ConfidenceFactors, min_sources: int, min_conf: float) -> ClaimStatus:
        pol = policy_for(claim.claim_type, min_sources, min_conf)
        claim.sufficiency = pol.name
        if claim.origin == ClaimOrigin.HYPOTHESIS:
            claim.sufficiency = "hypothesis-never-verified"
            return ClaimStatus.UNVERIFIED  # V2 hypotheses are never promoted to findings
        if f.independent_sources == 0:
            return ClaimStatus.INSUFFICIENT
        if f.contradicting_sources > 0:
            return ClaimStatus.CONTESTED
        if pol.requires_observation and claim.origin != ClaimOrigin.OBSERVED:
            meets = False
        else:
            meets = f.independent_sources >= pol.min_independent_sources
        if f.calibrated >= pol.min_confidence and meets:
            return ClaimStatus.VERIFIED
        if f.calibrated >= 0.5:
            return ClaimStatus.PARTIALLY_VERIFIED
        return ClaimStatus.SUPPORTED

    def verify(self, graph: EvidenceGraph, min_sources: int = 2, min_conf: float = 0.7) -> None:
        for claim in graph.claims.values():
            claim.claim_type = classify_claim(claim)
        self.cross_check(graph)
        for claim in graph.claims.values():
            f = self.score(graph, claim)
            self.factors[claim.id] = f
            claim.confidence = round(f.calibrated, 3)
            claim.confidence_method = "v1-heuristic+calibrated" if self.calibrator.calibrated else "v1-heuristic"
            claim.status = self.status(claim, f, min_sources, min_conf)


def scope_mismatch(a: Claim, b: Claim) -> str:
    """Why two claims about the same subject cover different ground ('' if they don't)."""
    overlap = a.scope.overlaps_time(b.scope)
    if overlap is False:
        return (f"different periods: {a.scope.valid_from}..{a.scope.valid_to} vs "
                f"{b.scope.valid_from}..{b.scope.valid_to}")
    if a.scope.same_place(b.scope) is False:
        return f"different places: {a.scope.geography} vs {b.scope.geography}"
    return ""


def resolution_for(a: Claim, b: Claim, reason: str) -> str:
    period = a.scope.valid_from[:4] if a.scope.valid_from else "the period in question"
    place = a.scope.geography or b.scope.geography or "the place in question"
    if reason.startswith("values differ"):
        return f"the primary figure for {place}, {period}: official statistics or the original filing"
    if reason.startswith("opposite direction"):
        return f"an independent time series for {place} covering {period}, or direct observation (imagery, permits)"
    return f"primary documentation or direct observation of the disputed fact for {place}, {period}"
