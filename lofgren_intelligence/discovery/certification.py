"""V2 Discovery certification (`lofgren certify --v2`): the code terms of the V2ReadyForV3 gate.

Every scenario builds a real V1 run (document, sensor and fixture adapters, heuristic provider, no network, no
model), runs real V2 code over it and inspects the artifacts: object kinds and counts, statuses, outcomes,
fingerprints, receipt verification, handoff validation and forbidden wording. A scenario passes only by
returning; an AssertionError or any crash is a failure. A term is TRUE only if every scenario under it passed.

Code terms:

    V1Certified  EvidenceBoundaryPreserved  PriorArtImplemented  GapAnalysisImplemented  HypothesisLifecycleSafe
    CandidateLifecycleSafe  SimulationReproducible  SensitivityImplemented  OptimizationVerified
    DiscoveryVerifierPassing  CostLedgerComplete  DiscoveryReceiptsReproducible  MCPContractsStable
    ProviderAbstractionStable  AdversarialSuitePassing  E2ECertificationPassing  V3HandoffValidated

The process terms (V2RegressionPassing, V1BoundaryCertified, PackageGatePassing, GitHubCIPassing,
WorkingTreeClean, ExactSHAPinned) are evaluated by scripts/v2_gate.py, the only place that prints V2ReadyForV3.
"""

from __future__ import annotations

import copy
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable

from .. import __version__
from ..certification import AGREE_AND_CONFLICT, OBJECTIVE, SCOPED, STALE, SYNDICATED, _docs, _run, run_certification
from ..evidence.graph import EvidenceGraph
from ..kernel.knowledge_map import export_knowledge_map
from ..models.provider import HeuristicProvider
from . import fixtures as F
from .context import DiscoveryContext
from .errors import (
    DiscoveryError,
    FalseNovelty,
    MalformedInput,
    NonFiniteValue,
    PromotionRefused,
    UnitMismatch,
    UnknownReference,
)
from .expr import Relation, Unit, Var
from .handoff import validate_handoff
from .optimize import optimize, problem_from_json, verify_solution
from .pipeline import discover_from_run, run_discovery
from .receipt import RECEIPT_SCHEMA, objects_match_receipt, verify_discovery_receipt
from .report import discovery_summary, render_discovery_markdown
from .types import (
    CandidateStatus,
    DiscoveryFinding,
    DiscoveryOutcome,
    FindingKind,
    GapBasis,
    Hypothesis,
    HypothesisStatus,
    OptimizationResult,
    OptimizationStatus,
    PriorArtConclusion,
    Robustness,
    SEARCH_ABSENCE_MAX_CONFIDENCE,
    UncertaintyReason,
)
from .verifier import verify_discovery

CODE_TERMS = ("V1Certified", "EvidenceBoundaryPreserved", "PriorArtImplemented", "GapAnalysisImplemented",
              "HypothesisLifecycleSafe", "CandidateLifecycleSafe", "SimulationReproducible", "SensitivityImplemented",
              "OptimizationVerified", "DiscoveryVerifierPassing", "CostLedgerComplete", "DiscoveryReceiptsReproducible",
              "MCPContractsStable", "ProviderAbstractionStable", "AdversarialSuitePassing", "E2ECertificationPassing",
              "V3HandoffValidated")
PROCESS_TERMS = ("V2RegressionPassing", "V1BoundaryCertified", "PackageGatePassing", "GitHubCIPassing",
                 "WorkingTreeClean", "ExactSHAPinned")
GATE_ORDER = CODE_TERMS + PROCESS_TERMS
FORBIDDEN_NOVELTY = re.compile(r"\b(novel|unprecedented|never[\s-]+attempted|first[\s-]+of[\s-]+its[\s-]+kind)\b", re.I)


@dataclass
class V2Check:
    term: str
    scenario: str
    passed: bool
    detail: str


_SCENARIOS: list[tuple[str, str, Callable[[], str]]] = []


def scenario(term: str, name: str):
    def register(fn):
        _SCENARIOS.append((term, name, fn))
        return fn
    return register


def refused(fn: Callable[[], object], *errors: type[BaseException]) -> BaseException:
    try:
        fn()
    except errors as exc:
        return exc
    raise AssertionError(f"accepted; expected {' or '.join(e.__name__ for e in errors)}")


_CACHE: dict[str, object] = {}


def _get(key: str, build: Callable[[], object]):
    if key not in _CACHE:
        _CACHE[key] = build()
    return _CACHE[key]


def _phoenix():
    return _get("phoenix", lambda: _run(OBJECTIVE, _docs(AGREE_AND_CONFLICT)))


def _business():
    return _get("business", lambda: discover_from_run(_phoenix(), "Choose a warehouse size (fictional)",
                                                      design=F.warehouse_design(), at=F.AT))


def _rendered(result) -> str:
    return render_discovery_markdown(result) + json.dumps(discovery_summary(result)) + json.dumps(result.handoff or {})


