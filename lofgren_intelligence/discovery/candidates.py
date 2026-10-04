"""Candidates: possible solutions, checked, simulated, stress-tested and chosen by an explicit rule.

A candidate is never invented from prose. It comes from a structured design space (`DesignSpace`): named
parameters with units, an outcome model (structured expressions), constraints that rest on known facts or explicit
assumptions, and optionally an optimization problem whose verified solution becomes one more candidate. A
ReasoningProvider may add design ideas only as text; without parameters they stay `requires_research`.

For every candidate:

    constraints   each one satisfied, violated, or not evaluable (a variable has no value), with the reason
    simulation    the outcome model run on its parameters (Monte Carlo when distributions are declared)
    sensitivity   elasticities, break-even points, failure thresholds and robustness
    measures      technical feasibility, economic feasibility, expected value and robustness, kept separate:
                  technical = share of simulated draws that break none of the *stated* constraints (so 1.0 when
                  none is stated: it never vouches for constraints nobody declared); economic = share that meet the
                  success relation; expected value = mean of the value metric. A measure given in the design
                  space is used when the engine cannot derive it, and says so.

Status: `infeasible` if a constraint is violated at the candidate's own parameters; `requires_research` if a
constraint cannot be evaluated; `dominated` if another viable candidate is at least as good on every measure both
have and strictly better on one (the dominating candidate is named); otherwise `viable`. Order is not ranking.

The decision applies one recorded rule (default: maximize expected value subject to technical feasibility >= 0.5
and robustness >= moderate; ties go to the smallest candidate id). The four measures are never combined into one
score.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping

from .context import DiscoveryContext
from .errors import MalformedInput, UnknownReference
from .expr import Relation
from .frame import FrameResult
from .optimize import optimize, problem_from_json
from .principles import ground_constraint, state_assumption
from .sensitivity import analyze_sensitivity
from .simulate import ExpressionModel, MonteCarloModel, check_constraints, simulate
from .types import (
    Assumption,
    Candidate,
    CandidateStatus,
    Constraint,
    DiscoveryDecision,
    DiscoveryOutcome,
    Hypothesis,
    OptimizationResult,
    OptimizationStatus,
    Robustness,
    Scenario,
    SensitivityResult,
    Simulation,
    check_no_novelty_claim,
    finite,
    integer,
    mapping,
    text,
    texts,
    unit_interval,
    utcnow,
)

ROBUSTNESS_RANK = {Robustness.UNKNOWN: -1, Robustness.FRAGILE: 0, Robustness.MODERATE: 1, Robustness.ROBUST: 2}
MEASURES = ("technical_feasibility", "economic_feasibility", "expected_value", "robustness")
DEFAULT_SEED = 7
DEFAULT_ITERATIONS = 1000
MAX_CANDIDATES = 50


@dataclass(frozen=True)
class DecisionRule:
    maximize: str = "expected_value"
    min_technical_feasibility: float | None = 0.5
    min_economic_feasibility: float | None = None
    min_robustness: Robustness = Robustness.MODERATE

    def __post_init__(self) -> None:
        if self.maximize not in ("expected_value", "technical_feasibility", "economic_feasibility"):
            raise MalformedInput("a decision maximizes expected_value, technical_feasibility or economic_feasibility",
                                 "DecisionRule.maximize")
        unit_interval(self.min_technical_feasibility, "DecisionRule.min_technical_feasibility")
        unit_interval(self.min_economic_feasibility, "DecisionRule.min_economic_feasibility")
        object.__setattr__(self, "min_robustness", Robustness(self.min_robustness))

    @property
    def text(self) -> str:
        parts = []
        if self.min_technical_feasibility is not None:
            parts.append(f"technical_feasibility >= {self.min_technical_feasibility:g}")
        if self.min_economic_feasibility is not None:
            parts.append(f"economic_feasibility >= {self.min_economic_feasibility:g}")
        parts.append(f"robustness >= {self.min_robustness.value}")
        return (f"maximize {self.maximize} among viable candidates subject to {' and '.join(parts)}; "
                "ties go to the smallest candidate id")

    @staticmethod
    def from_json(data: Mapping | None) -> "DecisionRule":
        data = dict(data or {})
        extra = set(data) - {"maximize", "min_technical_feasibility", "min_economic_feasibility", "min_robustness"}
        if extra:
            raise MalformedInput(f"unknown decision rule fields {sorted(extra)}", "DecisionRule")
        return DecisionRule(**data)


def _param(name: str, value: Any) -> tuple[float, str]:
    if isinstance(value, Mapping):
        return finite(value.get("value"), f"parameter {name}", allow_none=False), str(value.get("unit", ""))
    if isinstance(value, (list, tuple)) and len(value) == 2:
        return finite(value[0], f"parameter {name}", allow_none=False), str(value[1])
    return finite(value, f"parameter {name}", allow_none=False), ""


@dataclass
class DesignSpace:
    """The structured inputs a discovery can choose among. Parsed from plain JSON and validated."""

    model: ExpressionModel | None = None
    value_metric: str | None = None
    success: Relation | None = None
    distributions: dict = field(default_factory=dict)
    assumptions: list[dict] = field(default_factory=list)
    constraints: list[dict] = field(default_factory=list)
    candidates: list[dict] = field(default_factory=list)
    optimization: dict | None = None
    decision_rule: DecisionRule = field(default_factory=DecisionRule)
    seed: int = DEFAULT_SEED
    iterations: int = DEFAULT_ITERATIONS

    @staticmethod
    def from_json(data: Mapping | None) -> "DesignSpace":
        data = dict(data or {})
        known = {"model", "value_metric", "success", "distributions", "assumptions", "constraints", "candidates",
                 "optimization", "decision_rule", "simulation"}
        extra = set(data) - known
        if extra:
            raise MalformedInput(f"unknown design-space fields {sorted(extra)}", "DesignSpace")
        model = ExpressionModel.from_json(data["model"]) if data.get("model") else None
        value_metric = data.get("value_metric")
        if value_metric is not None and (model is None or value_metric not in model.outcomes):
            raise MalformedInput("value_metric must name one of the model's outcomes", "DesignSpace.value_metric")
        if model is not None and value_metric is None:
            value_metric = next(iter(model.outcomes))
        success = Relation.from_json(data["success"]) if data.get("success") else None
        sim = mapping(data.get("simulation", {}), "DesignSpace.simulation")
        if set(sim) - {"seed", "iterations"}:
            raise MalformedInput("simulation settings are seed and iterations", "DesignSpace.simulation")
        candidates = data.get("candidates", [])
        if not isinstance(candidates, list) or len(candidates) > MAX_CANDIDATES:
            raise MalformedInput(f"candidates is a list of at most {MAX_CANDIDATES}", "DesignSpace.candidates")
        for key in ("assumptions", "constraints"):
            if not isinstance(data.get(key, []), list):
                raise MalformedInput(f"{key} is a list", f"DesignSpace.{key}")
        if data.get("distributions") and model is None:
            raise MalformedInput("distributions need a model", "DesignSpace.distributions")
        return DesignSpace(model, value_metric, success, mapping(data.get("distributions", {}), "DesignSpace.distributions"),
                           list(data.get("assumptions", [])), list(data.get("constraints", [])), list(candidates),
                           data.get("optimization"), DecisionRule.from_json(data.get("decision_rule")),
                           integer(sim.get("seed", DEFAULT_SEED), "DesignSpace.simulation.seed", 0, 2 ** 63 - 1),
                           integer(sim.get("iterations", DEFAULT_ITERATIONS), "DesignSpace.simulation.iterations", 1,
                                   200_000))


@dataclass
class CandidateResult:
    assumptions: list[Assumption] = field(default_factory=list)
    constraints: list[Constraint] = field(default_factory=list)
    candidates: list[Candidate] = field(default_factory=list)
    scenarios: list[Scenario] = field(default_factory=list)
    simulations: list[Simulation] = field(default_factory=list)
    sensitivities: list[SensitivityResult] = field(default_factory=list)
    sensitivity_tables: dict = field(default_factory=dict)
    optimization: OptimizationResult | None = None
    decision: DiscoveryDecision | None = None
    notes: list[str] = field(default_factory=list)


def ground_design(context: DiscoveryContext, space: DesignSpace, at: str) -> tuple[list[Assumption], list[Constraint]]:
    """Register the design space's assumptions and constraints. A constraint rests on one known fact or one named
    assumption; an uncertain claim cannot ground it (state the value as an assumption instead)."""
    assumptions: list[Assumption] = []
    by_name: dict[str, Assumption] = {}
    for i, a in enumerate(space.assumptions):
        if not isinstance(a, Mapping):
            raise MalformedInput("an assumption is an object", f"DesignSpace.assumptions[{i}]")
        extra = set(a) - {"statement", "why_assumed", "name", "value", "unit"}
        if extra:
            raise MalformedInput(f"unknown assumption fields {sorted(extra)}", f"DesignSpace.assumptions[{i}]")
        asm = state_assumption(context, a.get("statement"), a.get("why_assumed", ""), a.get("name", ""),
                               a.get("value"), a.get("unit", ""), at=at)
        assumptions.append(asm)
        if asm.name:
            by_name[asm.name] = asm
    constraints: list[Constraint] = []
    for i, c in enumerate(space.constraints):
        if not isinstance(c, Mapping):
            raise MalformedInput("a constraint is an object", f"DesignSpace.constraints[{i}]")
        extra = set(c) - {"name", "kind", "relation", "variable_units", "fact", "assumption"}
        if extra:
            raise MalformedInput(f"unknown constraint fields {sorted(extra)}", f"DesignSpace.constraints[{i}]")
        assumption_id = None
        if c.get("assumption") is not None:
            ref = c["assumption"]
            asm = by_name.get(ref) or next((a for a in assumptions if a.id == ref), None)
            if asm is None:
                raise UnknownReference(f"constraint rests on unknown assumption {ref!r}", f"DesignSpace.constraints[{i}]")
            assumption_id = asm.id
        constraints.append(ground_constraint(context, c.get("name"), c.get("kind"), c.get("relation"),
                                             dict(c.get("variable_units", {})), fact=c.get("fact"),
                                             assumption=assumption_id, at=at))
    return assumptions, constraints


def _linked_hypotheses(hypotheses: Iterable[Hypothesis], support: set[str]) -> list[str]:
    return sorted(h.id for h in hypotheses if support & set(h.supporting_claim_ids))


def evaluate_candidates(context: DiscoveryContext, framed: FrameResult, space: DesignSpace,
                        hypotheses: Iterable[Hypothesis] = (), provider_ideas: Iterable[str] = (),
                        decide_now: bool = True, at: str | None = None) -> CandidateResult:
    """Register and evaluate the design space's candidates, and (unless `decide_now` is False, so a verifier can
    run first) decide among them. See the module docstring."""
    at = at or utcnow()
    res = CandidateResult()
    hypotheses = list(hypotheses)
    res.assumptions, res.constraints = ground_design(context, space, at)
    base_values = {a.name: a.value for a in res.assumptions if a.name and a.value is not None}
    base_units = {a.name: a.unit for a in res.assumptions if a.name}
    objective_text = framed.objective.objective

    specs = list(space.candidates)
    if space.optimization:
        problem = context.ensure(problem_from_json(space.optimization, at))
        res.optimization = optimize(context, problem, at=at)
        o = res.optimization
        if o.status in (OptimizationStatus.OPTIMAL, OptimizationStatus.FEASIBLE):
            units = {v.name: v.unit for v in problem.variables}
            specs.append({"description": "Optimized: " + ", ".join(f"{k}={v:g}{(' ' + units[k]) if units[k] else ''}"
                                                                     for k, v in sorted(o.solution.items())),
                          "parameters": {k: [v, units[k]] for k, v in o.solution.items()},
                          "_optimization": o.id})
        else:
            res.notes.append(f"Optimization ended {o.status.value}: {o.proof}"
                             + (f" ({'; '.join(o.violations)})" if o.violations else ""))
    for idea in provider_ideas:
        specs.append({"description": idea, "parameters": {}, "_provider": True})

    for i, spec in enumerate(specs):
        res.candidates.append(_evaluate_one(context, framed, space, spec, i, hypotheses, res, base_values,
                                            base_units, objective_text, at))
    _mark_dominated(res.candidates)
    if decide_now:
        res.decision = decide(context, res.candidates, space.decision_rule, at)
    return res


def _evaluate_one(context, framed, space, spec, i, hypotheses, res, base_values, base_units, objective_text, at):
    w = f"DesignSpace.candidates[{i}]"
    if not isinstance(spec, Mapping):
        raise MalformedInput("a candidate is an object", w)
    allowed = {"description", "parameters", "hypotheses", "evidence_support", "evidence_against", "technical_feasibility",
               "economic_feasibility", "reversible", "benefits", "risks", "dependencies", "test_requirements", "costs",
               "required_conditions", "problem", "_optimization", "_provider"}
    extra = set(spec) - allowed
    if extra:
        raise MalformedInput(f"unknown candidate fields {sorted(extra)}", w)
    description = check_no_novelty_claim(text(spec.get("description"), f"{w}.description"), f"{w}.description")
    params = {k: _param(k, v) for k, v in mapping(spec.get("parameters", {}), f"{w}.parameters").items()}
    support = sorted(set(spec.get("evidence_support", [])))
    linked = sorted(set(spec.get("hypotheses", [])) | set(_linked_hypotheses(hypotheses, set(support))))
    cand = context.register(Candidate(
        description, originating_gap=objective_text, problem=text(spec.get("problem", ""), f"{w}.problem",
                                                                  required=False) or objective_text,
        hypothesis_ids=linked, assumptions=[a.id for a in res.assumptions],
        constraints=[c.id for c in res.constraints], evidence_support=support,
        evidence_against=sorted(set(spec.get("evidence_against", []))),
        benefits=texts(spec.get("benefits", []), f"{w}.benefits"), risks=texts(spec.get("risks", []), f"{w}.risks"),
        dependencies=texts(spec.get("dependencies", []), f"{w}.dependencies"),
        test_requirements=texts(spec.get("test_requirements", []), f"{w}.test_requirements"),
        required_conditions=texts(spec.get("required_conditions", []), f"{w}.required_conditions"),
        costs=mapping(spec.get("costs", {}), f"{w}.costs"), reversible=spec.get("reversible"),
        derived_from=[framed.frame.id] + ([spec["_optimization"]] if spec.get("_optimization") else []),
        created_at=at))
    values = {**base_values, **{k: v for k, (v, _) in params.items()}}
    units = {**base_units, **{k: u for k, (_, u) in params.items()}}
    notes = []

    # Constraints at the candidate's own parameters.
    outcomes = {}
    if space.model is not None and not (space.model.inputs - set(values)):
        outcomes = space.model.evaluate(values)
        units.update(space.model.output_units())
    checks = check_constraints(res.constraints, values, outcomes, {**units, **(space.model.units if space.model else {})})
    cand.constraints_satisfied = sorted(cid for cid, ok in checks.items() if ok)
    cand.constraints_violated = sorted(cid for cid, ok in checks.items() if ok is False)
    not_evaluable = sorted(cid for cid, ok in checks.items() if ok is None)
    if not_evaluable:
        cand.unknowns = sorted(set(cand.unknowns) | {f"constraint {cid} cannot be evaluated: a variable has no value"
                                                     for cid in not_evaluable})

    sim = sens = None
    if space.model is not None and not (space.model.inputs - set(values)):
        scenario = context.ensure(Scenario(cand.id, {k: {"value": v, "unit": units.get(k, "")} for k, v in values.items()},
                                           [a.id for a in res.assumptions if a.name in values],
                                           derived_from=[cand.id], created_at=at))
        res.scenarios.append(scenario)
        dists = {k: v for k, v in space.distributions.items() if k in space.model.inputs}
        model = MonteCarloModel(space.model, dists) if dists else space.model
        sim = simulate(context, cand, scenario, model, seed=space.seed if dists else None,
                       iterations=space.iterations if dists else 1, constraints=res.constraints,
                       success=space.success, at=at)
        res.simulations.append(sim)
        sens, table = analyze_sensitivity(context, cand, space.model, values, space.value_metric, success=space.success,
                                          constraints=res.constraints, assumptions=res.assumptions, at=at)
        res.sensitivities.append(sens)
        res.sensitivity_tables[cand.id] = table
        cand.expected_value = sim.outcomes[space.value_metric]["mean"]
        cand.simulation_results = {"simulation_id": sim.id, "outcomes": sim.outcomes}
        cand.sensitivity = {"sensitivity_id": sens.id, "elasticities": sens.elasticities,
                            "break_even": sens.break_even}
        cand.robustness = sens.robustness
        # Share of draws that break none of the stated constraints (1.0 when none is stated: nothing stated breaks).
        cand.technical_feasibility = round(1 - max(sim.failure_states.values(), default=0.0), 6)
        if space.success is not None:
            cand.economic_feasibility = sim.uncertainty["success_share"]
        cand.expected_outcome = (f"simulated {space.value_metric}: mean {sim.outcomes[space.value_metric]['mean']:g}, "
                                 f"p5 {sim.outcomes[space.value_metric]['p5']:g}, p95 "
                                 f"{sim.outcomes[space.value_metric]['p95']:g} "
                                 f"{sim.outcomes[space.value_metric]['unit']} (simulated, not observed)")
    elif space.model is not None:
        notes.append(f"not simulated: no value for {sorted(space.model.inputs - set(values))}")
    for name in ("technical_feasibility", "economic_feasibility"):
        if getattr(cand, name) is None and spec.get(name) is not None:
            setattr(cand, name, unit_interval(spec[name], f"{w}.{name}"))
            notes.append(f"{name} taken from the design space, not derived")
    cand.uncertainty = "; ".join(notes)

    if cand.constraints_violated:
        cand.status = CandidateStatus.INFEASIBLE
    elif not_evaluable or spec.get("_provider") or sim is None:
        cand.status = CandidateStatus.REQUIRES_RESEARCH
    else:
        cand.status = CandidateStatus.VIABLE
    return cand


def _measures(c: Candidate) -> dict:
    return {"technical_feasibility": c.technical_feasibility, "economic_feasibility": c.economic_feasibility,
            "expected_value": c.expected_value,
            "robustness": None if c.robustness == Robustness.UNKNOWN else ROBUSTNESS_RANK[c.robustness]}


def dominates(a: Candidate, b: Candidate) -> bool:
    """a is at least as good as b on every measure both have, and strictly better on one."""
    ma, mb = _measures(a), _measures(b)
    shared = [k for k in MEASURES if ma[k] is not None and mb[k] is not None]
    return bool(shared) and all(ma[k] >= mb[k] for k in shared) and any(ma[k] > mb[k] for k in shared)


def _mark_dominated(candidates: list[Candidate]) -> None:
    viable = sorted((c for c in candidates if c.status == CandidateStatus.VIABLE), key=lambda c: c.id)
    for c in viable:
        better = next((o for o in viable if o is not c and dominates(o, c)), None)
        if better is not None:
            c.status = CandidateStatus.DOMINATED
            c.risks = sorted(set(c.risks) | {f"dominated by {better.id}"})


def _reason(c: Candidate, rule: DecisionRule) -> str | None:
    """Why `c` cannot be selected under `rule`, or None if it can."""
    if c.status == CandidateStatus.INFEASIBLE:
        return f"infeasible: violates {', '.join(c.constraints_violated)}"
    if c.status == CandidateStatus.DOMINATED:
        return next((r for r in c.risks if r.startswith("dominated by")), "dominated")
    if c.status == CandidateStatus.REQUIRES_RESEARCH:
        return "requires research: " + ("; ".join(c.unknowns) or "not evaluable from the design space")
    if ROBUSTNESS_RANK[c.robustness] < ROBUSTNESS_RANK[rule.min_robustness]:
        return f"robustness {c.robustness.value} is below {rule.min_robustness.value}"
    for name, floor in (("technical_feasibility", rule.min_technical_feasibility),
                        ("economic_feasibility", rule.min_economic_feasibility)):
        value = getattr(c, name)
        if floor is not None and (value is None or value < floor):
            return f"{name} {value if value is not None else 'unknown'} is below {floor:g}"
    if getattr(c, rule.maximize) is None:
        return f"{rule.maximize} is unknown"
    return None


def decide(context: DiscoveryContext, candidates: list[Candidate], rule: DecisionRule, at: str) -> DiscoveryDecision:
    eligible = sorted((c for c in candidates if _reason(c, rule) is None),
                      key=lambda c: (-getattr(c, rule.maximize), c.id))
    alternatives = {}
    selected = eligible[0] if eligible else None
    for c in sorted(candidates, key=lambda c: c.id):
        if c is selected:
            continue
        why = _reason(c, rule)
        alternatives[c.id] = why or (f"lower {rule.maximize} ({getattr(c, rule.maximize):g}) than "
                                     f"{selected.id} ({getattr(selected, rule.maximize):g})")
    if selected is not None:
        selected.status = CandidateStatus.SELECTED
        outcome = DiscoveryOutcome.CANDIDATE_SELECTED
    elif candidates and all(c.status == CandidateStatus.INFEASIBLE for c in candidates):
        outcome = DiscoveryOutcome.INFEASIBLE
    elif any(c.status == CandidateStatus.REQUIRES_RESEARCH for c in candidates):
        outcome = DiscoveryOutcome.REQUIRES_RESEARCH
    elif candidates:
        outcome = DiscoveryOutcome.UNKNOWN  # viable candidates exist, but none passes the rule's thresholds
    else:
        outcome = DiscoveryOutcome.REQUIRES_RESEARCH
    tie = ""
    if selected is not None and len(eligible) > 1 and getattr(eligible[1], rule.maximize) == getattr(selected, rule.maximize):
        tie = f"tied with {eligible[1].id} on {rule.maximize}; the smaller id was taken"
    return context.ensure(DiscoveryDecision(rule.text, selected.id if selected else None, alternatives, outcome, tie,
                                            derived_from=sorted(c.id for c in candidates), created_at=at))
