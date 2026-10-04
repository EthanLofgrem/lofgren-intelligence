"""Optimization over structured problems, with independent verification. Standard library only.

Solvers (chosen by `OptimizationProblem.solver`, or automatically with "auto"):

    exhaustive  every point of a bounded all-integer domain (at most MAX_EXHAUSTIVE points). Optimal, with
                proof "exhaustive over N points"; ties are broken by the lexicographically smallest solution.
    simplex     linear objective and constraints over continuous variables: a two-phase simplex in exact
                rational arithmetic (fractions.Fraction) with Bland's rule, so degenerate problems cannot cycle.
                Optimal only with its certificate (every reduced cost has the optimal sign at the final basis).
                Detects infeasible and unbounded problems; an infeasible one reports a minimal infeasible
                subset of its constraints (deletion filter).
    grid        bounded grid search for anything else (nonlinear or mixed). It can report `feasible`, never
                `optimal`; finding no feasible grid point proves nothing, so that is `unknown`.

Every returned solution is re-checked independently by `verify_solution` with the expression interpreter (bounds,
integrality, every constraint, the objective value). A solution that fails the re-check is reported `infeasible`.
"""

from __future__ import annotations

import itertools
import math
import time
from dataclasses import dataclass
from fractions import Fraction
from typing import Mapping

from .context import DiscoveryContext
from .errors import MalformedInput
from .expr import Add, Const, Div, Expr, Max, Min, Mul, Neg, Pow, Quantity, Relation, Sub, Unit, Var
from .types import DecisionVariable, OptimizationProblem, OptimizationResult, OptimizationStatus, utcnow

MAX_EXHAUSTIVE = 100_000
GRID_BUDGET = 20_000
DEADLINE_S = 10.0
SOLVERS = ("auto", "exhaustive", "simplex", "grid")
TOLERANCE = 1e-9


class NotLinear(Exception):
    pass


def linear_form(e: Expr) -> tuple[dict[str, Fraction], Fraction]:
    """coefficients, constant of a linear expression; NotLinear otherwise. Exact rationals."""
    if isinstance(e, Const):
        return {}, Fraction(e.value)
    if isinstance(e, Var):
        return {e.name: Fraction(1)}, Fraction(0)
    if isinstance(e, Neg):
        c, k = linear_form(e.a)
        return {n: -v for n, v in c.items()}, -k
    if isinstance(e, (Add, Sub)):
        ca, ka = linear_form(e.a)
        cb, kb = linear_form(e.b)
        sign = 1 if isinstance(e, Add) else -1
        out = dict(ca)
        for n, v in cb.items():
            out[n] = out.get(n, Fraction(0)) + sign * v
        return {n: v for n, v in out.items() if v != 0}, ka + sign * kb
    if isinstance(e, Mul):
        ca, ka = linear_form(e.a)
        cb, kb = linear_form(e.b)
        if ca and cb:
            raise NotLinear("product of two variables")
        if not ca:
            return {n: ka * v for n, v in cb.items() if ka * v != 0}, ka * kb
        return {n: kb * v for n, v in ca.items() if kb * v != 0}, ka * kb
    if isinstance(e, Div):
        ca, ka = linear_form(e.a)
        cb, kb = linear_form(e.b)
        if cb or kb == 0:
            raise NotLinear("division by a variable or by zero")
        return {n: v / kb for n, v in ca.items()}, ka / kb
    if isinstance(e, Pow) and e.exponent == 1:
        return linear_form(e.a)
    if isinstance(e, (Min, Max, Pow)):
        raise NotLinear(f"{type(e).__name__} is not linear")
    raise NotLinear(f"unsupported node {type(e).__name__}")


def _env(problem: OptimizationProblem, solution: Mapping[str, float]) -> dict[str, Quantity]:
    return {v.name: Quantity(float(solution[v.name]), Unit.parse(v.unit)) for v in problem.variables}


