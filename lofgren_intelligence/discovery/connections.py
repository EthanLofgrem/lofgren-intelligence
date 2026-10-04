"""Connections across known facts, uncertainties, gaps, prior art, constraints and hypotheses.

Every connection says how strong its basis is, and the strength is decided by rule, not by score:

    observed     both endpoints are V1 known facts and the same evidence states both (cites those EV ids)
    derived      computed deterministically from structured fields: same place and overlapping period,
                 one period strictly before another in the same place, two constraints on the same variable
    speculative  anything else (shared content words with a gap, a prior-art match or a hypothesis mechanism)

A speculative connection can only feed a hypothesis. It never becomes a finding: `DiscoveryFinding` of kind
`verified_fact` may rest only on known facts, and the discovery verifier flags any finding that cites one.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from ..evidence.types import Scope, topic_tokens
from .context import DiscoveryContext
from .frame import FrameResult
from .types import (
    Connection,
    ConnectionStrength,
    Constraint,
    Gap,
    Hypothesis,
    KnownFact,
    PriorArtAssessment,
    utcnow,
)

MIN_SHARED_WORDS = 2  # content words two texts must share before a speculative link is drawn
MAX_SPECULATIVE = 50  # cap: speculative links are leads, not an inventory of every word overlap
_SYMMETRIC = {"stated in the same evidence", "same place and overlapping period"}  # endpoints stored sorted


@dataclass(frozen=True)
class ConnectionResult:
    connections: tuple[Connection, ...]
    notes: tuple[str, ...]

    def by_strength(self, strength: ConnectionStrength) -> tuple[Connection, ...]:
        return tuple(c for c in self.connections if c.strength == strength)


def claim_evidence(context: DiscoveryContext, claim_id: str) -> tuple[str, ...]:
    """The evidence ids a V1 claim rests on, under either knowledge-map schema."""
    view = context.reference(claim_id, ("claim",), "claim_evidence")
    return tuple(view.get("supporting", view.get("evidence", ())))


def _same_place(a: Scope, b: Scope) -> bool:
    return bool(a.geography and b.geography and a.geography.strip().lower() == b.geography.strip().lower())


def _words(*parts: str) -> set[str]:
    return {w for p in parts for w in topic_tokens(p or "")}


def find_connections(context: DiscoveryContext, framed: FrameResult, gaps: Iterable[Gap] = (),
                     assessments: Iterable[PriorArtAssessment] = (), constraints: Iterable[Constraint] = (),
                     hypotheses: Iterable[Hypothesis] = (), at: str | None = None) -> ConnectionResult:
    """Every connection the inputs support, registered in `context`. Deterministic and order-independent."""
    at = at or utcnow()
    found: dict[str, Connection] = {}
    notes: list[str] = []

    def add(a: str, b: str, relation: str, strength: ConnectionStrength, evidence: Iterable[str] = ()) -> None:
        a, b = sorted((a, b)) if relation in _SYMMETRIC else (a, b)
        conn = context.ensure(Connection(a, b, relation, strength, sorted(set(evidence)), created_at=at))
        found[conn.id] = conn

    facts: list[KnownFact] = sorted(framed.known_facts, key=lambda f: f.id)
    for i, fa in enumerate(facts):
        ev_a = set(claim_evidence(context, fa.claim_id))
        for fb in facts[i + 1:]:
            shared = ev_a & set(claim_evidence(context, fb.claim_id))
            if shared:
                add(fa.id, fb.id, "stated in the same evidence", ConnectionStrength.OBSERVED, shared)
            if _same_place(fa.scope, fb.scope):
                overlap = fa.scope.overlaps_time(fb.scope)
                if overlap:
                    add(fa.id, fb.id, "same place and overlapping period", ConnectionStrength.DERIVED,
                        (fa.claim_id, fb.claim_id))
                elif overlap is False:
                    first, second = (fa, fb) if (fa.scope.valid_to or "") < (fb.scope.valid_from or "") else (fb, fa)
                    add(first.id, second.id, "precedes, in the same place", ConnectionStrength.DERIVED,
                        (first.claim_id, second.claim_id))

    cons = sorted(constraints, key=lambda c: c.id)
    for i, ca in enumerate(cons):
        for cb in cons[i + 1:]:
            shared_vars = ca.relation.variables() & cb.relation.variables()
            if shared_vars:
                add(ca.id, cb.id, f"constrain the same variable ({', '.join(sorted(shared_vars))})",
                    ConnectionStrength.DERIVED)

    subjects: list[tuple[str, set[str]]] = [(f.id, _words(f.statement)) for f in facts]
    subjects += [(u.id, _words(u.statement)) for u in sorted(framed.uncertainties, key=lambda u: u.id)]
    leads: list[tuple[str, str, str]] = []
    for g in sorted(gaps, key=lambda g: g.id):
        gw = _words(g.missing, g.why_it_matters)
        leads += [(sid, g.id, "may bear on the gap") for sid, sw in subjects if len(sw & gw) >= MIN_SHARED_WORDS]
    for a in sorted(assessments, key=lambda a: a.id):
        for m in a.matches:
            mw = _words(m["title"], m.get("summary", ""))
            leads += [(sid, a.id, f"resembles prior art: {m['title']}") for sid, sw in subjects
                      if len(sw & mw) >= MIN_SHARED_WORDS]
    hyps = sorted(hypotheses, key=lambda h: h.id)
    for i, ha in enumerate(hyps):
        for hb in hyps[i + 1:]:
            if ha.mechanism and hb.mechanism and len(_words(ha.mechanism) & _words(hb.mechanism)) >= MIN_SHARED_WORDS:
                leads.append((ha.id, hb.id, "share a proposed mechanism"))
    if len(leads) > MAX_SPECULATIVE:
        notes.append(f"{len(leads)} speculative leads found; the first {MAX_SPECULATIVE} in id order are kept.")
    for a, b, relation in sorted(set(leads))[:MAX_SPECULATIVE]:
        add(a, b, relation, ConnectionStrength.SPECULATIVE)
    return ConnectionResult(tuple(found[k] for k in sorted(found)), tuple(notes))
