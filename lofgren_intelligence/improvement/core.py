"""V6 Improvement Intelligence.

V6 consumes a measured V5 outcome and may recommend a reviewed improvement.
It never silently rewrites models, policies, prompts, evidence, receipts or a
running system. Candidate changes are evaluated against a distinct baseline on
a named held-out dataset with minimum sample size, required gain and safety
constraints. The result is an immutable recommendation plus a next-cycle
handoff, not an autonomous mutation.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from typing import Any, Mapping

IMPROVEMENT_RECEIPT_SCHEMA = "lofgren.improvement-receipt/1"
NEXT_CYCLE_SCHEMA = "lofgren.next-cycle/1"
V6_HANDOFF_SCHEMA = "lofgren.v6-handoff/1"


class ImprovementError(ValueError):
    pass


def _canonical(v: Any) -> str:
    return json.dumps(v, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def _hash(v: Any) -> str:
    return hashlib.sha256(_canonical(v).encode("utf-8")).hexdigest()


def _finite(v: Any, where: str) -> float:
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        raise ImprovementError(f"{where} must be numeric")
    x = float(v)
    if not math.isfinite(x):
        raise ImprovementError(f"{where} must be finite")
    return x


@dataclass(frozen=True)
class EvaluationDataset:
    dataset_id: str
    content_hash: str
    sample_count: int
    held_out: bool = True

    def __post_init__(self) -> None:
        if not self.dataset_id or not self.content_hash:
            raise ImprovementError("evaluation dataset identity is required")
        if self.sample_count < 1:
            raise ImprovementError("evaluation sample_count must be positive")
        if not self.held_out:
            raise ImprovementError("improvement certification requires a held-out dataset")


@dataclass(frozen=True)
class MetricObservation:
    metric: str
    baseline: float
    candidate: float
    direction: str = "higher_is_better"

    def __post_init__(self) -> None:
        if not self.metric:
            raise ImprovementError("metric is required")
        _finite(self.baseline, f"{self.metric}.baseline")
        _finite(self.candidate, f"{self.metric}.candidate")
        if self.direction not in {"higher_is_better", "lower_is_better"}:
            raise ImprovementError("unsupported metric direction")

    @property
    def gain(self) -> float:
        if self.direction == "higher_is_better":
            return float(self.candidate) - float(self.baseline)
        return float(self.baseline) - float(self.candidate)


@dataclass(frozen=True)
class SafetyConstraint:
    metric: str
    baseline: float
    candidate: float
    max_regression: float

    def __post_init__(self) -> None:
        if not self.metric:
            raise ImprovementError("safety metric is required")
        _finite(self.baseline, f"{self.metric}.baseline")
        _finite(self.candidate, f"{self.metric}.candidate")
        if _finite(self.max_regression, f"{self.metric}.max_regression") < 0:
            raise ImprovementError("max_regression must be >= 0")

    @property
    def regression(self) -> float:
        return max(0.0, float(self.baseline) - float(self.candidate))

    @property
    def passed(self) -> bool:
        return self.regression <= float(self.max_regression) + 1e-12


@dataclass(frozen=True)
class ImprovementProposal:
    proposal_id: str
    baseline_id: str
    candidate_id: str
    change_summary: str
    evaluation_dataset: EvaluationDataset
    primary_metric: MetricObservation
    min_gain: float
    safety_constraints: tuple[SafetyConstraint, ...]
    requires_human_review: bool = True

    def __post_init__(self) -> None:
        if not self.proposal_id or not self.baseline_id or not self.candidate_id:
            raise ImprovementError("proposal, baseline and candidate identities are required")
        if self.baseline_id == self.candidate_id:
            raise ImprovementError("baseline and candidate must be distinct")
        if not self.change_summary.strip():
            raise ImprovementError("change summary is required")
        if _finite(self.min_gain, "min_gain") < 0:
            raise ImprovementError("min_gain must be >= 0")
        if not self.requires_human_review:
            raise ImprovementError("V6 proposals must require human review")


@dataclass(frozen=True)
class ImprovementResult:
    decision: str
    receipt: dict[str, Any]
    next_cycle: dict[str, Any]


def verify_improvement_receipt(receipt: Mapping[str, Any]) -> bool:
    if receipt.get("schema") != IMPROVEMENT_RECEIPT_SCHEMA:
        return False
    body = dict(receipt)
    supplied = body.pop("receipt_hash", None)
    return supplied == "IR-" + _hash(body)


def evaluate_improvement(
    v6_handoff: Mapping[str, Any],
    proposal: ImprovementProposal,
    *,
    minimum_samples: int = 30,
) -> ImprovementResult:
    """Evaluate one candidate improvement without applying it."""
    if v6_handoff.get("schema") != V6_HANDOFF_SCHEMA:
        raise ImprovementError("invalid V6 handoff")
    if v6_handoff.get("improvement_allowed") is not True:
        raise ImprovementError("V5 outcome does not permit improvement evaluation")
    if not v6_handoff.get("outcome_receipt_hash"):
        raise ImprovementError("outcome provenance is missing")
    if proposal.evaluation_dataset.sample_count < minimum_samples:
        raise ImprovementError(
            f"held-out evaluation has {proposal.evaluation_dataset.sample_count} samples; "
            f"minimum is {minimum_samples}"
        )

    gain = proposal.primary_metric.gain
    primary_passed = gain + 1e-12 >= float(proposal.min_gain)
    failed_safety = [x.metric for x in proposal.safety_constraints if not x.passed]
    safety_passed = not failed_safety

    decision = "recommend_review" if primary_passed and safety_passed else "reject_candidate"
    body = {
        "schema": IMPROVEMENT_RECEIPT_SCHEMA,
        "proposal_id": proposal.proposal_id,
        "baseline_id": proposal.baseline_id,
        "candidate_id": proposal.candidate_id,
        "change_summary": proposal.change_summary,
        "outcome_receipt_hash": v6_handoff["outcome_receipt_hash"],
        "action_id": v6_handoff.get("action_id"),
        "artifact_id": v6_handoff.get("artifact_id"),
        "evaluation_dataset": {
            "dataset_id": proposal.evaluation_dataset.dataset_id,
            "content_hash": proposal.evaluation_dataset.content_hash,
            "sample_count": proposal.evaluation_dataset.sample_count,
            "held_out": proposal.evaluation_dataset.held_out,
        },
        "primary_metric": {
            "metric": proposal.primary_metric.metric,
            "baseline": proposal.primary_metric.baseline,
            "candidate": proposal.primary_metric.candidate,
            "direction": proposal.primary_metric.direction,
            "gain": gain,
            "min_gain": proposal.min_gain,
            "passed": primary_passed,
        },
        "safety_constraints": [
            {
                "metric": x.metric,
                "baseline": x.baseline,
                "candidate": x.candidate,
                "max_regression": x.max_regression,
                "regression": x.regression,
                "passed": x.passed,
            }
            for x in proposal.safety_constraints
        ],
        "safety_passed": safety_passed,
        "failed_safety_metrics": failed_safety,
        "decision": decision,
        "requires_human_review": True,
        "mutation_performed": False,
    }
    receipt = {**body, "receipt_hash": "IR-" + _hash(body)}
    if not verify_improvement_receipt(receipt):
        raise ImprovementError("improvement receipt failed self-verification")

    next_cycle = {
        "schema": NEXT_CYCLE_SCHEMA,
        "source_improvement_receipt_hash": receipt["receipt_hash"],
        "proposal_id": proposal.proposal_id,
        "decision": decision,
        "review_required": True,
        "apply_change": False,
        "recommended_candidate_id": proposal.candidate_id if decision == "recommend_review" else None,
        "objective": v6_handoff.get("objective", ""),
        "lessons": {
            "primary_gain": gain,
            "failed_safety_metrics": failed_safety,
            "outcome_status": v6_handoff.get("status"),
        },
    }
    return ImprovementResult(decision, receipt, next_cycle)
