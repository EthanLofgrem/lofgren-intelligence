"""The V2 -> V3 handoff (`lofgren.v3-handoff/1`): everything V3 needs to build the selected candidate, typed.

Produced only when a candidate was selected. Otherwise there is no handoff: the outcome (`infeasible`,
`requires_research`, ...) and its evidence requirements are the result.

`validate_handoff` fails closed when:

    - V2-authored free text (candidate description, rejection reasons, risks, dependencies, test requirements)
      contains a number with no typed counterpart in the specifications, assumptions, constraints, expected outcomes
      or alternative measures ("no hidden untyped assumptions"); V1 evidence text is evidence, not V2 assertion
    - anything other than a V1 known claim sits under `verified_evidence` (a hypothesis there is a promotion)
    - an acceptance criterion is not machine-evaluable (structured relation, every variable typed, units agree)
    - a fingerprint or receipt id does not match the discovery receipt, or the receipt itself does not verify
    - the selected candidate is infeasible, dominated or requires research
"""

from __future__ import annotations

import re
from typing import Any, Mapping

from .context import DiscoveryContext
from .errors import DiscoveryError, MalformedInput
from .expr import Relation, Unit
from .receipt import verify_discovery_receipt
from .types import CandidateStatus, HypothesisStatus

HANDOFF_SCHEMA = "lofgren.v3-handoff/1"
_ID = re.compile(r"\b[A-Z]{1,5}-[0-9A-Za-z]+\b")
_NUMBER = re.compile(r"(?<![\w.])-?\d+(?:[.,]\d+)*(?:\.\d+)?")
_FREE_TEXT_FIELDS = ("risks", "dependencies", "test_requirements")


class HandoffInvalid(MalformedInput):
    def __init__(self, problems: list[str]) -> None:
        self.problems = list(problems)
        super().__init__("V3 handoff refused: " + "; ".join(self.problems[:5]), "v3_handoff")


def _numbers(text: str) -> list[float]:
    out = []
    for token in _NUMBER.findall(_ID.sub(" ", text or "")):
        try:
            out.append(float(token.replace(",", "")))
        except ValueError:
            continue
    return out


def _typed_numbers(h: Mapping) -> set[float]:
    found: set[float] = set()

    def collect(value: Any) -> None:
        if isinstance(value, bool):
            return
        if isinstance(value, (int, float)):
            found.add(round(float(value), 6))
        elif isinstance(value, Mapping):
            for v in value.values():
                collect(v)
        elif isinstance(value, (list, tuple)):
            for v in value:
                collect(v)

    for key in ("specifications", "assumptions", "constraints", "expected_outcomes", "acceptance_criteria",
                "resource_requirements", "cost_estimates"):
        collect(h.get(key))
    for alt in h.get("alternatives", []):
        collect(alt.get("measures"))
    collect(h.get("selected_candidate", {}).get("measures"))
    return found


def validate_handoff(h: Any, receipt: Mapping | None = None, context: DiscoveryContext | None = None) -> list[str]:
    """Every problem with a handoff; empty when it is valid."""
    if not isinstance(h, Mapping) or h.get("schema") != HANDOFF_SCHEMA:
        return [f"not a {HANDOFF_SCHEMA} handoff"]
    problems: list[str] = []
    typed = _typed_numbers(h)
    sel = h.get("selected_candidate") or {}
    texts = [("selected_candidate.description", sel.get("description", ""))]
    texts += [(f"alternatives[{i}].reason", a.get("reason", "")) for i, a in enumerate(h.get("alternatives", []))]
    texts += [(f"{name}[{i}]", t) for name in _FREE_TEXT_FIELDS for i, t in enumerate(h.get(name, []))]
    for where, text in texts:
        hidden = [n for n in _numbers(text) if round(n, 6) not in typed]
        if hidden:
            problems.append(f"{where} states {hidden} with no typed counterpart (hidden untyped assumption)")

    for i, ev in enumerate(h.get("verified_evidence", [])):
        cid = ev.get("claim_id", "")
        if ev.get("kind") != "verified_fact" or not str(cid).startswith("CL-"):
            problems.append(f"verified_evidence[{i}] is {cid!r} of kind {ev.get('kind')!r}: only V1 known claims "
                            "may stand as verified evidence")
        elif context is not None:
            try:
                context.reference(cid, ("claim:known",))
            except DiscoveryError as exc:
                problems.append(f"verified_evidence[{i}] {cid} is not a known claim in this knowledge map: {exc}")

    declared = {s["name"]: s.get("unit", "") for s in h.get("specifications", [])}
    declared.update({o["metric"]: o.get("unit", "") for o in h.get("expected_outcomes", [])})
    if not h.get("acceptance_criteria"):
        problems.append("there are no acceptance criteria to test the built artifact against")
    for i, crit in enumerate(h.get("acceptance_criteria", [])):
        try:
            rel = Relation.from_json(crit.get("relation"))
            units = {**declared, **crit.get("variable_units", {})}
            missing = rel.variables() - set(units)
            if missing:
                problems.append(f"acceptance_criteria[{i}] uses untyped variables {sorted(missing)}")
            else:
                rel.check_units({k: Unit.parse(v) for k, v in units.items() if k in rel.variables()})
        except DiscoveryError as exc:
            problems.append(f"acceptance_criteria[{i}] is not machine-evaluable: {exc}")

    if sel.get("status") != CandidateStatus.SELECTED.value:
        problems.append(f"the handed-off candidate has status {sel.get('status')!r}, not selected")
    if context is not None and sel.get("id") in context:
        status = context.get(sel["id"]).status
        if status != CandidateStatus.SELECTED:
            problems.append(f"{sel['id']} is {status.value} in this context")

    if receipt is not None:
        if not verify_discovery_receipt(receipt):
            problems.append("the discovery receipt does not verify")
        if h.get("discovery_receipt_id") != receipt.get("discovery_id"):
            problems.append("discovery_receipt_id does not match the receipt")
        if h.get("discovery_fingerprint") != receipt.get("discovery_fingerprint"):
            problems.append("discovery_fingerprint does not match the receipt")
        if h.get("evidence_fingerprint") != receipt.get("evidence"):
            problems.append("evidence_fingerprint does not match the receipt")
        if sel.get("id") != receipt.get("selected_candidate_id"):
            problems.append("the selected candidate is not the one the receipt recorded")
    for i, hyp in enumerate(h.get("hypotheses", [])):
        if hyp.get("status") not in {s.value for s in HypothesisStatus}:
            problems.append(f"hypotheses[{i}] has status {hyp.get('status')!r}")
    return problems


def check_handoff(h: Any, receipt: Mapping | None = None, context: DiscoveryContext | None = None) -> Mapping:
    problems = validate_handoff(h, receipt, context)
    if problems:
        raise HandoffInvalid(problems)
    return h
