"""The V1 -> V2 step-4 gate: evaluates every term of V1ReadyForV2Step4 and prints the verdict.

Run it from a fresh clone checked out at the exact commit, after CI for that commit has finished:

    python scripts/boundary_gate.py --sha <40-hex commit>

Terms and their evidence:

    code terms (11)        `python -m lofgren_intelligence certify-boundary` in this checkout
    V2RegressionPassing    the full test suite (V1 and V2), normal and UTF-8 mode: OK with nothing skipped
    PackageGatePassing     GitHub Actions, every matrix job: install, installed-package smoke, clean-wheel steps
    GitHubCIPassing        GitHub Actions: a completed push run for this exact commit, every job successful
    WorkingTreeClean       `git status --porcelain` empty before and after everything above
    ExactSHAPinned         HEAD is exactly --sha

Anything that cannot be evidenced (no network, CI still running, a step missing) is FALSE: UNKNOWN = FALSE.
Read-only: it changes nothing locally or remotely. Exit status 0 only when V1ReadyForV2Step4 = TRUE.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from lofgren_intelligence.boundary_certification import CODE_TERMS, GATE_ORDER  # noqa: E402

MATRIX = ("3.10", "3.11", "3.12")
WORKFLOW = "tests"
PACKAGE_STEPS = ("Install package", "Installed-package smoke test",
                 "Package smoke test (clean wheel, installed and run outside the source tree)")
REQUIRED_STEPS = PACKAGE_STEPS + ("Run tests", "CLI smoke test", "V1 certification gate", "Static check",
                                  "V1 -> V2 boundary certification")


def sh(*args: str, env: dict | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(args, cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace",
                          env={**os.environ, **(env or {})})


def git_clean() -> tuple[bool, str]:
    out = sh("git", "status", "--porcelain", "--untracked-files=all")
    if out.returncode != 0:  # a failed status is no evidence of a clean tree
        return False, f"git status failed (exit {out.returncode}): {out.stderr.strip()[:200]}"
    return not out.stdout.strip(), out.stdout.strip() or "clean"


def suite(utf8: bool) -> tuple[bool, str]:
    env = {"PYTHONUTF8": "1"} if utf8 else {"PYTHONUTF8": "0"}
    out = sh(sys.executable, "-m", "unittest", "discover", "-s", "tests", "-t", ".", "-v", env=env)
    text = out.stdout + out.stderr
    ran = re.search(r"^Ran (\d+) tests?", text, re.M)
    skipped = len(re.findall(r"\.\.\. skipped", text))
    ok = out.returncode == 0 and bool(re.search(r"^OK\s*$", text, re.M)) and skipped == 0
    return ok, f"{ran.group(1) if ran else '?'} tests, {skipped} skipped, exit {out.returncode}"


def boundary() -> tuple[dict, str]:
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "boundary.json"
        out = sh(sys.executable, "-m", "lofgren_intelligence", "certify-boundary", "--out", str(path),
                 env={"PYTHONUTF8": "1"})
        if not path.exists():
            return {t: False for t in CODE_TERMS}, f"certify-boundary produced no result (exit {out.returncode})"
        cert = json.loads(path.read_text(encoding="utf-8"))
    failed = [f"{s['scenario']}: {s['detail']}" for s in cert["scenarios"] if not s["passed"]]
    return cert["terms"], "; ".join(failed) or f"{len(cert['scenarios'])} scenarios passed"


def get(url: str) -> dict:
    req = urllib.request.Request(url, headers={"Accept": "application/vnd.github+json",
                                               "User-Agent": "lofgren-boundary-gate"})
    with urllib.request.urlopen(req, timeout=30) as resp:  # public, read-only
        return json.load(resp)


def ci(repo: str, sha: str) -> tuple[bool, str, bool, str]:
    """(GitHubCIPassing, evidence, PackageGatePassing, evidence)."""
    try:
        runs = get(f"https://api.github.com/repos/{repo}/actions/runs?head_sha={sha}&event=push")["workflow_runs"]
    except Exception as exc:  # no evidence is no pass
        return False, f"GitHub API unavailable: {exc}", False, "no CI evidence"
    runs = [r for r in runs if r["name"] == WORKFLOW and r["head_sha"] == sha]
    if not runs:
        return False, f"no '{WORKFLOW}' push run for {sha[:7]}", False, "no CI evidence"
    run = max(runs, key=lambda r: r["run_number"])
    if run["status"] != "completed":
        return False, f"run {run['id']} is {run['status']}", False, "CI not finished"
    try:
        jobs = get(f"https://api.github.com/repos/{repo}/actions/runs/{run['id']}/jobs")["jobs"]
    except Exception as exc:
        return False, f"GitHub API unavailable: {exc}", False, "no CI evidence"
    by_python = {}
    for j in jobs:
        m = re.search(r"\((3\.\d+)\)", j["name"])
        if m:
            by_python[m.group(1)] = j
    problems, package = [], []
    for py in MATRIX:
        j = by_python.get(py)
        if j is None:
            problems.append(f"no job for Python {py}")
            package.append(f"no job for Python {py}")
            continue
        steps = {s["name"]: s["conclusion"] for s in j["steps"]}
        if j["conclusion"] != "success":
            problems.append(f"Python {py} job {j['conclusion']}")
        for name in REQUIRED_STEPS:
            if steps.get(name) != "success":
                problems.append(f"Python {py} step '{name}': {steps.get(name, 'missing')}")
        for name in PACKAGE_STEPS:
            if steps.get(name) != "success":
                package.append(f"Python {py} step '{name}': {steps.get(name, 'missing')}")
    ci_ok = run["conclusion"] == "success" and not problems
    ci_ev = f"run {run['id']} {run['conclusion']}" + (f"; {'; '.join(problems)}" if problems else
                                                       f"; Python {', '.join(MATRIX)} all steps passed")
    pkg_ev = "; ".join(package) or f"run {run['id']}: install, installed-package and clean-wheel steps passed " \
                                   f"on Python {', '.join(MATRIX)}"
    return ci_ok, ci_ev, not package, pkg_ev


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--sha", required=True, help="the exact 40-hex commit being certified")
    parser.add_argument("--repo", default="EthanLofgrem/lofgren-intelligence")
    args = parser.parse_args(argv)
    for stream in (sys.stdout, sys.stderr):  # the report uses non-ASCII characters; never crash or garble them
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")

    terms: dict[str, bool] = {}
    evidence: dict[str, str] = {}
    head = sh("git", "rev-parse", "HEAD").stdout.strip()
    terms["ExactSHAPinned"] = bool(re.fullmatch(r"[0-9a-f]{40}", args.sha)) and head == args.sha
    evidence["ExactSHAPinned"] = f"HEAD {head or '?'}; requested {args.sha}"
    clean_before, before = git_clean()

    code, code_ev = boundary()
    for t in CODE_TERMS:
        terms[t] = bool(code.get(t))
        evidence[t] = "certify-boundary" if terms[t] else code_ev

    plain_ok, plain = suite(utf8=False)
    utf8_ok, utf8 = suite(utf8=True)
    terms["V2RegressionPassing"] = plain_ok and utf8_ok
    evidence["V2RegressionPassing"] = f"normal: {plain}; UTF-8: {utf8}"

    ci_ok, ci_ev, pkg_ok, pkg_ev = ci(args.repo, args.sha)
    terms["GitHubCIPassing"], evidence["GitHubCIPassing"] = ci_ok, ci_ev
    terms["PackageGatePassing"], evidence["PackageGatePassing"] = pkg_ok, pkg_ev

    clean_after, after = git_clean()
    terms["WorkingTreeClean"] = clean_before and clean_after
    evidence["WorkingTreeClean"] = f"before: {before}; after: {after}"

    print(f"V1ReadyForV2Step4 gate — {args.repo} @ {args.sha}")
    for t in GATE_ORDER:
        print(f"  {'TRUE ' if terms.get(t) else 'FALSE'}  {t}: {evidence.get(t, 'no evidence')}")
    ready = all(terms.get(t, False) for t in GATE_ORDER)
    print()
    if ready:
        print("V1ReadyForV2Step4 = TRUE")
        print(f"Pinned SHA: {args.sha}")
        return 0
    print("V1ReadyForV2Step4 = FALSE")
    print("Blockers: " + ", ".join(t for t in GATE_ORDER if not terms.get(t, False)))
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
