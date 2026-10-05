"""Specification compiler: a validated V3 handoff -> typed, numbered requirements.

Every requirement V3 must satisfy gets a stable id, a kind and the typed source it came from. The planner maps
requirements to files and the verifier checks that every one is covered. Compilation fails closed: a duplicate or
unnamed specification, a non-finite value, an unparseable unit, an acceptance criterion over an undefined variable or
with inconsistent units, or a handoff with no acceptance criteria is refused.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Any, Mapping

from ..discovery.errors import DiscoveryError
from ..discovery.expr import Relation, Unit
from .errors import SpecificationError

_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
REQUIREMENT_KINDS = ("specification", "outcome", "acceptance", "constraint", "test", "dependency")
_PREFIX = {"specification": "SPEC", "outcome": "OUT", "acceptance": "ACC", "constraint": "CON", "test": "TST",
           "dependency": "DEP"}


@dataclass(frozen=True)
class Requirement:
    id: str
    kind: str
    name: str
    detail: str
    source_id: str

    def as_dict(self) -> dict[str, str]:
        return {"id": self.id, "kind": self.kind, "name": self.name, "detail": self.detail,
                "source_id": self.source_id}


@dataclass(frozen=True)
class ProductionSpec:
    requirements: tuple[Requirement, ...]
    values: Mapping[str, tuple[float, str]]  # specification values and expected-outcome means, with units
    checks: tuple[tuple[str, str, str, dict], ...]  # (requirement id, kind, name, canonical relation JSON)

    @property
    def criteria(self) -> tuple[tuple[str, dict], ...]:
        return tuple((name, rel) for _, kind, name, rel in self.checks if kind == "acceptance")

    def ids(self, kind: str | None = None) -> list[str]:
        return [r.id for r in self.requirements if kind is None or r.kind == kind]

    def as_list(self) -> list[dict[str, str]]:
        return [r.as_dict() for r in self.requirements]


def _number(value: Any, where: str, problems: list[str]) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        problems.append(f"{where}: value must be a number")
        return None
    if not math.isfinite(value):
        problems.append(f"{where}: value is not finite")
        return None
    return float(value)


def _unit(text: Any, where: str, problems: list[str]) -> str:
    try:
        Unit.parse(text or "")
    except DiscoveryError as exc:
        problems.append(f"{where}: {exc}")
    return text or ""


def compile_specification(handoff: Mapping[str, Any]) -> ProductionSpec:
    """Compile a V3 handoff into numbered requirements. Raises SpecificationError listing every problem."""
    problems: list[str] = []
    reqs: list[Requirement] = []
    values: dict[str, tuple[float, str]] = {}

    def add(kind: str, name: str, detail: str, source_id: str) -> None:
        n = sum(1 for r in reqs if r.kind == kind) + 1
        reqs.append(Requirement(f"REQ-{_PREFIX[kind]}-{n:03d}", kind, name, detail, source_id))

    for i, spec in enumerate(handoff.get("specifications") or []):
        name = spec.get("name") if isinstance(spec, Mapping) else None
        where = f"specifications[{i}]"
        if not isinstance(name, str) or not _NAME.match(name):
            problems.append(f"{where}: name {name!r} is not a valid identifier")
            continue
        if name in values:
            problems.append(f"{where}: duplicate specification {name!r}")
            continue
        value = _number(spec.get("value"), where, problems)
        unit = _unit(spec.get("unit"), where, problems)
        if value is None:
            continue
        values[name] = (value, unit)
        add("specification", name, f"{name} = {value:g} {unit}".rstrip(), str(spec.get("source_id", "")))

    for i, out in enumerate(handoff.get("expected_outcomes") or []):
        name = out.get("metric") if isinstance(out, Mapping) else None
        where = f"expected_outcomes[{i}]"
        if not isinstance(name, str) or not _NAME.match(name):
            problems.append(f"{where}: metric {name!r} is not a valid identifier")
            continue
        if name in values:
            problems.append(f"{where}: metric {name!r} collides with a specification")
            continue
        value = _number(out.get("mean"), where, problems)
        unit = _unit(out.get("unit"), where, problems)
        if value is None:
            continue
        values[name] = (value, unit)
        add("outcome", name, f"expected {name} (simulated mean) = {value:g} {unit}".rstrip(),
            str(out.get("simulation_id", "")))

    checks: list[tuple[str, str, str, dict]] = []
    units = {k: Unit.parse(u) for k, (_, u) in values.items()}

    def relation_of(raw: Any, where: str, name: str, required: bool) -> Relation | None:
        try:
            relation = Relation.from_json(raw)
        except DiscoveryError as exc:
            if required:
                problems.append(f"{where}: {exc}")
            return None
        undefined = sorted(relation.variables() - set(values))
        if undefined:
            if required:
                problems.append(f"{where} ({name}): undefined variables {undefined}")
            return None
        try:
            relation.check_units(units)
        except DiscoveryError as exc:
            problems.append(f"{where} ({name}): {exc}")
            return None
        return relation

    for i, criterion in enumerate(handoff.get("acceptance_criteria") or []):
        where = f"acceptance_criteria[{i}]"
        criterion = criterion if isinstance(criterion, Mapping) else {}
        name = criterion.get("name")
        name = name if isinstance(name, str) and name.strip() else f"criterion-{i + 1}"
        relation = relation_of(criterion.get("relation"), where, name, required=True)
        if relation is not None:
            add("acceptance", name, str(relation), f"criterion-{i + 1}")
            checks.append((reqs[-1].id, "acceptance", name, relation.to_json()))
    if not any(kind == "acceptance" for _, kind, _, _ in checks) and not problems:
        problems.append("the handoff has no acceptance criteria")

    # A constraint over specified values becomes an executable check; one over values the handoff does not carry
    # (a design-time variable) stays a documented requirement.
    for i, c in enumerate(handoff.get("constraints") or []):
        if not isinstance(c, Mapping):
            continue
        name = str(c.get("name") or f"constraint-{i + 1}")
        relation = relation_of(c.get("relation"), f"constraints[{i}]", name, required=False)
        add("constraint", name, str(relation) if relation is not None else str(c.get("relation") or ""),
            str(c.get("id", "")))
        if relation is not None:
            checks.append((reqs[-1].id, "constraint", name, relation.to_json()))
    for i, t in enumerate(handoff.get("test_requirements") or []):
        add("test", f"test-{i + 1}", t if isinstance(t, str) else str(t), "")
    for i, d in enumerate(handoff.get("dependencies") or []):
        add("dependency", f"dependency-{i + 1}", d if isinstance(d, str) else str(d), "")

    if problems:
        raise SpecificationError("; ".join(problems))
    return ProductionSpec(tuple(reqs), values, tuple(checks))
