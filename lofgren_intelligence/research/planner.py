"""Research planner: which evidence is needed, from which source, in what order.

The plan is a set of gather tasks, one per (question, adapter) pair, ordered
by expected value per unit of cost. A requirement no connected source can
answer becomes an explicit gap rather than a silent omission.

Stopping rule: research stops when the expected value of the remaining
information falls below its cost (EVI < C). In V1 the value of a task is
    weight(question) * remaining_uncertainty(question)
and uncertainty halves each time a source returns evidence for the question.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..adapters.base import AdapterRegistry
from ..evidence.types import Unknown
from ..intent.compiler import OutcomeContract, Question

# Work units a single gather call costs, by capability.
CAPABILITY_WORK_UNITS = {
    "text": 1.0,
    "orbital_passes": 2.0,
    "imagery_catalog": 2.0,
    "sensor": 1.0,
}
VALUE_FLOOR = 0.15  # below this expected value a task is not worth its cost

# Where evidence for each capability could come from, if connected.
CAPABILITY_SOURCES = {
    "text": ["your documents", "web search", "public records and filings"],
    "orbital_passes": ["public orbital elements (CelesTrak)"],
    "imagery_catalog": ["open imagery catalogs (Sentinel-2, Landsat)", "licensed commercial imagery (paid)"],
    "sensor": ["IoT sensors you own or are authorized to read"],
}


class DependencyCycle(ValueError):
    pass


def question_depths(questions: list[Question]) -> dict[str, int]:
    """Depth of each question in the dependency graph (0 = no prerequisites)."""
    by_id = {q.id: q for q in questions}
    depth: dict[str, int] = {}
    visiting: set[str] = set()

    def visit(qid: str) -> int:
        if qid in depth:
            return depth[qid]
        if qid in visiting:
            raise DependencyCycle(f"research questions depend on each other in a cycle at {qid}")
        visiting.add(qid)
        deps = [d for d in by_id[qid].depends_on if d in by_id]
        depth[qid] = 1 + max((visit(d) for d in deps), default=-1)
        visiting.discard(qid)
        return depth[qid]

    for q in questions:
        visit(q.id)
    return depth


@dataclass
class GatherTask:
    question_id: str
    question: str
    capability: str
    adapter_id: str
    work_units: float
    weight: float
    depth: int = 0


@dataclass
class Gap:
    question_id: str
    question: str
    capability: str
    reason: str


@dataclass
class ResearchPlan:
    tasks: list[GatherTask] = field(default_factory=list)
    gaps: list[Gap] = field(default_factory=list)

    @property
    def estimated_work_units(self) -> float:
        return sum(t.work_units for t in self.tasks)


def plan_research(contract: OutcomeContract, registry: AdapterRegistry) -> ResearchPlan:
    plan = ResearchPlan()
    depths = question_depths(contract.questions)
    for q in contract.questions:
        for cap in q.needs:
            adapters = registry.find(cap)
            if not adapters:
                plan.gaps.append(Gap(q.id, q.text, cap, f"no connected source provides '{cap}'"))
                continue
            for a in adapters:
                plan.tasks.append(GatherTask(q.id, q.text, cap, a.id,
                                             CAPABILITY_WORK_UNITS.get(cap, 1.0), q.weight, depths[q.id]))
    # Prerequisites first; within a level, most value per unit of cost first.
    plan.tasks.sort(key=lambda t: (t.depth, -(t.weight / t.work_units)))
    return plan


def gap_unknowns(plan: ResearchPlan, contract: OutcomeContract, rate_usd_per_unit: float) -> list[Unknown]:
    """Turn each missing capability into an acquisition plan."""
    weights = {q.id: q.weight for q in contract.questions}
    total = sum(weights.values()) or 1.0
    by_cap: dict[str, list[Gap]] = {}
    for g in plan.gaps:
        by_cap.setdefault(g.capability, []).append(g)
    out: list[Unknown] = []
    for cap, gaps in by_cap.items():
        n = len(gaps)
        gain = min(1.0, sum(weights.get(g.question_id, 1.0) for g in gaps) / total)
        sources = CAPABILITY_SOURCES.get(cap, [cap])
        out.append(Unknown(
            description=f"No connected source provides '{cap}' (needed by {n} question{'s' if n > 1 else ''})",
            question_id=gaps[0].question_id if n == 1 else None,
            capability=cap,
            source_types=sources,
            expected_gain=round(gain, 3),
            est_cost_usd=round(CAPABILITY_WORK_UNITS.get(cap, 1.0) * n * rate_usd_per_unit, 4),
            needs_approval=any("paid" in s for s in sources),
        ))
    return out


class StoppingRule:
    """Tracks per-question uncertainty and decides whether a task is still worth running."""

    def __init__(self) -> None:
        self.uncertainty: dict[str, float] = {}

    def value(self, task: GatherTask) -> float:
        return task.weight * self.uncertainty.get(task.question_id, 1.0)

    def worth_it(self, task: GatherTask, remaining_budget_units: float) -> bool:
        if task.work_units > remaining_budget_units:
            return False
        return self.value(task) / task.work_units >= VALUE_FLOOR

    def observe(self, question_id: str, found_evidence: bool) -> None:
        if found_evidence:
            self.uncertainty[question_id] = self.uncertainty.get(question_id, 1.0) * 0.5
