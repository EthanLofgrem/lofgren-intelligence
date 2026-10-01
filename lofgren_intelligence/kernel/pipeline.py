"""The kernel pipeline: runs an Outcome Contract through the loop.

V1 delivers Intent -> Plan -> Sense -> Research -> Verify -> Report:

    Outcome Contract -> research question graph -> source selection -> acquisition
    -> normalization (scope, calculations) -> evidence graph -> claim extraction
    -> lineage -> independent verification -> contradiction graph -> skeptic
    -> unknowns -> findings -> report + research receipt

Later stages are recorded as not yet available so every run shows the whole
loop and exactly how far this version carries it. The report is a view of the
typed state this pipeline produces; it never holds facts of its own.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timezone

from ..adapters.base import AdapterRegistry
from ..authority.engine import Action, decide
from ..billing.pricing import HEAVY_WORK_UNITS, JOB_CLASSES, PLANS, Estimate, estimate
from ..evidence.graph import EvidenceGraph
from ..evidence.lineage import LineageLink, resolve_lineage
from ..evidence.scope import infer_scope
from ..evidence.types import Calculation, Claim, ClaimOrigin, Finding, Unknown, utcnow
from ..intent.compiler import OutcomeContract
from ..models.provider import HeuristicProvider, ReasoningProvider
from ..research.planner import CAPABILITY_SOURCES, ResearchPlan, StoppingRule, gap_unknowns, plan_research
from ..verification.calibration import PredictionLog
from ..verification.engine import ConfidenceFactors, Verifier
from ..verification.skeptic import SkepticReport, review
from .findings import build_findings, post_verification_unknowns
from .ledger import CostLedger
from .stages import KERNEL_LOOP, STAGE_VERSION, VERSION_NAMES, Stage

CURRENT_VERSION = 1
CLAIMS_PER_VERIFY_UNIT = 25
REPORT_UNITS = 1.0


@dataclass
class StageRecord:
    stage: Stage
    status: str  # done | stopped | not_available | not_run
    detail: str = ""
    work_units: float = 0.0


@dataclass
class RunResult:
    contract: OutcomeContract
    plan: ResearchPlan
    graph: EvidenceGraph
    stages: list[StageRecord] = field(default_factory=list)
    unknowns: list[Unknown] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)
    estimate: Estimate | None = None
    charge: Estimate | None = None
    factors: dict[str, ConfidenceFactors] = field(default_factory=dict)
    provider: str = ""
    provider_info: dict = field(default_factory=dict)
    calibrated: bool = False
    lineage: list[LineageLink] = field(default_factory=list)
    skeptic: SkepticReport | None = None
    ledger: CostLedger | None = None
    receipt: dict = field(default_factory=dict)
    stopped_reason: str = ""
    started_at: str = field(default_factory=utcnow)
    finished_at: str = ""

    @property
    def completed(self) -> bool:
        return not self.stopped_reason

    @property
    def gaps(self) -> list[str]:
        """Open unknowns as plain sentences (kept for readability and older callers)."""
        return [u.description for u in self.unknowns if u.status == "open"]

    def add_unknown(self, u: Unknown) -> None:
        if all(x.id != u.id for x in self.unknowns):
            self.unknowns.append(u)

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


def _calculation(spec: dict, evidence_id: str) -> Calculation:
    inputs = [{**i, "evidence_id": i.get("evidence_id") or evidence_id} for i in spec.get("inputs", [])]
    return Calculation(spec["name"], spec["formula"], inputs, float(spec["result"]), spec.get("unit", ""))


def run_investigation(
    contract: OutcomeContract,
    registry: AdapterRegistry,
    provider: ReasoningProvider | None = None,
    plan_id: str = "payg",
    verifier: Verifier | None = None,
    approved: bool = False,
    prediction_log: PredictionLog | None = None,
) -> RunResult:
    provider = provider or HeuristicProvider()
    verifier = verifier or Verifier()
    graph = EvidenceGraph()
    rs = plan_research(contract, registry)
    ledger = CostLedger(PLANS[plan_id].rate)
    result = RunResult(contract, rs, graph, provider=provider.name, calibrated=verifier.calibrator.calibrated,
                       ledger=ledger)
    log = result.stages.append

    log(StageRecord(Stage.INTENT, "done",
                    f"mode={contract.mode}; {len(contract.questions)} questions; cap ${contract.max_spend_usd:.2f}"))

    # ---- PLAN + cost gate ---------------------------------------------------
    est = estimate_run(rs, plan_id)
    result.estimate = est
    for u in gap_unknowns(rs, contract, PLANS[plan_id].rate):
        result.add_unknown(u)
    log(StageRecord(Stage.PLAN, "done",
                    f"{len(rs.tasks)} gather tasks over {len(contract.questions)} linked questions, "
                    f"{len(rs.gaps)} capability gaps; estimated {est.job_class} job, "
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
        _finish(result, provider)
        return result

    # ---- SENSE + RESEARCH ---------------------------------------------------
    budget_units = _class_ceiling(est.job_class, est.work_units) - REPORT_UNITS
    used_units = 0.0
    rule = StoppingRule()
    gathered = skipped = 0
    questions = {q.id: q for q in contract.questions}
    for task in rs.tasks:
        if not rule.worth_it(task, budget_units - used_units):
            skipped += 1
            continue
        adapter = registry.get(task.adapter_id)
        out = adapter.gather(questions[task.question_id], contract)
        used_units += task.work_units
        gathered += 1
        ledger.record("sense", "retrieval", adapter.id, task.work_units, out.cost_usd,
                      f"{task.capability} for {task.question_id}", len(out.items))
        for note in out.notes:
            result.add_unknown(Unknown(description=note, question_id=task.question_id, capability=task.capability,
                                       source_types=CAPABILITY_SOURCES.get(task.capability, []), expected_gain=0.3))
        rule.observe(task.question_id, bool(out.items))
        for source, ev in out.items:
            graph.add_source(source)
            ev = graph.add_evidence(ev)
            observed = ev.data.get("observed_claims")
            if observed:
                found, origin = observed, ClaimOrigin.OBSERVED
            else:
                before = dict(provider.usage)
                found, origin = provider.extract_claims(ev.content, contract.objective), ClaimOrigin.EXTRACTED
                tokens = (provider.usage["input_tokens"] - before["input_tokens"],
                          provider.usage["output_tokens"] - before["output_tokens"])
                ledger.record("research", "model", provider.name, 0.0, 0.0,
                              f"claim extraction on {ev.id}; tokens in/out {tokens[0]}/{tokens[1]}", len(found))
            for c in found:
                claim = graph.add_claim(Claim(c["statement"], origin=origin, value=c.get("value"),
                                              unit=c.get("unit", ""), polarity=c.get("polarity", 1),
                                              subject=c.get("subject", ""), question_id=task.question_id,
                                              scope=infer_scope(c["statement"], ev, contract.location)),
                                        supported_by=[ev.id])
                if c.get("calculation"):
                    claim.calculation_id = graph.add_calculation(_calculation(c["calculation"], ev.id)).id
    log(StageRecord(Stage.SENSE, "done", f"{gathered} source calls, {skipped} skipped by the stopping rule",
                    used_units))
    result.lineage = resolve_lineage(graph)
    log(StageRecord(Stage.RESEARCH, "done",
                    f"{len(graph.sources)} sources in {len({s.independence_group for s in graph.sources.values()})} "
                    f"independent lineages, {len(graph.evidence)} evidence items, {len(graph.claims)} claims"))

    # ---- VERIFY -------------------------------------------------------------
    verifier.verify(graph, contract.evidence_standard.min_independent_sources,
                    contract.evidence_standard.min_confidence)
    result.factors = dict(verifier.factors)
    result.skeptic = review(graph, verifier.now)
    verify_units = float(math.ceil(len(graph.claims) / CLAIMS_PER_VERIFY_UNIT)) if graph.claims else 0.0
    used_units += verify_units
    ledger.record("verify", "compute", "verifier+skeptic", verify_units, 0.0,
                  f"{len(graph.claims)} claims cross-checked; {result.skeptic.count} skeptic issues")
    log(StageRecord(Stage.VERIFY, "done",
                    f"{len(graph.contradictions)} contradictions, {result.skeptic.count} skeptic issues "
                    f"({len(result.skeptic.downgraded)} downgraded); statuses {result.status_counts()}",
                    verify_units))
    for u in post_verification_unknowns(result):
        result.add_unknown(u)

    # ---- REPORT -------------------------------------------------------------
    result.findings = build_findings(result)
    used_units += REPORT_UNITS
    ledger.record("report", "report", "report", REPORT_UNITS, 0.0, f"{len(result.findings)} findings")
    log(StageRecord(Stage.REPORT, "done", f"{len(result.findings)} findings, {len(result.gaps)} open unknowns",
                    REPORT_UNITS))

    actual = estimate(plan_id, used_units)
    # Never charge more than the estimate the user saw.
    result.charge = actual if actual.total_usd <= est.total_usd else est
    _finish(result, provider)
    if prediction_log is not None:
        prediction_log.append([
            {"claim_id": c.id, "statement": c.statement, "confidence": c.confidence,
             "raw": round(result.factors[c.id].raw, 4), "method": c.confidence_method,
             "status": c.status.value, "run_id": result.receipt.get("research_id"), "at": result.finished_at}
            for c in graph.claims.values() if c.id in result.factors
        ])
    return result


def _finish(result: RunResult, provider: ReasoningProvider) -> None:
    done = {r.stage for r in result.stages}
    for stage in KERNEL_LOOP:
        if stage not in done:
            v = STAGE_VERSION[stage]
            status = "not_available" if v > CURRENT_VERSION else "not_run"
            detail = f"arrives in V{v} ({VERSION_NAMES[v]})" if v > CURRENT_VERSION else "stopped before this stage"
            result.stages.append(StageRecord(stage, status, detail))
    result.finished_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    result.provider_info = provider.describe()
    # No authoritative receipt from an inconsistent graph: validate() raises GraphValidationError.
    result.graph.validate()
    from .receipt import build_receipt  # late import: the receipt reads the finished result

    result.receipt = build_receipt(result)
