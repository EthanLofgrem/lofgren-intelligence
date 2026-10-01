"""The evidence graph: sources -> evidence -> claims, with typed edges.

Relations
    provided_by   evidence -> source
    supports      evidence -> claim
    contradicts   evidence -> claim
    conflicts     claim    -> claim   (a recorded Contradiction)
    answers       claim    -> question
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from .types import (
    Calculation,
    Claim,
    ClaimOrigin,
    ClaimStatus,
    Contradiction,
    Evidence,
    Location,
    Source,
    to_dict,
)


@dataclass
class Edge:
    src: str
    dst: str
    relation: str


@dataclass
class EvidenceGraph:
    sources: dict[str, Source] = field(default_factory=dict)
    evidence: dict[str, Evidence] = field(default_factory=dict)
    claims: dict[str, Claim] = field(default_factory=dict)
    contradictions: dict[str, Contradiction] = field(default_factory=dict)
    calculations: dict[str, Calculation] = field(default_factory=dict)
    edges: list[Edge] = field(default_factory=list)

    # -- building ---------------------------------------------------------
    def add_source(self, source: Source) -> Source:
        return self.sources.setdefault(source.id, source)

    def add_evidence(self, item: Evidence) -> Evidence:
        if item.source_id not in self.sources:
            raise KeyError(f"unknown source {item.source_id}; add the source first")
        if item.id not in self.evidence:
            self.evidence[item.id] = item
            self._edge(item.id, item.source_id, "provided_by")
        return self.evidence[item.id]

    def add_claim(self, claim: Claim, supported_by: Iterable[str] = ()) -> Claim:
        existing = self.claims.get(claim.id)
        if existing is None:
            self.claims[claim.id] = claim
            existing = claim
        for ev_id in supported_by:
            self.link(ev_id, existing.id, "supports")
        return existing

    def link(self, evidence_id: str, claim_id: str, relation: str) -> None:
        if evidence_id not in self.evidence:
            raise KeyError(f"unknown evidence {evidence_id}")
        claim = self.claims[claim_id]
        bucket = claim.supporting if relation == "supports" else claim.contradicting
        if evidence_id not in bucket:
            bucket.append(evidence_id)
            self._edge(evidence_id, claim_id, relation)

    def add_contradiction(self, cx: Contradiction) -> Contradiction:
        if cx.id not in self.contradictions:
            self.contradictions[cx.id] = cx
            self._edge(cx.claim_a, cx.claim_b, "conflicts")
        return self.contradictions[cx.id]

    def add_calculation(self, calc: Calculation) -> Calculation:
        if calc.id not in self.calculations:
            self.calculations[calc.id] = calc
            for inp in calc.inputs:
                if inp.get("evidence_id"):
                    self._edge(calc.id, inp["evidence_id"], "derived_from")
        return self.calculations[calc.id]

    def _edge(self, src: str, dst: str, relation: str) -> None:
        self.edges.append(Edge(src, dst, relation))

    # -- queries ----------------------------------------------------------
    def sources_for_claim(self, claim_id: str, relation: str = "supports") -> list[Source]:
        claim = self.claims[claim_id]
        ids = claim.supporting if relation == "supports" else claim.contradicting
        seen: dict[str, Source] = {}
        for ev_id in ids:
            src = self.sources[self.evidence[ev_id].source_id]
            seen[src.id] = src
        return list(seen.values())

    def trace(self, claim_id: str) -> dict:
        """Full provenance for one claim: claim -> evidence -> source."""
        claim = self.claims[claim_id]
        return {
            "claim": to_dict(claim),
            "supporting": [
                {"evidence": to_dict(self.evidence[e]), "source": to_dict(self.sources[self.evidence[e].source_id])}
                for e in claim.supporting
            ],
            "contradicting": [
                {"evidence": to_dict(self.evidence[e]), "source": to_dict(self.sources[self.evidence[e].source_id])}
                for e in claim.contradicting
            ],
        }

    def contradictions_for(self, claim_id: str) -> list[Contradiction]:
        return [c for c in self.contradictions.values() if claim_id in (c.claim_a, c.claim_b)]

    # -- persistence ------------------------------------------------------
    def to_json(self) -> dict:
        return {
            "sources": [to_dict(s) for s in self.sources.values()],
            "evidence": [to_dict(e) for e in self.evidence.values()],
            "claims": [to_dict(c) for c in self.claims.values()],
            "contradictions": [to_dict(c) for c in self.contradictions.values()],
            "calculations": [to_dict(c) for c in self.calculations.values()],
            "edges": [e.__dict__ for e in self.edges],
        }

    @classmethod
    def from_json(cls, data: dict) -> "EvidenceGraph":
        g = cls()
        for s in data.get("sources", []):
            g.sources[s["id"]] = Source(**s)
        for e in data.get("evidence", []):
            loc = e.get("location")
            e = {**e, "location": Location(**loc) if loc else None}
            g.evidence[e["id"]] = Evidence(**e)
        for c in data.get("claims", []):
            c = {**c, "origin": ClaimOrigin(c["origin"]), "status": ClaimStatus(c["status"])}
            g.claims[c["id"]] = Claim(**c)
        for c in data.get("contradictions", []):
            g.contradictions[c["id"]] = Contradiction(**c)
        for c in data.get("calculations", []):
            g.calculations[c["id"]] = Calculation(**c)
        g.edges = [Edge(**e) for e in data.get("edges", [])]
        return g

    def save(self, path: str | Path) -> None:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_text(json.dumps(self.to_json(), indent=2))

    @classmethod
    def load(cls, path: str | Path) -> "EvidenceGraph":
        return cls.from_json(json.loads(Path(path).read_text()))
