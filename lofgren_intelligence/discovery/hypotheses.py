"""Hypotheses and counter-hypotheses.

Generation strategies, each recorded in `Hypothesis.origin`:

    contradiction-explaining  for every incompatible V1 contradiction, competing explanations: either side may
                              be the error, or both hold under different periods, places or definitions
    gap-closing               for every uncertain claim, that independent evidence will settle it; for every
                              high-value gap, that the missing evidence exists and can be acquired
    cross-domain-transfer     for prior-art matches, that the matched approach carries over to the subject
    constraint-relaxation     for constraints resting on assumptions, that relaxing them widens what is feasible
    provider:<name>           text a ReasoningProvider proposed; unsupported until tied to V1 claim ids

Every hypothesis carries the frame's scope (time and place) unchanged, structured predicted observations and
falsification criteria (EvidenceRequirements that V1 can act on), and a provisional score of confidence kind
`hypothesis`. Every hypothesis whose score is at least COUNTER_THRESHOLD gets a CounterHypothesis, so the engine
never reports a single explanation where the evidence admits another.

The score is a stated heuristic, never a probability of truth: mean confidence of the supporting V1 claims, reduced
by half the mean confidence of the contradicting ones; a fixed low prior when nothing supports it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable

from ..models.provider import ReasoningProvider
from .context import DiscoveryContext
from .errors import MalformedInput
from .frame import FrameResult
from .requirements import requirement_for_gap
from .types import (
    Constraint,
    CounterHypothesis,
    EvidenceRequirement,
    Gap,
    GapBasis,
    Hypothesis,
    PriorArtAssessment,
    PriorArtConclusion,
    UncertaintyReason,
    check_no_novelty_claim,
    text,
    utcnow,
)

COUNTER_THRESHOLD = 0.5
HIGH_VALUE_GAP = 0.3  # information gain at which a gap earns its own hypothesis
UNSUPPORTED_PRIOR = 0.3  # provisional score of a hypothesis no V1 claim supports
MAX_PER_STRATEGY = 10
MAX_PROVIDER_PROPOSALS = 10


@dataclass
class HypothesisResult:
    hypotheses: list[Hypothesis] = field(default_factory=list)
    counters: list[CounterHypothesis] = field(default_factory=list)
    requirements: list[EvidenceRequirement] = field(default_factory=list)
    provider: dict = field(default_factory=dict)  # name, version, proposals, error: for the receipt only
    notes: list[str] = field(default_factory=list)

    @property
    def all(self) -> list[Hypothesis]:
        return [*self.hypotheses, *self.counters]


class _Builder:
    def __init__(self, context: DiscoveryContext, framed: FrameResult, at: str) -> None:
        self.ctx, self.framed, self.at = context, framed, at
        frame = framed.frame
        self.scope = frame.scope if frame is not None else None
        self.place = self.scope.geography if self.scope is not None else None
        self.period = (self.scope.valid_from, self.scope.valid_to) if self.scope is not None else (None, None)
        self.result = HypothesisResult()
        self.pending: dict[str, dict] = {}

    def requirement(self, description: str, derived_from: Iterable[str] = ()) -> EvidenceRequirement:
        """One requirement per distinct test: the same test asked for twice is the same requirement."""
        req = EvidenceRequirement(description, "text", self.place, self.period, derived_from=sorted(set(derived_from)),
                                  created_at=self.at)
        req = self.ctx.get(req.id) if req.id in self.ctx else self.ctx.register(req)
        if req.id not in {r.id for r in self.result.requirements}:
            self.result.requirements.append(req)
        return req

    def confidence(self, claim_id: str) -> float:
        return float(self.ctx.reference(claim_id, ("claim",), "hypothesis.claim")["confidence"])

    def score(self, supporting: list[str], contradicting: list[str]) -> float:
        if not supporting:
            return UNSUPPORTED_PRIOR
        s = sum(self.confidence(c) for c in supporting) / len(supporting)
        k = sum(self.confidence(c) for c in contradicting) / len(contradicting) if contradicting else 0.0
        return round(min(0.95, s * (1 - 0.5 * k)), 3)

    def add(self, statement: str, origin: str, *, mechanism: str = "", supporting: Iterable[str] = (),
            contradicting: Iterable[str] = (), originating: Iterable[str] = (), predicted: Iterable[str] = (),
            falsified_by: Iterable[str] = (), score: float | None = None, assumptions: Iterable[str] = ()) -> None:
        """Queue a hypothesis. The same statement reached twice (say, two contradictions with the same reason) is
        one hypothesis resting on everything that led to it."""
        statement = check_no_novelty_claim(statement, "Hypothesis.statement")
        spec = self.pending.setdefault(statement, {
            "origin": origin, "mechanism": mechanism, "score": score, "supporting": set(), "contradicting": set(),
            "originating": set(), "predicted": set(), "falsified_by": set(), "assumptions": set()})
        for key, values in (("supporting", supporting), ("contradicting", contradicting),
                            ("originating", originating), ("predicted", predicted),
                            ("falsified_by", falsified_by), ("assumptions", assumptions)):
            spec[key] |= set(values)

    def finalize(self) -> None:
        for statement in sorted(self.pending):
            spec = self.pending[statement]
            supporting = sorted(spec["supporting"] - spec["contradicting"])
            contradicting = sorted(spec["contradicting"] - spec["supporting"])
            h = self.ctx.ensure(Hypothesis(
                statement, originating=sorted(spec["originating"]), assumptions=sorted(spec["assumptions"]),
                origin=spec["origin"], supporting_claim_ids=supporting, contradicting_claim_ids=contradicting,
                scope=self.scope or {}, mechanism=spec["mechanism"], predicted_observations=sorted(spec["predicted"]),
                falsification_criteria=sorted(spec["falsified_by"] - spec["predicted"]),
                provisional_score=self.score(supporting, contradicting) if spec["score"] is None else spec["score"],
                derived_from=sorted(spec["originating"]), created_at=self.at))
            self.result.hypotheses.append(h)


def _statement(context: DiscoveryContext, claim_id: str) -> str:
    return context.reference(claim_id, ("claim",), "hypothesis.claim")["statement"].rstrip(". ")


def generate_hypotheses(context: DiscoveryContext, framed: FrameResult, gaps: Iterable[Gap] = (),
                        assessments: Iterable[PriorArtAssessment] = (), constraints: Iterable[Constraint] = (),
                        provider: ReasoningProvider | None = None, max_per_strategy: int = MAX_PER_STRATEGY,
                        at: str | None = None) -> HypothesisResult:
    """Hypotheses, counter-hypotheses and their evidence requirements, all registered in `context`."""
    at = at or utcnow()
    b = _Builder(context, framed, at)
    if framed.frame is None:
        b.result.notes.append("No problem frame (insufficient evidence): no hypotheses were generated.")
        return b.result

    # Contradiction-explaining: competing explanations, never one.
    for cx_id in sorted(framed.frame.contradiction_ids)[:max_per_strategy]:
        cx = context.reference(cx_id, ("contradiction",), "generate_hypotheses.contradiction")
        if cx["kind"] != "incompatible":
            continue
        a, c = cx["claim_a"], cx["claim_b"]
        settle = b.requirement(f"Independent primary evidence that settles: {cx['reason'] or 'the disagreement'}"
                               f"{'; ' + cx['resolution'] if cx['resolution'] else ''}", [cx_id])
        for keep, drop in ((a, c), (c, a)):
            confirm = b.requirement(f"Independent evidence confirming: {_statement(context, keep)}", [cx_id, keep])
            refute = b.requirement(f"Independent evidence confirming: {_statement(context, drop)}", [cx_id, drop])
            b.add(f"{_statement(context, keep)}, not '{_statement(context, drop)}': the conflicting report is in error",
                  "contradiction-explaining",
                  mechanism="source or reporting error in one report", supporting=[keep], contradicting=[drop],
                  originating=[cx_id], predicted=[confirm.id, settle.id], falsified_by=[refute.id])
        both = b.requirement(f"The definitions, periods and places each report used, for: {cx['reason']}", [cx_id])
        b.add(f"Both reports can hold: they differ in definition, period or place ({cx['reason']})",
              "contradiction-explaining", mechanism="different definitions, a time lag or a different place",
              originating=[cx_id], predicted=[both.id], falsified_by=[settle.id],
              score=UNSUPPORTED_PRIOR)

    # Gap-closing, from uncertain claims (contested ones are explained above).
    for u in sorted(framed.uncertainties, key=lambda u: u.id)[:max_per_strategy]:
        if u.reason == UncertaintyReason.CONTESTED:
            continue
        stmt = u.statement.rstrip(". ")
        confirm = b.requirement(f"An independent source confirming: {stmt}", [u.claim_id])
        refute = b.requirement(f"Independent evidence contradicting: {stmt}", [u.claim_id])
        b.add(f"{stmt}, and independent evidence will confirm it", "gap-closing",
              mechanism=f"the claim falls short only through {u.reason.value.replace('_', ' ')}",
              supporting=[u.claim_id], originating=[u.claim_id], predicted=[confirm.id], falsified_by=[refute.id])

    # Gap-closing, from high-value gaps.
    explained = set(framed.frame.contradiction_ids)
    valuable = sorted((g for g in gaps if g.information_gain >= HIGH_VALUE_GAP and g.basis != GapBasis.CONTRADICTION
                       and not any(cx in g.missing for cx in explained)),  # contradictions are explained above
                      key=lambda g: (-g.information_gain, g.id))[:max_per_strategy]
    for g in valuable:
        req = requirement_for_gap(context, g, framed, at=at)
        if req.id not in {r.id for r in b.result.requirements}:
            b.result.requirements.append(req)
        absent = b.requirement(f"A search of {', '.join(g.ways_to_close) or 'the named sources'} that finds no "
                               f"evidence for: {g.missing}", [g.id])
        b.add(f"Evidence that closes '{g.missing}' exists and can be acquired", "gap-closing",
              mechanism="the gap is a missing acquisition, not a missing fact", originating=[g.id],
              predicted=[req.id], falsified_by=[absent.id], score=round(min(0.6, g.information_gain), 3))

    # Cross-domain transfer, from matched prior art.
    for a in sorted(assessments, key=lambda a: a.id):
        if a.conclusion != PriorArtConclusion.MATCH_FOUND:
            continue
        for m in a.matches[:3]:
            test = b.requirement(f"Evidence that the approach in '{m['title']}' works for {a.subject}", [a.id])
            fail = b.requirement(f"Evidence that conditions behind '{m['title']}' do not hold for {a.subject}",
                                 [a.id])
            b.add(f"The approach in '{m['title']}' carries over to {a.subject}", "cross-domain-transfer",
                  mechanism=m.get("summary") or m["title"], originating=[a.id], predicted=[test.id],
                  falsified_by=[fail.id], score=UNSUPPORTED_PRIOR)

    # Constraint relaxation: a hypothesis about value, never a fact.
    for con in sorted((c for c in constraints if c.source_assumption_id), key=lambda c: c.id)[:max_per_strategy]:
        test = b.requirement(f"Evidence for what changes if '{con.name}' is relaxed", [con.id])
        b.add(f"Relaxing '{con.name}' would make more candidate solutions feasible", "constraint-relaxation",
              mechanism=f"the constraint rests on an assumption ({con.source_assumption_id}), not on evidence",
              originating=[con.id], predicted=[test.id], score=UNSUPPORTED_PRIOR,
              assumptions=[con.source_assumption_id])

    _from_provider(b, context, framed, provider)
    b.finalize()
    _counters(b)
    return b.result


def _from_provider(b: _Builder, context: DiscoveryContext, framed: FrameResult,
                   provider: ReasoningProvider | None) -> None:
    if provider is None:
        return
    b.result.provider = {"name": provider.name, "version": provider.version, "proposals": 0}
    summary = {
        "objective": framed.objective.objective,
        "known": [f.statement for f in framed.known_facts],
        "uncertain": [u.statement for u in framed.uncertainties],
        "contradictions": list(framed.frame.contradiction_ids),
    }
    try:
        proposals = provider.propose_hypotheses(summary)
        if not isinstance(proposals, list):
            raise MalformedInput("propose_hypotheses must return a list", "provider")
    except Exception as exc:  # a provider failure leaves the deterministic strategies intact
        b.result.provider["error"] = f"{type(exc).__name__}: {exc}"
        b.result.notes.append(f"Provider {provider.name} failed to propose hypotheses; engine strategies only.")
        return
    origin = f"provider:{provider.name}"
    for p in proposals[:MAX_PROVIDER_PROPOSALS]:
        if not isinstance(p, dict):
            b.result.notes.append("A provider proposal that was not an object was ignored.")
            continue
        try:
            stmt = text(p.get("statement"), "provider.statement")
            mechanism = text(p.get("mechanism", ""), "provider.mechanism", required=False)
            req = b.requirement(f"Evidence for: {stmt}", [])
            # Only the text is used: any evidence, ids or confidence a provider claims is ignored.
            b.add(stmt, origin, mechanism=mechanism, predicted=[req.id])
            b.result.provider["proposals"] += 1
        except Exception as exc:  # a malformed proposal is recorded, not fatal
            b.result.notes.append(f"A provider proposal was refused: {exc}")


def _counters(b: _Builder) -> None:
    alternative = {
        "contradiction-explaining": "a measurement, timing or reporting artifact rather than the stated mechanism",
        "gap-closing": "a single source or a scope-limited observation rather than a general pattern",
        "cross-domain-transfer": "conditions that do not carry over to this setting",
        "constraint-relaxation": "a binding constraint elsewhere, so relaxing this one changes nothing",
    }
    for h in list(b.result.hypotheses):
        if h.provisional_score is None or h.provisional_score < COUNTER_THRESHOLD:
            continue
        why = alternative.get(h.origin, "an explanation the evidence has not yet ruled out")
        test = b.ctx.reference(h.falsification_criteria[0], (EvidenceRequirement,), "counter") \
            if h.falsification_criteria else None
        ch = CounterHypothesis(
            f"Not established: the support for '{h.statement}' may reflect {why}", originating=[h.id],
            origin=f"counter:{h.origin}", supporting_claim_ids=h.contradicting_claim_ids,
            contradicting_claim_ids=h.supporting_claim_ids, scope=h.scope, mechanism=why,
            predicted_observations=[test.id] if test is not None else [],
            falsification_criteria=list(h.predicted_observations), parent_ids=[h.id],
            provisional_score=round(1 - h.provisional_score, 3), counters=h.id, derived_from=[h.id],
            created_at=b.at)
        ch = b.ctx.ensure(ch)
        if ch.id not in {c.id for c in b.result.counters}:
            b.result.counters.append(ch)
