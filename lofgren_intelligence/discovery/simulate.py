"""Simulation: structured models run over a candidate's parameters. Results are predictions, never observations.

Models are named, versioned objects:

    ExpressionModel   evaluates one structured expression per outcome metric over the parameters (deterministic)
    MonteCarloModel   samples declared parameters from declared distributions with random.Random(seed) (never the
                      global random state), evaluates the ExpressionModel on every draw, and summarizes

A `Simulation` records model, version, parameters, seed, iterations, a distribution summary per metric (mean, p5,
p50, p95, unit), the share of draws violating each constraint, runtime and cost. Its `kind` is always "simulated".
The same candidate, scenario, model, seed and iterations give a byte-identical summary.
"""

from __future__ import annotations

import math
import random
import time
from dataclasses import dataclass, field
from typing import Iterable, Mapping

from .context import DiscoveryContext
from .errors import MalformedInput, UnsupportedAlgorithm
from .expr import Expr, Quantity, Relation, Unit, Var, from_json as expr_from_json
from .types import MAX_ITERATIONS, Candidate, Constraint, Scenario, Simulation, finite, integer, text, utcnow

MAX_DRAWS = 200_000  # iterations x metrics evaluated per simulation; above this the run is refused
DIGITS = 6  # summaries are rounded so equal inputs give byte-identical output


def _env(values: Mapping[str, float], units: Mapping[str, str]) -> dict[str, Quantity]:
    out = {}
    for name, value in values.items():
        Var(name)
        out[name] = Quantity(finite(value, f"parameter {name}", allow_none=False), Unit.parse(units.get(name, "")))
    return out


@dataclass(frozen=True)
class ExpressionModel:
    """outcome metric -> structured expression over named, unit-declared variables."""

    name: str
    version: str
    outcomes: Mapping[str, Expr]
    units: Mapping[str, str]  # variable -> unit, for every variable the expressions use

    def __post_init__(self) -> None:
        text(self.name, "ExpressionModel.name")
        text(self.version, "ExpressionModel.version")
        if not self.outcomes:
            raise MalformedInput("a model computes at least one outcome", "ExpressionModel.outcomes")
        parsed = {}
        for metric, e in self.outcomes.items():
            Var(metric)
            parsed[metric] = expr_from_json(e) if isinstance(e, dict) else e
            if not isinstance(parsed[metric], Expr):
                raise MalformedInput("an outcome is a structured expression", f"ExpressionModel.outcomes.{metric}")
        object.__setattr__(self, "outcomes", dict(sorted(parsed.items())))
        units = {k: str(v) for k, v in self.units.items()}
        for k, v in units.items():
            Var(k)
            Unit.parse(v)
        missing = set().union(*(e.variables() for e in parsed.values())) - set(units)
        if missing:
            raise MalformedInput(f"declare units for {sorted(missing)}", "ExpressionModel.units")
        var_units = {k: Unit.parse(v) for k, v in units.items()}
        for metric, e in parsed.items():
            e.unit(var_units)  # unit-checks every expression up front: UnitMismatch fails closed
        object.__setattr__(self, "units", dict(sorted(units.items())))

    @property
    def inputs(self) -> set[str]:
        return set().union(*(e.variables() for e in self.outcomes.values()))

    def output_units(self) -> dict[str, str]:
        var_units = {k: Unit.parse(v) for k, v in self.units.items()}
        return {m: str(e.unit(var_units)) for m, e in self.outcomes.items()}

    def evaluate(self, values: Mapping[str, float]) -> dict[str, float]:
        missing = self.inputs - set(values)
        if missing:
            raise MalformedInput(f"the model needs values for {sorted(missing)}", f"{self.name}.evaluate")
        env = _env({k: values[k] for k in self.inputs}, self.units)
        return {m: e.evaluate(env).value for m, e in self.outcomes.items()}

    def to_json(self) -> dict:
        return {"name": self.name, "version": self.version, "units": dict(self.units),
                "outcomes": {m: e.to_json() for m, e in self.outcomes.items()}}

    @staticmethod
    def from_json(data: Mapping) -> "ExpressionModel":
        if not isinstance(data, Mapping) or not {"name", "version", "outcomes", "units"} <= set(data):
            raise MalformedInput("a model has name, version, outcomes and units", "ExpressionModel")
        return ExpressionModel(data["name"], data["version"], dict(data["outcomes"]), dict(data["units"]))