def _artifacts_sound(result) -> None:
    """What every scenario checks: a verifying receipt that matches its objects, no novelty wording, a valid handoff
    exactly when a candidate was selected, and simulations that never read as observations."""
    assert verify_discovery_receipt(result.receipt), "the receipt does not verify"
    assert not objects_match_receipt(result.receipt, result.context.objects()), "the receipt and objects disagree"
    m = FORBIDDEN_NOVELTY.search(_rendered(result))
    assert m is None, f"novelty wording in output: {m.group(0) if m else ''}"
    assert (result.handoff is not None) == (result.outcome == DiscoveryOutcome.CANDIDATE_SELECTED)
    if result.handoff is not None:
        problems = validate_handoff(result.handoff, result.receipt, result.context)
        assert not problems, problems
    for f in result.findings:
        if f.kind == FindingKind.SIMULATION_RESULT:
            assert not re.search(r"\b(observed|measured|verified)\b", f.statement, re.I), f.statement


# ---- E2E: the twelve scenarios -----------------------------------------------------

@scenario("E2ECertificationPassing", "1 scientific reasoning: competing mechanisms")
def e_scientific() -> str:
    r = discover_from_run(_phoenix(), "Explain the conflicting construction reports (fictional)", at=F.AT)
    cx = r.framed.frame.contradiction_ids
    assert cx, "the fixture has contradictions"
    for c in cx:
        explaining = [h for h in r.hypotheses.hypotheses if c in h.originating and h.origin == "contradiction-explaining"]
        assert len(explaining) >= 2, f"{c} has {len(explaining)} explanations; at least two are required"
    assert r.hypotheses.counters, "no counter-hypothesis"
    assert r.outcome in (DiscoveryOutcome.REQUIRES_RESEARCH, DiscoveryOutcome.CONTRADICTED), r.outcome
    _artifacts_sound(r)
    return (f"{len(cx)} contradictions, each with >= 2 competing explanations; {len(r.hypotheses.counters)} "
            f"counter-hypotheses; outcome {r.outcome.value}")


@scenario("E2ECertificationPassing", "2 technical engineering: constraints and simulation")
def e_engineering() -> str:
    run = _get("sensor", F.sensor_run)
    cid = next(iter(run.graph.claims))
    r = discover_from_run(run, "Size the warehouse cooling (fictional)", design=F.engineering_design(cid), at=F.AT)
    by = {c.description: c for c in r.candidates.candidates}
    assert by["Install 20 kW of cooling (fictional)"].status == CandidateStatus.INFEASIBLE
    assert r.selected and r.selected.description == "Install 40 kW of cooling (fictional)", r.selected
    assert cid in r.selected.evidence_support, "the measured temperature is not cited as the fact it is"
    spec = {s["name"]: s for s in r.handoff["specifications"]}
    assert spec["temp"]["source_id"] == cid and spec["temp"]["value"] == 42.0, spec["temp"]
    _artifacts_sound(r)
    return "20 kW breaks the target; 40 kW selected; temp is the verified 42 degree C claim, cited in the handoff"


@scenario("E2ECertificationPassing", "3 business and economics: break-even and sensitivity")
def e_business() -> str:
    r = _business()
    sens = {s.candidate_id: s for s in r.candidates.sensitivities}[r.selected.id]
    rent = sens.break_even["rent"]["value"]
    assert abs(rent - 8 / 0.85) < 1e-3, rent  # profit = sqft*(rent*occupancy - build_cost) = 0
    assert abs(sens.break_even["occupancy"]["value"] - 8 / 12) < 1e-3
    assert r.selected.description == "Build 150000 sqft (fictional)"
    _artifacts_sound(r)
    return f"break-even rent {rent:.4f} (= 8 / 0.85) and occupancy 0.6667 (= 8 / 12); 150000 sqft selected"


@scenario("E2ECertificationPassing", "4 geospatial and physical: scope preserved")
def e_geospatial() -> str:
    run = _get("scoped", lambda: _run(OBJECTIVE, _docs(SCOPED)))
    r = discover_from_run(run, "Compare the reported construction volumes (fictional)", at=F.AT)
    frame = r.framed.frame.scope
    for h in r.hypotheses.all:
        assert (h.scope.valid_from, h.scope.valid_to, h.scope.geography) == \
               (frame.valid_from, frame.valid_to, frame.geography), f"{h.id} changed scope"
    assert "scope-mismatch" not in r.verifier.codes()
    _artifacts_sound(r)
    return f"{len(r.hypotheses.all)} hypotheses keep the frame scope ({r.framed.frame.scope_note[:60]})"


@scenario("E2ECertificationPassing", "5 software and system design: dependencies and handoff specs")
def e_software() -> str:
    run = _get("software", lambda: _run("Can the order service handle more traffic?", _docs(F.SOFTWARE_DOCS)))
    r = discover_from_run(run, "Size the order service (fictional)", design=F.software_design(), at=F.AT)
    assert r.selected.description == "Run 2 instances (fictional)", r.selected
    h = r.handoff
    assert h["dependencies"] == ["Load balancer in front of the instances", "Shared session store"]
    assert {s["name"] for s in h["specifications"]} >= {"instances", "load", "slo_ms"}
    assert any(c["name"] == "latency within objective" for c in h["acceptance_criteria"])
    _artifacts_sound(r)
    return "2 instances meet the 200 ms objective at peak; dependencies, specs and acceptance criteria handed off"


