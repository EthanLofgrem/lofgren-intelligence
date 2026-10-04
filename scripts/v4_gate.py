"""Exact-SHA V4 -> V5 certification gate."""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from lofgren_intelligence.execution.certification import CODE_TERMS, GATE_ORDER, run_v4_certification

REQUIRED_STEPS = (
    "Install package",
    "Run tests",
    "Installed-package smoke test",
    "CLI smoke test",
    "V1 certification gate",
    "V2 discovery certification",
    "V3 production certification",
    "V4 execution certification",
    "Static check",
    "Package smoke test (clean wheel, installed and run outside the source tree)",
    "V1 -> V2 boundary certification",
)


def sh(*args: str, env: dict | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        args, cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace",
        env={**os.environ, **(env or {})},
    )


def clean() -> tuple[bool, str]:
    out = sh("git", "status", "--porcelain", "--untracked-files=all")
    if out.returncode != 0:
        return False, f"git status failed: {out.stderr.strip()}"
    return not out.stdout.strip(), out.stdout.strip() or "clean"


def suite(utf8: bool) -> tuple[bool, str]:
    out = sh(sys.executable, "-m", "unittest", "discover", "-s", "tests", "-t", ".", "-v",
             env={"PYTHONUTF8": "1" if utf8 else "0"})
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
    cert = run_v4_certification()
    for term in CODE_TERMS:
        terms[term] = cert["terms"].get(term) is True
        evidence[term] = "lofgren certify --v4"

    normal_ok, normal = suite(False)
    utf8_ok, utf8 = suite(True)
    terms["V4RegressionPassing"] = normal_ok and utf8_ok
    evidence["V4RegressionPassing"] = f"normal: {normal}; UTF-8: {utf8}"

    terms["GitHubCIPassing"] = args.ci_run_id is not None
    evidence["GitHubCIPassing"] = f"dependent final job in run {args.ci_run_id}" if args.ci_run_id else "no CI run id"
    terms["PackageGatePassing"] = args.ci_run_id is not None
    evidence["PackageGatePassing"] = f"matrix package step required before run {args.ci_run_id}" if args.ci_run_id else "no CI evidence"

    after_ok, after = clean()
    terms["WorkingTreeClean"] = before_ok and after_ok
    evidence["WorkingTreeClean"] = f"before: {before}; after: {after}"

    print(f"V4ReadyForV5 gate @ {args.sha}")
    for term in GATE_ORDER:
        print(f"  {'TRUE ' if terms[term] else 'FALSE'}  {term}: {evidence.get(term, '')}")
    ready = all(terms.values())
    print()
    print(f"V4ReadyForV5 = {'TRUE' if ready else 'FALSE'}")
    if ready:
        print(f"Pinned SHA: {args.sha}")
        return 0
    print("Blockers: " + ", ".join(k for k, v in terms.items() if not v))
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
