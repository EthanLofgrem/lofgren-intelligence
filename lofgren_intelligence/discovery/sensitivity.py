"""Sensitivity: how a candidate's outcome responds to each input, and where it stops working.

For every swept variable (candidate parameters and assumption values) with a nonzero base value:

    swings        the metric at -50%, -10%, -5%, +5%, +10%, +50% of the base value (one at a time)
    elasticity    relative change in the metric per relative change in the input, from the +/-10% swing
    break-even    where the success relation flips, found by bisection between the base and a +/-100% swing
    failure       where a constraint first breaks, by the same bisection
    robust range  the contiguous multiplier range around the base (scanned over 0.5x .. 1.5x) where the candidate
                  still succeeds and satisfies every evaluable constraint

Robustness, from the swings: `fragile` when a +/-5% change of any input flips success or breaks a constraint,
`robust` when nothing flips within +/-50%, `moderate` otherwise, `unknown` when the base itself fails or cannot be
evaluated. A fragile candidate is never selected by the default decision rule.
"""

from __future__ import annotations

from typing import Iterable, Mapping

from .context import DiscoveryContext
from .errors import MalformedInput, NonFiniteValue
from .expr import Relation
from .expr import Var
from .simulate import ExpressionModel, _constraint_env, check_constraints
from .types import Assumption, Candidate, Constraint, Robustness, SensitivityResult, utcnow

SWINGS = (-0.5, -0.1, -0.05, 0.05, 0.1, 0.5)
HIGH_ELASTICITY = 1.0
LOW_ELASTICITY = 0.1
BISECTION_STEPS = 50
SCAN_POINTS = 101  # multipliers 0.5 .. 1.5


def _ok(model: ExpressionModel, values: Mapping[str, float], metric: str, success: Relation | None,
        constraints: list[Constraint]) -> bool | None:
    """Whether the candidate works at these values: success holds and no evaluable constraint breaks."""
    try:
        outcomes = model.evaluate(values)
    except NonFiniteValue:
        return False
    units = {**model.units, **model.output_units()}
    if success is not None:
        env = _constraint_env({k: values[k] for k in values if k in success.variables()},
                              {k: outcomes[k] for k in outcomes if k in success.variables()}, units)
        if not success.check(env).satisfied:
            return False
    return all(ok is not False for ok in check_constraints(constraints, values, outcomes, units).values())


def _metric(model: ExpressionModel, values: Mapping[str, float], metric: str) -> float | None:
    try:
        return model.evaluate(values)[metric]
    except NonFiniteValue:
        return None


def _bisect(works, lo: float, hi: float) -> float:
    """The boundary between lo (works(lo) is True) and hi (works(hi) is False)."""
    for _ in range(BISECTION_STEPS):
        mid = (lo + hi) / 2
        if works(mid):
            lo = mid
        else:
            hi = mid
    return round((lo + hi) / 2, 6)