@scenario("E2ECertificationPassing", "6 resource optimization: provably optimal")
def e_resource() -> str:
    r = discover_from_run(_phoenix(), "Plan production (fictional)", design=F.resource_design(), at=F.AT)
    o = r.candidates.optimization
    assert o.status == OptimizationStatus.OPTIMAL and o.verified and o.solution == {"x": 2.0, "y": 6.0}, o
    assert "reduced costs" in o.proof
    assert r.selected.description.startswith("Optimized"), r.selected
    _artifacts_sound(r)
    return f"simplex optimum x=2, y=6, objective {o.objective_value:g}, certificate recorded, re-checked, selected"


@scenario("E2ECertificationPassing", "7 contradictory evidence")
def e_contradicted() -> str:
    run = _get("contra", lambda: _run("Is warehouse vacancy in Tucson rising?", _docs(F.CONTRADICTED_DOCS)))
    r = discover_from_run(run, "Decide whether to enter Tucson (fictional)", design=F.warehouse_design(), at=F.AT)
    assert r.outcome == DiscoveryOutcome.CONTRADICTED and r.handoff is None, r.outcome
    assert len([h for h in r.hypotheses.hypotheses if h.origin == "contradiction-explaining"]) >= 2
    _artifacts_sound(r)
    return "contested evidence and nothing known: outcome contradicted, competing explanations, no handoff"


@scenario("E2ECertificationPassing", "8 insufficient evidence")
def e_insufficient() -> str:
    run = _get("empty", lambda: _run("Is warehouse vacancy in Tucson rising?", _docs(F.EMPTY_DOCS)))
    r = discover_from_run(run, "Decide whether to enter Tucson (fictional)", design=F.warehouse_design(), at=F.AT)
    assert r.outcome == DiscoveryOutcome.INSUFFICIENT_EVIDENCE and r.handoff is None
    assert not r.hypotheses.all and not r.candidates.candidates, "work continued as if evidence existed"
    _artifacts_sound(r)
    return "nothing known or uncertain: insufficient_evidence, no hypotheses, no candidates, no handoff"


@scenario("E2ECertificationPassing", "9 no feasible solution")
def e_infeasible() -> str:
    r = discover_from_run(_phoenix(), "Choose a warehouse size (fictional)", design=F.infeasible_design(), at=F.AT)
    assert r.outcome == DiscoveryOutcome.INFEASIBLE and r.handoff is None, r.outcome
    o = r.candidates.optimization
    assert o.status == OptimizationStatus.INFEASIBLE and any("minimal infeasible set" in v for v in o.violations)
    _artifacts_sound(r)
    return f"every candidate breaks the budget; the plan is infeasible ({o.violations[0][:70]})"


@scenario("E2ECertificationPassing", "10 prior-art heavy")
def e_prior_art() -> str:
    r = discover_from_run(_phoenix(), "Rail-served cold storage (fictional)", prior_art=F.PRIOR_ART_MATCHING, at=F.AT)
    a = r.prior_art[0]
    assert a.conclusion == PriorArtConclusion.MATCH_FOUND and a.statement.startswith("Matching prior art found")
    assert any(h.origin == "cross-domain-transfer" for h in r.hypotheses.hypotheses)
    _artifacts_sound(r)
    return f"{len(a.matches)} match cited; transfer hypotheses drawn from it"


@scenario("E2ECertificationPassing", "11 apparently novel")
def e_novel() -> str:
    r = discover_from_run(_phoenix(), "Drone-loaded warehouses (fictional)", prior_art=F.PRIOR_ART_NONE, at=F.AT)
    a = r.prior_art[0]
    assert a.conclusion == PriorArtConclusion.NO_MATCH_WITHIN_COVERAGE
    assert a.statement.endswith("this does not establish novelty."), a.statement
    _artifacts_sound(r)
    return "no match within coverage, stated with its coverage; no novelty wording anywhere in the output"


@scenario("E2ECertificationPassing", "12 sensitivity-fragile")
def e_fragile() -> str:
    r = discover_from_run(_phoenix(), "Choose a lease (fictional)", design=F.fragile_design(), at=F.AT)
    big = next(c for c in r.candidates.candidates if c.description.startswith("Lease 10000000"))
    assert big.robustness == Robustness.FRAGILE and big.expected_value > r.selected.expected_value
    assert r.decision.alternatives[big.id] == "robustness fragile is below moderate"
    _artifacts_sound(r)
    return "the higher-value lease is fragile and not selected under the default rule; the robust one is"


# ---- term scenarios ------------------------------------------------------------------

@scenario("V1Certified", "V1 certification")
def t_v1() -> str:
    cert = run_certification()
    assert cert["v1_ready"], [s["scenario"] for s in cert["scenarios"] if not s["passed"]]
    return f"{len(cert['scenarios'])}/{len(cert['scenarios'])} V1 scenarios; V1Ready = TRUE"