def verify_solution(problem: OptimizationProblem, solution: Mapping[str, float],
                    claimed_objective: float | None = None) -> tuple[bool, list[str], float | None]:
    """Independent re-check: bounds, integrality, every constraint, and the objective value."""
    violations = []
    names = {v.name for v in problem.variables}
    if set(solution) != names:
        return False, [f"solution assigns {sorted(solution)}, the problem has {sorted(names)}"], None
    for v in problem.variables:
        x = float(solution[v.name])
        if not math.isfinite(x):
            violations.append(f"{v.name} is not finite")
            continue
        if v.lower is not None and x < v.lower - TOLERANCE:
            violations.append(f"{v.name}={x} is below its lower bound {v.lower}")
        if v.upper is not None and x > v.upper + TOLERANCE:
            violations.append(f"{v.name}={x} is above its upper bound {v.upper}")
        if v.integer and abs(x - round(x)) > TOLERANCE:
            violations.append(f"{v.name}={x} is not an integer")
    if violations:
        return False, violations, None
    env = _env(problem, solution)
    for i, c in enumerate(problem.constraints):
        check = c.check(env)
        if not check.satisfied:
            violations.append(f"constraint {i} ({c}) is violated: {check.lhs:g} {c.op} {check.rhs:g}")
    value = problem.objective.evaluate(env).value
    if claimed_objective is not None and abs(value - claimed_objective) > 1e-6 * max(1.0, abs(value)):
        violations.append(f"the claimed objective {claimed_objective} differs from the re-evaluated {value}")
    return not violations, violations, value


# ---- exact two-phase simplex -------------------------------------------------------

@dataclass
class _LP:
    """minimize c.y subject to rows (a, op, b) with y >= 0, all rationals; b >= 0 after normalization."""

    c: list[Fraction]
    rows: list[tuple[list[Fraction], str, Fraction]]


def _simplex(lp: _LP) -> tuple[str, list[Fraction] | None, Fraction | None, str]:
    """('optimal'|'infeasible'|'unbounded', y, objective, certificate)."""
    n = len(lp.c)
    rows = []
    for a, op, b in lp.rows:
        if b < 0:
            a, b, op = [-x for x in a], -b, {"<=": ">=", ">=": "<=", "==": "=="}[op]
        rows.append((a, op, b))
    m = len(rows)
    n_slack = sum(1 for _, op, _ in rows if op != "==")
    n_art = sum(1 for _, op, _ in rows if op != "<=")
    width = n + n_slack + n_art
    tableau, basis = [], []
    s_col, a_col = n, n + n_slack
    artificial = set()
    for a, op, b in rows:
        row = list(a) + [Fraction(0)] * (n_slack + n_art) + [b]
        if op == "<=":
            row[s_col] = Fraction(1)
            basis.append(s_col)
            s_col += 1
        else:
            if op == ">=":
                row[s_col] = Fraction(-1)
                s_col += 1
            row[a_col] = Fraction(1)
            basis.append(a_col)
            artificial.add(a_col)
            a_col += 1
        tableau.append(row)

    def pivot(r: int, col: int) -> None:
        p = tableau[r][col]
        tableau[r] = [x / p for x in tableau[r]]
        for i in range(m):
            if i != r and tableau[i][col] != 0:
                f = tableau[i][col]
                tableau[i] = [x - f * y for x, y in zip(tableau[i], tableau[r])]
        basis[r] = col

    def run(cost: list[Fraction], allowed: set[int]) -> str:
        while True:
            # Reduced costs r_j = c_j - c_B . column_j ; Bland: smallest j with r_j < 0.
            cb = [cost[basis[i]] for i in range(m)]
            entering = None
            for j in sorted(allowed):
                if j in basis:
                    continue
                r = cost[j] - sum(cb[i] * tableau[i][j] for i in range(m))
                if r < 0:
                    entering = j
                    break
            if entering is None:
                return "optimal"
            best = None
            for i in range(m):
                if tableau[i][entering] > 0:
                    ratio = tableau[i][-1] / tableau[i][entering]
                    if best is None or ratio < best[0] or (ratio == best[0] and basis[i] < basis[best[1]]):
                        best = (ratio, i)
            if best is None:
                return "unbounded"
            pivot(best[1], entering)

    all_cols = set(range(width))
    if artificial:
        phase1 = [Fraction(1) if j in artificial else Fraction(0) for j in range(width)]
        run(phase1, all_cols)
        infeasibility = sum(tableau[i][-1] for i in range(m) if basis[i] in artificial)
        if infeasibility > 0:
            return "infeasible", None, None, f"phase 1 minimum of the artificial variables is {float(infeasibility):g} > 0"
        # Drive any artificial left in the basis (at zero) out, where possible.
        for i in range(m):
            if basis[i] in artificial:
                for j in range(n + n_slack):
                    if tableau[i][j] != 0 and j not in basis:
                        pivot(i, j)
                        break
    cost = list(lp.c) + [Fraction(0)] * (n_slack + n_art)
    allowed = {j for j in all_cols if j not in artificial}
    status = run(cost, allowed)
    if status == "unbounded":
        return "unbounded", None, None, "an improving direction with no limiting constraint exists"
    y = [Fraction(0)] * width
    for i in range(m):
        y[basis[i]] = tableau[i][-1]
    cb = [cost[basis[i]] for i in range(m)]
    reduced = [cost[j] - sum(cb[i] * tableau[i][j] for i in range(m)) for j in sorted(allowed) if j not in basis]
    objective = sum(c * v for c, v in zip(lp.c, y[:n]))
    smallest = min(reduced) if reduced else Fraction(0)
    certificate = (f"two-phase simplex in exact rational arithmetic with Bland's rule; optimality certificate: all "
                   f"{len(reduced)} nonbasic reduced costs are >= 0 at the final basis (smallest {float(smallest):g})")
    return "optimal", y[:n], objective, certificate


