"""Exact-SHA V6 completion certification gate. Unknown is false."""

from __future__ import annotations
import argparse, os, re, subprocess, sys
from pathlib import Path

ROOT=Path(__file__).resolve().parent.parent
sys.path.insert(0,str(ROOT))
from lofgren_intelligence.improvement.certification import CODE_TERMS, GATE_ORDER, run_v6_certification
from lofgren_intelligence.release.ci import verify_exact_ci

REQUIRED_STEPS=(
    "Install package","Run tests","Installed-package smoke test","CLI smoke test",
    "V1 certification gate","V2 discovery certification","V3 production certification",
    "V4 execution certification","V5 outcome certification","V6 improvement certification",
    "Static check","Package smoke test (clean wheel, installed and run outside the source tree)",
    "V1 -> V2 boundary certification",
)

def sh(*args:str,env:dict|None=None):
    return subprocess.run(args,cwd=ROOT,capture_output=True,text=True,encoding="utf-8",errors="replace",env={**os.environ,**(env or {})})
def clean():
    out=sh("git","status","--porcelain","--untracked-files=all")
    return out.returncode==0 and not out.stdout.strip(),out.stdout.strip() or "clean"
def suite(utf8:bool):
    out=sh(sys.executable,"-m","unittest","discover","-s","tests","-t",".","-v",env={"PYTHONUTF8":"1" if utf8 else "0"})
    txt=out.stdout+out.stderr; ran=re.search(r"^Ran (\d+) tests?",txt,re.M); skipped=len(re.findall(r"\.\.\. skipped",txt))
    return out.returncode==0 and bool(re.search(r"^OK\s*$",txt,re.M)) and skipped==0,f"{ran.group(1) if ran else '?'} tests, {skipped} skipped, exit {out.returncode}"

def main(argv=None):
    p=argparse.ArgumentParser(); p.add_argument("--sha",required=True); p.add_argument("--ci-run-id",type=int); p.add_argument("--repo",default="EthanLofgrem/lofgren-intelligence"); a=p.parse_args(argv)
    terms={k:False for k in GATE_ORDER}; ev={}; head=sh("git","rev-parse","HEAD").stdout.strip()
    terms["ExactSHAPinned"]=head==a.sha and bool(re.fullmatch(r"[0-9a-f]{40}",a.sha)); ev["ExactSHAPinned"]=f"HEAD {head}; requested {a.sha}"
    before,btxt=clean(); cert=run_v6_certification()
    for term in CODE_TERMS: terms[term]=cert["terms"].get(term) is True; ev[term]="lofgren certify --v6"
    n,nt=suite(False); u,ut=suite(True); terms["V6RegressionPassing"]=n and u; ev["V6RegressionPassing"]=f"normal: {nt}; UTF-8: {ut}"
    ci,cie,pkg,pkge=verify_exact_ci(repo=a.repo,sha=a.sha,run_id=a.ci_run_id,required_steps=REQUIRED_STEPS)
    terms["GitHubCIPassing"]=ci; ev["GitHubCIPassing"]=cie; terms["PackageGatePassing"]=pkg; ev["PackageGatePassing"]=pkge
    after,atxt=clean(); terms["WorkingTreeClean"]=before and after; ev["WorkingTreeClean"]=f"before: {btxt}; after: {atxt}"
    print(f"V6Complete gate @ {a.sha}")
    for term in GATE_ORDER: print(f"  {'TRUE ' if terms[term] else 'FALSE'}  {term}: {ev.get(term,'')}")
    ready=all(terms.values()); print(); print(f"V6Complete = {'TRUE' if ready else 'FALSE'}")
    if ready: print(f"Pinned SHA: {a.sha}"); return 0
    print("Blockers: "+", ".join(k for k,v in terms.items() if not v)); return 1
if __name__=="__main__": raise SystemExit(main())
