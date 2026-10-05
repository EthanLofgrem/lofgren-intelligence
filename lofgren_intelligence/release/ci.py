"""GitHub Actions evidence verification for exact-SHA process gates."""

from __future__ import annotations

import json
import os
import re
import urllib.request
from typing import Iterable

MATRIX = ("3.10", "3.11", "3.12")
PACKAGE_STEP = "Package smoke test (clean wheel, installed and run outside the source tree)"


def _get(url: str) -> dict:
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "lofgren-release-gate",
    }
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)


def verify_exact_ci(
    *,
    repo: str,
    sha: str,
    run_id: int | None,
    required_steps: Iterable[str],
    workflow_name: str = "tests",
) -> tuple[bool, str, bool, str]:
    """Verify the exact workflow, SHA, Python matrix and required steps.

    A run id is only a locator. It is never accepted as evidence by itself.
    """
    if run_id is None:
        return False, "no CI run id supplied", False, "no package evidence"
    try:
        run = _get(f"https://api.github.com/repos/{repo}/actions/runs/{run_id}")
        if run.get("head_sha") != sha:
            return False, f"run {run_id} tested {run.get('head_sha')} not {sha}", False, "wrong SHA"
        if run.get("name") != workflow_name:
            return False, f"run {run_id} is workflow {run.get('name')!r}, expected {workflow_name!r}", False, "wrong workflow"
        if run.get("event") != "push":
            return False, f"run {run_id} event is {run.get('event')!r}, expected push", False, "wrong event"
        jobs = _get(f"https://api.github.com/repos/{repo}/actions/runs/{run_id}/jobs").get("jobs", [])
    except Exception as exc:
        return False, f"GitHub API unavailable: {exc}", False, "no package evidence"

    by_python: dict[str, dict] = {}
    for job in jobs:
        match = re.search(r"\((3\.\d+)\)", str(job.get("name") or ""))
        if match:
            by_python[match.group(1)] = job

    problems: list[str] = []
    package_problems: list[str] = []
    required_steps = tuple(required_steps)
    for version in MATRIX:
        job = by_python.get(version)
        if job is None:
            problems.append(f"missing Python {version} job")
            package_problems.append(f"missing Python {version} job")
            continue
        if job.get("status") != "completed" or job.get("conclusion") != "success":
            problems.append(f"Python {version} job is {job.get('status')}/{job.get('conclusion')}")
        steps = {step.get("name"): step.get("conclusion") for step in job.get("steps", [])}
        for step in required_steps:
            if steps.get(step) != "success":
                problems.append(f"Python {version} step {step!r}: {steps.get(step, 'missing')}")
        if steps.get(PACKAGE_STEP) != "success":
            package_problems.append(f"Python {version} package step: {steps.get(PACKAGE_STEP, 'missing')}")

    ci_ok = not problems
    pkg_ok = not package_problems
    return (
        ci_ok,
        "; ".join(problems) or f"run {run_id}: exact SHA and all required matrix steps passed",
        pkg_ok,
        "; ".join(package_problems) or f"run {run_id}: clean-wheel package step passed on {', '.join(MATRIX)}",
    )
