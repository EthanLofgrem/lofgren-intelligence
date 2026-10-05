from __future__ import annotations

import copy
import unittest
from datetime import datetime, timedelta, timezone

from lofgren_intelligence.execution import (
    ActionRequest,
    ApprovalRecord,
    CapabilityGrant,
    ExecutionError,
    InMemoryAdapter,
    execute_authorized,
    verify_action_receipt,
)
from lofgren_intelligence.execution.certification import CODE_TERMS, run_v4_certification
from lofgren_intelligence.production.certification import _fixture as v3_fixture
from lofgren_intelligence.production.core import build_artifact

NOW = datetime(2026, 10, 4, 12, 0, tzinfo=timezone.utc)


class V4Fixture(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        ctx, discovery = v3_fixture()
        cls.product = build_artifact(discovery.handoff, discovery_receipt=discovery.receipt, context=ctx)

    def base(self, *, reversible=True):
        req = ActionRequest(
            self.product.artifact_id,
            self.product.artifact["fingerprint"],
            "memory",
            "state/item",
            {"value": 7},
            1.0,
            reversible,
        )
        grant = CapabilityGrant(
            "GRANT-test", "user-1", "memory", "state/", 5.0,
            (NOW + timedelta(hours=1)).isoformat(), False,
        )
        approval = ApprovalRecord(
            "APR-test", "user-1", req.action_id, req.action_hash,
            req.artifact_id, (NOW - timedelta(seconds=1)).isoformat(),
        )
        return req, grant, approval


class ExecutionTests(V4Fixture):
    def test_commit_and_receipt(self):
        req, grant, approval = self.base()
        out = execute_authorized(
            self.product.v4_handoff, req, grant, approval, InMemoryAdapter(),
            subject="user-1", now=NOW,
        )
        self.assertEqual(out.status, "committed")
        self.assertTrue(verify_action_receipt(out.receipt))
        self.assertIsNotNone(out.v5_handoff)

    def test_wrong_user_refused(self):
        req, grant, approval = self.base()
        with self.assertRaises(ExecutionError):
            execute_authorized(
                self.product.v4_handoff, req, grant, approval, InMemoryAdapter(),
                subject="user-2", now=NOW,
            )

    def test_expired_grant_refused(self):
        req, grant, approval = self.base()
        grant = CapabilityGrant(
            grant.grant_id, grant.subject, grant.action_kind, grant.target_prefix,
            grant.max_cost_usd, (NOW - timedelta(seconds=1)).isoformat(),
        )
        with self.assertRaises(ExecutionError):
            execute_authorized(
                self.product.v4_handoff, req, grant, approval, InMemoryAdapter(),
                subject="user-1", now=NOW,
            )

    def test_changed_payload_cannot_reuse_approval(self):
        req, grant, approval = self.base()
        changed = ActionRequest(
            req.artifact_id, req.artifact_fingerprint, req.kind, req.target,
            {"value": 8}, req.cost_usd, req.reversible,
        )
        with self.assertRaises(ExecutionError):
            execute_authorized(
                self.product.v4_handoff, changed, grant, approval, InMemoryAdapter(),
                subject="user-1", now=NOW,
            )

    def test_failed_verification_rolls_back(self):
        req, grant, approval = self.base()
        adapter = InMemoryAdapter(fail_verify=True)
        out = execute_authorized(
            self.product.v4_handoff, req, grant, approval, adapter,
            subject="user-1", now=NOW,
        )
        self.assertEqual(out.status, "rolled_back")
        self.assertNotIn(req.target, adapter.state)
        self.assertIsNone(out.v5_handoff)

    def test_receipt_tamper_fails(self):
        req, grant, approval = self.base()
        out = execute_authorized(
            self.product.v4_handoff, req, grant, approval, InMemoryAdapter(),
            subject="user-1", now=NOW,
        )
        bad = copy.deepcopy(out.receipt)
        bad["subject"] = "other"
        self.assertFalse(verify_action_receipt(bad))

    def test_reversible_only_grant_blocks_irreversible_action(self):
        req, grant, approval = self.base(reversible=False)
        grant = CapabilityGrant(
            grant.grant_id, grant.subject, grant.action_kind, grant.target_prefix,
            grant.max_cost_usd, grant.expires_at, True,
        )
        with self.assertRaises(ExecutionError):
            execute_authorized(
                self.product.v4_handoff, req, grant, approval, InMemoryAdapter(),
                subject="user-1", now=NOW,
            )


class V4CertificationTests(unittest.TestCase):
    def test_all_terms_pass(self):
        cert = run_v4_certification()
        self.assertEqual(set(cert["terms"]), set(CODE_TERMS))
        self.assertTrue(cert["code_terms_certified"], cert)
        self.assertTrue(all(cert["terms"].values()))


if __name__ == "__main__":
    unittest.main()
