"""Exact-SHA V3 -> V4 certification gate.

Run from a clean checkout of one pushed commit, with the package's declared dependencies installed
(`pip install ".[hosted]"`), after the GitHub Actions matrix for that commit has finished:

    python scripts/v3_gate.py --sha <40-hex sha>

Inside the dependent CI job, pass `--ci-run-id <id>`: the gate then reads that run's completed matrix jobs. Either
way CI evidence is read from the GitHub Actions API for the exact SHA; a run id alone is never evidence.
Unknown is false. This is the only place that prints V3ReadyForV4.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from lofgren_intelligence.production.certification import CODE_TERMS, GATE_ORDER, run_v3_certification  # noqa: E402

MATRIX = ("3.10", "3.11", "3.12")
WORKFLOW = "tests"
PACKAGE_STEPS = (
    "Install package",
    "Installed-package smoke test",
    "Package smoke test (clean wheel, installed and run outside the source tree)",
)
REQUIRED_STEPS = PACKAGE_STEPS + (
    "Run tests",
    "CLI smoke test",
    "V1 certification gate",
    "V2 discovery certification",
    "V3 production certification",
    "Static check",
    "V1 -> V2 boundary certification",
)


def sh(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(args, cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace")


def clean() -> tuple[bool, str]:
    out = sh("git", "status", "--porcelain", "--untracked-files=all")
    if out.returncode != 0:
        return False, f"git status failed: {out.stderr.strip()}"
    return not out.stdout.strip(), out.stdout.strip() or "clean"


def suite(utf8: bool) -> tuple[bool, str]:
    env = dict(os.environ)
    env["PYTHONUTF8"] = "1" if utf8 else "0"
    out = subprocess.run(
        [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-t", ".", "-v"],
        cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace", env=env,
    )
    text = out.stdout + out.stderr
    ran = re.search(r"^Ran (\d+) tests?", text, re.M)
    skipped = len(re.findall(r"\.\.\. skipped", text))
    ok = out.returncode == 0 and bool(re.search(r"^OK\s*$", text, re.M)) and skipped == 0
    return ok, f"{ran.group(1) if ran else '?'} tests, {skipped} skipped, exit {out.returncode}"


def get(url: str) -> dict:
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "lofgren-v3-gate"}
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=30) as resp:
        return json.load(resp)


def ci(repo: str, sha: str, run_id: int | None) -> tuple[bool, str, bool, str]:
    """(CI ok, evidence, package ok, evidence) for the exact SHA, from the GitHub Actions API."""
    try:
        if run_id is not None:
            run = get(f"https://api.github.com/repos/{repo}/actions/runs/{run_id}")
            if run.get("head_sha") != sha:
                return False, f"run {run_id} head {run.get('head_sha')} != {sha}", False, "wrong run SHA"
            if run.get("name") != WORKFLOW:
                return False, f"run {run_id} is not workflow {WORKFLOW}", False, "wrong workflow"
        else:
            runs = get(f"https://api.github.com/repos/{repo}/actions/runs?head_sha={sha}&event=push")["workflow_runs"]
            runs = [r for r in runs if r["name"] == WORKFLOW and r["head_sha"] == sha and r["status"] == "completed"]
            if not runs:
                return False, f"no completed '{WORKFLOW}' push run for {sha[:7]}", False, "no CI evidence"
            run = max(runs, key=lambda r: r["run_number"])
        jobs = get(f"https://api.github.com/repos/{repo}/actions/runs/{run['id']}/jobs")["jobs"]
    except Exception as exc:  # no evidence is false, never an error that passes
        return False, f"GitHub API unavailable: {exc}", False, "no CI evidence"

    by_python = {}
    for job in jobs:
        m = re.search(r"\((3\.\d+)\)", job["name"])
        if m:
            by_python[m.group(1)] = job
    problems, package = [], []
    for py in MATRIX:
        job = by_python.get(py)
        if job is None:
            problems.append(f"no job for Python {py}")
            package.append(f"no job for Python {py}")
            continue
        steps = {s["name"]: s["conclusion"] for s in job["steps"]}
        if job["conclusion"] != "success":
            problems.append(f"Python {py} job {job['conclusion']}")
        problems += [f"Python {py} step '{n}': {steps.get(n, 'missing')}" for n in REQUIRED_STEPS
                     if steps.get(n) != "success"]
        package += [f"Python {py} step '{n}': {steps.get(n, 'missing')}" for n in PACKAGE_STEPS
                    if steps.get(n) != "success"]
    if run_id is None:
        ok, state = run.get("conclusion") == "success" and not problems, str(run.get("conclusion"))
    else:
        ok, state = not problems, "matrix-complete (dependent job)"
    ev = f"run {run['id']} {state}; " + ("; ".join(problems) or f"Python {', '.join(MATRIX)} all required steps passed")
    pev = "; ".join(package) or f"run {run['id']}: install and clean-wheel steps passed on Python {', '.join(MATRIX)}"
    return ok, ev, not package, pev


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--sha", required=True, help="exact 40-hex commit being certified")
    p.add_argument("--repo", default="EthanLofgrem/lofgren-intelligence")
    p.add_argument("--ci-run-id", type=int, help="current GitHub Actions run id, from a dependent final gate job")
    args = p.parse_args(argv)
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")

    terms = {k: False for k in GATE_ORDER}
    evidence: dict[str, str] = {}

    head = sh("git", "rev-parse", "HEAD").stdout.strip()
    terms["ExactSHAPinned"] = head == args.sha and bool(re.fullmatch(r"[0-9a-f]{40}", args.sha))
    evidence["ExactSHAPinned"] = f"HEAD {head}; requested {args.sha}"

    before_ok, before = clean()

    cert = run_v3_certification()
    failed = [s["scenario"] for s in cert["scenarios"] if not s["passed"]]
    for term in CODE_TERMS:
        terms[term] = cert["terms"].get(term) is True
        evidence[term] = "lofgren certify --v3" + ("" if terms[term] else f" (failed: {failed})")

    normal_ok, normal = suite(False)
    utf8_ok, utf8 = suite(True)
    terms["V3RegressionPassing"] = normal_ok and utf8_ok
    evidence["V3RegressionPassing"] = f"normal: {normal}; UTF-8: {utf8}"

    ci_ok, ci_ev, pkg_ok, pkg_ev = ci(args.repo, args.sha, args.ci_run_id)
    terms["GitHubCIPassing"], evidence["GitHubCIPassing"] = ci_ok, ci_ev
    terms["PackageGatePassing"], evidence["PackageGatePassing"] = pkg_ok, pkg_ev

    after_ok, after = clean()
    terms["WorkingTreeClean"] = before_ok and after_ok
    evidence["WorkingTreeClean"] = f"before: {before}; after: {after}"

    print(f"V3ReadyForV4 gate @ {args.sha}")
    for term in GATE_ORDER:
        print(f"  {'TRUE ' if terms[term] else 'FALSE'}  {term}: {evidence.get(term, '')}")
    ready = all(terms.values())
    print()
    print(f"V3ReadyForV4 = {'TRUE' if ready else 'FALSE'}")
    if ready:
        print(f"Pinned SHA: {args.sha}")
        return 0
    print("Blockers: " + ", ".join(k for k, v in terms.items() if not v))
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
