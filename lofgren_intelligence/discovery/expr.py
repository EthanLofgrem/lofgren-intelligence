"""Structured expressions: the only way V2 states constraints, objectives and models.

Discovery never optimizes or simulates prose. A constraint like
"monthly cost must stay under the budget" becomes

    Constraint(Mul(Var("units"), Var("unit_cost")), "<=", Var("budget"))

and is evaluated by the small interpreter below: no eval, no exec, no
attribute access. Units are checked; NaN, Infinity and division by zero raise
typed errors instead of propagating.

Serialized form (for receipts and the V3 handoff):
    {"op": "const", "value": 3.5, "unit": "kWh"}
    {"op": "var", "name": "units"}
    {"op": "add" | "sub" | "mul" | "div" | "min" | "max", "args": [expr, expr]}
    {"op": "neg", "args": [expr]}
    {"op": "pow", "args": [expr], "exponent": 2}
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Mapping, Union

from .errors import InputTooLarge, MalformedInput, NonFiniteValue, UnitMismatch, UnknownReference, UnsafeName

MAX_NODES = 10_000
MAX_DEPTH = 64
MAX_POWER = 12
_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")

# Canonical spellings for common units (extends the V1 sensor normalization).
UNIT_CANON = {
    "": "", "1": "", "none": "", "dimensionless": "",
    "percent": "%", "pct": "%", "%": "%",
    "usd": "usd", "$": "usd", "dollar": "usd", "dollars": "usd",
    "c": "°C", "°c": "°C", "degc": "°C", "celsius": "°C",
    "kwh": "kWh", "wh": "Wh", "w": "W", "kw": "kW",
    "kpa": "kPa", "m/s": "m/s", "mm": "mm", "m": "m", "km": "km", "kg": "kg", "t": "t",
    "h": "h", "hour": "h", "hours": "h", "d": "d", "day": "d", "days": "d", "month": "month", "months": "month",
    "year": "year", "years": "year", "unit": "unit", "units": "unit", "sqft": "sqft", "acre": "acre",
}


# ---- units -------------------------------------------------------------------

@dataclass(frozen=True)
class Unit:
    """A product of symbols with integer exponents, e.g. usd/unit = {usd: 1, unit: -1}."""

    dims: tuple[tuple[str, int], ...] = ()

    @staticmethod
    def parse(text: str) -> "Unit":
        text = (text or "").strip()
        if not text:
            return DIMENSIONLESS
        num, _, den = text.partition("/")
        dims: dict[str, int] = {}
        for part, sign in ((num, 1), (den, -1)):
            for sym in filter(None, (s.strip() for s in part.split("*"))):
                base, _, exp = sym.partition("^")
                canon = UNIT_CANON.get(base.lower(), base)
                if not canon:
                    continue
                try:
                    power = int(exp) if exp else 1
                except ValueError as exc:
                    raise MalformedInput(f"bad unit exponent in '{text}'") from exc
                dims[canon] = dims.get(canon, 0) + sign * power
        return Unit(tuple(sorted((k, v) for k, v in dims.items() if v)))

    def __mul__(self, other: "Unit") -> "Unit":
        d = dict(self.dims)
        for k, v in other.dims:
            d[k] = d.get(k, 0) + v
        return Unit(tuple(sorted((k, v) for k, v in d.items() if v)))

    def __truediv__(self, other: "Unit") -> "Unit":
        return self * Unit(tuple((k, -v) for k, v in other.dims))

    def __pow__(self, n: int) -> "Unit":
        return Unit(tuple((k, v * n) for k, v in self.dims if v * n))

    def __str__(self) -> str:
        num = "*".join(k if v == 1 else f"{k}^{v}" for k, v in self.dims if v > 0)
        den = "*".join(k if v == -1 else f"{k}^{-v}" for k, v in self.dims if v < 0)
        return (num or ("1" if den else "")) + (f"/{den}" if den else "")


DIMENSIONLESS = Unit()


@dataclass(frozen=True)
class Quantity:
    value: float
    unit: Unit = DIMENSIONLESS

    def __post_init__(self) -> None:
        if not isinstance(self.value, (int, float)) or isinstance(self.value, bool):
            raise MalformedInput(f"quantity value must be a number, got {type(self.value).__name__}")
        if not math.isfinite(self.value):
            raise NonFiniteValue(f"non-finite value {self.value}")


def _finite(x: float, where: str) -> float:
    if not math.isfinite(x):
        raise NonFiniteValue(f"result is {x}", where)
    return x


# ---- expression nodes --------------------------------------------------------

class Expr:
    op = "expr"

    def evaluate(self, env: Mapping[str, Quantity]) -> Quantity:
        raise NotImplementedError

    def unit(self, var_units: Mapping[str, Unit]) -> Unit:
        raise NotImplementedError

    def variables(self) -> set[str]:
        return set()

    def to_json(self) -> dict:
        raise NotImplementedError

    def __str__(self) -> str:  # human-readable, for reports only
        return self.op


@dataclass(frozen=True, eq=True)
class Const(Expr):
    value: float
    unit_text: str = ""
    op = "const"

    def __post_init__(self) -> None:
        Quantity(self.value)  # validates number and finiteness
        Unit.parse(self.unit_text)

    def evaluate(self, env):
        return Quantity(float(self.value), Unit.parse(self.unit_text))

    def unit(self, var_units):
        return Unit.parse(self.unit_text)

    def to_json(self):
        return {"op": "const", "value": self.value, "unit": self.unit_text}

    def __str__(self):
        return f"{self.value:g}{(' ' + self.unit_text) if self.unit_text else ''}"


@dataclass(frozen=True, eq=True)
class Var(Expr):
    name: str
    op = "var"

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not _NAME.match(self.name):
            raise UnsafeName(f"variable name {self.name!r} must match {_NAME.pattern}")

    def evaluate(self, env):
        if self.name not in env:
            raise UnknownReference(f"variable '{self.name}' has no value", self.name)
        q = env[self.name]
        return q if isinstance(q, Quantity) else Quantity(q)

    def unit(self, var_units):
        if self.name not in var_units:
            raise UnknownReference(f"variable '{self.name}' has no declared unit", self.name)
        return var_units[self.name]

    def variables(self):
        return {self.name}

    def to_json(self):
        return {"op": "var", "name": self.name}

    def __str__(self):
        return self.name


@dataclass(frozen=True, eq=True)
class _Binary(Expr):
    a: Expr
    b: Expr

    def variables(self):
        return self.a.variables() | self.b.variables()

    def to_json(self):
        return {"op": self.op, "args": [self.a.to_json(), self.b.to_json()]}


def _same_unit(ua: Unit, ub: Unit, op: str) -> Unit:
    if ua != ub:
        raise UnitMismatch(f"cannot {op} '{ua}' and '{ub}'")
    return ua


class Add(_Binary):
    op = "add"

    def evaluate(self, env):
        x, y = self.a.evaluate(env), self.b.evaluate(env)
        return Quantity(_finite(x.value + y.value, str(self)), _same_unit(x.unit, y.unit, "add"))

    def unit(self, vu):
        return _same_unit(self.a.unit(vu), self.b.unit(vu), "add")

    def __str__(self):
        return f"({self.a} + {self.b})"


class Sub(_Binary):
    op = "sub"

    def evaluate(self, env):
        x, y = self.a.evaluate(env), self.b.evaluate(env)
        return Quantity(_finite(x.value - y.value, str(self)), _same_unit(x.unit, y.unit, "subtract"))

    def unit(self, vu):
        return _same_unit(self.a.unit(vu), self.b.unit(vu), "subtract")

    def __str__(self):
        return f"({self.a} - {self.b})"


class Mul(_Binary):
    op = "mul"

    def evaluate(self, env):
        x, y = self.a.evaluate(env), self.b.evaluate(env)
        return Quantity(_finite(x.value * y.value, str(self)), x.unit * y.unit)

    def unit(self, vu):
        return self.a.unit(vu) * self.b.unit(vu)

    def __str__(self):
        return f"({self.a} × {self.b})"


class Div(_Binary):
    op = "div"

    def evaluate(self, env):
        x, y = self.a.evaluate(env), self.b.evaluate(env)
        if y.value == 0:
            raise NonFiniteValue("division by zero", str(self))
        return Quantity(_finite(x.value / y.value, str(self)), x.unit / y.unit)

    def unit(self, vu):
        return self.a.unit(vu) / self.b.unit(vu)

    def __str__(self):
        return f"({self.a} / {self.b})"


class Min(_Binary):
    op = "min"

    def evaluate(self, env):
        x, y = self.a.evaluate(env), self.b.evaluate(env)
        return Quantity(min(x.value, y.value), _same_unit(x.unit, y.unit, "compare"))

    def unit(self, vu):
        return _same_unit(self.a.unit(vu), self.b.unit(vu), "compare")

    def __str__(self):
        return f"min({self.a}, {self.b})"


class Max(_Binary):
    op = "max"

    def evaluate(self, env):
        x, y = self.a.evaluate(env), self.b.evaluate(env)
        return Quantity(max(x.value, y.value), _same_unit(x.unit, y.unit, "compare"))

    def unit(self, vu):
        return _same_unit(self.a.unit(vu), self.b.unit(vu), "compare")

    def __str__(self):
        return f"max({self.a}, {self.b})"


@dataclass(frozen=True, eq=True)
class Neg(Expr):
    a: Expr
    op = "neg"

    def evaluate(self, env):
        x = self.a.evaluate(env)
        return Quantity(-x.value, x.unit)

    def unit(self, vu):
        return self.a.unit(vu)

    def variables(self):
        return self.a.variables()

    def to_json(self):
        return {"op": "neg", "args": [self.a.to_json()]}

    def __str__(self):
        return f"-{self.a}"


@dataclass(frozen=True, eq=True)
class Pow(Expr):
    a: Expr
    exponent: int
    op = "pow"

    def __post_init__(self) -> None:
        if not isinstance(self.exponent, int) or isinstance(self.exponent, bool) or abs(self.exponent) > MAX_POWER:
            raise MalformedInput(f"exponent must be an integer with |n| <= {MAX_POWER}")

    def evaluate(self, env):
        x = self.a.evaluate(env)
        if x.value == 0 and self.exponent < 0:
            raise NonFiniteValue("zero raised to a negative power", str(self))
        return Quantity(_finite(x.value ** self.exponent, str(self)), x.unit ** self.exponent)

    def unit(self, vu):
        return self.a.unit(vu) ** self.exponent

    def variables(self):
        return self.a.variables()

    def to_json(self):
        return {"op": "pow", "args": [self.a.to_json()], "exponent": self.exponent}

    def __str__(self):
        return f"{self.a}^{self.exponent}"


_BINARY = {"add": Add, "sub": Sub, "mul": Mul, "div": Div, "min": Min, "max": Max}


def from_json(data: object, _depth: int = 0, _count: list[int] | None = None) -> Expr:
    """Parse a serialized expression, validating shape, names, numbers, depth and size."""
    count = _count if _count is not None else [0]
    count[0] += 1
    if count[0] > MAX_NODES:
        raise InputTooLarge(f"expression has more than {MAX_NODES} nodes")
    if _depth > MAX_DEPTH:
        raise InputTooLarge(f"expression is deeper than {MAX_DEPTH}")
    if not isinstance(data, dict) or "op" not in data:
        raise MalformedInput("expression node must be an object with 'op'")
    op = data["op"]
    if op == "const":
        if not isinstance(data.get("value"), (int, float)) or isinstance(data.get("value"), bool):
            raise MalformedInput("const needs a numeric 'value'")
        return Const(float(data["value"]), str(data.get("unit", "")))
    if op == "var":
        return Var(data.get("name", ""))
    args = data.get("args")
    if not isinstance(args, list):
        raise MalformedInput(f"'{op}' needs an 'args' list")
    if op in _BINARY:
        if len(args) != 2:
            raise MalformedInput(f"'{op}' takes exactly 2 arguments")
        return _BINARY[op](from_json(args[0], _depth + 1, count), from_json(args[1], _depth + 1, count))
    if op == "neg":
        if len(args) != 1:
            raise MalformedInput("'neg' takes exactly 1 argument")
        return Neg(from_json(args[0], _depth + 1, count))
    if op == "pow":
        if len(args) != 1:
            raise MalformedInput("'pow' takes exactly 1 argument")
        return Pow(from_json(args[0], _depth + 1, count), data.get("exponent"))
    raise MalformedInput(f"unknown expression op '{op}'")


# ---- constraints -------------------------------------------------------------

OPS = ("<=", ">=", "==")
EQ_TOLERANCE = 1e-9


@dataclass(frozen=True)
class ConstraintCheck:
    satisfied: bool
    lhs: float
    rhs: float
    slack: float  # >= 0 when satisfied; how far from the boundary
    unit: str


@dataclass(frozen=True)
class Relation:
    """lhs op rhs, with units checked against declared variable units."""

    lhs: Expr
    op: str
    rhs: Expr

    def __post_init__(self) -> None:
        if self.op not in OPS:
            raise MalformedInput(f"constraint operator must be one of {OPS}, got {self.op!r}")

    def check_units(self, var_units: Mapping[str, Unit]) -> Unit:
        return _same_unit(self.lhs.unit(var_units), self.rhs.unit(var_units), "compare")

    def check(self, env: Mapping[str, Quantity]) -> ConstraintCheck:
        x, y = self.lhs.evaluate(env), self.rhs.evaluate(env)
        unit = _same_unit(x.unit, y.unit, "compare")
        if self.op == "<=":
            slack = y.value - x.value
            ok = slack >= -EQ_TOLERANCE
        elif self.op == ">=":
            slack = x.value - y.value
            ok = slack >= -EQ_TOLERANCE
        else:
            slack = -abs(x.value - y.value)
            ok = abs(x.value - y.value) <= EQ_TOLERANCE * max(1.0, abs(x.value), abs(y.value))
        return ConstraintCheck(ok, x.value, y.value, slack, str(unit))

    def variables(self) -> set[str]:
        return self.lhs.variables() | self.rhs.variables()

    def to_json(self) -> dict:
        return {"lhs": self.lhs.to_json(), "op": self.op, "rhs": self.rhs.to_json()}

    @staticmethod
    def from_json(data: object) -> "Relation":
        if not isinstance(data, dict):
            raise MalformedInput("relation must be an object with lhs, op, rhs")
        return Relation(from_json(data.get("lhs")), data.get("op"), from_json(data.get("rhs")))

    def __str__(self) -> str:
        return f"{self.lhs} {self.op} {self.rhs}"


Number = Union[int, float]


def env_of(values: Mapping[str, Number | tuple[Number, str] | Quantity]) -> dict[str, Quantity]:
    """Build an evaluation environment: {name: value} or {name: (value, unit)}."""
    out: dict[str, Quantity] = {}
    for name, v in values.items():
        Var(name)  # validates the name
        if isinstance(v, Quantity):
            out[name] = v
        elif isinstance(v, tuple):
            out[name] = Quantity(v[0], Unit.parse(v[1]))
        else:
            out[name] = Quantity(v)
    return out
