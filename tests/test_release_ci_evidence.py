from __future__ import annotations

import io
import json
import tarfile
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from lofgren_intelligence.release.ci import (
    ArtifactManifestError,
    PACKAGE_STEP,
    build_artifact_manifest,
    normalize_source_distribution,
    verify_artifact_manifest,
    verify_exact_ci,
    write_artifact_manifest,
)


SHA = "a" * 40
RUN_ID = 123


def good_run():
    return {"id": RUN_ID, "head_sha": SHA, "name": "tests", "event": "push", "status": "in_progress"}


def good_jobs(required):
    steps = [{"name": name, "conclusion": "success"} for name in required]
    if PACKAGE_STEP not in required:
        steps.append({"name": PACKAGE_STEP, "conclusion": "success"})
    return {
        "jobs": [
            {"name": f"test ({py})", "status": "completed", "conclusion": "success", "steps": steps}
            for py in ("3.10", "3.11", "3.12")
        ]
    }


class ReleaseCIEvidenceTests(unittest.TestCase):
    def test_exact_sha_matrix_and_steps_pass(self):
        required = ("Install package", "Run tests", PACKAGE_STEP)
        with patch("lofgren_intelligence.release.ci._get", side_effect=[good_run(), good_jobs(required)]):
            ci, _, pkg, _ = verify_exact_ci(repo="o/r", sha=SHA, run_id=RUN_ID, required_steps=required)
        self.assertTrue(ci)
        self.assertTrue(pkg)

    def test_fabricated_run_id_without_matching_sha_fails(self):
        required = ("Run tests", PACKAGE_STEP)
        bad = {**good_run(), "head_sha": "b" * 40}
        with patch("lofgren_intelligence.release.ci._get", return_value=bad):
            ci, evidence, pkg, _ = verify_exact_ci(repo="o/r", sha=SHA, run_id=RUN_ID, required_steps=required)
        self.assertFalse(ci)
        self.assertFalse(pkg)
        self.assertIn("not", evidence)

    def test_pull_request_run_does_not_prove_release_ci(self):
        required = ("Run tests", PACKAGE_STEP)
        bad = {**good_run(), "event": "pull_request"}
        with patch("lofgren_intelligence.release.ci._get", return_value=bad):
            ci, evidence, pkg, _ = verify_exact_ci(repo="o/r", sha=SHA, run_id=RUN_ID, required_steps=required)
        self.assertFalse(ci)
        self.assertFalse(pkg)
        self.assertIn("expected push", evidence)

    def test_missing_required_step_fails(self):
        required = ("Run tests", "V6 improvement certification", PACKAGE_STEP)
        jobs = good_jobs(("Run tests", PACKAGE_STEP))
        with patch("lofgren_intelligence.release.ci._get", side_effect=[good_run(), jobs]):
            ci, evidence, pkg, _ = verify_exact_ci(repo="o/r", sha=SHA, run_id=RUN_ID, required_steps=required)
        self.assertFalse(ci)
        self.assertTrue(pkg)
        self.assertIn("V6 improvement certification", evidence)

    def test_failed_python_job_fails(self):
        required = ("Run tests", PACKAGE_STEP)
        jobs = good_jobs(required)
        jobs["jobs"][1]["conclusion"] = "failure"
        with patch("lofgren_intelligence.release.ci._get", side_effect=[good_run(), jobs]):
            ci, evidence, _, _ = verify_exact_ci(repo="o/r", sha=SHA, run_id=RUN_ID, required_steps=required)
        self.assertFalse(ci)
        self.assertIn("Python 3.11", evidence)

    def test_wrong_workflow_fails(self):
        required = ("Run tests", PACKAGE_STEP)
        bad = {**good_run(), "name": "other"}
        with patch("lofgren_intelligence.release.ci._get", return_value=bad):
            ci, evidence, pkg, _ = verify_exact_ci(repo="o/r", sha=SHA, run_id=RUN_ID, required_steps=required)
        self.assertFalse(ci)
        self.assertFalse(pkg)
        self.assertIn("expected", evidence)

    def test_cancelled_python_job_fails(self):
        required = ("Run tests", PACKAGE_STEP)
        jobs = good_jobs(required)
        jobs["jobs"][0]["status"] = "completed"
        jobs["jobs"][0]["conclusion"] = "cancelled"
        with patch("lofgren_intelligence.release.ci._get", side_effect=[good_run(), jobs]):
            ci, evidence, _, _ = verify_exact_ci(repo="o/r", sha=SHA, run_id=RUN_ID, required_steps=required)
        self.assertFalse(ci)
        self.assertIn("cancelled", evidence)

    def test_missing_python_version_fails(self):
        required = ("Run tests", PACKAGE_STEP)
        jobs = good_jobs(required)
        jobs["jobs"] = [job for job in jobs["jobs"] if "(3.12)" not in job["name"]]
        with patch("lofgren_intelligence.release.ci._get", side_effect=[good_run(), jobs]):
            ci, evidence, pkg, _ = verify_exact_ci(repo="o/r", sha=SHA, run_id=RUN_ID, required_steps=required)
        self.assertFalse(ci)
        self.assertFalse(pkg)
        self.assertIn("3.12", evidence)

    def test_skipped_package_step_fails_package_and_ci(self):
        required = ("Run tests", PACKAGE_STEP)
        jobs = good_jobs(required)
        for job in jobs["jobs"]:
            for step in job["steps"]:
                if step["name"] == PACKAGE_STEP:
                    step["conclusion"] = "skipped"
        with patch("lofgren_intelligence.release.ci._get", side_effect=[good_run(), jobs]):
            ci, evidence, pkg, package_evidence = verify_exact_ci(
                repo="o/r", sha=SHA, run_id=RUN_ID, required_steps=required
            )
        self.assertFalse(ci)
        self.assertFalse(pkg)
        self.assertIn("Package smoke test", evidence)
        self.assertIn("skipped", package_evidence)


class ReleaseArtifactManifestTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.dist = self.root / "dist"
        self.dist.mkdir()
        self.wheel = self.dist / "lofgren_intelligence-0.7.0-py3-none-any.whl"
        self.sdist = self.dist / "lofgren_intelligence-0.7.0.tar.gz"
        self._write_wheel("Lofgren-Intelligence", "0.7.0")
        self._write_sdist("Lofgren-Intelligence", "0.7.0")

    def tearDown(self):
        self.temporary.cleanup()

    @staticmethod
    def _metadata(name, version):
        return f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n\n".encode()

    def _write_wheel(self, name, version):
        with zipfile.ZipFile(self.wheel, "w") as archive:
            archive.writestr(
                "lofgren_intelligence-0.7.0.dist-info/METADATA",
                self._metadata(name, version),
            )
            archive.writestr("lofgren_intelligence/__init__.py", b"__version__ = '0.7.0'\n")

    def _write_sdist(self, name, version):
        metadata = self._metadata(name, version)
        member = tarfile.TarInfo("lofgren_intelligence-0.7.0/PKG-INFO")
        member.size = len(metadata)
        with tarfile.open(self.sdist, "w:gz") as archive:
            archive.addfile(member, io.BytesIO(metadata))

    def test_create_and_verify_exact_artifact_identity(self):
        output = self.root / "release-artifact-manifest.json"
        manifest = build_artifact_manifest(self.dist, "1" * 40, "2" * 40)
        write_artifact_manifest(manifest, output)
        verified = verify_artifact_manifest(
            output, self.dist, expected_sha="1" * 40, expected_tree="2" * 40
        )
        self.assertEqual(verified["schema"], "lofgren.release-artifact-manifest/1")
        self.assertEqual(verified["source"], {"commit": "1" * 40, "tree": "2" * 40})
        self.assertEqual({item["kind"] for item in verified["artifacts"]}, {"wheel", "sdist"})
        self.assertEqual(json.loads(output.read_text()), verified)

    def test_verify_rejects_artifact_substitution(self):
        output = self.root / "release-artifact-manifest.json"
        write_artifact_manifest(build_artifact_manifest(self.dist, "1" * 40, "2" * 40), output)
        with self.wheel.open("ab") as stream:
            stream.write(b"substituted")
        with self.assertRaisesRegex(ArtifactManifestError, "does not match"):
            verify_artifact_manifest(output, self.dist, expected_sha="1" * 40)

    def test_rejects_unexpected_distribution_file(self):
        (self.dist / "unreviewed.bin").write_bytes(b"unexpected")
        with self.assertRaisesRegex(ArtifactManifestError, "exactly one wheel"):
            build_artifact_manifest(self.dist, "1" * 40, "2" * 40)

    def test_rejects_mismatched_package_metadata(self):
        self._write_sdist("Lofgren-Intelligence", "0.7.1")
        with self.assertRaisesRegex(ArtifactManifestError, "do not match"):
            build_artifact_manifest(self.dist, "1" * 40, "2" * 40)

    def test_verify_rejects_a_different_expected_commit(self):
        output = self.root / "release-artifact-manifest.json"
        write_artifact_manifest(build_artifact_manifest(self.dist, "1" * 40, "2" * 40), output)
        with self.assertRaisesRegex(ArtifactManifestError, "expected SHA"):
            verify_artifact_manifest(output, self.dist, expected_sha="3" * 40)

    def test_sdist_normalization_is_reproducible(self):
        first = self.sdist.read_bytes()
        normalize_source_distribution(self.dist, 1234567890)
        normalized = self.sdist.read_bytes()
        self.assertNotEqual(first, normalized)

        self._write_sdist("Lofgren-Intelligence", "0.7.0")
        normalize_source_distribution(self.dist, 1234567890)
        self.assertEqual(self.sdist.read_bytes(), normalized)


if __name__ == "__main__":
    unittest.main()
