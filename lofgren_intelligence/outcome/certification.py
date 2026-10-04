"""Offline certification for V5 Outcome Intelligence."""

from __future__ import annotations

import copy
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable

from ..execution.certification import _base as v4_base, NOW
from ..execution.core import InMemoryAdapter, execute_authorized
from .core import (
    Measurement,
    OutcomeError,
    build_measurement_contract,
    evaluate_outcome,
    verify_outcome_receipt,
)

CODE_TERMS = (
    "V4ActionBoundaryValidated",
    "MeasurementContractsTyped",
    "ExpectedVsActualMeasured",
    "MissingMeasurementFailsClosed",
    "UnitsEnforced",
    "CausalityNotAssumed",
    "ValidatedCausalDesignBounded",
    "OutcomeReceiptVerified",
    "V6HandoffValidated",
    "V5E2ECertificationPassing",
)
PROCESS_TERMS = (
    "V5RegressionPassing",
    "PackageGatePassing",
    "GitHubCIPassing",
    "WorkingTreeClean",
    "ExactSHAPinned",
)
GATE_ORDER = CODE_TERMS + PROCESS_TERMS


@dataclass
class Scenario:
    term: str
    scenario: str
    passed: bool
    detail: str


def _one(term: str, scenario: str, fn: Callable[[], str]) -> Scenario:
    try:
        return Scenario(term, scenario, True, fn())
    except Exception as exc:
        return Scenario(term, scenario, False, f"{type(exc).__name__}: {exc}")


def _fixture():
    product, request, grant, approval = v4_base()
    executed = execute_authorized(product.v4_handoff, request, grant, approval, InMemoryAdapter(), subject="user-cert", now=NOW)
    assert executed.v5_handoff is not None
    contracts = build_measurement_contract(executed.v5_handoff)
    measurements = [
        Measurement(c.metric, c.expected, c.unit, (NOW + timedelta(days=30)).isoformat(), "certified-fixture", f"OBS-{i}")
        for i, c in enumerate(contracts, 1)
    ]
    return executed, contracts, measurements


def run_v5_certification() -> dict:
    rows: list[Scenario] = []

    def boundary():
        executed, _, _ = _fixture()
        assert executed.v5_handoff and executed.v5_handoff["measurement_required"] is True
        return executed.v5_handoff["action_id"]
    rows.append(_one("V4ActionBoundaryValidated", "only committed V4 action reaches V5", boundary))

    def contracts():
        _, cs, _ = _fixture()
        assert cs and all(c.tolerance >= 0 for c in cs)
        return f"{len(cs)} typed measurement contracts"
    rows.append(_one("MeasurementContractsTyped", "expected outcomes compile to contracts", contracts))

    def expected_actual():
        executed, cs, ms = _fixture()
        out = evaluate_outcome(executed.v5_handoff, ms, contracts=cs)
        assert out.receipt["status"] == "successful"
        assert all(x["met"] for x in out.receipt["measurements"])
        return "expected vs actual measured and within tolerance"
    rows.append(_one("ExpectedVsActualMeasured", "measurements are compared to expectations", expected_actual))

    def missing():
        executed, cs, ms = _fixture()
        out = evaluate_outcome(executed.v5_handoff, ms[:-1], contracts=cs)
        assert out.receipt["status"] == "insufficient_measurement"
        assert out.receipt["missing_metrics"]
        assert out.v6_handoff["improvement_allowed"] is False
        return "missing measurement blocks improvement"
    rows.append(_one("MissingMeasurementFailsClosed", "missing metric is explicit", missing))

    def units():
        executed, cs, ms = _fixture()
        bad = list(ms)
        bad[0] = Measurement(ms[0].metric, ms[0].value, "wrong-unit", ms[0].observed_at, ms[0].source)
        try:
            evaluate_outcome(executed.v5_handoff, bad, contracts=cs)
        except OutcomeError:
            return "unit mismatch refused"
        raise AssertionError("unit mismatch accepted")
    rows.append(_one("UnitsEnforced", "measurement units must match contract", units))

    def causal_default():
        executed, cs, ms = _fixture()
        out = evaluate_outcome(executed.v5_handoff, ms, contracts=cs)
        assert out.receipt["causal"]["standing"] == "descriptive_only"
        return "successful result still does not imply causality"
    rows.append(_one("CausalityNotAssumed", "correlation/observation is not causal proof", causal_default))

    def causal_validated():
        executed, cs, ms = _fixture()
        design = {"kind": "randomized_experiment", "validated": True, "scope": "fixture only"}
        out = evaluate_outcome(executed.v5_handoff, ms, contracts=cs, causal_design=design)
        assert out.receipt["causal"]["standing"] == "causal_supported"
        return "validated randomized design permits bounded causal standing"
    rows.append(_one("ValidatedCausalDesignBounded", "causal claim requires validated controlled design", causal_validated))

    def receipt():
        executed, cs, ms = _fixture()
        out = evaluate_outcome(executed.v5_handoff, ms, contracts=cs)
        assert verify_outcome_receipt(out.receipt)
        bad = copy.deepcopy(out.receipt)
        bad["status"] = "forged"
        assert not verify_outcome_receipt(bad)
        return out.receipt["receipt_hash"]
    rows.append(_one("OutcomeReceiptVerified", "outcome receipt detects tampering", receipt))

    def v6():
        executed, cs, ms = _fixture()
        out = evaluate_outcome(executed.v5_handoff, ms, contracts=cs)
        h = out.v6_handoff
        assert h["schema"] == "lofgren.v6-handoff/1"
        assert h["outcome_receipt_hash"] == out.receipt["receipt_hash"]
        assert h["improvement_allowed"] is True
        return "verified measured outcome handed to V6"
    rows.append(_one("V6HandoffValidated", "V6 handoff carries measured state", v6))

    def e2e():
        executed, cs, ms = _fixture()
        out = evaluate_outcome(executed.v5_handoff, ms, contracts=cs)
        assert verify_outcome_receipt(out.receipt)
        assert out.v6_handoff["schema"] == "lofgren.v6-handoff/1"
        return "V4 action -> measurement -> expected/actual -> receipt -> V6"
    rows.append(_one("V5E2ECertificationPassing", "complete bounded V5 journey", e2e))

    terms = {term: any(r.term == term for r in rows) and all(r.passed for r in rows if r.term == term) for term in CODE_TERMS}
    return {"schema": "lofgren.v5-certification/1", "terms": terms, "scenarios": [r.__dict__ for r in rows],
            "code_terms_certified": all(terms.values())}


def render_v5_certification(cert: dict) -> str:
    lines = ["Lofgren Intelligence V5 Outcome Certification", "=" * 49]
    for row in cert["scenarios"]:
        lines.append(f"[{'PASS' if row['passed'] else 'FAIL'}] {row['term']}: {row['scenario']} — {row['detail']}")
    lines += ["-" * 49, f"V5CodeTerms = {'TRUE' if cert['code_terms_certified'] else 'FALSE'}"]
    return "\n".join(lines)