@scenario("EvidenceBoundaryPreserved", "V1 state is read-only from V2 (invariant 20)")
def t_read_only() -> str:
    run = _run(OBJECTIVE, _docs(AGREE_AND_CONFLICT))
    before = (json.dumps(run.graph.to_json(), sort_keys=True), json.dumps(run.receipt, sort_keys=True, default=str))
    discover_from_run(run, "Choose a warehouse size (fictional)", design=F.warehouse_design(), at=F.AT)
    after = (json.dumps(run.graph.to_json(), sort_keys=True), json.dumps(run.receipt, sort_keys=True, default=str))
    assert before == after, "discovery changed the V1 run"
    return "graph and receipt byte-identical after a full discovery"


@scenario("EvidenceBoundaryPreserved", "no V2 object becomes evidence (invariants 1, 2, 3)")
def t_no_promotion() -> str:
    from . import promote

    r = _business()
    refused(lambda: promote(r.hypotheses.all[0]), PromotionRefused)
    refused(lambda: promote(r.selected), PromotionRefused)
    refused(lambda: EvidenceGraph().add_claim(r.selected), TypeError)
    assert "verified" not in {s.value for s in HypothesisStatus}

    class Fabricator(HeuristicProvider):
        def propose_hypotheses(self, frame: dict) -> list[dict]:
            return [{"statement": "Rents doubled in 2026 (fictional)", "evidence": "EV-fabricated0",
                     "claim_ids": ["CL-fabricated"], "confidence": 0.99}]

    ctx = DiscoveryContext(export_knowledge_map(_phoenix()), _phoenix().receipt)
    evidence = {e["id"] for e in ctx.entities("evidence")}
    res = run_discovery(ctx, "Choose a warehouse size (fictional)", provider=Fabricator(), at=F.AT)
    h = next(h for h in res.hypotheses.hypotheses if h.origin == "provider:heuristic")
    assert not h.supporting_claim_ids and h.status == HypothesisStatus.REQUIRES_RESEARCH, h
    assert {e["id"] for e in ctx.entities("evidence")} == evidence
    assert not any("fabricated" in json.dumps(o.to_dict()) for o in res.objects)
    return "promotion refused; a candidate cannot enter the graph; provider 'evidence' and ids are ignored"


@scenario("EvidenceBoundaryPreserved", "missing evidence and copies (invariants 4, 5)")
def t_missing_and_copies() -> str:
    r = discover_from_run(_phoenix(), "Drone-loaded warehouses (fictional)", prior_art=F.PRIOR_ART_NONE, at=F.AT)
    absence = [g for g in r.gaps.gaps if g.basis == GapBasis.SEARCH_ABSENCE]
    assert absence and all(g.confidence <= SEARCH_ABSENCE_MAX_CONFIDENCE for g in absence)
    refused(lambda: Hypothesis("x", supporting_claim_ids=["UNK-1"]), MalformedInput)
    synd = _run("Is industrial vacancy in the Phoenix metro falling?", _docs(SYNDICATED))
    s = discover_from_run(synd, "Act on vacancy (fictional)", at=F.AT)
    assert not s.framed.known_facts, "copies produced a known fact"
    return "search absence capped at 0.4; unknowns never stand as evidence; syndicated copies stay uncertain"


@scenario("PriorArtImplemented", "conclusions and coverage")
def t_prior_art() -> str:
    m = discover_from_run(_phoenix(), "x (fictional)", prior_art=F.PRIOR_ART_MATCHING, at=F.AT).prior_art[0]
    n = discover_from_run(_phoenix(), "y (fictional)", prior_art=F.PRIOR_ART_NONE, at=F.AT).prior_art[0]
    assert (m.conclusion, n.conclusion) == (PriorArtConclusion.MATCH_FOUND, PriorArtConclusion.NO_MATCH_WITHIN_COVERAGE)
    assert n.limitations and "does not establish novelty" in n.statement
    refused(lambda: check_subject("a first-ever design"), FalseNovelty)
    return "match_found cites matches; no_match states coverage and disclaims novelty; novelty claims refused"


def check_subject(subject: str):
    from .types import check_no_novelty_claim

    return check_no_novelty_claim(subject, "subject")


@scenario("GapAnalysisImplemented", "gaps from unknowns, contradictions and search absence")
def t_gaps() -> str:
    r = discover_from_run(_phoenix(), "Drone-loaded warehouses (fictional)", prior_art=F.PRIOR_ART_NONE, at=F.AT)
    bases = {g.basis for g in r.gaps.gaps}
    assert {GapBasis.UNKNOWN, GapBasis.CONTRADICTION, GapBasis.SEARCH_ABSENCE} <= bases, bases
    return f"{len(r.gaps.gaps)} gaps with bases {sorted(b.value for b in bases)}"