def _to_lp(problem: OptimizationProblem, constraints: list[Relation]):
    """Substitute bounded/free variables so every LP variable is >= 0. Returns (lp, back, sign, offset)."""
    columns: list[tuple[str, int, Fraction]] = []  # (variable, sign, offset): x = offset + sign * y
    for v in problem.variables:
        if v.lower is not None:
            columns.append((v.name, 1, Fraction(v.lower)))
        elif v.upper is not None:
            columns.append((v.name, -1, Fraction(v.upper)))
        else:
            columns.append((v.name, 1, Fraction(0)))
            columns.append((v.name, -1, Fraction(0)))

    def substitute(coeffs: dict[str, Fraction], const: Fraction) -> tuple[list[Fraction], Fraction]:
        row = [Fraction(0)] * len(columns)
        k = const
        seen = set()
        for j, (name, sign, offset) in enumerate(columns):
            a = coeffs.get(name, Fraction(0))
            row[j] = a * sign
            if name not in seen:
                k += a * offset
                seen.add(name)
        return row, k

    oc, ok = linear_form(problem.objective)
    c, c0 = substitute(oc, ok)
    if problem.direction == "maximize":
        c = [-x for x in c]
    rows = []
    for rel in constraints:
        lc, lk = linear_form(rel.lhs)
        rc, rk = linear_form(rel.rhs)
        coeffs = dict(lc)
        for name, v in rc.items():
            coeffs[name] = coeffs.get(name, Fraction(0)) - v
        a, k = substitute(coeffs, lk - rk)
        rows.append((a, rel.op, -k))
    for v in problem.variables:  # the bound not absorbed by the substitution becomes a row
        if v.lower is not None and v.upper is not None:
            a, k = substitute({v.name: Fraction(1)}, Fraction(0))
            rows.append((a, "<=", Fraction(v.upper) - k))
    return _LP(c, rows), columns


def _lp_feasible(problem: OptimizationProblem, constraints: list[Relation]) -> bool:
    lp, _ = _to_lp(problem, constraints)
    lp.c = [Fraction(0)] * len(lp.c)
    return _simplex(lp)[0] != "infeasible"


