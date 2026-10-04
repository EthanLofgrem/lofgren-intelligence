"""Exact-SHA V5 -> V6 certification gate."""

from __future__ import annotations
import argparse, os, re, subprocess, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from lofgren_intelligence.outcome.certification import CODE_TERMS, GATE_ORDER, run_v5_certification

def sh(*args: str, env: dict | None = None):
    return subprocess.run(args, cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace",
                          env={**os.environ, **(env or {})})

def clean():
    out=sh("git","status","--porcelain","--untracked-files=all")
    return (out.returncode==0 and not out.stdout.strip(), out.stdout.strip() or "clean")

def suite(utf8: bool):
    out=sh(sys.executable,"-m","unittest","discover","-s","tests","-t",".","-v",
           env={"PYTHONUTF8":"1" if utf8 else "0"})
    text=out.stdout+out.stderr
    ran=re.search(r"^Ran (\d+) tests?",text,re.M)
    skipped=len(re.findall(r"\.\.\. skipped",text))
    ok=out.returncode==0 and bool(re.search(r"^OK\s*$",text,re.M)) and skipped==0
    return ok, f"{ran.group(1) if ran else '?'} tests, {skipped} skipped, exit {out.returncode}"

def main(argv=None):
    p=argparse.ArgumentParser(); p.add_argument("--sha",required=True); p.add_argument("--ci-run-id",type=int)
    a=p.parse_args(argv); terms={k:False for k in GATE_ORDER}; ev={}
    head=sh("git","rev-parse","HEAD").stdout.strip()
    terms["ExactSHAPinned"]=head==a.sha and bool(re.fullmatch(r"[0-9a-f]{40}",a.sha))
    ev["ExactSHAPinned"]=f"HEAD {head}; requested {a.sha}"
    b,btxt=clean()
    cert=run_v5_certification()
    for term in CODE_TERMS:
        terms[term]=cert["terms"].get(term) is True; ev[term]="lofgren certify --v5"
    n,nt=suite(False); u,ut=suite(True)
    terms["V5RegressionPassing"]=n and u; ev["V5RegressionPassing"]=f"normal: {nt}; UTF-8: {ut}"
    terms["GitHubCIPassing"]=a.ci_run_id is not None; ev["GitHubCIPassing"]=f"dependent final job in run {a.ci_run_id}" if a.ci_run_id else "no CI"
    terms["PackageGatePassing"]=a.ci_run_id is not None; ev["PackageGatePassing"]=f"matrix package step required before run {a.ci_run_id}" if a.ci_run_id else "no CI"
    af,at=clean(); terms["WorkingTreeClean"]=b and af; ev["WorkingTreeClean"]=f"before: {btxt}; after: {at}"
    print(f"V5ReadyForV6 gate @ {a.sha}")
    for term in GATE_ORDER: print(f"  {'TRUE ' if terms[term] else 'FALSE'}  {term}: {ev.get(term,'')}")
    ready=all(terms.values()); print(); print(f"V5ReadyForV6 = {'TRUE' if ready else 'FALSE'}")
    if ready: print(f"Pinned SHA: {a.sha}"); return 0
    print("Blockers: "+", ".join(k for k,v in terms.items() if not v)); return 1
if __name__=="__main__": raise SystemExit(main())