@scenario("HypothesisLifecycleSafe", "lifecycle, counters and requirements")
def t_hypotheses() -> str:
    r = _business()
    for h in r.hypotheses.all:
        assert h.confidence_kind.value == "hypothesis" and h.status.value != "verified"
        assert h.predicted_observations, f"{h.id} predicts nothing testable"
    strong = [h for h in r.hypotheses.hypotheses if (h.provisional_score or 0) >= 0.5]
    assert all(any(c.counters == h.id for c in r.hypotheses.counters) for h in strong), "a strong hypothesis has no counter"
    h = r.hypotheses.hypotheses[0]
    if h.status == HypothesisStatus.REQUIRES_RESEARCH:
        refused(lambda: h.transition(HypothesisStatus.SURVIVES), DiscoveryError)
    return f"{len(r.hypotheses.all)} hypotheses: no verified state, every one testable, every strong one countered"


@scenario("CandidateLifecycleSafe", "statuses, dominance and the explicit rule")
def t_candidates() -> str:
    r = _business()
    statuses = {c.description: c.status for c in r.candidates.candidates}
    assert statuses["Build 250000 sqft (fictional)"] == CandidateStatus.INFEASIBLE
    assert statuses["Build 100000 sqft (fictional)"] == CandidateStatus.DOMINATED
    assert r.decision.rule.startswith("maximize expected_value")
    assert r.receipt["decision_rule"] == r.decision.rule
    for c in r.candidates.candidates:
        assert not hasattr(c, "score"), "measures were combined into one score"
    return "infeasible never selected, dominated named, rule recorded in the receipt, measures kept separate"


@scenario("SimulationReproducible", "same seed, same summary")
def t_simulation() -> str:
    a = discover_from_run(_phoenix(), "Choose a warehouse size (fictional)", design=F.warehouse_design(), at=F.AT)
    b = discover_from_run(_phoenix(), "Choose a warehouse size (fictional)", design=F.warehouse_design(), at=F.AT)
    sa = [json.dumps(s.outcomes, sort_keys=True) for s in a.candidates.simulations]
    sb = [json.dumps(s.outcomes, sort_keys=True) for s in b.candidates.simulations]
    assert sa == sb, "same inputs gave different summaries"
    c = discover_from_run(_phoenix(), "Choose a warehouse size (fictional)",
                          design=F.warehouse_design(simulation={"seed": 12, "iterations": 500}), at=F.AT)
    assert [json.dumps(s.outcomes, sort_keys=True) for s in c.candidates.simulations] != sa, "the seed had no effect"
    assert all(s.kind == "simulated" for s in a.candidates.simulations)
    refused(lambda: DiscoveryFinding("Profit was observed at 1 USD", FindingKind.SIMULATION_RESULT, ["SIM-1"]),
            MalformedInput)
    return "byte-identical summaries for the same seed; a different seed differs; never described as observed"


@scenario("SensitivityImplemented", "elasticity, break-even, robustness")
def t_sensitivity() -> str:
    r = _business()
    s = {x.candidate_id: x for x in r.candidates.sensitivities}[r.selected.id]
    assert s.elasticities and s.high_sensitivity and s.robust_ranges
    assert s.robustness in (Robustness.MODERATE, Robustness.ROBUST)
    f = discover_from_run(_phoenix(), "Choose a lease (fictional)", design=F.fragile_design(), at=F.AT)
    assert any(c.robustness == Robustness.FRAGILE for c in f.candidates.candidates)
    return f"elasticities {sorted(s.elasticities)}; break-even points; a fragile lease detected"


@scenario("OptimizationVerified", "solvers, proofs and the independent re-check")
def t_optimization() -> str:
    ctx = DiscoveryContext(export_knowledge_map(_phoenix()), _phoenix().receipt)
    V, K, add, mul, rel = F.V, F.K, F.add, F.mul, F.rel

    def solve(d):
        return optimize(ctx, ctx.ensure(problem_from_json(d, F.AT)), at=F.AT)

    lp = F.resource_design()["optimization"]
    assert solve(lp).status == OptimizationStatus.OPTIMAL
    assert solve({**lp, "constraints": lp["constraints"] + [rel(add(V("x"), V("y")), ">=", K(100))]}).status \
        == OptimizationStatus.INFEASIBLE
    unb = {"variables": [{"name": "x", "lower": 0}], "objective": V("x"), "direction": "maximize",
           "constraints": [rel(V("x"), ">=", K(1))]}
    assert solve(unb).status == OptimizationStatus.UNBOUNDED
    nl = {"variables": [{"name": "x", "lower": 0, "upper": 4}], "objective": mul(V("x"), F.sub(K(4), V("x"))),
          "direction": "maximize"}
    assert solve(nl).status == OptimizationStatus.FEASIBLE, "a grid search was labelled optimal"
    problem = ctx.ensure(problem_from_json(lp, F.AT))
    ok, violations, _ = verify_solution(problem, {"x": 4.0, "y": 6.0})
    assert not ok and violations, "an infeasible solution passed the re-check"
    refused(lambda: OptimizationResult(problem.id, "optimal", solution={"x": 1.0, "y": 1.0}, verified=True),
            MalformedInput)
    return "optimal (certificate), infeasible, unbounded, grid feasible-only; re-check rejects a bad solution"