def minimal_infeasible_set(problem: OptimizationProblem) -> list[int]:
    """A minimal subset of the constraints (by index) that is infeasible together with the bounds."""
    keep = list(range(len(problem.constraints)))
    for i in list(keep):
        trial = [j for j in keep if j != i]
        if not _lp_feasible(problem, [problem.constraints[j] for j in trial]):
            keep = trial
    return keep


def _solve_simplex(problem: OptimizationProblem) -> dict:
    lp, columns = _to_lp(problem, list(problem.constraints))
    status, y, objective, certificate = _simplex(lp)
    if status == "infeasible":
        iis = minimal_infeasible_set(problem)
        return {"status": OptimizationStatus.INFEASIBLE, "proof": certificate,
                "violations": [f"minimal infeasible set: constraints {iis} "
                               f"({'; '.join(str(problem.constraints[i]) for i in iis)})"]}
    if status == "unbounded":
        return {"status": OptimizationStatus.UNBOUNDED, "proof": certificate}
    values: dict[str, Fraction] = {}
    for (name, sign, offset), yj in zip(columns, y):
        values[name] = values.get(name, offset) + sign * yj
    solution = {k: float(v) for k, v in values.items()}
    return {"status": OptimizationStatus.OPTIMAL, "solution": solution, "proof": certificate}


def _domain(v: DecisionVariable) -> range:
    return range(math.ceil(v.lower), math.floor(v.upper) + 1)


def _better(direction: str, a: float, b: float) -> bool:
    return a > b + TOLERANCE if direction == "maximize" else a < b - TOLERANCE


def _solve_exhaustive(problem: OptimizationProblem, deadline: float) -> dict:
    domains = [_domain(v) for v in problem.variables]
    size = math.prod(len(d) for d in domains)
    names = [v.name for v in problem.variables]
    best, best_value, ties, checked = None, None, 0, 0
    for point in itertools.product(*domains):  # lexicographic order: the first optimum is the smallest
        checked += 1
        if time.monotonic() > deadline:
            return {"status": OptimizationStatus.TIMEOUT, "solution": best or {},
                    "proof": f"stopped after {checked} of {size} points"}
        sol = dict(zip(names, map(float, point)))
        ok, _, value = verify_solution(problem, sol)
        if not ok:
            continue
        if best is None or _better(problem.direction, value, best_value):
            best, best_value, ties = sol, value, 1
        elif abs(value - best_value) <= TOLERANCE:
            ties += 1
    if best is None:
        return {"status": OptimizationStatus.INFEASIBLE, "proof": f"exhaustive over {size} points: none feasible",
                "violations": ["no point of the integer domain satisfies every constraint"]}
    tie = f"; {ties} points tie, the lexicographically smallest is reported" if ties > 1 else ""
    return {"status": OptimizationStatus.OPTIMAL, "solution": best, "proof": f"exhaustive over {size} points{tie}"}


def _solve_grid(problem: OptimizationProblem, budget: int, deadline: float) -> dict:
    n = len(problem.variables)
    per = max(2, int(budget ** (1 / n)))
    axes = []
    for v in problem.variables:
        if v.integer:
            d = _domain(v)
            step = max(1, math.ceil(len(d) / per))
            axes.append([float(x) for x in d[::step]] + ([float(d[-1])] if (len(d) - 1) % step else []))
        else:
            axes.append([v.lower + (v.upper - v.lower) * i / (per - 1) for i in range(per)])
    names = [v.name for v in problem.variables]
    best, best_value, checked = None, None, 0
    for point in itertools.product(*axes):
        checked += 1
        if time.monotonic() > deadline:
            return {"status": OptimizationStatus.TIMEOUT, "solution": best or {},
                    "proof": f"stopped after {checked} grid points"}
        sol = dict(zip(names, point))
        ok, _, value = verify_solution(problem, sol)
        if ok and (best is None or _better(problem.direction, value, best_value)):
            best, best_value = sol, value
    if best is None:
        return {"status": OptimizationStatus.UNKNOWN,
                "proof": f"no feasible point on a {checked}-point grid; a grid cannot prove infeasibility"}
    return {"status": OptimizationStatus.FEASIBLE, "solution": best,
            "proof": f"best of {checked} grid points; a grid search does not establish optimality"}