def analyze_sensitivity(context: DiscoveryContext, candidate: Candidate, model: ExpressionModel,
                        base: Mapping[str, float], metric: str, *, success: Relation | None = None,
                        constraints: Iterable[Constraint] = (), assumptions: Iterable[Assumption] = (),
                        at: str | None = None) -> tuple[SensitivityResult, dict]:
    """One-at-a-time sensitivity of `metric` around `base`, registered in `context`. Returns the result and the raw
    swing table (metric value at each swing, per variable) for reports."""
    at = at or utcnow()
    context.reference(candidate.id, (Candidate,), "analyze_sensitivity.candidate")
    if metric not in model.outcomes:
        raise MalformedInput(f"the model has no outcome {metric!r}", "analyze_sensitivity.metric")
    base = {k: float(v) for k, v in base.items()}
    for k in base:
        Var(k)
    constraints = sorted(constraints, key=lambda c: c.id)
    by_variable = {a.name: a for a in assumptions if a.name}
    y0 = _metric(model, base, metric)
    base_ok = _ok(model, base, metric, success, constraints) if y0 is not None else None
    swept = sorted(v for v in model.inputs if base.get(v) not in (None, 0.0))

    elasticities: dict[str, float] = {}
    swings: dict[str, dict] = {}
    break_even: dict[str, dict] = {}
    failure_thresholds: dict[str, dict] = {}
    robust_ranges: dict[str, list] = {}
    flips_at_5 = flips_at_50 = False
    unit_of = model.units

    def at_multiplier(var: str, m: float) -> dict:
        return {**base, var: base[var] * m}

    for var in swept:
        row = {}
        for s in SWINGS:
            y = _metric(model, at_multiplier(var, 1 + s), metric)
            row[f"{s:+.0%}"] = None if y is None else round(y, 6)
            works = _ok(model, at_multiplier(var, 1 + s), metric, success, constraints) if y is not None else False
            if base_ok and not works:
                if abs(s) <= 0.05:
                    flips_at_5 = True
                if abs(s) <= 0.5:
                    flips_at_50 = True
        swings[var] = row
        lo, hi = row["-10%"], row["+10%"]
        if y0 not in (None, 0.0) and lo is not None and hi is not None:
            elasticities[var] = round(((hi - lo) / abs(y0)) / 0.2, 6)
        if base_ok:
            for direction in (-1.0, 1.0):
                far = 1 + direction  # 0x or 2x the base value
                if not _ok(model, at_multiplier(var, far), metric, success, constraints):
                    edge = _bisect(lambda m: bool(_ok(model, at_multiplier(var, m), metric, success, constraints)),
                                   1.0, far)
                    point = {"value": round(base[var] * edge, 6), "unit": unit_of.get(var, ""),
                             "multiplier": edge}
                    if success is not None and _ok(model, at_multiplier(var, far), metric, None, constraints):
                        break_even.setdefault(var, point)  # success flips first
                    else:
                        failure_thresholds.setdefault(var, point)
            grid = [0.5 + i / (SCAN_POINTS - 1) for i in range(SCAN_POINTS)]
            good = [m for m in grid if _ok(model, at_multiplier(var, m), metric, success, constraints)]
            centre = min(range(len(grid)), key=lambda i: abs(grid[i] - 1.0))
            if grid[centre] in good:
                i = j = centre
                while i > 0 and grid[i - 1] in good:
                    i -= 1
                while j < len(grid) - 1 and grid[j + 1] in good:
                    j += 1
                robust_ranges[var] = [round(base[var] * grid[i], 6), round(base[var] * grid[j], 6)]

    if not base_ok:
        robustness = Robustness.UNKNOWN
    elif flips_at_5:
        robustness = Robustness.FRAGILE
    elif flips_at_50:
        robustness = Robustness.MODERATE
    else:
        robustness = Robustness.ROBUST
    ranked = sorted(elasticities, key=lambda v: (-abs(elasticities[v]), v))
    high = [v for v in ranked if abs(elasticities[v]) >= HIGH_ELASTICITY]
    low = [v for v in ranked if abs(elasticities[v]) < LOW_ELASTICITY]
    def near_edge(v: str) -> bool:
        edge = failure_thresholds.get(v) or break_even.get(v)
        return edge is not None and abs(edge["multiplier"] - 1) <= 0.1

    # An assumption is fragile when the outcome is highly elastic to it or breaks within a 10% change of it.
    fragile = sorted(a.id for v, a in by_variable.items() if v in high or near_edge(v))
    for v, a in by_variable.items():
        if v in elasticities:
            a.sensitivity = elasticities[v]  # assumptions carry their measured sensitivity
    result = SensitivityResult(candidate.id, elasticities=elasticities, high_sensitivity=high, low_sensitivity=low,
                               fragile_assumptions=fragile, break_even=break_even, failure_thresholds=failure_thresholds,
                               robust_ranges=robust_ranges, robustness=robustness,
                               derived_from=[candidate.id, *sorted(a.id for a in by_variable.values())],
                               created_at=at)
    result = context.get(result.id) if result.id in context else context.register(result)
    return result, {"metric": metric, "base_value": None if y0 is None else round(y0, 6), "swings": swings,
                    "base_succeeds": base_ok}
