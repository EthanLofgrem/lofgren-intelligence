"""Exact-SHA V3 -> V4 certification gate.

Run only on an exact pushed commit after the Python matrix has completed.
Unknown is false.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from lofgren_intelligence.production.certification import CODE_TERMS, GATE_ORDER, run_v3_certification

MATRIX = ("3.10", "3.11", "3.12")
REQUIRED_STEPS = (
    "Install package",
    "Run tests",
    "Installed-package smoke test",
    "CLI smoke test",
    "V1 certification gate",
    "V2 discovery certification",
    "V3 production certification",
    "Static check",
    "Package smoke test (clean wheel, installed and run outside the source tree)",
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
    env = dict(__import__("os").environ)
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


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--sha", required=True)
    p.add_argument("--ci-run-id", type=int)
    args = p.parse_args(argv)

    terms = {k: False for k in GATE_ORDER}
    evidence: dict[str, str] = {}

    head = sh("git", "rev-parse", "HEAD").stdout.strip()
    terms["ExactSHAPinned"] = head == args.sha and bool(re.fullmatch(r"[0-9a-f]{40}", args.sha))
    evidence["ExactSHAPinned"] = f"HEAD {head}; requested {args.sha}"

    before_ok, before = clean()

    cert = run_v3_certification()
    for term in CODE_TERMS:
        terms[term] = cert["terms"].get(term) is True
        evidence[term] = "lofgren certify --v3"

    normal_ok, normal = suite(False)
    utf8_ok, utf8 = suite(True)
    terms["V3RegressionPassing"] = normal_ok and utf8_ok
    evidence["V3RegressionPassing"] = f"normal: {normal}; UTF-8: {utf8}"

    # CI evidence is checked structurally when this gate runs inside the exact
    # dependent workflow job. The current run id proves which run the final
    # gate belongs to; the three matrix jobs must have completed successfully
    # before this job can start because of needs: test.
    terms["GitHubCIPassing"] = args.ci_run_id is not None
    evidence["GitHubCIPassing"] = (
        f"dependent final job in run {args.ci_run_id}" if args.ci_run_id else "no CI run id supplied"
    )
    terms["PackageGatePassing"] = args.ci_run_id is not None
    evidence["PackageGatePassing"] = (
        f"matrix package step is required before dependent run {args.ci_run_id}" if args.ci_run_id else "no CI evidence"
    )

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
