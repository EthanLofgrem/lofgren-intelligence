"""Discovery Intelligence (V2) — construction starts here.

This package holds the V2 contracts only. V2 engines (prior art, gap
discovery, hypothesis generation, simulation, optimization) will implement
against these types and read V1 state exclusively through
`kernel.state.export_state`.

Two rules are enforced in code, not only in documentation:

1. Novelty, feasibility and expected value are separate measurements; a
   candidate has no single "score" that blends them.
2. A hypothesis can enter the evidence graph only as origin=HYPOTHESIS. The
   verifier never marks such a claim verified, and `promote` refuses.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..evidence.graph import EvidenceGraph
from ..evidence.types import Claim, ClaimOrigin, make_id


class PromotionRefused(PermissionError):
    """Raised when anything tries to turn a hypothesis into verified evidence."""


@dataclass
class Hypothesis:
    statement: str
    originating: list[str]  # ids of V1 unknowns, contradictions or claims it responds to
    assumptions: list[str] = field(default_factory=list)
    test: str = ""  # the observation or experiment that would confirm or refute it
    id: str = ""

    def __post_init__(self) -> None:
        if not self.id:
            self.id = make_id("HYP", self.statement)

    def as_claim(self) -> Claim:
        return Claim(self.statement, origin=ClaimOrigin.HYPOTHESIS)


@dataclass
class Candidate:
    """A possible solution. Its three measures are never collapsed into one."""

    description: str
    originating_gap: str
    prior_art: list[str] = field(default_factory=list)
    assumptions: list[str] = field(default_factory=list)
    required_conditions: list[str] = field(default_factory=list)
    constraints: list[str] = field(default_factory=list)
    evidence_support: list[str] = field(default_factory=list)  # V1 claim ids
    evidence_against: list[str] = field(default_factory=list)  # V1 claim ids
    novelty: float | None = None
    technical_feasibility: float | None = None
    economic_feasibility: float | None = None
    expected_value: float | None = None
    legal_constraints: list[str] = field(default_factory=list)
    uncertainty: str = ""
    estimated_cost_usd: float | None = None
    simulation_results: dict = field(default_factory=dict)
    sensitivity: dict = field(default_factory=dict)
    failure_modes: list[str] = field(default_factory=list)
    next_experiment: str = ""
    id: str = ""

    def __post_init__(self) -> None:
        if not self.id:
            self.id = make_id("CAND", self.description)


def add_hypothesis(graph: EvidenceGraph, hyp: Hypothesis) -> Claim:
    """Record a hypothesis in the graph without giving it evidential weight."""
    return graph.add_claim(hyp.as_claim())


def promote(claim: Claim) -> None:
    """There is no path from hypothesis to finding except new evidence through V1."""
    if claim.origin == ClaimOrigin.HYPOTHESIS:
        raise PromotionRefused("a hypothesis becomes a finding only when new V1 evidence verifies a separate claim")
    raise PromotionRefused("claims are verified by the V1 verifier, not promoted")
