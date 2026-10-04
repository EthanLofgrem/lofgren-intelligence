"""The complete V2 -> V3 gate: evaluates every term of V2ReadyForV3.

Run this only from a fresh clone checked out at the exact commit, after CI for
that commit has finished:

    python scripts/v2_gate.py --sha <40-hex commit>

Unknown is false. The script is read-only and exits 0 only when every V2 code
and process term is proven on the same exact SHA.
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

from lofgren_intelligence.discovery.certification import CODE_TERMS, GATE_ORDER  # noqa: E402

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
    "V1 -> V2 boundary certification",
    "V2 discovery certification",
    "Static check",
)


def sh(*args: str, env: dict | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        args,
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env={**os.environ, **(env or {})},
    )


def git_clean() -> tuple[bool, str]:
    out = sh("git", "status", "--porcelain", "--untracked-files=all")
    if out.returncode != 0:
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


def v2_cert() -> tuple[dict[str, bool], str]:
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "v2-cert.json"
        out = sh(
            sys.executable,
            "-m",
            "lofgren_intelligence",
            "certify",
            "--v2",
            "--out",
            str(path),
            env={"PYTHONUTF8": "1"},
        )
        if not path.exists():
            return {t: False for t in CODE_TERMS}, f"V2 certification produced no result (exit {out.returncode})"
        cert = json.loads(path.read_text(encoding="utf-8"))
    failed = [f"{s['scenario']}: {s['detail']}" for s in cert.get("scenarios", []) if not s.get("passed")]
    return cert.get("terms", {}), "; ".join(failed) or f"{len(cert.get('scenarios', []))} scenarios passed"


def v1_boundary() -> tuple[bool, str]:
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "boundary.json"
        out = sh(
            sys.executable,
            "-m",
            "lofgren_intelligence",
            "certify-boundary",
            "--out",
            str(path),
            env={"PYTHONUTF8": "1"},
        )
        if out.returncode != 0 or not path.exists():
            return False, f"certify-boundary exit {out.returncode}; result file={path.exists()}"
        cert = json.loads(path.read_text(encoding="utf-8"))
    failed = [s["scenario"] for s in cert.get("scenarios", []) if not s.get("passed")]
    ok = bool(cert.get("terms")) and all(bool(v) for v in cert["terms"].values()) and not failed
    return ok, f"{len(cert.get('scenarios', []))} scenarios; failed={failed or 'none'}"


def get(url: str) -> dict:
    req = urllib.request.Request(
        url,
        headers={"Accept": "application/vnd.github+json", "User-Agent": "lofgren-v2-gate"},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.load(resp)


def ci(repo: str, sha: str) -> tuple[bool, str, bool, str]:
    try:
        runs = get(f"https://api.github.com/repos/{repo}/actions/runs?head_sha={sha}&event=push")["workflow_runs"]
    except Exception as exc:
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

    by_python: dict[str, dict] = {}
    for job in jobs:
        match = re.search(r"\((3\.\d+)\)", job["name"])
        if match:
            by_python[match.group(1)] = job

    problems: list[str] = []
    package_problems: list[str] = []
    for py in MATRIX:
        job = by_python.get(py)
        if job is None:
            problems.append(f"no job for Python {py}")
            package_problems.append(f"no job for Python {py}")
            continue
        steps = {s["name"]: s["conclusion"] for s in job["steps"]}
        if job["conclusion"] != "success":
            problems.append(f"Python {py} job {job['conclusion']}")
        for name in REQUIRED_STEPS:
            if steps.get(name) != "success":
                problems.append(f"Python {py} step '{name}': {steps.get(name, 'missing')}")
        for name in PACKAGE_STEPS:
            if steps.get(name) != "success":
                package_problems.append(f"Python {py} step '{name}': {steps.get(name, 'missing')}")

    ci_ok = run["conclusion"] == "success" and not problems
    ci_ev = (
        f"run {run['id']} {run['conclusion']}"
        + (f"; {'; '.join(problems)}" if problems else f"; Python {', '.join(MATRIX)} all required steps passed")
    )
    pkg_ev = "; ".join(package_problems) or (
        f"run {run['id']}: install, installed-package and clean-wheel steps passed on Python {', '.join(MATRIX)}"
    )
    return ci_ok, ci_ev, not package_problems, pkg_ev


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--sha", required=True, help="exact 40-hex commit being certified")
    parser.add_argument("--repo", default="EthanLofgrem/lofgren-intelligence")
    args = parser.parse_args(argv)

    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")

    terms: dict[str, bool] = {}
    evidence: dict[str, str] = {}

    head = sh("git", "rev-parse", "HEAD").stdout.strip()
    terms["ExactSHAPinned"] = bool(re.fullmatch(r"[0-9a-f]{40}", args.sha)) and head == args.sha
    evidence["ExactSHAPinned"] = f"HEAD {head or '?'}; requested {args.sha}"
    clean_before, before = git_clean()

    code, code_ev = v2_cert()
    for term in CODE_TERMS:
        terms[term] = code.get(term) is True
        evidence[term] = "lofgren certify --v2" if terms[term] else code_ev

    plain_ok, plain = suite(False)
    utf8_ok, utf8 = suite(True)
    terms["V2RegressionPassing"] = plain_ok and utf8_ok
    evidence["V2RegressionPassing"] = f"normal: {plain}; UTF-8: {utf8}"

    boundary_ok, boundary_ev = v1_boundary()
    terms["V1BoundaryCertified"] = boundary_ok
    evidence["V1BoundaryCertified"] = boundary_ev

    ci_ok, ci_ev, pkg_ok, pkg_ev = ci(args.repo, args.sha)
    terms["GitHubCIPassing"] = ci_ok
    evidence["GitHubCIPassing"] = ci_ev
    terms["PackageGatePassing"] = pkg_ok
    evidence["PackageGatePassing"] = pkg_ev

    clean_after, after = git_clean()
    terms["WorkingTreeClean"] = clean_before and clean_after
    evidence["WorkingTreeClean"] = f"before: {before}; after: {after}"

    print(f"V2ReadyForV3 gate — {args.repo} @ {args.sha}")
    for term in GATE_ORDER:
        print(f"  {'TRUE ' if terms.get(term) else 'FALSE'}  {term}: {evidence.get(term, 'no evidence')}")
    ready = all(terms.get(term, False) for term in GATE_ORDER)
    print()
    if ready:
        print("V2ReadyForV3 = TRUE")
        print(f"Pinned SHA: {args.sha}")
        return 0
    print("V2ReadyForV3 = FALSE")
    print("Blockers: " + ", ".join(term for term in GATE_ORDER if not terms.get(term, False)))
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
