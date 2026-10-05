"""Exact-SHA V6 completion certification gate."""

from __future__ import annotations
import argparse, json, os, re, subprocess, sys, urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from lofgren_intelligence.improvement.certification import CODE_TERMS, GATE_ORDER, run_v6_certification

MATRIX=("3.10","3.11","3.12")
REQUIRED_STEPS=(
    "Install package","Run tests","Installed-package smoke test","CLI smoke test",
    "V1 certification gate","V2 discovery certification","V3 production certification",
    "V4 execution certification","V5 outcome certification","V6 improvement certification",
    "Static check","Package smoke test (clean wheel, installed and run outside the source tree)",
    "V1 -> V2 boundary certification",
)

def sh(*args:str,env:dict|None=None):
    return subprocess.run(args,cwd=ROOT,capture_output=True,text=True,encoding="utf-8",errors="replace",
                          env={**os.environ,**(env or {})})

def clean():
    out=sh("git","status","--porcelain","--untracked-files=all")
    return (out.returncode==0 and not out.stdout.strip(),out.stdout.strip() or "clean")

def suite(utf8:bool):
    out=sh(sys.executable,"-m","unittest","discover","-s","tests","-t",".","-v",
           env={"PYTHONUTF8":"1" if utf8 else "0"})
    txt=out.stdout+out.stderr
    ran=re.search(r"^Ran (\d+) tests?",txt,re.M)
    skipped=len(re.findall(r"\.\.\. skipped",txt))
    ok=out.returncode==0 and bool(re.search(r"^OK\s*$",txt,re.M)) and skipped==0
    return ok,f"{ran.group(1) if ran else '?'} tests, {skipped} skipped, exit {out.returncode}"

def get(url:str):
    req=urllib.request.Request(url,headers={"Accept":"application/vnd.github+json","User-Agent":"lofgren-v6-gate",
                                           **({"Authorization":f"Bearer {os.environ['GITHUB_TOKEN']}"} if os.environ.get("GITHUB_TOKEN") else {})})
    with urllib.request.urlopen(req,timeout=30) as resp:
        return json.load(resp)

def ci(repo:str,sha:str,run_id:int|None):
    if run_id is None:
        return False,"no CI run id",False,"no package evidence"
    try:
        run=get(f"https://api.github.com/repos/{repo}/actions/runs/{run_id}")
        if run.get("head_sha")!=sha or run.get("name")!="tests":
            return False,"CI run identity mismatch",False,"CI run identity mismatch"
        jobs=get(f"https://api.github.com/repos/{repo}/actions/runs/{run_id}/jobs").get("jobs",[])
    except Exception as exc:
        return False,f"GitHub API unavailable: {exc}",False,"no package evidence"
    by_py={}
    for job in jobs:
        m=re.search(r"\((3\.\d+)\)",job.get("name",""))
        if m: by_py[m.group(1)]=job
    problems=[]; pkg=[]
    for py in MATRIX:
        job=by_py.get(py)
        if not job:
            problems.append(f"no job for Python {py}"); pkg.append(f"no job for Python {py}"); continue
        steps={s["name"]:s.get("conclusion") for s in job.get("steps",[])}
        if job.get("conclusion")!="success": problems.append(f"Python {py} job {job.get('conclusion')}")
        for name in REQUIRED_STEPS:
            if steps.get(name)!="success": problems.append(f"Python {py} step {name}: {steps.get(name,'missing')}")
        if steps.get("Package smoke test (clean wheel, installed and run outside the source tree)")!="success":
            pkg.append(f"Python {py} package smoke failed/missing")
    return (not problems,"; ".join(problems) or f"run {run_id}: all required steps passed on {', '.join(MATRIX)}",
            not pkg,"; ".join(pkg) or f"run {run_id}: clean-wheel package smoke passed on {', '.join(MATRIX)}")

def main(argv=None):
    p=argparse.ArgumentParser(); p.add_argument("--sha",required=True); p.add_argument("--ci-run-id",type=int)
    p.add_argument("--repo",default="EthanLofgrem/lofgren-intelligence")
    a=p.parse_args(argv)
    terms={k:False for k in GATE_ORDER}; ev={}
    head=sh("git","rev-parse","HEAD").stdout.strip()
    terms["ExactSHAPinned"]=head==a.sha and bool(re.fullmatch(r"[0-9a-f]{40}",a.sha)); ev["ExactSHAPinned"]=f"HEAD {head}; requested {a.sha}"
    b,btxt=clean()
    cert=run_v6_certification()
    for term in CODE_TERMS: terms[term]=cert["terms"].get(term) is True; ev[term]="lofgren certify --v6"
    n,nt=suite(False); u,ut=suite(True)
    terms["V6RegressionPassing"]=n and u; ev["V6RegressionPassing"]=f"normal: {nt}; UTF-8: {ut}"
    ci_ok,ci_ev,pkg_ok,pkg_ev=ci(a.repo,a.sha,a.ci_run_id)
    terms["GitHubCIPassing"]=ci_ok; ev["GitHubCIPassing"]=ci_ev
    terms["PackageGatePassing"]=pkg_ok; ev["PackageGatePassing"]=pkg_ev
    af,at=clean(); terms["WorkingTreeClean"]=b and af; ev["WorkingTreeClean"]=f"before: {btxt}; after: {at}"
    print(f"V6Complete gate @ {a.sha}")
    for term in GATE_ORDER: print(f"  {'TRUE ' if terms[term] else 'FALSE'}  {term}: {ev.get(term,'')}")
    ready=all(terms.values()); print(); print(f"V6Complete = {'TRUE' if ready else 'FALSE'}")
    if ready: print(f"Pinned SHA: {a.sha}"); return 0
    print("Blockers: "+", ".join(k for k,v in terms.items() if not v)); return 1
if __name__=="__main__": raise SystemExit(main())