_DISTRIBUTIONS = {"uniform": {"low", "high"}, "normal": {"mean", "sd"}, "triangular": {"low", "mode", "high"},
                  "fixed": {"value"}}


def check_distribution(name: str, spec: Mapping) -> dict:
    """A declared parameter distribution, validated. Unknown kinds are UnsupportedAlgorithm."""
    if not isinstance(spec, Mapping) or "dist" not in spec:
        raise MalformedInput("a distribution names its kind ('dist')", f"distribution.{name}")
    kind = spec["dist"]
    if kind not in _DISTRIBUTIONS:
        raise UnsupportedAlgorithm(f"distribution {kind!r} is not supported; use {sorted(_DISTRIBUTIONS)}",
                                   f"distribution.{name}")
    if set(spec) != _DISTRIBUTIONS[kind] | {"dist"}:
        raise MalformedInput(f"a {kind} distribution has exactly {sorted(_DISTRIBUTIONS[kind])}", f"distribution.{name}")
    out = {"dist": kind, **{k: finite(spec[k], f"distribution.{name}.{k}", allow_none=False) for k in _DISTRIBUTIONS[kind]}}
    if kind == "uniform" and out["low"] > out["high"]:
        raise MalformedInput("uniform low exceeds high", f"distribution.{name}")
    if kind == "normal" and out["sd"] < 0:
        raise MalformedInput("a standard deviation is never negative", f"distribution.{name}")
    if kind == "triangular" and not out["low"] <= out["mode"] <= out["high"]:
        raise MalformedInput("triangular needs low <= mode <= high", f"distribution.{name}")
    return out


def _draw(rng: random.Random, spec: Mapping) -> float:
    k = spec["dist"]
    if k == "uniform":
        return rng.uniform(spec["low"], spec["high"])
    if k == "normal":
        return rng.gauss(spec["mean"], spec["sd"])
    if k == "triangular":
        return rng.triangular(spec["low"], spec["high"], spec["mode"])
    return spec["value"]


@dataclass(frozen=True)
class MonteCarloModel:
    """Samples the named parameters; every other parameter keeps its scenario value."""

    base: ExpressionModel
    distributions: Mapping[str, Mapping] = field(default_factory=dict)

    def __post_init__(self) -> None:
        checked = {name: check_distribution(name, spec) for name, spec in sorted(self.distributions.items())}
        unknown = set(checked) - self.base.inputs
        if unknown:
            raise MalformedInput(f"distributions for variables the model does not use: {sorted(unknown)}",
                                 "MonteCarloModel.distributions")
        object.__setattr__(self, "distributions", checked)

    @property
    def name(self) -> str:
        return f"monte-carlo:{self.base.name}"

    @property
    def version(self) -> str:
        return f"1/{self.base.version}"


def _percentile(sorted_values: list[float], p: float) -> float:
    """Nearest-rank percentile: deterministic, no interpolation."""
    k = max(0, min(len(sorted_values) - 1, math.ceil(p / 100 * len(sorted_values)) - 1))
    return sorted_values[k]


def _summary(values: list[float], unit: str) -> dict:
    s = sorted(values)
    mean = math.fsum(s) / len(s)
    return {"mean": round(mean, DIGITS), "p5": round(_percentile(s, 5), DIGITS), "p50": round(_percentile(s, 50), DIGITS),
            "p95": round(_percentile(s, 95), DIGITS), "unit": unit}


def _constraint_env(values: Mapping[str, float], outcomes: Mapping[str, float], units: Mapping[str, str]
                    ) -> dict[str, Quantity]:
    return _env({**values, **outcomes}, units)


