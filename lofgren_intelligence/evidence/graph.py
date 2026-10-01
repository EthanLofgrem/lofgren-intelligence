"""The evidence graph: sources -> evidence -> claims, with typed edges.

Relations (EdgeRelation)
    provided_by   evidence    -> source
    supports      evidence    -> claim
    contradicts   evidence    -> claim
    conflicts     claim       -> claim     (a recorded Contradiction)
    derived_from  calculation -> evidence
    derived_from:declared | :syndicated | :quotes   source -> source   (lineage)

Same identity does not mean same object state. Adding a claim whose proposition is already present merges
the evidence associations of a compatible observation, refuses an incompatible one (ClaimConflict), and never
hands a hypothesis back as an existing non-hypothesis claim (HypothesisCollision). `validate()` checks the
whole graph; the pipeline runs it before any receipt is issued.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Iterable

from .types import (
    CLAIM_IDENTITY_VERSIONS,
    EVIDENCE_IDENTITY_VERSIONS,
    Calculation,
    Claim,
    ClaimIdentityError,
    ClaimOrigin,
    ClaimStatus,
    Contradiction,
    Evidence,
    EvidenceIdentityError,
    Location,
    Source,
    claim_id,
    evidence_id,
    to_dict,
)


class EdgeRelation(str, Enum):
    PROVIDED_BY = "provided_by"
    SUPPORTS = "supports"
    CONTRADICTS = "contradicts"
    CONFLICTS = "conflicts"
    DERIVED_FROM = "derived_from"
    # Source lineage (evidence/lineage.py): source -> the source it copies.
    SOURCE_DECLARED_DERIVATION = "derived_from:declared"
    SOURCE_SYNDICATES = "derived_from:syndicated"
    SOURCE_QUOTES = "derived_from:quotes"


LINK_RELATIONS = (EdgeRelation.SUPPORTS, EdgeRelation.CONTRADICTS)  # what link() implements


class GraphIntegrityError(ValueError):
    """The evidence graph would hold, or holds, inconsistent state."""


class UnknownRelation(GraphIntegrityError):
    """A relation outside the graph's vocabulary, or one an operation does not implement."""


class ClaimConflict(GraphIntegrityError):
    """A claim with an existing proposition id carries state that cannot be merged with the stored claim."""

    def __init__(self, message: str, claim_id: str, field_name: str) -> None:
        super().__init__(message)
        self.claim_id = claim_id
        self.field = field_name


class HypothesisCollision(ClaimConflict):
    """A hypothesis states the same proposition as a stored non-hypothesis claim (or the reverse).

    The graph refuses rather than return the stored claim, which may be verified. `existing_origin` and
    `existing_status` tell the caller what the proposition already is in V1, without handing over the object.
    """

    def __init__(self, claim_id: str, existing_origin: ClaimOrigin, existing_status: ClaimStatus) -> None:
        super().__init__(f"proposition {claim_id} is already held as a {existing_origin.value} claim with status "
                         f"{existing_status.value}; a hypothesis cannot share or become that claim", claim_id, "origin")
        self.existing_origin = existing_origin
        self.existing_status = existing_status


class GraphValidationError(GraphIntegrityError):
    """`validate()` found problems. `problems` lists each one with the offending object or reference."""

    def __init__(self, problems: list[str]) -> None:
        super().__init__(f"{len(problems)} graph integrity problem(s): " + "; ".join(problems[:10])
                         + (" ..." if len(problems) > 10 else ""))
        self.problems = problems


def _relation(relation: EdgeRelation | str) -> EdgeRelation:
    try:
        return EdgeRelation(relation)
    except ValueError:
        raise UnknownRelation(f"unknown relation {relation!r}; known: {[r.value for r in EdgeRelation]}") from None


