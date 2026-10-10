"""Public hosted execution fails closed despite valid action approvals."""

import os
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from unittest.mock import patch

from lofgren_intelligence.execution.certification import NOW, _base
from lofgren_intelligence.execution.core import InMemoryAdapter, execute_authorized
from lofgren_intelligence.hosted.service import PublicService, PublicServiceError
from .test_public_hosted import FakeStore


class ExternalExecutionContainmentTests(unittest.TestCase):
    def setUp(self):
        product, request, grant, approval = _base()
        self.action_id = request.action_id
        self.store = FakeStore()
        self.store.save_artifact({
            "user_id": "user-cert", "artifact_id": request.artifact_id,
            "v4_handoff": product.v4_handoff,
        })
        self.store.save_action({
            "user_id": "user-cert", "action_id": request.action_id,
            "artifact_id": request.artifact_id, "status": "approved",
            "request": asdict(request), "grant_record": asdict(grant),
            "approval_record": asdict(approval),
        })
        self.result = execute_authorized(
            product.v4_handoff, request, grant, approval, InMemoryAdapter(),
            subject="user-cert", now=NOW,
        )

    def call(self):
        return PublicService(self.store).execute_action(
            "user-cert", {"action_id": self.action_id},
        )

    def test_disabled_concurrent_calls_never_invoke_execution(self):
        with patch.dict(os.environ, {}, clear=True), patch(
            "lofgren_intelligence.hosted.service.execute_authorized"
        ) as execute:
            def denied(_):
                with self.assertRaisesRegex(PublicServiceError, "EXTERNAL_EXECUTION_DISABLED"):
                    self.call()
            with ThreadPoolExecutor(max_workers=2) as pool:
                list(pool.map(denied, range(2)))
            execute.assert_not_called()
        self.assertEqual(self.store.get_action("user-cert", self.action_id)["status"], "approved")

    def test_only_exact_operator_opt_in_enables_execution(self):
        for value in ("false", "1", "TRUE", "yes", " true "):
            with self.subTest(value=value), patch.dict(os.environ, {"LI_EXTERNAL_EXECUTION_ENABLED": value}):
                with self.assertRaisesRegex(PublicServiceError, "EXTERNAL_EXECUTION_DISABLED"):
                    self.call()
        with patch.dict(os.environ, {"LI_EXTERNAL_EXECUTION_ENABLED": "true"}), patch(
            "lofgren_intelligence.hosted.service.execute_authorized", return_value=self.result,
        ) as execute:
            self.assertEqual(self.call()["status"], "executed")
            execute.assert_called_once()

    def test_existing_receipt_remains_readable_when_disabled(self):
        row = self.store.get_action("user-cert", self.action_id)
        self.store.save_action({**row, "status": "executed", "receipt": self.result.receipt})
        with patch.dict(os.environ, {}, clear=True), patch(
            "lofgren_intelligence.hosted.service.execute_authorized"
        ) as execute:
            self.assertTrue(self.call()["receipt_intact"])
            execute.assert_not_called()