def check_constraints(constraints: Iterable[Constraint], values: Mapping[str, float], outcomes: Mapping[str, float],
                      units: Mapping[str, str]) -> dict[str, bool | None]:
    """constraint id -> satisfied (True/False), or None when a variable it needs has no value."""
    out: dict[str, bool | None] = {}
    have = {**values, **outcomes}
    for c in constraints:
        if c.relation.variables() - set(have):
            out[c.id] = None
            continue
        env = _constraint_env({k: v for k, v in values.items() if k in c.relation.variables()},
                              {k: v for k, v in outcomes.items() if k in c.relation.variables()},
                              {**c.variable_units, **units})  # model units win: a mismatch fails
        out[c.id] = c.relation.check(env).satisfied
    return out


def simulate(context: DiscoveryContext, candidate: Candidate, scenario: Scenario,
             model: ExpressionModel | MonteCarloModel, *, seed: int | None = None, iterations: int = 1,
             constraints: Iterable[Constraint] = (), success: Relation | None = None,
             at: str | None = None) -> Simulation:
    """Run `model` on the scenario's parameters and register the Simulation. Deterministic for a given seed."""
    at = at or utcnow()
    context.reference(candidate.id, (Candidate,), "simulate.candidate")
    context.reference(scenario.id, (Scenario,), "simulate.scenario")
    if scenario.candidate_id != candidate.id:
        raise MalformedInput("the scenario belongs to another candidate", "simulate.scenario")
    iterations = integer(iterations, "simulate.iterations", 1, MAX_ITERATIONS)
    base = model.base if isinstance(model, MonteCarloModel) else model
    stochastic = isinstance(model, MonteCarloModel) and bool(model.distributions)
    if stochastic and seed is None:
        raise MalformedInput("a stochastic simulation needs a seed", "simulate.seed")
    if not stochastic:
        iterations = 1
    if iterations * max(1, len(base.outcomes)) > MAX_DRAWS:
        raise MalformedInput(f"more than {MAX_DRAWS} evaluations requested", "simulate.iterations")
    params = {k: (v["value"] if isinstance(v, dict) else v) for k, v in scenario.parameters.items()}
    rng = random.Random(seed) if stochastic else None
    constraints = sorted(constraints, key=lambda c: c.id)
    out_units = base.output_units()
    units = {**base.units, **out_units}
    draws: dict[str, list[float]] = {m: [] for m in base.outcomes}
    failures = {c.id: 0 for c in constraints}
    unknown = {c.id for c in constraints}
    successes = 0
    started = time.perf_counter()
    for _ in range(iterations):
        values = dict(params)
        if stochastic:
            for name, spec in model.distributions.items():
                values[name] = _draw(rng, spec)
        outcomes = base.evaluate(values)  # NaN, Infinity and unit errors fail closed with typed errors
        for m, v in outcomes.items():
            draws[m].append(v)
        for cid, ok in check_constraints(constraints, values, outcomes, units).items():
            if ok is not None:
                unknown.discard(cid)
            if ok is False:
                failures[cid] += 1
        if success is not None:
            env = _constraint_env({k: values[k] for k in values if k in success.variables()},
                                  {k: outcomes[k] for k in outcomes if k in success.variables()}, units)
            successes += success.check(env).satisfied
    runtime = time.perf_counter() - started
    summary = {m: _summary(v, out_units[m]) for m, v in draws.items()}
    failure_states = {cid: round(n / iterations, DIGITS) for cid, n in failures.items() if cid not in unknown}
    uncertainty = {"stochastic": stochastic,
                   "distributions": {k: dict(v) for k, v in (model.distributions.items() if stochastic else ())}}
    if success is not None:
        uncertainty["success_share"] = round(successes / iterations, DIGITS)
        uncertainty["success_relation"] = success.to_json()
    if unknown:
        uncertainty["constraints_not_evaluable"] = sorted(unknown)
    sim = Simulation(candidate.id, model.name, model.version, parameters=dict(scenario.parameters),
                     initial_conditions={}, assumptions=list(scenario.assumption_ids), seed=seed if stochastic else None,
                     iterations=iterations, outcomes=summary, uncertainty=uncertainty, failure_states=failure_states,
                     runtime_s=round(runtime, 6), cost_usd=0.0, derived_from=[candidate.id, scenario.id], created_at=at)
    # The same deterministic inputs give the same simulation; only its measured runtime would differ.
    return context.get(sim.id) if sim.id in context else context.register(sim)