@scenario("DiscoveryVerifierPassing", "the verifier finds what it must, and only lowers")
def t_verifier() -> str:
    r = _business()
    assert "missing-evidence" in r.verifier.codes() or all(h.supporting_claim_ids for h in r.hypotheses.all)
    for i in r.verifier.issues:
        assert not i.lowered or i.lowered.split(" -> ")[1] in ("challenged", "requires_research", "infeasible",
                                                               "refuted_by_analysis"), i
    ctx = DiscoveryContext(export_knowledge_map(_phoenix()), _phoenix().receipt)
    laundered = Hypothesis("A laundered idea (fictional)", supporting_claim_ids=["CL-notinmap0"], created_at=F.AT)
    a = Hypothesis("Idea A (fictional)", created_at=F.AT)
    b = Hypothesis("Idea B (fictional)", derived_from=[a.id], created_at=F.AT)
    object.__setattr__(a, "derived_from", [b.id])
    rep = verify_discovery(ctx, None, [laundered, a, b], texts=[("X", "an unprecedented design")])
    assert {"evidence-laundering", "circular-reasoning", "false-novelty"} <= rep.codes(), rep.codes()
    return f"laundering, cycles and novelty caught; {len(r.verifier.issues)} issues on the business run, all lowering"


@scenario("CostLedgerComplete", "every cost traces to an operation (invariant 14)")
def t_ledger() -> str:
    r = _business()
    led = r.receipt["ledger"]
    assert led["entries"] and abs(sum(e["usd"] for e in led["entries"]) - led["total_usd"]) < 1e-9
    assert {e["kind"] for e in led["entries"]} >= {"compute", "hypothesis_generation", "simulation", "verification"}
    bad = copy.deepcopy(r.receipt)
    bad["ledger"]["entries"][0]["usd"] += 1
    assert not verify_discovery_receipt(bad)
    refused(lambda: r.ledger.record("x", "lunch", "me", 1.0), MalformedInput)
    return f"{len(led['entries'])} entries sum to the total; unknown kinds refused; edits break the receipt"


@scenario("DiscoveryReceiptsReproducible", "fingerprints and tampering (invariant 15)")
def t_receipts() -> str:
    a = discover_from_run(_phoenix(), "Choose a warehouse size (fictional)", design=F.warehouse_design(), at=F.AT)
    b = discover_from_run(_phoenix(), "Choose a warehouse size (fictional)", design=F.warehouse_design(),
                          at="2026-09-30T18:00:00+00:00")
    assert a.receipt["schema"] == RECEIPT_SCHEMA
    assert a.receipt["discovery_fingerprint"] == b.receipt["discovery_fingerprint"], "not reproducible"
    for field in ("objective", "outcome", "decision_rule", "selected_candidate_id", "config", "seeds"):
        t = copy.deepcopy(a.receipt)
        t[field] = "tampered" if not isinstance(t[field], dict) else {**t[field], "x": 1}
        assert not verify_discovery_receipt(t), f"tampering {field} went undetected"
    return "identical fingerprints at different times; tampering any field fails verification"


@scenario("MCPContractsStable", "V1 tools unchanged, V2 tools typed")
def t_mcp() -> str:
    from ..mcp.server import ALIASES, CONTRACT, DISCOVERY_TOOLS, TOOLS, Server

    v1 = ["compile_objective", "plan_research", "investigate", "verify_claim", "get_finding", "find_contradictions",
          "find_gaps", "trace_claim", "get_receipt", "export_state", "render_report", "satellite_passes", "pricing"]
    assert [t["name"] for t in TOOLS[:len(v1)]] == v1, "a V1 tool changed"
    assert ALIASES == {"compile_intent": "compile_objective", "estimate_cost": "plan_research"}
    assert CONTRACT == "lofgren.mcp/2"
    assert all("inputSchema" in t for t in DISCOVERY_TOOLS)
    srv = Server()

    def call(name, args):
        return srv.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name,
                                                                                          "arguments": args}})["result"]

    run = call("investigate", {"objective": OBJECTIVE, "texts": AGREE_AND_CONFLICT})["structuredContent"]["run_id"]
    d = call("discover", {"run_id": run, "objective": "Choose a warehouse size (fictional)",
                          "design": F.warehouse_design()})
    sc = d["structuredContent"]
    assert not d["isError"] and sc["kind"] == "discovery" and sc["outcome"] == "candidate_selected"
    for name in ("generate_hypotheses", "generate_candidates", "verify_discovery", "get_discovery_receipt",
                 "create_v3_handoff"):
        out = call(name, {"discovery_id": sc["discovery_id"]})
        assert not out["isError"] and "kind" in out["structuredContent"], name
    assert call("get_discovery_receipt", {"discovery_id": "DR-none"})["isError"]
    return f"13 V1 tools unchanged; {len(DISCOVERY_TOOLS)} V2 tools typed; discover end to end; errors are isError"


