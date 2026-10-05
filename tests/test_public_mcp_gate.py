import base64
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from lofgren_intelligence.hosted.certification import (
    ARTIFACT_SCHEMA,
    MANIFEST_SCHEMA,
    TRUST_SCHEMA,
    TERMS,
    evaluate_public_mcp_manifest,
)


SHA = "a" * 40


def keypair():
    private = Ed25519PrivateKey.generate()
    public = private.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return private, base64.b64encode(public).decode()


def trust_policy(public_key: str, *, revoked=False, terms=None):
    return {
        "schema": TRUST_SCHEMA,
        "keys": {
            "test-key": {
                "public_key": public_key,
                "producer": "test-collector",
                "verifier": "test-verifier",
                "terms": list(terms or TERMS),
                "revoked": revoked,
                "mode": "automated",
                "source": "test-suite",
            }
        },
    }


def write_artifact(root: Path, term: str, private: Ed25519PrivateKey, *, details=None, passed=True, sha=SHA):
    merged_details = {"source": "test-suite", "result_id": f"result-{term}"}
    merged_details.update(details or {})
    payload = {
        "schema": ARTIFACT_SCHEMA,
        "term": term,
        "sha": sha,
        "passed": passed,
        "producer": "test-collector",
        "verifier": "test-verifier",
        "details": merged_details,
    }
    path = root / f"{term}.json"
    raw = json.dumps(payload, sort_keys=True).encode()
    path.write_bytes(raw)
    return {
        "term": term,
        "path": path.name,
        "sha256": hashlib.sha256(raw).hexdigest(),
        "key_id": "test-key",
        "signature": base64.b64encode(private.sign(raw)).decode(),
    }


class PublicMCPGateTests(unittest.TestCase):
    def test_unknown_is_false(self):
        private, public = keypair()
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            manifest = {"schema": MANIFEST_SCHEMA, "expected_sha": SHA, "artifacts": []}
            gate = evaluate_public_mcp_manifest(
                manifest, manifest_dir=root, trust_policy=trust_policy(public), local_head=SHA
            )
        self.assertFalse(gate.ready)
        self.assertEqual(gate.passed_count, 0)

    def test_all_terms_require_signed_hashed_exact_sha_artifacts(self):
        private, public = keypair()
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            artifacts = []
            for term in TERMS:
                details = {}
                if term == "DeployedSHAMatches":
                    details["deployed_sha"] = SHA
                elif term == "ExactHeadCIPassing":
                    details.update({"tested_sha": SHA, "conclusion": "success"})
                elif term == "FullV1V6RemoteIntegrationCertified":
                    details.update({
                        "oauth_authenticated": True,
                        "tenant_isolation": True,
                        "durable_restart_retrieval": True,
                        "v1_research_remote": True,
                        "v2_discovery_remote": True,
                        "v3_artifact_remote": True,
                        "v4_browser_approval": True,
                        "v4_execution_remote": True,
                        "v5_outcome_remote": True,
                        "v6_improvement_remote": True,
                        "all_receipts_verified": True,
                    })
                artifacts.append(write_artifact(root, term, private, details=details))
            manifest = {"schema": MANIFEST_SCHEMA, "expected_sha": SHA, "artifacts": artifacts}
            gate = evaluate_public_mcp_manifest(
                manifest, manifest_dir=root, trust_policy=trust_policy(public), local_head=SHA
            )
        self.assertTrue(gate.ready, gate.reasons)
        self.assertEqual(gate.passed_count, len(TERMS))

    def test_tampered_artifact_fails_even_when_manifest_still_says_pass(self):
        private, public = keypair()
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            entry = write_artifact(root, "CoreV1Certified", private)
            (root / entry["path"]).write_text('{"tampered":true}', encoding="utf-8")
            manifest = {"schema": MANIFEST_SCHEMA, "expected_sha": SHA, "artifacts": [entry]}
            gate = evaluate_public_mcp_manifest(
                manifest, manifest_dir=root, trust_policy=trust_policy(public), local_head=SHA
            )
        self.assertFalse(gate.values["CoreV1Certified"])
        self.assertIn("digest mismatch", gate.reasons["CoreV1Certified"])

    def test_wrong_artifact_sha_fails(self):
        private, public = keypair()
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            entry = write_artifact(root, "CoreV1Certified", private, sha="b" * 40)
            manifest = {"schema": MANIFEST_SCHEMA, "expected_sha": SHA, "artifacts": [entry]}
            gate = evaluate_public_mcp_manifest(
                manifest, manifest_dir=root, trust_policy=trust_policy(public), local_head=SHA
            )
        self.assertFalse(gate.values["CoreV1Certified"])
        self.assertIn("SHA mismatch", gate.reasons["CoreV1Certified"])

    def test_unsigned_artifact_fails(self):
        private, public = keypair()
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            entry = write_artifact(root, "CoreV1Certified", private)
            entry.pop("signature")
            manifest = {"schema": MANIFEST_SCHEMA, "expected_sha": SHA, "artifacts": [entry]}
            gate = evaluate_public_mcp_manifest(
                manifest, manifest_dir=root, trust_policy=trust_policy(public), local_head=SHA
            )
        self.assertFalse(gate.values["CoreV1Certified"])
        self.assertIn("signature metadata missing", gate.reasons["CoreV1Certified"])

    def test_revoked_key_fails(self):
        private, public = keypair()
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            entry = write_artifact(root, "CoreV1Certified", private)
            manifest = {"schema": MANIFEST_SCHEMA, "expected_sha": SHA, "artifacts": [entry]}
            gate = evaluate_public_mcp_manifest(
                manifest, manifest_dir=root,
                trust_policy=trust_policy(public, revoked=True),
                local_head=SHA,
            )
        self.assertFalse(gate.values["CoreV1Certified"])
        self.assertIn("revoked", gate.reasons["CoreV1Certified"])

    def test_duplicate_term_fails(self):
        private, public = keypair()
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            first = write_artifact(root, "CoreV1Certified", private)
            second = dict(first)
            manifest = {"schema": MANIFEST_SCHEMA, "expected_sha": SHA, "artifacts": [first, second]}
            gate = evaluate_public_mcp_manifest(
                manifest, manifest_dir=root, trust_policy=trust_policy(public), local_head=SHA
            )
        self.assertFalse(gate.values["CoreV1Certified"])
        self.assertEqual(gate.reasons["CoreV1Certified"], "duplicate evidence for term")

    def test_full_remote_integration_requires_restart_and_every_stage(self):
        private, public = keypair()
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            entry = write_artifact(root, "FullV1V6RemoteIntegrationCertified", private, details={
                "oauth_authenticated": True,
                "tenant_isolation": True,
                "durable_restart_retrieval": False,
                "v1_research_remote": True,
                "v2_discovery_remote": True,
                "v3_artifact_remote": True,
                "v4_browser_approval": True,
                "v4_execution_remote": True,
                "v5_outcome_remote": True,
                "v6_improvement_remote": True,
                "all_receipts_verified": True,
            })
            manifest = {"schema": MANIFEST_SCHEMA, "expected_sha": SHA, "artifacts": [entry]}
            gate = evaluate_public_mcp_manifest(
                manifest, manifest_dir=root, trust_policy=trust_policy(public), local_head=SHA
            )
        self.assertFalse(gate.values["FullV1V6RemoteIntegrationCertified"])
        self.assertIn("durable_restart_retrieval", gate.reasons["FullV1V6RemoteIntegrationCertified"])


if __name__ == "__main__":
    unittest.main()