def choose_solver(problem: OptimizationProblem) -> str:
    bounded = all(v.lower is not None and v.upper is not None for v in problem.variables)
    if all(v.integer for v in problem.variables) and bounded and \
            math.prod(len(_domain(v)) for v in problem.variables) <= MAX_EXHAUSTIVE:
        return "exhaustive"
    if not any(v.integer for v in problem.variables):
        try:
            linear_form(problem.objective)
            for c in problem.constraints:
                linear_form(c.lhs)
                linear_form(c.rhs)
            return "simplex"
        except NotLinear:
            pass
    return "grid" if bounded else "unsupported"


def optimize(context: DiscoveryContext, problem: OptimizationProblem, *, budget: int = GRID_BUDGET,
             deadline_s: float = DEADLINE_S, at: str | None = None) -> OptimizationResult:
    """Solve `problem`, re-check the answer independently, and register the result."""
    at = at or utcnow()
    context.reference(problem.id, (OptimizationProblem,), "optimize.problem")
    deadline = time.monotonic() + deadline_s
    requested = problem.solver
    if requested not in SOLVERS:
        out = {"status": OptimizationStatus.UNSUPPORTED, "proof": f"solver {requested!r} is not one of {SOLVERS}"}
        solver = requested
    else:
        solver = choose_solver(problem) if requested == "auto" else requested
        try:
            if solver == "exhaustive":
                if choose_solver(problem) != "exhaustive":
                    raise NotLinear("exhaustive search needs a small, bounded, all-integer domain")
                out = _solve_exhaustive(problem, deadline)
            elif solver == "simplex":
                if any(v.integer for v in problem.variables):
                    raise NotLinear("the simplex solver handles continuous variables only")
                out = _solve_simplex(problem)
            elif solver == "grid":
                if not all(v.lower is not None and v.upper is not None for v in problem.variables):
                    raise NotLinear("a grid search needs every variable bounded")
                out = _solve_grid(problem, budget, deadline)
            else:
                out = {"status": OptimizationStatus.UNSUPPORTED,
                       "proof": "no supported solver: unbounded variables in a nonlinear or integer problem"}
        except NotLinear as exc:
            out = {"status": OptimizationStatus.UNSUPPORTED, "proof": f"{solver}: {exc}"}
    solution = out.get("solution") or {}
    verified, violations, value = False, list(out.get("violations", [])), None
    if solution:
        verified, extra, value = verify_solution(problem, solution)
        violations += extra
    status = out["status"]
    if status in (OptimizationStatus.OPTIMAL, OptimizationStatus.FEASIBLE) and not verified:
        status = OptimizationStatus.INFEASIBLE  # the independent re-check overrules the solver
    result = OptimizationResult(problem.id, status, solution=solution if status != OptimizationStatus.INFEASIBLE else {},
                                objective_value=None if value is None else round(value, 9), proof=out.get("proof", ""),
                                verified=verified and status in (OptimizationStatus.OPTIMAL, OptimizationStatus.FEASIBLE),
                                violations=violations if status not in (OptimizationStatus.OPTIMAL,
                                                                        OptimizationStatus.FEASIBLE) else [],
                                derived_from=[problem.id], created_at=at)
    return context.get(result.id) if result.id in context else context.register(result)


def problem_from_json(data: Mapping, at: str | None = None) -> OptimizationProblem:
    if not isinstance(data, Mapping) or not {"variables", "objective", "direction"} <= set(data):
        raise MalformedInput("an optimization problem has variables, objective and direction", "optimization")
    return OptimizationProblem(list(data["variables"]), data["objective"], data["direction"],
                               list(data.get("constraints", [])), data.get("solver", "auto"),
                               created_at=at or utcnow())