@scenario("ProviderAbstractionStable", "provider choice never changes the domain schema (invariant 16)")
def t_providers() -> str:
    class Canned(HeuristicProvider):
        name, version = "canned", "test"

        def propose_hypotheses(self, frame):
            return [{"statement": "Rail access raises occupancy (fictional)", "mechanism": "logistics"}]

    class Failing(HeuristicProvider):
        name, version = "failing", "test"

        def propose_hypotheses(self, frame):
            raise RuntimeError("provider down")

    shapes = []
    for p in (HeuristicProvider(), Canned(), Failing()):
        r = discover_from_run(_phoenix(), "Choose a warehouse size (fictional)", design=F.warehouse_design(),
                              provider=p, at=F.AT)
        shapes.append((sorted({k for h in r.hypotheses.all for k in h.to_dict()}),
                       sorted({k for c in r.candidates.candidates for k in c.to_dict()}), r.outcome.value))
        assert r.receipt["provider"]["name"] == p.name
    assert shapes[0] == shapes[1] == shapes[2], "the domain schema depends on the provider"
    return "heuristic, canned and failing providers give the same domain schema and outcome; metadata in the receipt"


@scenario("V3HandoffValidated", "valid, refused when tampered, absent without a selection (invariant 19)")
def t_handoff() -> str:
    r = _business()
    assert not validate_handoff(r.handoff, r.receipt, r.context)
    tampers = {
        "hypothesis as evidence": lambda h: h["verified_evidence"].append(
            {"claim_id": r.hypotheses.all[0].id, "kind": "hypothesis"}),
        "hidden number": lambda h: h["risks"].append("costs could rise by 37 percent"),
        "non-evaluable criterion": lambda h: h["acceptance_criteria"].append({"name": "x", "relation": "profit > 0"}),
        "fingerprint mismatch": lambda h: h.update(discovery_fingerprint="DFP-0"),
        "unselectable candidate": lambda h: h["selected_candidate"].update(status="dominated"),
    }
    for name, edit in tampers.items():
        h = copy.deepcopy(r.handoff)
        edit(h)
        assert validate_handoff(h, r.receipt, r.context), f"{name} accepted"
    assert _get("empty_r", lambda: discover_from_run(_run("Is warehouse vacancy in Tucson rising?", _docs(F.EMPTY_DOCS)),
                                                     "x (fictional)", at=F.AT)).handoff is None
    return "valid handoff accepted; five tampered handoffs refused; none without a selection"


# ---- adversarial (directive section 22) --------------------------------------------------

@scenario("AdversarialSuitePassing", "conflicting, copied and outdated sources")
def a_sources() -> str:
    c = discover_from_run(_run("Is warehouse vacancy in Tucson rising?", _docs(F.CONTRADICTED_DOCS)), "x (fictional)",
                          at=F.AT)
    assert c.outcome == DiscoveryOutcome.CONTRADICTED
    s = discover_from_run(_run("Is industrial vacancy in the Phoenix metro falling?", _docs(SYNDICATED)), "x (fictional)",
                          at=F.AT)
    assert not s.framed.known_facts
    o = discover_from_run(_run(OBJECTIVE, _docs(STALE)), "x (fictional)", at=F.AT)
    assert any(u.reason == UncertaintyReason.STALE for u in o.framed.uncertainties), "the stale claim is not marked"
    assert not [f for f in o.framed.known_facts if "2019" in f.statement and "according" not in f.statement]
    return "conflict -> contradicted; copies verify nothing; a stale factual claim is not a known fact"


@scenario("AdversarialSuitePassing", "units, citations and impossible constraints")
def a_inputs() -> str:
    d = F.warehouse_design(distributions={})
    d["constraints"][0]["variable_units"] = {"sqft": "sqft", "max_sqft": "usd"}
    refused(lambda: discover_from_run(_phoenix(), "x (fictional)", design=d, at=F.AT), UnitMismatch)
    refused(lambda: Unit.parse("furlong per ????"), MalformedInput)
    d = F.warehouse_design()
    d["candidates"][0]["evidence_support"] = ["CL-fabricated"]
    refused(lambda: discover_from_run(_phoenix(), "x (fictional)", design=d, at=F.AT), UnknownReference)
    r = discover_from_run(_phoenix(), "x (fictional)", design=F.infeasible_design(), at=F.AT)
    assert r.outcome == DiscoveryOutcome.INFEASIBLE
    return "wrong and ambiguous units fail closed; a fabricated citation is refused; impossible constraints -> infeasible"


@scenario("AdversarialSuitePassing", "unsupported, circular and falsely novel ideas")
def a_ideas() -> str:
    d = F.warehouse_design()
    d["candidates"][1]["description"] = "The world's first warehouse of its size (fictional)"
    refused(lambda: discover_from_run(_phoenix(), "x (fictional)", design=d, at=F.AT), FalseNovelty)
    d = F.warehouse_design(distributions={"occupancy": {"dist": "cauchy", "x0": 0, "gamma": 1}})
    r = discover_from_run(_phoenix(), "x (fictional)", design=d, at=F.AT)
    assert r.outcome == DiscoveryOutcome.UNSUPPORTED and r.handoff is None
    refused(lambda: Relation.from_json({"lhs": Var("x").to_json(), "op": "<", "rhs": Var("y").to_json()}),
            MalformedInput)
    return "novelty claims refused; an unsupported distribution -> unsupported outcome; malformed relations refused"


