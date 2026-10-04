"""Offline certification for V4 Execution Intelligence."""

from __future__ import annotations

import copy
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable

from ..production.certification import _fixture as v3_fixture
from ..production.core import build_artifact
from .core import (
    ActionRequest,
    ApprovalRecord,
    CapabilityGrant,
    ExecutionError,
    InMemoryAdapter,
    _validate_https_target,
    execute_authorized,
    verify_action_receipt,
)

NOW = datetime(2026, 10, 4, 12, 0, tzinfo=timezone.utc)

CODE_TERMS = (
    "V3ArtifactBoundaryValidated",
    "IdentityBoundAuthority",
    "CapabilityScopeEnforced",
    "BudgetEnforced",
    "ApprovalBindsExactAction",
    "TransactionalExecution",
    "RollbackVerified",
    "IdempotencyPreserved",
    "ActionReceiptVerified",
    "UnsafeTargetsRejected",
    "V5HandoffValidated",
    "V4E2ECertificationPassing",
)
PROCESS_TERMS = (
    "V4RegressionPassing",
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


def _base(reversible: bool = True):
    context, discovery = v3_fixture()
    product = build_artifact(discovery.handoff, discovery_receipt=discovery.receipt, context=context)
    request = ActionRequest(
        product.artifact_id,
        product.artifact["fingerprint"],
        "memory",
        "state/project",
        {"status": "published"},
        2.0,
        reversible,
    )
    grant = CapabilityGrant(
        "GRANT-cert",
        "user-cert",
        "memory",
        "state/",
        10.0,
        (NOW + timedelta(hours=1)).isoformat(),
        False,
    )
    approval = ApprovalRecord(
        "APR-cert",
        "user-cert",
        request.action_id,
        request.action_hash,
        product.artifact_id,
        (NOW - timedelta(seconds=1)).isoformat(),
    )
    return product, request, grant, approval


def run_v4_certification() -> dict:
    rows: list[Scenario] = []

    def boundary():
        product, request, grant, approval = _base()
        out = execute_authorized(product.v4_handoff, request, grant, approval, InMemoryAdapter(), subject="user-cert", now=NOW)
        assert out.status == "committed"
        return product.artifact_id
    rows.append(_one("V3ArtifactBoundaryValidated", "only verified V3 artifact handoff executes", boundary))

    def identity():
        product, request, grant, approval = _base()
        try:
            execute_authorized(product.v4_handoff, request, grant, approval, InMemoryAdapter(), subject="other", now=NOW)
        except ExecutionError:
            return "wrong authenticated subject refused"
        raise AssertionError("identity mismatch executed")
    rows.append(_one("IdentityBoundAuthority", "grant and approval belong to authenticated identity", identity))

    def scope():
        product, request, grant, approval = _base()
        bad = ActionRequest(request.artifact_id, request.artifact_fingerprint, "memory", "other/project", request.payload, 2, True)
        bad_approval = ApprovalRecord("APR-bad", "user-cert", bad.action_id, bad.action_hash, bad.artifact_id, approval.approved_at)
        try:
            execute_authorized(product.v4_handoff, bad, grant, bad_approval, InMemoryAdapter(), subject="user-cert", now=NOW)
        except ExecutionError:
            return "out-of-scope target refused"
        raise AssertionError("scope escape executed")
    rows.append(_one("CapabilityScopeEnforced", "target must remain inside grant", scope))

    def budget():
        product, request, grant, approval = _base()
        expensive = ActionRequest(request.artifact_id, request.artifact_fingerprint, "memory", request.target, request.payload, 11, True)
        appr = ApprovalRecord("APR-exp", "user-cert", expensive.action_id, expensive.action_hash, expensive.artifact_id, approval.approved_at)
        try:
            execute_authorized(product.v4_handoff, expensive, grant, appr, InMemoryAdapter(), subject="user-cert", now=NOW)
        except ExecutionError:
            return "over-budget action refused"
        raise AssertionError("over-budget action executed")
    rows.append(_one("BudgetEnforced", "action cost cannot exceed grant", budget))

    def exact_approval():
        product, request, grant, approval = _base()
        changed = ActionRequest(request.artifact_id, request.artifact_fingerprint, "memory", request.target, {"status":"different"}, 2, True)
        try:
            execute_authorized(product.v4_handoff, changed, grant, approval, InMemoryAdapter(), subject="user-cert", now=NOW)
        except ExecutionError:
            return "approval replay onto changed payload refused"
        raise AssertionError("approval did not bind exact action")
    rows.append(_one("ApprovalBindsExactAction", "approval hash binds exact payload and target", exact_approval))

    def transaction():
        product, request, grant, approval = _base()
        adapter = InMemoryAdapter()
        out = execute_authorized(product.v4_handoff, request, grant, approval, adapter, subject="user-cert", now=NOW)
        assert adapter.state[request.target] == request.payload
        assert out.status == "committed" and out.v5_handoff is not None
        return "preflight, snapshot, execute, verify, commit"
    rows.append(_one("TransactionalExecution", "successful action follows transaction flow", transaction))

    def rollback():
        product, request, grant, approval = _base()
        adapter = InMemoryAdapter(fail_verify=True)
        out = execute_authorized(product.v4_handoff, request, grant, approval, adapter, subject="user-cert", now=NOW)
        assert out.status == "rolled_back"
        assert request.target not in adapter.state
        assert out.v5_handoff is None
        return "failed verification rolled back; no V5 handoff"
    rows.append(_one("RollbackVerified", "failed reversible action rolls back", rollback))

    def idempotent():
        product, request, grant, approval = _base()
        adapter = InMemoryAdapter()
        a = execute_authorized(product.v4_handoff, request, grant, approval, adapter, subject="user-cert", now=NOW)
        b = execute_authorized(product.v4_handoff, request, grant, approval, adapter, subject="user-cert", now=NOW)
        assert len(adapter.executions) == 1
        assert a.receipt["response"] == b.receipt["response"]
        return "same action hash executes adapter once"
    rows.append(_one("IdempotencyPreserved", "exact replay reuses idempotency key", idempotent))

    def receipt():
        product, request, grant, approval = _base()
        out = execute_authorized(product.v4_handoff, request, grant, approval, InMemoryAdapter(), subject="user-cert", now=NOW)
        assert verify_action_receipt(out.receipt)
        bad = copy.deepcopy(out.receipt)
        bad["target"] = "changed"
        assert not verify_action_receipt(bad)
        return out.receipt["receipt_hash"]
    rows.append(_one("ActionReceiptVerified", "action receipt is tamper-evident", receipt))

    def unsafe():
        for target in ("http://example.com/hook", "https://localhost/hook", "https://127.0.0.1/hook"):
            try:
                _validate_https_target(target)
            except ExecutionError:
                continue
            raise AssertionError(f"unsafe target accepted: {target}")
        return "non-HTTPS/local targets refused"
    rows.append(_one("UnsafeTargetsRejected", "webhook adapter fails closed on local/unsafe targets", unsafe))

    def v5():
        product, request, grant, approval = _base()
        out = execute_authorized(product.v4_handoff, request, grant, approval, InMemoryAdapter(), subject="user-cert", now=NOW)
        h = out.v5_handoff
        assert h and h["schema"] == "lofgren.v5-handoff/1"
        assert h["measurement_required"] is True
        assert h["action_receipt_hash"] == out.receipt["receipt_hash"]
        return "committed action produces measurement-required V5 handoff"
    rows.append(_one("V5HandoffValidated", "only committed verified action reaches V5", v5))

    def e2e():
        product, request, grant, approval = _base()
        out = execute_authorized(product.v4_handoff, request, grant, approval, InMemoryAdapter(), subject="user-cert", now=NOW)
        assert out.status == "committed" and verify_action_receipt(out.receipt) and out.v5_handoff
        return "verified artifact -> grant -> approval -> execute -> verify -> receipt -> V5"
    rows.append(_one("V4E2ECertificationPassing", "complete bounded V4 journey", e2e))

    terms = {term: all(r.passed for r in rows if r.term == term) and any(r.term == term for r in rows) for term in CODE_TERMS}
    return {
        "schema": "lofgren.v4-certification/1",
        "terms": terms,
        "scenarios": [r.__dict__ for r in rows],
        "code_terms_certified": all(terms.values()),
    }


def render_v4_certification(cert: dict) -> str:
    out = ["Lofgren Intelligence V4 Execution Certification", "=" * 51]
    for row in cert["scenarios"]:
        out.append(f"[{'PASS' if row['passed'] else 'FAIL'}] {row['term']}: {row['scenario']} — {row['detail']}")
    out += ["-" * 51, f"V4CodeTerms = {'TRUE' if cert['code_terms_certified'] else 'FALSE'}"]
    return "\n".join(out)
