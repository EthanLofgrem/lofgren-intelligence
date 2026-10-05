"""Constraints and assumptions.

A constraint is a structured relation with a declared unit for every variable, checked when it is
built. It rests on exactly one source:

- a V1 claim that is *known* in this context (or the `KnownFact` built from it), or
- an explicit `Assumption` registered in this context.

An uncertain or contested claim cannot ground a constraint: state the needed value as an
assumption instead, so it stays visible and replaceable.
"""

from __future__ import annotations

from .context import DiscoveryContext
from .errors import MalformedInput
from .expr import Relation
from .types import Assumption, Constraint, ConstraintKind, utcnow


def state_assumption(context: DiscoveryContext, statement: str, why_assumed: str, name: str = "",
                     value: float | None = None, unit: str = "", derived_from: list[str] | tuple[str, ...] = (),
                     at: str | None = None) -> Assumption:
    return context.ensure(Assumption(statement, name, value, unit, why_assumed, derived_from=list(derived_from),
                                     created_at=at or utcnow()))


def ground_constraint(context: DiscoveryContext, name: str, kind: ConstraintKind | str, relation: Relation | dict,
                      variable_units: dict[str, str], *, fact: str | None = None, assumption: str | None = None,
                      at: str | None = None) -> Constraint:
    """A unit-checked constraint resting on one known fact or one registered assumption."""
    if (fact is None) == (assumption is None):
        raise MalformedInput("ground a constraint on exactly one of a known fact or an assumption",
                             "ground_constraint")
    return context.register(Constraint(name, kind, relation, fact, assumption, variable_units,
                                       created_at=at or utcnow()))
