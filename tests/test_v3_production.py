from __future__ import annotations

import copy
import unittest

from lofgren_intelligence.certification import AGREE_AND_CONFLICT, OBJECTIVE, _docs, _run
from lofgren_intelligence.discovery.context import DiscoveryContext
from lofgren_intelligence.discovery.fixtures import AT, warehouse_design
from lofgren_intelligence.discovery.pipeline import run_discovery
from lofgren_intelligence.kernel.knowledge_map import export_knowledge_map
from lofgren_intelligence.production import (
    ProductionError,
    build_artifact,
    verify_artifact,
    verify_production_receipt,
)
from lofgren_intelligence.production.certification import CODE_TERMS, run_v3_certification


class V3Fixture(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.v1 = _run(OBJECTIVE, _docs(AGREE_AND_CONFLICT))
        cls.context = DiscoveryContext(export_knowledge_map(cls.v1), cls.v1.receipt)
        cls.discovery = run_discovery(
            cls.context,
            "Choose a fictional warehouse size",
            design=warehouse_design(),
            at=AT,
        )
        assert cls.discovery.handoff is not None

    def build(self, kind="structured_bundle"):
        return build_artifact(
            self.discovery.handoff,
            discovery_receipt=self.discovery.receipt,
            context=self.context,
            kind=kind,
        )


class ProductionPipelineTests(V3Fixture):
    def test_all_supported_artifact_kinds_build_verify_and_receipt(self):
        for kind in ("structured_bundle", "markdown", "python_module"):
            with self.subTest(kind=kind):
                result = self.build(kind)
                self.assertTrue(verify_artifact(result.artifact).passed)
                self.assertTrue(verify_production_receipt(result.receipt))
                self.assertTrue(result.v4_handoff["artifact_verified"])
                self.assertTrue(result.v4_handoff["acceptance_passed"])
                self.assertTrue(result.v4_handoff["authority_required"])
                self.assertEqual(result.v4_handoff["requested_actions"], [])

    def test_build_is_deterministic(self):
        a = self.build()
        b = self.build()
        self.assertEqual(a.artifact_id, b.artifact_id)
        self.assertEqual(a.artifact["fingerprint"], b.artifact["fingerprint"])
        self.assertEqual(a.receipt["receipt_hash"], b.receipt["receipt_hash"])

    def test_tampered_file_is_detected(self):
        result = self.build()
        bad = copy.deepcopy(result.artifact)
        bad["files"][0]["content"] += "\nchanged"
        check = verify_artifact(bad)
        self.assertFalse(check.passed)
        self.assertTrue(any("hash mismatch" in x for x in check.problems))

    def test_tampered_receipt_is_detected(self):
        result = self.build()
        bad = copy.deepcopy(result.receipt)
        bad["artifact_id"] = "AF-forged"
        self.assertFalse(verify_production_receipt(bad))

    def test_bad_upstream_receipt_fails_closed(self):
        bad = copy.deepcopy(self.discovery.receipt)
        bad["discovery_fingerprint"] = "DFP-" + "0" * 64
        with self.assertRaises(ProductionError):
            build_artifact(
                self.discovery.handoff,
                discovery_receipt=bad,
                context=self.context,
            )

    def test_unsupported_kind_fails_closed(self):
        with self.assertRaises(ProductionError):
            self.build("binary_executable")

    def test_python_artifact_compiles(self):
        result = self.build("python_module")
        py = next(x for x in result.artifact["files"] if x["path"] == "artifact.py")
        compile(py["content"], py["path"], "exec")


class V3CertificationTests(unittest.TestCase):
    def test_all_code_terms_pass(self):
        cert = run_v3_certification()
        self.assertEqual(set(cert["terms"]), set(CODE_TERMS))
        self.assertTrue(cert["code_terms_certified"], cert)
        self.assertTrue(all(cert["terms"].values()))


if __name__ == "__main__":
    unittest.main()
