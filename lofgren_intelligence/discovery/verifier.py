"""The discovery verifier: an adversarial pass over the structured discovery objects only.

It never sees generator reasoning or rendered text, never adds evidence and can only lower standing:

    hypothesis  survives/proposed -> challenged or requires_research
    candidate   viable/selected   -> requires_research or infeasible

Checks (each finding is a `VerifierIssue` with a stable code, the object id and the reason):

    unsupported-assumption   an assumption or constraint source that is not registered here
    missing-evidence         a hypothesis no V1 claim supports (provider ideas, transfers), or a selected candidate
                             resting on such a hypothesis
    false-novelty            any V2 text asserting novelty
    duplicate-candidate      two candidates with the same structure under different wording
    constraint-violation     a viable or selected candidate whose parameters break a constraint (re-evaluated)
    scope-mismatch           a hypothesis whose time or place differs from the frame's without a recorded widening
    unit-mismatch            a constraint or model whose units do not agree (re-checked)
    invalid-calculation      a simulation summary that is not ordered (p5 <= p50 <= p95) or not finite
    simulation-as-fact       a finding that presents a simulation as observed, measured or verified
    optimization-unproven    `optimal` without a proof, or a solution that fails the independent re-check
    circular-reasoning       a cycle in the derived_from / parent graph of the discovery objects
    evidence-laundering      a V1 id cited anywhere that this knowledge map does not contain
    hypothesis-as-fact       a verified-fact finding that rests on anything but a V1 known fact
    citation-gap             a finding whose `rests_on` ids do not resolve here
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Iterable

from .context import DiscoveryContext
from .errors import DiscoveryError, UnitMismatch
from .expr import Unit
from .optimize import verify_solution
from .simulate import check_constraints
from .types import (
    NOVELTY_CLAIMS,
    Assumption,
    Candidate,
    CandidateStatus,
    Constraint,
    DiscoveryFinding,
    FindingKind,
    Hypothesis,
    HypothesisStatus,
    OptimizationProblem,
    OptimizationResult,
    OptimizationStatus,
    ProblemFrame,
    Scenario,
    Simulation,
    _Obj,
)

ISSUE_CODES = ("unsupported-assumption", "missing-evidence", "false-novelty", "duplicate-candidate",
               "constraint-violation", "scope-mismatch", "unit-mismatch", "invalid-calculation", "simulation-as-fact",
               "optimization-unproven", "circular-reasoning", "evidence-laundering", "hypothesis-as-fact",
               "citation-gap")
_V1_ID = re.compile(r"^(CL|EV|SRC|CX|UNK|CALC|F|Q)-[A-Za-z0-9]+$")  # an id, never a sentence
_FACT_WORDS = re.compile(r"\b(observed|measured|verified|confirmed|proven)\b", re.I)


@dataclass(frozen=True)
class VerifierIssue:
    code: str
    object_id: str
    reason: str
    lowered: str = ""  # "old -> new" when the issue lowered a status


@dataclass
class VerifierReport:
    issues: list[VerifierIssue] = field(default_factory=list)
    checked: int = 0

    @property
    def passed(self) -> bool:
        """No issue of any kind. A report with issues is still a valid result: standing was lowered."""
        return not self.issues

    def codes(self) -> set[str]:
        return {i.code for i in self.issues}

    def to_json(self) -> dict:
        return {"checked": self.checked, "issues": [i.__dict__ for i in self.issues]}


def _lower(obj: _Obj, status, report: VerifierReport, code: str, reason: str) -> None:
    old = obj.status
    try:
        obj.transition(status)
        lowered = f"{old.value} -> {obj.status.value}" if obj.status is not old else ""
    except DiscoveryError:
        lowered = ""  # already at or below this standing
    report.issues.append(VerifierIssue(code, obj.id, reason, lowered))


def _point(scenario: Scenario) -> tuple[dict[str, float], dict[str, str]]:
    values = {k: (v["value"] if isinstance(v, dict) else v) for k, v in scenario.parameters.items()}
    units = {k: v.get("unit", "") for k, v in scenario.parameters.items() if isinstance(v, dict)}
    return values, units


def verify_discovery(context: DiscoveryContext, frame: ProblemFrame | None, objects: Iterable[_Obj],
                     *, scenarios: Iterable[Scenario] = (), texts: Iterable[tuple[str, str]] = ()) -> VerifierReport:
    """Check the discovery objects; lower standing where a check fails. `texts` are (object id, text) pairs V2
    asserted (statements, descriptions) to scan for novelty claims."""
    objects = list(objects)
    report = VerifierReport(checked=len(objects))
    by_id = {o.id: o for o in objects}
    scenarios = {s.candidate_id: s for s in scenarios}

    # Evidence laundering and citation gaps: every V1 id must exist in this map.
    for o in objects:
        for name, value in o.to_dict().items():
            refs = value if isinstance(value, list) else [value]
            for r in refs:
                if isinstance(r, str) and _V1_ID.match(r):
                    try:
                        context.reference(r)
                    except DiscoveryError:
                        report.issues.append(VerifierIssue("evidence-laundering", o.id,
                                                           f"{name} cites {r}, which this knowledge map does not contain"))

    # Circular reasoning in the derivation graph.
    edges = {o.id: set(o.derived_from) | set(getattr(o, "parent_ids", [])) for o in objects}
    state: dict[str, int] = {}

    def visit(n: str, path: list[str]) -> None:
        state[n] = 1
        for m in sorted(edges.get(n, ())):
            if state.get(m) == 1:
                cycle = path[path.index(m):] + [m] if m in path else [n, m]
                report.issues.append(VerifierIssue("circular-reasoning", n, " -> ".join(cycle)))
            elif m in edges and state.get(m) is None:
                visit(m, path + [m])
        state[n] = 2

    for n in sorted(edges):
        if state.get(n) is None:
            visit(n, [n])

    for o in objects:
        if isinstance(o, Hypothesis):
            _check_hypothesis(context, frame, o, report)
        elif isinstance(o, Assumption):
            pass
        elif isinstance(o, Constraint):
            if o.source_assumption_id and o.source_assumption_id not in context:
                report.issues.append(VerifierIssue("unsupported-assumption", o.id,
                                                   f"rests on {o.source_assumption_id}, which is not registered"))
            try:
                o.relation.check_units({k: Unit.parse(v) for k, v in o.variable_units.items()})
            except UnitMismatch as exc:
                report.issues.append(VerifierIssue("unit-mismatch", o.id, str(exc)))
        elif isinstance(o, Simulation):
            for metric, s in o.outcomes.items():
                vals = [s.get(k) for k in ("p5", "p50", "p95", "mean")]
                if not all(isinstance(v, (int, float)) and math.isfinite(v) for v in vals) or \
                        not s["p5"] <= s["p50"] <= s["p95"]:
                    report.issues.append(VerifierIssue("invalid-calculation", o.id,
                                                       f"summary of {metric} is not ordered and finite: {s}"))
        elif isinstance(o, OptimizationResult):
            _check_optimization(context, o, report)
        elif isinstance(o, DiscoveryFinding):
            _check_finding(context, o, report)

    candidates = sorted((o for o in objects if isinstance(o, Candidate)), key=lambda c: c.id)
    seen: dict[tuple, str] = {}
    for c in candidates:
        scenario = scenarios.get(c.id)
        if scenario is not None:  # same parameters and hypotheses under different wording is one candidate
            key = (tuple(sorted(c.hypothesis_ids)), tuple(sorted(_point(scenario)[0].items())))
            if key in seen:
                report.issues.append(VerifierIssue("duplicate-candidate", c.id, f"same structure as {seen[key]}"))
            seen.setdefault(key, c.id)
        _check_candidate(context, c, by_id, scenario, report)

    for oid, value in texts:
        m = NOVELTY_CLAIMS.search(value or "")
        if m:
            report.issues.append(VerifierIssue("false-novelty", oid, f"asserts novelty: {m.group(0)!r}"))
    return report


def _check_hypothesis(context: DiscoveryContext, frame: ProblemFrame | None, h: Hypothesis,
                      report: VerifierReport) -> None:
    for a in h.assumptions:
        if a.startswith("ASM-") and a not in context:
            report.issues.append(VerifierIssue("unsupported-assumption", h.id, f"assumes {a}, which is not registered"))
    if not h.supporting_claim_ids and h.status in (HypothesisStatus.PROPOSED, HypothesisStatus.SURVIVES):
        _lower(h, HypothesisStatus.REQUIRES_RESEARCH, report, "missing-evidence",
               f"no V1 claim supports it (origin {h.origin}); its evidence requirements must be met first")
    if frame is not None:
        f, s = frame.scope, h.scope
        differs = (s.valid_from, s.valid_to, (s.geography or "").lower()) != \
                  (f.valid_from, f.valid_to, (f.geography or "").lower())
        if differs and "widen" not in frame.scope_note.lower():
            _lower(h, HypothesisStatus.CHALLENGED, report, "scope-mismatch",
                   f"scope {s.valid_from}..{s.valid_to} {s.geography} differs from the frame's "
                   f"{f.valid_from}..{f.valid_to} {f.geography} without a recorded widening")


def _check_candidate(context: DiscoveryContext, c: Candidate, by_id: dict, scenario: Scenario | None,
                     report: VerifierReport) -> None:
    if c.status not in (CandidateStatus.VIABLE, CandidateStatus.SELECTED):
        return
    weak = [h for h in c.hypothesis_ids if isinstance(by_id.get(h), Hypothesis)
            and not by_id[h].supporting_claim_ids]
    if weak and c.status == CandidateStatus.SELECTED:
        _lower(c, CandidateStatus.REQUIRES_RESEARCH, report, "missing-evidence",
               f"rests on hypotheses no V1 claim supports: {', '.join(weak)}")
        return
    if scenario is None:
        return
    values, units = _point(scenario)
    constraints = [by_id[cid] for cid in c.constraints if isinstance(by_id.get(cid), Constraint)]
    # Re-evaluated at the candidate's own parameters; constraints on simulated outcomes are covered by the
    # simulation's failure shares, so they are not evaluable here and are skipped.
    for cid, ok in check_constraints(constraints, values, {}, units).items():
        if ok is False:
            _lower(c, CandidateStatus.INFEASIBLE, report, "constraint-violation", f"violates {cid} at its parameters")
            return


def _check_optimization(context: DiscoveryContext, o: OptimizationResult, report: VerifierReport) -> None:
    if o.status != OptimizationStatus.OPTIMAL and o.status != OptimizationStatus.FEASIBLE:
        return
    if o.status == OptimizationStatus.OPTIMAL and not o.proof:
        report.issues.append(VerifierIssue("optimization-unproven", o.id, "optimal without a proof"))
    if o.status == OptimizationStatus.OPTIMAL and "grid" in o.proof:
        report.issues.append(VerifierIssue("optimization-unproven", o.id, "a grid search cannot prove optimality"))
    problem = context.get(o.problem_id) if o.problem_id in context else None
    if isinstance(problem, OptimizationProblem):
        ok, violations, _ = verify_solution(problem, o.solution, o.objective_value)
        if not ok:
            report.issues.append(VerifierIssue("optimization-unproven", o.id,
                                               "independent re-check failed: " + "; ".join(violations)))


def _check_finding(context: DiscoveryContext, f: DiscoveryFinding, report: VerifierReport) -> None:
    for r in f.rests_on:
        try:
            context.reference(r)
        except DiscoveryError:
            report.issues.append(VerifierIssue("citation-gap", f.id, f"rests on {r}, which does not resolve here"))
    if f.kind == FindingKind.SIMULATION_RESULT and _FACT_WORDS.search(f.statement):
        report.issues.append(VerifierIssue("simulation-as-fact", f.id, "a simulation is described as fact"))
    if f.kind == FindingKind.VERIFIED_FACT and not all(r.startswith("KF-") for r in f.rests_on):
        report.issues.append(VerifierIssue("hypothesis-as-fact", f.id, "a verified-fact finding rests on non-facts"))
