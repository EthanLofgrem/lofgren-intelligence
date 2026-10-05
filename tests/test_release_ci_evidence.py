from __future__ import annotations

import unittest
from unittest.mock import patch

from lofgren_intelligence.release.ci import PACKAGE_STEP, verify_exact_ci


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


if __name__ == "__main__":
    unittest.main()
