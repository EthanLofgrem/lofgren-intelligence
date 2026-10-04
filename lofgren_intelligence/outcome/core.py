"""V5 Outcome Intelligence.

V5 compares expected outcomes from an executed V4 action with actual
observations. It records measurement lineage, expected-vs-actual error,
success/failure, limitations and causal standing. Descriptive observation is
not promoted into causal attribution.

V5 never rewrites V1 evidence, V2 discovery, V3 artifacts or V4 action
receipts. It produces a new immutable outcome receipt and a V6 handoff.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from typing import Any, Mapping

OUTCOME_RECEIPT_SCHEMA = "lofgren.outcome-receipt/1"
V5_HANDOFF_SCHEMA = "lofgren.v5-handoff/1"
V6_HANDOFF_SCHEMA = "lofgren.v6-handoff/1"


class OutcomeError(ValueError):
    pass


def _canonical(v: Any) -> str:
    return json.dumps(v, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def _hash(v: Any) -> str:
    return hashlib.sha256(_canonical(v).encode("utf-8")).hexdigest()


def _finite(value: Any, where: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise OutcomeError(f"{where} must be numeric")
    x = float(value)
    if not math.isfinite(x):
        raise OutcomeError(f"{where} must be finite")
    return x


@dataclass(frozen=True)
class MeasurementContract:
    metric: str
    unit: str
    expected: float
    tolerance: float
    direction: str = "two_sided"  # two_sided, at_least, at_most

    def __post_init__(self) -> None:
        if not self.metric:
            raise OutcomeError("measurement metric is required")
        _finite(self.expected, "expected")
        if _finite(self.tolerance, "tolerance") < 0:
            raise OutcomeError("tolerance must be >= 0")
        if self.direction not in {"two_sided", "at_least", "at_most"}:
            raise OutcomeError("unsupported measurement direction")


@dataclass(frozen=True)
class Measurement:
    metric: str
    value: float
    unit: str
    observed_at: str
    source: str
    observation_id: str | None = None

    def __post_init__(self) -> None:
        if not self.metric or not self.source or not self.observed_at:
            raise OutcomeError("measurement requires metric, observed_at and source")
        _finite(self.value, "measurement value")


@dataclass(frozen=True)
class OutcomeResult:
    receipt: dict[str, Any]
    v6_handoff: dict[str, Any]


def build_measurement_contract(v5_handoff: Mapping[str, Any], *, default_tolerance_fraction: float = 0.1) -> tuple[MeasurementContract, ...]:
    if v5_handoff.get("schema") != V5_HANDOFF_SCHEMA:
        raise OutcomeError("invalid V5 handoff")
    if v5_handoff.get("measurement_required") is not True:
        raise OutcomeError("V5 handoff does not require measurement")
    fraction = _finite(default_tolerance_fraction, "default_tolerance_fraction")
    if not 0 <= fraction <= 1:
        raise OutcomeError("default tolerance fraction must be within [0,1]")

    out: list[MeasurementContract] = []
    for item in v5_handoff.get("expected_outcomes", []):
        metric = str(item.get("metric") or "")
        expected = _finite(item.get("mean"), f"{metric}.mean")
        unit = str(item.get("unit") or "")
        tol = abs(expected) * fraction
        out.append(MeasurementContract(metric, unit, expected, tol))
    if not out:
        raise OutcomeError("V5 handoff has no expected outcomes to measure")
    return tuple(out)


def _success(contract: MeasurementContract, actual: float) -> bool:
    if contract.direction == "at_least":
        return actual + contract.tolerance >= contract.expected
    if contract.direction == "at_most":
        return actual - contract.tolerance <= contract.expected
    return abs(actual - contract.expected) <= contract.tolerance


def verify_outcome_receipt(receipt: Mapping[str, Any]) -> bool:
    if receipt.get("schema") != OUTCOME_RECEIPT_SCHEMA:
        return False
    body = dict(receipt)
    supplied = body.pop("receipt_hash", None)
    return supplied == "OR-" + _hash(body)


def evaluate_outcome(
    v5_handoff: Mapping[str, Any],
    measurements: list[Measurement],
    *,
    contracts: tuple[MeasurementContract, ...] | None = None,
    causal_design: Mapping[str, Any] | None = None,
) -> OutcomeResult:
    """Evaluate expected vs actual without silently claiming causality."""
    contracts = contracts or build_measurement_contract(v5_handoff)
    by_metric: dict[str, list[Measurement]] = {}
    for m in measurements:
        by_metric.setdefault(m.metric, []).append(m)

    rows: list[dict[str, Any]] = []
    missing: list[str] = []
    for contract in contracts:
        candidates = by_metric.get(contract.metric, [])
        if not candidates:
            missing.append(contract.metric)
            continue
        # Multiple observations are retained; the latest supplied observation is
        # the assessment point. Input order is explicit caller evidence order.
        m = candidates[-1]
        if m.unit != contract.unit:
            raise OutcomeError(f"unit mismatch for {contract.metric}: {m.unit!r} != {contract.unit!r}")
        actual = _finite(m.value, f"{contract.metric}.actual")
        error = actual - contract.expected
        pct = None if contract.expected == 0 else error / abs(contract.expected)
        rows.append({
            "metric": contract.metric,
            "unit": contract.unit,
            "expected": contract.expected,
            "actual": actual,
            "error": error,
            "absolute_error": abs(error),
            "percent_error": pct,
            "tolerance": contract.tolerance,
            "direction": contract.direction,
            "met": _success(contract, actual),
            "measurement": {
                "source": m.source,
                "observed_at": m.observed_at,
                "observation_id": m.observation_id,
            },
        })

    status = "insufficient_measurement" if missing else ("successful" if all(x["met"] for x in rows) else "underperformed")
    causal = {
        "standing": "descriptive_only",
        "claim": "Observed outcomes are associated with the executed action; causality has not been established.",
        "design": None,
    }
    if causal_design is not None:
        kind = causal_design.get("kind")
        if kind not in {"randomized_experiment", "controlled_quasi_experiment"}:
            raise OutcomeError("unsupported causal design; causal claims require an accepted controlled design")
        if causal_design.get("validated") is not True:
            raise OutcomeError("causal design is not independently validated")
        causal = {
            "standing": "causal_supported",
            "claim": "Causal interpretation is permitted only within the validated design and its stated scope.",
            "design": json.loads(json.dumps(causal_design)),
        }

    body = {
        "schema": OUTCOME_RECEIPT_SCHEMA,
        "action_id": v5_handoff.get("action_id"),
        "action_receipt_hash": v5_handoff.get("action_receipt_hash"),
        "artifact_id": v5_handoff.get("artifact_id"),
        "objective": v5_handoff.get("objective", ""),
        "status": status,
        "measurements": rows,
        "missing_metrics": sorted(missing),
        "causal": causal,
    }
    receipt = {**body, "receipt_hash": "OR-" + _hash(body)}
    if not verify_outcome_receipt(receipt):
        raise OutcomeError("outcome receipt failed self-verification")

    v6 = {
        "schema": V6_HANDOFF_SCHEMA,
        "outcome_receipt_hash": receipt["receipt_hash"],
        "action_id": body["action_id"],
        "artifact_id": body["artifact_id"],
        "objective": body["objective"],
        "status": status,
        "measurements": rows,
        "missing_metrics": sorted(missing),
        "causal": causal,
        "improvement_allowed": status != "insufficient_measurement",
    }
    return OutcomeResult(receipt, v6)
