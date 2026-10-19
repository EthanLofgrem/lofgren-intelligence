from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from lofgren_intelligence.hosted.activation_package import (
    REQUIRED_PROOFS,
    SCHEMA,
    validate_activation_package,
)


class ActivationPackageTests(unittest.TestCase):
    CANDIDATE = "a" * 40
    ROLLBACK = "b" * 40

    def _package(self, root: Path) -> dict:
        proofs = {}
        for name in REQUIRED_PROOFS:
            path = root / f"{name}.json"
            raw = json.dumps(
                {"candidate_sha": self.CANDIDATE, "passed": True, "result_id": name},
                sort_keys=True,
            ).encode()
            path.write_bytes(raw)
            proofs[name] = {"path": path.name, "sha256": hashlib.sha256(raw).hexdigest()}
        return {
            "schema": SCHEMA,
            "candidate_sha": self.CANDIDATE,
            "rollback_sha": self.ROLLBACK,
            "environment": "staging",
            "deployment_origin": "https://li-staging.example",
            "deployed_sha": self.CANDIDATE,
            "forward_migrations": ["20261018120000_release_probe.sql"],
            "rollback_compatibility": {
                "application_rollback_safe": True,
                "database_rollback_required": False,
                "reason": "additive schema remains readable by the rollback build",
            },
            "proofs": proofs,
            "commands": {"activate": ["promote exact deployment"], "rollback": ["restore prior deployment"]},
            "billing_enabled": False,
            "external_execution_enabled": False,
            "owner_authorized": False,
        }

    def test_complete_package_is_ready_for_owner_request_but_not_public_ready(self):
        with tempfile.TemporaryDirectory() as directory:
            result = validate_activation_package(
                self._package(Path(directory)), package_dir=Path(directory)
            )
        self.assertTrue(result.ready)
        self.assertFalse(result.as_dict()["PublicMCPReady"])
        self.assertFalse(result.as_dict()["owner_authorized"])

    def test_wrong_sha_digest_path_and_duplicate_migrations_fail_closed(self):
        mutations = (
            lambda package: package.update(deployed_sha=self.ROLLBACK),
            lambda package: package["proofs"]["backup_restore"].update(sha256="0" * 64),
            lambda package: package["proofs"]["rollback_rehearsal"].update(path="../escape.json"),
            lambda package: package.update(forward_migrations=["20261018120000_x.sql"] * 2),
        )
        for mutate in mutations:
            with self.subTest(mutate=mutate), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                package = self._package(root)
                mutate(package)
                self.assertFalse(validate_activation_package(package, package_dir=root).ready)

    def test_missing_or_extra_proof_and_live_capabilities_fail_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            package = self._package(root)
            package["proofs"].pop("remote_client_interop")
            package["proofs"]["invented"] = {}
            package["billing_enabled"] = True
            package["external_execution_enabled"] = True
            result = validate_activation_package(package, package_dir=root)
        self.assertFalse(result.ready)
        self.assertFalse(result.checks["proof_set_exact"])
        self.assertFalse(result.checks["billing_fail_closed"])
        self.assertFalse(result.checks["external_execution_fail_closed"])

    def test_rollback_must_be_distinct_and_database_rollback_is_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            package = self._package(root)
            package["rollback_sha"] = self.CANDIDATE
            package["rollback_compatibility"]["database_rollback_required"] = True
            result = validate_activation_package(package, package_dir=root)
        self.assertFalse(result.ready)
        self.assertFalse(result.checks["rollback_sha"])
        self.assertFalse(result.checks["rollback_compatibility"])


if __name__ == "__main__":
    unittest.main()
