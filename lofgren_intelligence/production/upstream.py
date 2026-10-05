"""Bind a V2 -> V3 handoff to the discovery that produced it, value by value.

V2's handoff validator checks ids, fingerprints, typing and the selected candidate. It does not check that each
number in the handoff equals the object it cites, so a handoff whose rent was edited from 12 to 1 still passes.
V3 closes that before building anything:

1. the discovery context's objects match the digests recorded in the (verified) discovery receipt;
2. every specification equals its cited source: a named assumption, the selected candidate's scenario parameter or
   a V1 known claim;
3. every expected outcome equals the selected candidate's simulation;
4. every constraint equals the context constraint with its id;
5. the "success" criterion equals the design recorded in the receipt and every other criterion equals a constraint.
"""

from __future__ import annotations

from typing import Any, Mapping

from ..discovery.context import DiscoveryContext
from ..discovery.errors import DiscoveryError
from ..discovery.receipt import objects_match_receipt


def _same(a: Any, b: Any) -> bool:
    if isinstance(a, (int, float)) and isinstance(b, (int, float)) and not isinstance(a, bool):
        return float(a) == float(b)
    return a == b


def handoff_integrity_problems(handoff: Mapping[str, Any], receipt: Mapping[str, Any],
                               context: DiscoveryContext) -> list[str]:
    problems = [f"discovery context: {p}" for p in objects_match_receipt(receipt, context.objects())[:5]]
    if problems:
        return problems
    selected = (handoff.get("selected_candidate") or {}).get("id")

    def obj(oid: str):
        return context.get(oid) if isinstance(oid, str) and oid in context else None

    for i, s in enumerate(handoff.get("specifications") or []):
        where, name, sid = f"specifications[{i}] {s.get('name')!r}", s.get("name"), s.get("source_id")
        if isinstance(sid, str) and sid.startswith("CL-"):
            try:
                claim = context.reference(sid, ("claim:known",))
            except DiscoveryError as exc:
                problems.append(f"{where}: {sid} is not a known V1 claim: {exc}")
                continue
            got = (claim.get("value"), claim.get("unit") or "")
        else:
            o = obj(sid)
            kind = type(o).__name__
            if kind == "Assumption":
                if o.name != name:
                    problems.append(f"{where}: cites assumption {sid} named {o.name!r}")
                    continue
                got = (o.value, o.unit or "")
            elif kind == "Scenario":
                if o.candidate_id != selected:
                    problems.append(f"{where}: cites scenario {sid} of another candidate")
                    continue
                p = o.parameters.get(name)
                got = (p.get("value"), p.get("unit") or "") if p else (None, None)
            else:
                problems.append(f"{where}: source {sid!r} is not an assumption, scenario or known claim here")
                continue
        if not (_same(got[0], s.get("value")) and got[1] == (s.get("unit") or "")):
            problems.append(f"{where}: handoff says {s.get('value')} {s.get('unit')!r}, its source {sid} says "
                            f"{got[0]} {got[1]!r}")

    for i, out in enumerate(handoff.get("expected_outcomes") or []):
        where, sim = f"expected_outcomes[{i}] {out.get('metric')!r}", obj(out.get("simulation_id"))
        if type(sim).__name__ != "Simulation" or sim.candidate_id != selected:
            problems.append(f"{where}: {out.get('simulation_id')!r} is not the selected candidate's simulation")
            continue
        stats = sim.outcomes.get(out.get("metric"))
        if stats is None or any(not _same(stats.get(k), out.get(k)) for k in ("mean", "p5", "p50", "p95", "unit")):
            problems.append(f"{where}: differs from simulation {sim.id}")

    constraints = {}
    for i, c in enumerate(handoff.get("constraints") or []):
        o = obj(c.get("id"))
        if type(o).__name__ != "Constraint" or o.name != c.get("name") or o.relation.to_json() != c.get("relation"):
            problems.append(f"constraints[{i}] {c.get('name')!r}: differs from context constraint {c.get('id')!r}")
        else:
            constraints[o.name] = o.relation.to_json()

    success = ((receipt.get("config") or {}).get("design") or {}).get("success")
    for i, crit in enumerate(handoff.get("acceptance_criteria") or []):
        name, rel = crit.get("name"), crit.get("relation")
        expected = success if name == "success" and success is not None else constraints.get(name)
        if expected is None or expected != rel:
            problems.append(f"acceptance_criteria[{i}] {name!r}: not the recorded design success relation or a "
                            "constraint of this discovery")
    return problems
