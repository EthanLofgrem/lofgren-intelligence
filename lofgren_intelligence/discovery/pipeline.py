"""The discovery pipeline: one call from a validated V1 knowledge map to a decision, a receipt and a V3 handoff.

    run_discovery(context, objective, design=..., prior_art=..., provider=...) -> DiscoveryResult

Stages, in order: frame -> prior art -> gaps -> hypotheses (+ counter-hypotheses) -> connections -> candidates
(constraints, simulation, sensitivity, optimization) -> discovery verifier -> decision -> findings -> receipt ->
handoff. Each stage records a ledger entry. V1 state is only read, through the context; nothing here writes to an
evidence graph, and the only route back to V1 is `reverify`, which runs a new, separate V1 investigation.

First-class outcomes (results, not errors):

    candidate_selected     a candidate passed the decision rule; a validated V3 handoff is produced
    insufficient_evidence  V1 established nothing to frame (no known and no uncertain claims)
    contradicted           the evidence is contested and nothing is known: competing hypotheses, no selection
    infeasible             every candidate violates a constraint, or the optimization is infeasible
    requires_research      candidates or hypotheses need evidence V1 does not have yet (see the requirements)
    unsupported            the design space asks for a model, distribution or solver that is not supported
    unknown                viable candidates exist but none meets the decision rule's thresholds
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Any, Mapping

from .. import __version__
from ..kernel.stages import Stage
from ..models.provider import ReasoningProvider
from .candidates import CandidateResult, DesignSpace, decide, evaluate_candidates
from .connections import ConnectionResult, find_connections
from .context import DiscoveryContext
from .errors import MalformedInput, UnsupportedAlgorithm
from .frame import FrameResult, frame_problem
from .gaps import GapResult, detect_gaps
from .handoff import HANDOFF_SCHEMA, check_handoff
from .hypotheses import HypothesisResult, generate_hypotheses
from .prior_art import FixturePriorArtProvider, PriorArtProvider, ProviderCoverage, assess_prior_art
from .receipt import DiscoveryLedger, build_discovery_receipt, evidence_fingerprint
from .types import (
    CandidateStatus,
    DiscoveryDecision,
    DiscoveryFinding,
    DiscoveryObjective,
    DiscoveryOutcome,
    FindingKind,
    HypothesisStatus,
    OptimizationStatus,
    PriorArtAssessment,
    _Obj,
    utcnow,
)
from .verifier import VerifierReport, verify_discovery

ALGORITHMS = {"pipeline": "1", "connections": "1", "hypotheses": "1", "candidates": "1", "simulation": "1",
              "sensitivity": "1", "optimization": "1", "verifier": "1", "receipt": "1", "handoff": "1"}
from ..billing.pricing import BASE_UNIT_USD  # noqa: E402  (pricing is a leaf module)


@dataclass
class DiscoveryResult:
    objective: DiscoveryObjective
    context: DiscoveryContext
    framed: FrameResult
    outcome: DiscoveryOutcome
    prior_art: list[PriorArtAssessment] = field(default_factory=list)
    gaps: GapResult | None = None
    hypotheses: HypothesisResult = field(default_factory=HypothesisResult)
    connections: ConnectionResult | None = None
    candidates: CandidateResult = field(default_factory=CandidateResult)
    verifier: VerifierReport = field(default_factory=VerifierReport)
    decision: DiscoveryDecision | None = None
    findings: list[DiscoveryFinding] = field(default_factory=list)
    receipt: dict = field(default_factory=dict)
    handoff: dict | None = None
    ledger: DiscoveryLedger = field(default_factory=lambda: DiscoveryLedger(BASE_UNIT_USD))
    stages: dict = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    @property
    def objects(self) -> list[_Obj]:
        """Every discovery object this run registered (all of the context's registry)."""
        return list(self.context.objects())

    @property
    def requirements(self):
        return self.hypotheses.requirements

    @property
    def selected(self):
        sid = self.decision.selected_candidate_id if self.decision else None
        return self.context.get(sid) if sid else None


def _prior_art_provider(spec: Mapping | PriorArtProvider | None) -> tuple[PriorArtProvider | None, dict]:
    if spec is None:
        return None, {}
    if isinstance(spec, Mapping) and "provider" in spec:
        return spec["provider"], dict(spec)
    if isinstance(spec, Mapping):
        cov = spec.get("coverage") or {}
        try:
            coverage = ProviderCoverage(tuple(cov.get("sources", ())), tuple(cov.get("domains", ())),
                                        tuple(cov.get("time_range", (None, None))), tuple(cov.get("limitations", ())))
        except TypeError:
            raise MalformedInput("prior-art coverage has sources, domains, time_range and limitations",
                                 "prior_art.coverage") from None
        return FixturePriorArtProvider(list(spec.get("records", [])), coverage), dict(spec)
    raise MalformedInput("prior_art is a fixture spec or {'provider': PriorArtProvider, ...}", "prior_art")


def run_discovery(context: DiscoveryContext, objective: str, *, design: Mapping | None = None,
                  prior_art: Mapping | None = None, provider: ReasoningProvider | None = None,
                  mode: str = "investigate", at: str | None = None) -> DiscoveryResult:
    """Run the full discovery pipeline over one validated knowledge map. Deterministic for a given `at`."""
    at = at or utcnow()
    started = at
    config = {"design": copy.deepcopy(dict(design or {})),
              "prior_art": {k: v for k, v in (prior_art or {}).items() if k != "provider"}}
    ledger = DiscoveryLedger(BASE_UNIT_USD)
    obj = context.ensure(DiscoveryObjective(objective, context.research_id, mode, created_at=at))
    framed = frame_problem(context, obj, at=at)
    ledger.record("frame", "compute", "discovery.frame", 0.1, detail="problem frame from the knowledge map",
                  evidence_items=len(framed.known_facts) + len(framed.uncertainties))
    result = DiscoveryResult(obj, context, framed, DiscoveryOutcome.UNKNOWN, ledger=ledger)
    result.notes += list(framed.notes)
    stages = {Stage.IMAGINE.value: "not_run", Stage.SIMULATE.value: "not_run", Stage.OPTIMIZE.value: "not_run"}

    pa_provider, pa_spec = _prior_art_provider(prior_art)
    pa_meta: dict = {}
    if framed.frame is not None and pa_provider is not None:
        queries = list(pa_spec.get("queries") or [objective])
        assessment, meta = assess_prior_art(context, pa_spec.get("subject", objective), queries, pa_provider,
                                            domains=pa_spec.get("domains", ()),
                                            time_range=tuple(pa_spec.get("time_range", (None, None))), at=at)
        result.prior_art.append(assessment)
        pa_meta = dict(meta.__dict__) if hasattr(meta, "__dict__") else dict(meta)
        ledger.record("prior_art", "search", f"prior-art:{pa_provider.name}", float(len(queries)),
                      detail=assessment.statement, evidence_items=len(assessment.matches))

    if framed.frame is None:
        result.outcome = DiscoveryOutcome.INSUFFICIENT_EVIDENCE
        result.notes.append("V1 established nothing to frame; close the missing evidence through V1 first.")
        return _finish(result, config, provider, pa_meta, {}, "no decision: insufficient evidence", stages, started, at)

    result.gaps = detect_gaps(context, framed, result.prior_art, at=at)
    ledger.record("gaps", "compute", "discovery.gaps", 0.1, evidence_items=len(result.gaps.gaps))

    space = None
    try:
        space = DesignSpace.from_json(design)
    except UnsupportedAlgorithm as exc:
        result.outcome = DiscoveryOutcome.UNSUPPORTED
        result.notes.append(f"The design space asks for something unsupported: {exc}")

    constraints_preview = []
    result.hypotheses = generate_hypotheses(context, framed, result.gaps.gaps, result.prior_art, constraints_preview,
                                            provider=provider, at=at)
    ledger.record("hypotheses", "hypothesis_generation", "discovery.hypotheses", 0.5,
                  detail=f"{len(result.hypotheses.hypotheses)} hypotheses, {len(result.hypotheses.counters)} counters",
                  evidence_items=len(result.hypotheses.all))
    if provider is not None:
        ledger.record("hypotheses", "model", f"provider:{provider.name}", 0.5,
                      detail=f"{result.hypotheses.provider.get('proposals', 0)} proposals")
    stages[Stage.IMAGINE.value] = "done"

    if space is not None:
        result.candidates = evaluate_candidates(context, framed, space, result.hypotheses.all, decide_now=False, at=at)
        sims = result.candidates.simulations
        if sims:
            stages[Stage.SIMULATE.value] = "done"
            ledger.record("simulate", "simulation", "discovery.simulate",
                          round(sum(s.iterations for s in sims) / 1000, 6), evidence_items=len(sims))
            ledger.record("sensitivity", "compute", "discovery.sensitivity", 0.1 * len(result.candidates.sensitivities),
                          evidence_items=len(result.candidates.sensitivities))
        if result.candidates.optimization is not None:
            stages[Stage.OPTIMIZE.value] = "done"
            ledger.record("optimize", "optimization", "discovery.optimize", 1.0,
                          detail=result.candidates.optimization.status.value)
        # Constraint-relaxation hypotheses need the grounded constraints: a second, additive pass.
        relax = generate_hypotheses(context, framed, (), (), result.candidates.constraints, at=at)
        for h in relax.hypotheses:
            if h.origin == "constraint-relaxation" and h.id not in {x.id for x in result.hypotheses.hypotheses}:
                result.hypotheses.hypotheses.append(h)
        for r in relax.requirements:
            if r.id not in {x.id for x in result.hypotheses.requirements}:
                result.hypotheses.requirements.append(r)
        result.notes += result.candidates.notes

    result.connections = find_connections(context, framed, result.gaps.gaps, result.prior_art,
                                          result.candidates.constraints, result.hypotheses.all, at=at)
    ledger.record("connections", "compute", "discovery.connections", 0.1,
                  evidence_items=len(result.connections.connections))

    texts = [(h.id, h.statement) for h in result.hypotheses.all] + \
            [(c.id, c.description) for c in result.candidates.candidates]
    result.verifier = verify_discovery(context, framed.frame, context.objects(), scenarios=result.candidates.scenarios,
                                       texts=texts)
    ledger.record("verify", "verification", "discovery.verifier", 0.2, detail=f"{len(result.verifier.issues)} issues")

    if space is not None:
        result.decision = decide(context, result.candidates.candidates, space.decision_rule, at)
        result.candidates.decision = result.decision
    result.outcome = _outcome(result, space)
    rule = result.decision.rule if result.decision else "no decision: " + result.outcome.value
    return _finish(result, config, provider, pa_meta, {"simulation": space.seed if space else None}, rule, stages,
                   started, at)


def _outcome(result: DiscoveryResult, space: DesignSpace | None) -> DiscoveryOutcome:
    if result.outcome == DiscoveryOutcome.UNSUPPORTED:
        return DiscoveryOutcome.UNSUPPORTED
    d = result.decision
    if d is not None and d.outcome == DiscoveryOutcome.CANDIDATE_SELECTED:
        return d.outcome
    framed = result.framed
    if framed.frame.contradiction_ids and not framed.known_facts:
        return DiscoveryOutcome.CONTRADICTED
    opt = result.candidates.optimization
    if opt is not None and opt.status == OptimizationStatus.UNSUPPORTED and not result.candidates.candidates:
        return DiscoveryOutcome.UNSUPPORTED
    if opt is not None and opt.status == OptimizationStatus.INFEASIBLE and \
            not any(c.status in (CandidateStatus.VIABLE, CandidateStatus.DOMINATED) for c in result.candidates.candidates):
        return DiscoveryOutcome.INFEASIBLE
    if d is not None and result.candidates.candidates:
        return d.outcome
    if result.hypotheses.requirements:
        return DiscoveryOutcome.REQUIRES_RESEARCH
    return DiscoveryOutcome.UNKNOWN


def _findings(result: DiscoveryResult, at: str) -> list[DiscoveryFinding]:
    ctx, out = result.context, []

    def add(statement: str, kind: FindingKind, rests_on: list[str]) -> None:
        out.append(ctx.ensure(DiscoveryFinding(statement, kind, sorted(set(rests_on)), created_at=at)))

    for f in sorted(result.framed.known_facts, key=lambda f: f.id):
        add(f"Verified by V1: {f.statement}", FindingKind.VERIFIED_FACT, [f.id])
    for h in sorted(result.hypotheses.hypotheses, key=lambda h: h.id):
        if h.status in (HypothesisStatus.PROPOSED, HypothesisStatus.SURVIVES) and h.supporting_claim_ids:
            add(f"Hypothesis (not established): {h.statement}", FindingKind.HYPOTHESIS, [h.id])
    sel = result.selected
    if sel is not None and sel.simulation_results:
        sim_id = sel.simulation_results["simulation_id"]
        for metric, s in sorted(sel.simulation_results["outcomes"].items()):
            add(f"Simulated {metric} for '{sel.description}': mean {s['mean']:g} {s['unit']} "
                f"(p5 {s['p5']:g}, p95 {s['p95']:g}); a model prediction", FindingKind.SIMULATION_RESULT, [sim_id])
    opt = result.candidates.optimization
    if opt is not None and opt.status in (OptimizationStatus.OPTIMAL, OptimizationStatus.FEASIBLE):
        add(f"Optimization {opt.status.value}: {', '.join(f'{k}={v:g}' for k, v in sorted(opt.solution.items()))} "
            f"({opt.proof})", FindingKind.OPTIMIZATION_RESULT, [opt.id])
    if result.gaps is not None:
        for g in sorted(result.gaps.gaps, key=lambda g: (-g.information_gain, g.id))[:5]:
            add(f"Gap ({g.type.value}): {g.missing}", FindingKind.GAP, [g.id])
    return out


def build_handoff(result: DiscoveryResult) -> dict | None:
    """The V3 handoff for the selected candidate, or None when nothing was selected."""
    sel = result.selected
    if sel is None:
        return None
    ctx, cands = result.context, result.candidates
    scenario = next((s for s in cands.scenarios if s.candidate_id == sel.id), None)
    by_id = {c.id: c for c in cands.candidates}
    specs = [{"name": k, "value": v["value"], "unit": v.get("unit", ""), "tolerance": None, "source_id": scenario.id}
             for k, v in sorted((scenario.parameters if scenario else {}).items())]
    sim = next((s for s in cands.simulations if s.candidate_id == sel.id), None)
    expected = [{"metric": m, **{k: s[k] for k in ("mean", "p5", "p50", "p95", "unit")}, "kind": "simulated",
                 "simulation_id": sim.id} for m, s in sorted((sim.outcomes if sim else {}).items())]

    def measures(c) -> dict:
        return {"technical_feasibility": c.technical_feasibility, "economic_feasibility": c.economic_feasibility,
                "expected_value": c.expected_value, "robustness": c.robustness.value}

    space_success = result.receipt.get("config", {}).get("design", {}).get("success")
    criteria = []
    if space_success:
        criteria.append({"name": "success", "relation": space_success, "variable_units": {}})
    for c in cands.constraints:
        criteria.append({"name": c.name, "relation": c.relation.to_json(), "variable_units": dict(c.variable_units)})
    open_hyps = [h for h in result.hypotheses.all
                 if h.status in (HypothesisStatus.PROPOSED, HypothesisStatus.REQUIRES_RESEARCH, HypothesisStatus.CHALLENGED)]
    linked = [ctx.get(h) for h in sel.hypothesis_ids]
    h = {
        "schema": HANDOFF_SCHEMA,
        "objective": result.objective.objective,
        "selected_candidate": {"id": sel.id, "description": sel.description, "status": sel.status.value,
                               "measures": measures(sel)},
        "alternatives": [{"id": cid, "description": by_id[cid].description, "reason": reason,
                          "measures": measures(by_id[cid])} for cid, reason in sorted(result.decision.alternatives.items())],
        "verified_evidence": [{"claim_id": f.claim_id, "statement": f.statement, "kind": "verified_fact",
                               "known_fact_id": f.id} for f in sorted(result.framed.known_facts, key=lambda f: f.id)],
        "hypotheses": [{"id": x.id, "statement": x.statement, "status": x.status.value, "kind": "hypothesis",
                        "confidence_kind": "hypothesis", "provisional_score": x.provisional_score} for x in linked],
        "assumptions": [{"id": a.id, "statement": a.statement, "name": a.name, "value": a.value, "unit": a.unit,
                         "sensitivity": a.sensitivity} for a in cands.assumptions],
        "constraints": [{"id": c.id, "name": c.name, "kind": c.kind.value, "relation": c.relation.to_json(),
                         "variable_units": dict(c.variable_units)} for c in cands.constraints],
        "specifications": specs,
        "expected_outcomes": expected,
        "acceptance_criteria": criteria,
        "test_requirements": list(sel.test_requirements),
        "unresolved_questions": [{"id": m.id, "kind": "missing_evidence", "text": m.description}
                                 for m in result.framed.missing_evidence] +
                                [{"id": x.id, "kind": "hypothesis", "text": x.statement} for x in open_hyps],
        "risks": list(sel.risks),
        "dependencies": list(sel.dependencies),
        "resource_requirements": dict(sel.costs),
        "cost_estimates": {"discovery_usd": result.ledger.total_usd},
        "evidence_fingerprint": result.receipt["evidence"],
        "discovery_fingerprint": result.receipt["discovery_fingerprint"],
        "discovery_receipt_id": result.receipt["discovery_id"],
    }
    return dict(check_handoff(h, result.receipt, ctx))


def _finish(result: DiscoveryResult, config: Mapping, provider: ReasoningProvider | None, pa_meta: Mapping,
            seeds: Mapping, rule: str, stages: dict, started: str, at: str) -> DiscoveryResult:
    result.findings = _findings(result, at) if result.framed.frame is not None else []
    result.stages = stages
    sel = result.decision.selected_candidate_id if result.decision else None
    result.receipt = build_discovery_receipt(
        objective=result.objective.objective, context=result.context, config=config,
        algorithms={**ALGORITHMS, "engine": __version__},
        provider={"name": provider.name, "version": provider.version} if provider else {},
        prior_art_provider=pa_meta, seeds=seeds, decision_rule=rule, outcome=result.outcome.value,
        selected_candidate_id=sel, objects=result.context.objects(), ledger=result.ledger,
        verifier=result.verifier.to_json(), notes=result.notes + list(result.hypotheses.notes),
        started_at=started, finished_at=at)
    result.handoff = build_handoff(result) if sel else None
    return result


def discover_from_run(run, objective: str, **kwargs: Any) -> DiscoveryResult:
    """Discovery over a finished V1 run, through its validated knowledge-map/2 and research receipt."""
    from ..kernel.knowledge_map import export_knowledge_map

    return run_discovery(DiscoveryContext(export_knowledge_map(run), run.receipt), objective, **kwargs)


def reverify(requirement, registry, provider: ReasoningProvider | None = None, **kwargs: Any):
    """The only route from V2 back to V1: a new, separate V1 investigation of what the requirement asks for. Its
    claims are verified by the V1 verifier like any others; nothing from discovery enters its evidence graph."""
    from ..intent.compiler import compile_intent
    from ..kernel.pipeline import run_investigation
    from ..models.provider import HeuristicProvider

    place = f" in {requirement.place}" if requirement.place else ""
    when = ""
    if requirement.period != (None, None):
        when = f" during {requirement.period[0] or '...'} to {requirement.period[1] or '...'}"
    return run_investigation(compile_intent(f"{requirement.description}{place}{when}"), registry,
                             provider or HeuristicProvider(), **kwargs)