# Claim fields that must agree before two observations of one proposition can merge. question_id is not here:
# one proposition can serve several questions, and question association is handled separately (still a
# pinned defect: the stored claim keeps its first question).
_PROTECTED_CLAIM_FIELDS = ("identity_version", "origin", "status", "confidence", "claim_type", "sufficiency",
                           "confidence_method")


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
        """Add a claim, or merge it into the stored claim for the same proposition.

        - A compatible observation of a stored proposition merges its evidence associations into the stored
          claim (this is how several sources corroborate one proposition). Evidence lists keep arrival order,
          because the verifier and skeptic read the first-arrived evidence; the set of evidence is the same
          whatever the order.
        - A hypothesis never shares a stored non-hypothesis claim, and a non-hypothesis claim never joins a stored
          hypothesis: HypothesisCollision.
        - Any other disagreement in protected state (origin, status, confidence, classification, identity
          version, calculation) raises ClaimConflict instead of keeping whichever arrived first.
        """
        # Only V1 Claim objects enter the graph. A V2 hypothesis enters solely through
        # Hypothesis.as_claim() (origin=hypothesis, never verified); candidates,
        # simulations and other discovery objects cannot be added at all.
        if not isinstance(claim, Claim):
            raise TypeError(f"only Claim objects can be added to the evidence graph, not {type(claim).__name__}")
        supported_by = list(supported_by)
        for ev_id in supported_by + claim.supporting + claim.contradicting:
            if ev_id not in self.evidence:
                raise KeyError(f"unknown evidence {ev_id}")
        existing = self.claims.get(claim.id)
        if existing is None:
            self.claims[claim.id] = claim
            existing = claim
        elif existing is not claim:
            self._check_mergeable(existing, claim)
            for ev_id in claim.supporting:
                self.link(ev_id, existing.id, EdgeRelation.SUPPORTS)
            for ev_id in claim.contradicting:
                self.link(ev_id, existing.id, EdgeRelation.CONTRADICTS)
            if existing.calculation_id is None and claim.calculation_id is not None:
                existing.calculation_id = claim.calculation_id
            # One proposition can serve several questions: keep every association (sorted, so arrival order
            # does not matter). question_id stays the first association.
            existing.question_ids = sorted(set(existing.question_ids) | set(claim.question_ids))
        for ev_id in supported_by:
            self.link(ev_id, existing.id, EdgeRelation.SUPPORTS)
        return existing

    @staticmethod
    def _check_mergeable(existing: Claim, incoming: Claim) -> None:
        hyp = ClaimOrigin.HYPOTHESIS
        if (existing.origin == hyp) != (incoming.origin == hyp):
            raise HypothesisCollision(existing.id, existing.origin, existing.status)
        for name in _PROTECTED_CLAIM_FIELDS:
            a, b = getattr(existing, name), getattr(incoming, name)
            if a != b:
                raise ClaimConflict(f"claim {existing.id}: {name} {b!r} conflicts with the stored {a!r}", existing.id,
                                    name)
        if None not in (existing.calculation_id, incoming.calculation_id) and \
                existing.calculation_id != incoming.calculation_id:
            raise ClaimConflict(f"claim {existing.id}: calculation {incoming.calculation_id} conflicts with the "
                                f"stored {existing.calculation_id}", existing.id, "calculation_id")

    def link(self, evidence_id: str, claim_id: str, relation: EdgeRelation | str) -> None:
        """Record that evidence supports or contradicts a claim. Any other relation raises UnknownRelation."""
        rel = _relation(relation)
        if rel not in LINK_RELATIONS:
            raise UnknownRelation(f"link() records only {[r.value for r in LINK_RELATIONS]}, not {rel.value!r}")
        if evidence_id not in self.evidence:
            raise KeyError(f"unknown evidence {evidence_id}")
        claim = self.claims[claim_id]
        bucket, other = ((claim.supporting, claim.contradicting) if rel is EdgeRelation.SUPPORTS
                         else (claim.contradicting, claim.supporting))
        if evidence_id in other:
            held = "contradicts" if rel is EdgeRelation.SUPPORTS else "supports"
            raise GraphIntegrityError(f"evidence {evidence_id} already {held} claim {claim_id}; "
                                      f"it cannot also be linked as {rel.value}")
        if evidence_id not in bucket:
            bucket.append(evidence_id)
            self._edge(evidence_id, claim_id, rel.value)

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

    # -- integrity --------------------------------------------------------
    def problems(self) -> list[str]:
        """Every integrity problem in the graph, deterministic and without side effects."""
        out: list[str] = []
        for kind, table in (("source", self.sources), ("evidence", self.evidence), ("claim", self.claims),
                            ("contradiction", self.contradictions), ("calculation", self.calculations)):
            for key, obj in table.items():
                if getattr(obj, "id", None) != key:
                    out.append(f"{kind} stored under key {key!r} has id {getattr(obj, 'id', None)!r}")
        for key, ev in self.evidence.items():
            if ev.source_id not in self.sources:
                out.append(f"evidence {key} cites missing source {ev.source_id}")
            if ev.identity_version not in EVIDENCE_IDENTITY_VERSIONS:
                out.append(f"evidence {key} has unsupported identity version {ev.identity_version!r}")
                continue
            try:
                expected = evidence_id(ev)
            except EvidenceIdentityError as exc:
                out.append(f"evidence {key} has malformed identity fields: {exc}")
                continue
            if ev.identity_version >= 2:
                if key != expected:
                    out.append(f"evidence {key} does not match its content (v2 id {expected})")
                if ev.content_hash != hashlib.sha256(ev.content.encode()).hexdigest():
                    out.append(f"evidence {key} content_hash does not match its content")
        for key, c in self.claims.items():
            if c.identity_version not in CLAIM_IDENTITY_VERSIONS:
                out.append(f"claim {key} has unsupported identity version {c.identity_version!r}")
            else:
                try:
                    if claim_id(c) != key:
                        out.append(f"claim {key} does not match its proposition (expected {claim_id(c)})")
                except ClaimIdentityError as exc:
                    out.append(f"claim {key} has malformed identity fields: {exc}")
            for name in ("supporting", "contradicting"):
                refs = getattr(c, name)
                if len(set(refs)) != len(refs):
                    out.append(f"claim {key} lists {name} evidence more than once")
                for ev_id in refs:
                    if ev_id not in self.evidence:
                        out.append(f"claim {key} cites missing {name} evidence {ev_id}")
            both = set(c.supporting) & set(c.contradicting)
            if both:
                out.append(f"claim {key} has evidence that both supports and contradicts it: {sorted(both)}")
            if c.origin == ClaimOrigin.HYPOTHESIS and c.status != ClaimStatus.UNVERIFIED:
                out.append(f"claim {key} is a hypothesis with status {c.status.value}; hypotheses are never verified")
            if c.calculation_id is not None and c.calculation_id not in self.calculations:
                out.append(f"claim {key} cites missing calculation {c.calculation_id}")
            if c.question_ids != sorted(set(c.question_ids)):
                out.append(f"claim {key} question associations are not a sorted, distinct list")
            if c.question_id is not None and c.question_id not in c.question_ids:
                out.append(f"claim {key} question_id {c.question_id} is missing from its question associations")
        for key, cx in self.contradictions.items():
            for end in (cx.claim_a, cx.claim_b):
                if end not in self.claims:
                    out.append(f"contradiction {key} cites missing claim {end}")
            if cx.claim_a == cx.claim_b:
                out.append(f"contradiction {key} sets a claim against itself")
        for key, calc in self.calculations.items():
            for inp in calc.inputs:
                ev_id = inp.get("evidence_id")
                if ev_id and ev_id not in self.evidence:
                    out.append(f"calculation {key} cites missing evidence {ev_id}")
        ends = {EdgeRelation.PROVIDED_BY: (self.evidence, self.sources),
                EdgeRelation.SUPPORTS: (self.evidence, self.claims),
                EdgeRelation.CONTRADICTS: (self.evidence, self.claims),
                EdgeRelation.CONFLICTS: (self.claims, self.claims),
                EdgeRelation.DERIVED_FROM: (self.calculations, self.evidence),
                EdgeRelation.SOURCE_DECLARED_DERIVATION: (self.sources, self.sources),
                EdgeRelation.SOURCE_SYNDICATES: (self.sources, self.sources),
                EdgeRelation.SOURCE_QUOTES: (self.sources, self.sources)}
        for i, e in enumerate(self.edges):
            try:
                rel = EdgeRelation(e.relation)
            except ValueError:
                out.append(f"edge {i} has unknown relation {e.relation!r}")
                continue
            src_table, dst_table = ends[rel]
            if e.src not in src_table or e.dst not in dst_table:
                out.append(f"edge {i} ({rel.value}) {e.src} -> {e.dst} has a missing endpoint")
        return out

    def validate(self) -> None:
        """Raise GraphValidationError if the graph holds any integrity problem. Detects; never repairs."""
        found = self.problems()
        if found:
            raise GraphValidationError(found)

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
            # Evidence saved before identity versions existed was identified under version 1; its stored id is kept.
            e = {"identity_version": 1, **e, "location": Location(**loc) if loc else None}
            g.evidence[e["id"]] = Evidence(**e)
        for c in data.get("claims", []):
            # A claim saved before identity versions existed was identified under version 1. Its stored id is
            # kept and keeps that meaning; it is never recomputed under a newer rule.
            c = {"identity_version": 1, **c, "origin": ClaimOrigin(c["origin"]), "status": ClaimStatus(c["status"])}
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
