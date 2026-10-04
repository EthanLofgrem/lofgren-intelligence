import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from lofgren_intelligence.hosted.certification import (
    ARTIFACT_SCHEMA,
    MANIFEST_SCHEMA,
    TERMS,
    evaluate_public_mcp_manifest,
)


SHA = "a" * 40


def write_artifact(root: Path, term: str, *, details=None, passed=True, sha=SHA):
    payload = {
        "schema": ARTIFACT_SCHEMA,
        "term": term,
        "sha": sha,
        "passed": passed,
        "producer": "test",
        "verifier": "python -m unittest",
        "details": details or {},
    }
    path = root / f"{term}.json"
    raw = json.dumps(payload, sort_keys=True).encode()
    path.write_bytes(raw)
    return {
        "term": term,
        "path": path.name,
        "sha256": hashlib.sha256(raw).hexdigest(),
    }


class PublicMCPGateTests(unittest.TestCase):
    def test_unknown_is_false(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            manifest = {"schema": MANIFEST_SCHEMA, "expected_sha": SHA, "artifacts": []}
            gate = evaluate_public_mcp_manifest(manifest, manifest_dir=root, local_head=SHA)
        self.assertFalse(gate.ready)
        self.assertEqual(gate.passed_count, 0)

    def test_all_terms_require_hashed_exact_sha_artifacts(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            artifacts = []
            for term in TERMS:
                details = {}
                if term == "DeployedSHAMatches":
                    details["deployed_sha"] = SHA
                elif term == "ExactHeadCIPassing":
                    details.update({"tested_sha": SHA, "conclusion": "success"})
                elif term == "V2RemoteIntegrationCertified":
                    details.update({
                        "oauth_authenticated": True,
                        "durable_restart_retrieval": True,
                        "tenant_isolation": True,
                        "v2_tools_remote": True,
                        "receipt_verified": True,
                    })
                artifacts.append(write_artifact(root, term, details=details))
            manifest = {"schema": MANIFEST_SCHEMA, "expected_sha": SHA, "artifacts": artifacts}
            gate = evaluate_public_mcp_manifest(manifest, manifest_dir=root, local_head=SHA)
        self.assertTrue(gate.ready)
        self.assertEqual(gate.passed_count, len(TERMS))

    def test_tampered_artifact_fails_even_when_manifest_still_says_pass(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            entry = write_artifact(root, "CoreV1Certified")
            (root / entry["path"]).write_text('{"tampered":true}', encoding="utf-8")
            manifest = {"schema": MANIFEST_SCHEMA, "expected_sha": SHA, "artifacts": [entry]}
            gate = evaluate_public_mcp_manifest(manifest, manifest_dir=root, local_head=SHA)
        self.assertFalse(gate.values["CoreV1Certified"])
        self.assertIn("digest mismatch", gate.reasons["CoreV1Certified"])

    def test_wrong_artifact_sha_fails(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            entry = write_artifact(root, "CoreV1Certified", sha="b" * 40)
            manifest = {"schema": MANIFEST_SCHEMA, "expected_sha": SHA, "artifacts": [entry]}
            gate = evaluate_public_mcp_manifest(manifest, manifest_dir=root, local_head=SHA)
        self.assertFalse(gate.values["CoreV1Certified"])
        self.assertIn("SHA mismatch", gate.reasons["CoreV1Certified"])

    def test_duplicate_term_fails(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            first = write_artifact(root, "CoreV1Certified")
            second_path = root / "second.json"
            payload = {
                "schema": ARTIFACT_SCHEMA,
                "term": "CoreV1Certified",
                "sha": SHA,
                "passed": True,
                "producer": "test2",
                "verifier": "test2",
                "details": {},
            }
            raw = json.dumps(payload, sort_keys=True).encode()
            second_path.write_bytes(raw)
            second = {
                "term": "CoreV1Certified",
                "path": second_path.name,
                "sha256": hashlib.sha256(raw).hexdigest(),
            }
            manifest = {"schema": MANIFEST_SCHEMA, "expected_sha": SHA, "artifacts": [first, second]}
            gate = evaluate_public_mcp_manifest(manifest, manifest_dir=root, local_head=SHA)
        self.assertFalse(gate.values["CoreV1Certified"])
        self.assertEqual(gate.reasons["CoreV1Certified"], "duplicate evidence for term")

    def test_v2_remote_integration_requires_durable_authenticated_tenant_safe_v2(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            entry = write_artifact(root, "V2RemoteIntegrationCertified", details={
                "oauth_authenticated": True,
                "durable_restart_retrieval": False,
                "tenant_isolation": True,
                "v2_tools_remote": True,
                "receipt_verified": True,
            })
            manifest = {"schema": MANIFEST_SCHEMA, "expected_sha": SHA, "artifacts": [entry]}
            gate = evaluate_public_mcp_manifest(manifest, manifest_dir=root, local_head=SHA)
        self.assertFalse(gate.values["V2RemoteIntegrationCertified"])
        self.assertIn("durable_restart_retrieval", gate.reasons["V2RemoteIntegrationCertified"])


if __name__ == "__main__":
    unittest.main()
