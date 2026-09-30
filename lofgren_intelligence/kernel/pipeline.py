"""The kernel pipeline: runs an Outcome Contract through the loop.

V1 delivers Intent -> Plan -> Sense -> Research -> Verify -> Report. Later
stages are recorded as not yet available so every run shows the whole loop
and exactly how far this version carries it.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timezone

from ..adapters.base import AdapterRegistry
from ..authority.engine import Action, decide
from ..billing.pricing import HEAVY_WORK_UNITS, JOB_CLASSES, Estimate, estimate
from ..evidence.graph import EvidenceGraph
from ..evidence.types import Claim, ClaimOrigin
from ..intent.compiler import OutcomeContract
from ..models.provider import HeuristicProvider, ModelProvider
from ..research.planner import ResearchPlan, StoppingRule, plan_research
from ..verification.engine import ConfidenceFactors, Verifier
from .answers import answer_all
from .stages import KERNEL_LOOP, STAGE_VERSION, VERSION_NAMES, Stage

CURRENT_VERSION = 1
CLAIMS_PER_VERIFY_UNIT = 25
REPORT_UNITS = 1.0


@dataclass
class StageRecord:
    stage: Stage
    status: str  # done | stopped | not_available
    detail: str = ""
    work_units: float = 0.0


@dataclass
class RunResult:
    contract: OutcomeContract
    plan: ResearchPlan
    graph: EvidenceGraph
    stages: list[StageRecord] = field(default_factory=list)
    gaps: list[str] = field(default_factory=list)
    estimate: Estimate | None = None
    charge: Estimate | None = None
    factors: dict[str, ConfidenceFactors] = field(default_factory=dict)
    provider: str = ""
    calibrated: bool = False
    stopped_reason: str = ""
    finished_at: str = ""

    @property
    def completed(self) -> bool:
        return not self.stopped_reason

    def status_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for c in self.graph.claims.values():
            counts[c.status.value] = counts.get(c.status.value, 0) + 1
        return counts


def estimate_run(plan: ResearchPlan, plan_id: str, expected_claims: int | None = None) -> Estimate:
    claims = expected_claims if expected_claims is not None else 4 * len(plan.tasks)
    units = plan.estimated_work_units + math.ceil(claims / CLAIMS_PER_VERIFY_UNIT) + REPORT_UNITS
    return estimate(plan_id, units)


def _class_ceiling(job_class: str, units: float) -> float:
    for name, max_units, _ in JOB_CLASSES:
        if name == job_class:
            return max_units
    return math.ceil(units / HEAVY_WORK_UNITS) * HEAVY_WORK_UNITS  # project


def run_investigation(
    contract: OutcomeContract,
    registry: AdapterRegistry,
    provider: ModelProvider | None = None,
    plan_id: str = "payg",
    verifier: Verifier | None = None,
    approved: bool = False,
) -> RunResult:
    provider = provider or HeuristicProvider()
    verifier = verifier or Verifier()
    graph = EvidenceGraph()
    rs = plan_research(contract, registry)
    result = RunResult(contract, rs, graph, provider=provider.name,
                       calibrated=verifier.calibrator.calibrated)
    log = result.stages.append

    log(StageRecord(Stage.INTENT, "done",
                    f"mode={contract.mode}; {len(contract.questions)} questions; cap ${contract.max_spend_usd:.2f}"))

    # ---- PLAN + cost gate ---------------------------------------------------
    est = estimate_run(rs, plan_id)
    result.estimate = est
    by_cap: dict[str, int] = {}
    for g in rs.gaps:
        by_cap[g.capability] = by_cap.get(g.capability, 0) + 1
    result.gaps += [f"No connected source provides '{cap}' (needed by {n} question{'s' if n > 1 else ''})"
                    for cap, n in by_cap.items()]
    log(StageRecord(Stage.PLAN, "done",
                    f"{len(rs.tasks)} gather tasks, {len(rs.gaps)} gaps; estimated {est.job_class} job, "
                    f"{est.work_units:g} WU, ${est.total_usd:.4f}"))
    if not est.allowed:
        result.stopped_reason = est.reason
    else:
        decision = decide(Action("research", "run the research plan", cost_usd=est.total_usd),
                          contract, approved=approved)
        if not decision.allowed:
            result.stopped_reason = "; ".join(decision.reasons)
    if result.stopped_reason:
        result.stages[-1].status = "stopped"
        _finish(result)
        return result

    # ---- SENSE + RESEARCH ---------------------------------------------------
    budget_units = _class_ceiling(est.job_class, est.work_units) - REPORT_UNITS
    used_units = 0.0
    rule = StoppingRule()
    gathered = skipped = 0
    for task in rs.tasks:
        if not rule.worth_it(task, budget_units - used_units):
            skipped += 1
            continue
        adapter = registry.get(task.adapter_id)
        out = adapter.gather(next(q for q in contract.questions if q.id == task.question_id), contract)
        used_units += task.work_units
        gathered += 1
        result.gaps += [n for n in out.notes if n not in result.gaps]
        rule.observe(task.question_id, bool(out.items))
        for source, ev in out.items:
            graph.add_source(source)
            ev = graph.add_evidence(ev)
            observed = ev.data.get("observed_claims")
            found = observed if observed else provider.extract_claims(ev.content, contract.objective)
            origin = ClaimOrigin.OBSERVED if observed else ClaimOrigin.EXTRACTED
            for c in found:
                claim = graph.add_claim(Claim(c["statement"], origin=origin, value=c.get("value"),
                                              unit=c.get("unit", ""), polarity=c.get("polarity", 1),
                                              subject=c.get("subject", ""),
                                              question_id=task.question_id), supported_by=[ev.id])
                if claim.question_id is None:
                    claim.question_id = task.question_id
    log(StageRecord(Stage.SENSE, "done", f"{gathered} source calls, {skipped} skipped by the stopping rule",
                    used_units))
    log(StageRecord(Stage.RESEARCH, "done",
                    f"{len(graph.sources)} sources, {len(graph.evidence)} evidence items, {len(graph.claims)} claims"))

    # ---- VERIFY -------------------------------------------------------------
    verifier.verify(graph, contract.evidence_standard.min_independent_sources,
                    contract.evidence_standard.min_confidence)
    result.factors = dict(verifier.factors)
    verify_units = float(math.ceil(len(graph.claims) / CLAIMS_PER_VERIFY_UNIT)) if graph.claims else 0.0
    used_units += verify_units
    log(StageRecord(Stage.VERIFY, "done",
                    f"{len(graph.contradictions)} contradictions; statuses {result.status_counts()}", verify_units))
    for ans in answer_all(result):
        if not ans.answered:
            gap = f"{ans.question.text}: no verified finding yet"
            if gap not in result.gaps:
                result.gaps.append(gap)

    # ---- REPORT -------------------------------------------------------------
    used_units += REPORT_UNITS
    log(StageRecord(Stage.REPORT, "done", "report assembled", REPORT_UNITS))

    actual = estimate(plan_id, used_units)
    # Never charge more than the estimate the user saw.
    result.charge = actual if actual.total_usd <= est.total_usd else est
    _finish(result)
    return result


def _finish(result: RunResult) -> None:
    done = {r.stage for r in result.stages}
    for stage in KERNEL_LOOP:
        if stage not in done:
            v = STAGE_VERSION[stage]
            status = "not_available" if v > CURRENT_VERSION else "not_run"
            detail = f"arrives in V{v} ({VERSION_NAMES[v]})" if v > CURRENT_VERSION else "stopped before this stage"
            result.stages.append(StageRecord(stage, status, detail))
    result.finished_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
