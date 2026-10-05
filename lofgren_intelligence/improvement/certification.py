"""Offline certification for V6 Improvement Intelligence."""

from __future__ import annotations

import copy
from dataclasses import dataclass
from datetime import timedelta
from typing import Callable

from ..execution.certification import NOW, _base as v4_base
from ..execution.core import InMemoryAdapter, execute_authorized
from ..outcome.core import Measurement, build_measurement_contract, evaluate_outcome
from .core import (
    EvaluationDataset,
    ImprovementError,
    ImprovementProposal,
    MetricObservation,
    SafetyConstraint,
    evaluate_improvement,
    verify_improvement_receipt,
)

CODE_TERMS = (
    "V5OutcomeBoundaryValidated",
    "HeldOutEvaluationRequired",
    "MinimumSampleSizeEnforced",
    "BaselineCandidateDistinct",
    "MinimumGainEnforced",
    "SafetyConstraintsEnforced",
    "NoSilentMutation",
    "HumanReviewRequired",
    "ImprovementReceiptVerified",
    "NextCycleHandoffValidated",
    "V6E2ECertificationPassing",
)
PROCESS_TERMS = (
    "V6RegressionPassing",
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


def _v6_handoff():
    product, request, grant, approval = v4_base()
    executed = execute_authorized(
        product.v4_handoff, request, grant, approval, InMemoryAdapter(),
        subject="user-cert", now=NOW,
    )
    assert executed.v5_handoff is not None
    contracts = build_measurement_contract(executed.v5_handoff)
    measurements = [
        Measurement(c.metric, c.expected, c.unit, (NOW + timedelta(days=30)).isoformat(), "v6-cert", f"OBS-{i}")
        for i, c in enumerate(contracts, 1)
    ]
    outcome = evaluate_outcome(executed.v5_handoff, measurements, contracts=contracts)
    return outcome.v6_handoff


def _proposal(*, candidate=0.82, baseline=0.75, samples=100, max_regression=0.02):
    return ImprovementProposal(
        proposal_id="IMP-cert",
        baseline_id="BASE-v1",
        candidate_id="CAND-v2",
        change_summary="Adjust deterministic routing threshold after held-out evaluation.",
        evaluation_dataset=EvaluationDataset("DS-heldout", "sha256:" + "a" * 64, samples, True),
        primary_metric=MetricObservation("task_success", baseline, candidate, "higher_is_better"),
        min_gain=0.03,
        safety_constraints=(
            SafetyConstraint("evidence_integrity", 0.99, 0.99 - max(0, max_regression - 0.01), max_regression),
        ),
        requires_human_review=True,
    )


def run_v6_certification() -> dict:
    rows: list[Scenario] = []

    def boundary():
        h = _v6_handoff()
        out = evaluate_improvement(h, _proposal())
        assert out.receipt["outcome_receipt_hash"] == h["outcome_receipt_hash"]
        return h["outcome_receipt_hash"]
    rows.append(_one("V5OutcomeBoundaryValidated", "V6 binds to verified V5 outcome receipt", boundary))

    def heldout():
        try:
            EvaluationDataset("DS", "hash", 100, False)
        except ImprovementError:
            return "non-held-out dataset refused"
        raise AssertionError("training/non-held-out data accepted")
    rows.append(_one("HeldOutEvaluationRequired", "held-out dataset is mandatory", heldout))

    def samples():
        try:
            evaluate_improvement(_v6_handoff(), _proposal(samples=5), minimum_samples=30)
        except ImprovementError:
            return "undersized evaluation refused"
        raise AssertionError("undersized evaluation accepted")
    rows.append(_one("MinimumSampleSizeEnforced", "minimum evaluation size is enforced", samples))

    def distinct():
        p = _proposal()
        try:
            ImprovementProposal(
                p.proposal_id, "SAME", "SAME", p.change_summary, p.evaluation_dataset,
                p.primary_metric, p.min_gain, p.safety_constraints, True,
            )
        except ImprovementError:
            return "identical baseline/candidate refused"
        raise AssertionError("same baseline/candidate accepted")
    rows.append(_one("BaselineCandidateDistinct", "candidate must differ from baseline identity", distinct))

    def gain():
        out = evaluate_improvement(_v6_handoff(), _proposal(candidate=0.76, baseline=0.75))
        assert out.decision == "reject_candidate"
        assert out.receipt["primary_metric"]["passed"] is False
        return "candidate below minimum gain rejected"
    rows.append(_one("MinimumGainEnforced", "candidate must meet declared gain", gain))

    def safety():
        p = _proposal()
        bad = ImprovementProposal(
            p.proposal_id, p.baseline_id, p.candidate_id, p.change_summary,
            p.evaluation_dataset, p.primary_metric, p.min_gain,
            (SafetyConstraint("evidence_integrity", 0.99, 0.80, 0.01),), True,
        )
        out = evaluate_improvement(_v6_handoff(), bad)
        assert out.decision == "reject_candidate"
        assert "evidence_integrity" in out.receipt["failed_safety_metrics"]
        return "unsafe candidate rejected despite primary gain"
    rows.append(_one("SafetyConstraintsEnforced", "safety regression blocks recommendation", safety))

    def mutation():
        out = evaluate_improvement(_v6_handoff(), _proposal())
        assert out.receipt["mutation_performed"] is False
        assert out.next_cycle["apply_change"] is False
        return "evaluation recommends only; no runtime mutation occurs"
    rows.append(_one("NoSilentMutation", "V6 cannot silently apply candidate", mutation))

    def review():
        out = evaluate_improvement(_v6_handoff(), _proposal())
        assert out.receipt["requires_human_review"] is True
        assert out.next_cycle["review_required"] is True
        return "human review remains mandatory"
    rows.append(_one("HumanReviewRequired", "recommended improvement requires review", review))

    def receipt():
        out = evaluate_improvement(_v6_handoff(), _proposal())
        assert verify_improvement_receipt(out.receipt)
        bad = copy.deepcopy(out.receipt)
        bad["decision"] = "forged"
        assert not verify_improvement_receipt(bad)
        return out.receipt["receipt_hash"]
    rows.append(_one("ImprovementReceiptVerified", "improvement receipt detects tampering", receipt))

    def next_cycle():
        out = evaluate_improvement(_v6_handoff(), _proposal())
        h = out.next_cycle
        assert h["schema"] == "lofgren.next-cycle/1"
        assert h["review_required"] is True
        assert h["apply_change"] is False
        return "review-only next-cycle handoff created"
    rows.append(_one("NextCycleHandoffValidated", "V6 closes loop without auto-mutation", next_cycle))

    def e2e():
        h = _v6_handoff()
        out = evaluate_improvement(h, _proposal())
        assert out.decision == "recommend_review"
        assert verify_improvement_receipt(out.receipt)
        assert out.next_cycle["source_improvement_receipt_hash"] == out.receipt["receipt_hash"]
        return "V5 outcome -> held-out evaluation -> review recommendation -> next cycle"
    rows.append(_one("V6E2ECertificationPassing", "complete bounded V6 journey", e2e))

    terms = {
        term: any(r.term == term for r in rows) and all(r.passed for r in rows if r.term == term)
        for term in CODE_TERMS
    }
    return {
        "schema": "lofgren.v6-certification/1",
        "terms": terms,
        "scenarios": [r.__dict__ for r in rows],
        "code_terms_certified": all(terms.values()),
    }


def render_v6_certification(cert: dict) -> str:
    lines = ["Lofgren Intelligence V6 Improvement Certification", "=" * 53]
    for row in cert["scenarios"]:
        lines.append(f"[{'PASS' if row['passed'] else 'FAIL'}] {row['term']}: {row['scenario']} — {row['detail']}")
    lines += ["-" * 53, f"V6CodeTerms = {'TRUE' if cert['code_terms_certified'] else 'FALSE'}"]
    return "\n".join(lines)