@scenario("AdversarialSuitePassing", "weak, empty and falsely confident evidence")
def a_weak() -> str:
    e = discover_from_run(_run("Is warehouse vacancy in Tucson rising?", _docs(F.EMPTY_DOCS)), "x (fictional)", at=F.AT)
    assert e.outcome == DiscoveryOutcome.INSUFFICIENT_EVIDENCE
    ctx = DiscoveryContext(export_knowledge_map(_phoenix()), _phoenix().receipt)
    contested = ctx.claims("contradicted")[0]
    d = F.warehouse_design()
    d["facts"] = {"rent_fact": contested["id"]}
    refused(lambda: run_discovery(ctx, "x (fictional)", design=d, at=F.AT), PromotionRefused, MalformedInput)
    return "empty map -> insufficient_evidence; a contested claim cannot set a design value"


@scenario("AdversarialSuitePassing", "malformed and tampered receipts; non-finite input")
def a_receipts() -> str:
    r = _business()
    assert not verify_discovery_receipt({"schema": RECEIPT_SCHEMA})
    assert not verify_discovery_receipt("not a receipt")
    t = copy.deepcopy(r.receipt)
    t["objects"][next(iter(t["objects"]))]["digest"] = "0" * 64
    assert not verify_discovery_receipt(t)
    d = F.warehouse_design()
    d["assumptions"][0]["value"] = float("nan")
    refused(lambda: discover_from_run(_phoenix(), "x (fictional)", design=d, at=F.AT), NonFiniteValue, MalformedInput)
    return "malformed and tampered receipts fail; NaN input fails closed"


@scenario("AdversarialSuitePassing", "future-dated evidence never supports discovery")
def a_future() -> str:
    import csv
    import tempfile
    from pathlib import Path

    from ..adapters import AdapterRegistry, SensorAdapter

    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "sensors.csv"
        with path.open("w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(["timestamp", "sensor_id", "metric", "value", "unit", "calibrated_at"])
            w.writerow(["2031-09-01T00:00:00Z", "t-1", "temperature", "40", "C", "2026-06-01T00:00:00Z"])
            w.writerow(["2031-09-02T00:00:00Z", "t-1", "temperature", "44", "C", "2026-06-01T00:00:00Z"])
        reg = AdapterRegistry()
        reg.register(SensorAdapter([path], authorized=True))
        run = _run("Is warehouse temperature too high for storage?", reg)
    claims = list(run.graph.claims.values())
    assert claims and all(c.status.value != "verified" for c in claims), "future readings verified a claim"
    assert any(i.startswith("future-dated:") for c in claims for i in c.issues)
    r = discover_from_run(run, "Size the warehouse cooling (fictional)", at=F.AT)
    assert not r.framed.known_facts, "discovery treated a future reading as known"
    cid = claims[0].id
    refused(lambda: discover_from_run(run, "x (fictional)", design=F.engineering_design(cid), at=F.AT),
            PromotionRefused, MalformedInput)
    return "readings dated 2031 verify nothing in V1, so discovery has no known fact and cannot bind a design to them"


# ---- runner -----------------------------------------------------------------------------

def _run_one(term: str, name: str, fn: Callable[[], str]) -> V2Check:
    try:
        return V2Check(term, name, True, fn())
    except AssertionError as exc:
        return V2Check(term, name, False, f"failed: {exc}")
    except Exception as exc:  # a crash is a failure, never a pass
        return V2Check(term, name, False, f"error: {type(exc).__name__}: {exc}")


def run_v2_certification() -> dict:
    _CACHE.clear()
    checks = [_run_one(*s) for s in _SCENARIOS]
    terms = {t: bool([c for c in checks if c.term == t]) and all(c.passed for c in checks if c.term == t)
             for t in CODE_TERMS}
    return {"engine_version": __version__,
            "certified_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
            "scenarios": [c.__dict__ for c in checks], "terms": terms, "code_terms_certified": all(terms.values()),
            "process_terms": list(PROCESS_TERMS)}


def render_v2_certification(cert: dict) -> str:
    lines = [f"Lofgren Intelligence {cert['engine_version']} — V2 Discovery certification", ""]
    for term in CODE_TERMS:
        lines.append(term)
        for s in cert["scenarios"]:
            if s["term"] == term:
                lines.append(f"  [{'PASS' if s['passed'] else 'FAIL'}] {s['scenario']}: {s['detail']}")
    lines.append("")
    for term, ok in cert["terms"].items():
        lines.append(f"  {'✓' if ok else '✗'} {term}")
    lines += ["", "Process terms (" + ", ".join(cert["process_terms"]) + ") are evaluated by scripts/v2_gate.py.",
              "V2CodeTerms = " + ("TRUE" if cert["code_terms_certified"] else "FALSE")]
    return "\n".join(lines)
