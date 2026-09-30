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
from ..intent.compiler import OutcomeContract

# Work units a single gather call costs, by capability.
CAPABILITY_WORK_UNITS = {
    "text": 1.0,
    "orbital_passes": 2.0,
    "imagery_catalog": 2.0,
    "sensor": 1.0,
}
VALUE_FLOOR = 0.15  # below this expected value a task is not worth its cost


@dataclass
class GatherTask:
    question_id: str
    question: str
    capability: str
    adapter_id: str
    work_units: float
    weight: float


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
    for q in contract.questions:
        for cap in q.needs:
            adapters = registry.find(cap)
            if not adapters:
                plan.gaps.append(Gap(q.id, q.text, cap, f"no connected source provides '{cap}'"))
                continue
            for a in adapters:
                plan.tasks.append(GatherTask(q.id, q.text, cap, a.id,
                                             CAPABILITY_WORK_UNITS.get(cap, 1.0), q.weight))
    plan.tasks.sort(key=lambda t: -(t.weight / t.work_units))
    return plan


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
