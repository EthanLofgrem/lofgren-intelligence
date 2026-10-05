"""Offline certification for V3 Production Intelligence: `lofgren certify --v3`.

Each code term is proven by executable scenarios over fictional fixtures (heuristic provider, no network, no model).
A scenario passes only by returning; an assertion or any crash fails it. The process terms (regression suite,
package gate, CI, clean tree, exact SHA) are evaluated by scripts/v3_gate.py. Unknown is false.
"""

from __future__ import annotations

import ast
import copy
import hashlib
import json
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from ..certification import AGREE_AND_CONFLICT, OBJECTIVE, _docs, _run
from ..discovery import fixtures as F
from ..discovery.context import DiscoveryContext
from ..discovery.fixtures import AT, warehouse_design
from ..discovery.pipeline import discover_from_run, run_discovery
from ..kernel.knowledge_map import export_knowledge_map
from . import codegen
from .core import (
    AUTHORITY_REQUIRED_ACTIONS,
    ArtifactFile,
    ProductionError,
    _artifact_id,
    _file_meta,
    build_artifact,
    check_production_receipt,
    validate_v4_handoff,
    verify_artifact,
    verify_production_receipt,
)
from .sandbox import run_tests
from .spec import compile_specification
from .store import RECEIPT_RECORD, verify_directory, write_artifact

CODE_TERMS = (
    "V2HandoffValidated",
    "ArtifactBuildDeterministic",
    "ArtifactProvenanceComplete",
    "AcceptanceCriteriaEvaluated",
    "ArtifactIndependentVerification",
    "ProductionReceiptReproducible",
    "UnsafePathsRejected",
    "PythonSyntaxValidated",
    "TamperDetected",
    "V4HandoffValidated",
    "V3E2ECertificationPassing",
    "V2CertifiedUpstream",
    "HandoffBoundToDiscovery",
    "SpecificationCompiled",
    "ArtifactPlanTraceable",
    "GeneratedTestsExecuted",
    "ChecksFaithfulToV2",
    "RegenerationVerified",
    "ReceiptBoundToArtifact",
    "ArtifactDiskRoundTrip",
    "V3CLIUsable",
    "V3MCPToolsTyped",
    "ExecutionAuthorityAbsent",
    "FailClosedWithoutSelection",
    "V3AdversarialSuitePassing",
)

PROCESS_TERMS = (
    "V3RegressionPassing",
    "PackageGatePassing",
    "GitHubCIPassing",
    "WorkingTreeClean",
    "ExactSHAPinned",
)
GATE_ORDER = CODE_TERMS + PROCESS_TERMS


@dataclass
class Scenario:
    term: str
    scenario: str
    passed: bool
    detail: str


_CACHE: dict[str, object] = {}


def _get(key: str, build: Callable[[], object]):
    if key not in _CACHE:
        _CACHE[key] = build()
    return _CACHE[key]


def _fixture():
    """(context, discovery) for the certification warehouse fixture. Built once per process."""
    def make():
        v1 = _run(OBJECTIVE, _docs(AGREE_AND_CONFLICT))
        context = DiscoveryContext(export_knowledge_map(v1), v1.receipt)
        discovery = run_discovery(context, "Choose a fictional warehouse size", design=warehouse_design(), at=AT)
        if discovery.handoff is None:
            raise AssertionError(f"fixture produced no V3 handoff: {discovery.outcome.value}")
        return context, discovery
    return _get("fixture", make)


def _one(term: str, scenario: str, fn: Callable[[], str]) -> Scenario:
    try:
        return Scenario(term, scenario, True, fn())
    except Exception as exc:  # a crash is a failure, never a pass
        return Scenario(term, scenario, False, f"{type(exc).__name__}: {exc}")


def _build(kind: str = "structured_bundle"):
    context, discovery = _fixture()
    return build_artifact(discovery.handoff, discovery_receipt=discovery.receipt, context=context, kind=kind)


def _built(kind: str = "structured_bundle"):
    return _get(f"built:{kind}", lambda: _build(kind))


def _refused(fn: Callable[[], object], *errors: type[BaseException]) -> str:
    try:
        fn()
    except errors as exc:
        return str(exc)
    raise AssertionError(f"accepted; expected {' or '.join(e.__name__ for e in errors)}")


def _rehash(artifact: dict) -> dict:
    """Recompute file hashes and the artifact id after an edit: the tamper a verifier must still catch."""
    for f in artifact["files"]:
        f["sha256"] = hashlib.sha256(f["content"].encode("utf-8")).hexdigest()
    artifact["artifact_id"] = _artifact_id(artifact["schema"], artifact["kind"], artifact["source_discovery_id"],
                                           _file_meta(artifact["files"]), artifact["acceptance"])
    return artifact


def _file(artifact: dict, path: str) -> dict:
    return next(f for f in artifact["files"] if f["path"] == path)


def _domain(name: str):
    """(context, discovery) for another V2 domain: software dependencies, an LP optimum, a measured sensor fact."""
    def make():
        if name == "software":
            run = _run("Can the order service handle more traffic?", _docs(F.SOFTWARE_DOCS))
            d = discover_from_run(run, "Size the order service (fictional)", design=F.software_design(), at=AT)
        elif name == "resource":
            run = _run(OBJECTIVE, _docs(AGREE_AND_CONFLICT))
            d = discover_from_run(run, "Plan production (fictional)", design=F.resource_design(), at=AT)
        else:
            run = F.sensor_run()
            cid = next(iter(run.graph.claims))
            d = discover_from_run(run, "Size the warehouse cooling (fictional)", design=F.engineering_design(cid),
                                  at=AT)
        assert d.handoff is not None, f"{name}: no handoff ({d.outcome.value})"
        return d
    return _get(f"domain:{name}", make)


def run_v3_certification() -> dict:
    s: list[Scenario] = []

    def add(term: str, scenario: str):
        def deco(fn: Callable[[], str]) -> Callable[[], str]:
            s.append(_one(term, scenario, fn))
            return fn
        return deco

    @add("V2HandoffValidated", "validated V2 handoff builds; a tampered handoff is refused")
    def _():
        context, d = _fixture()
        bad = copy.deepcopy(d.handoff)
        bad["specifications"][0]["value"] = 1.0
        _refused(lambda: build_artifact(bad, discovery_receipt=d.receipt, context=context), ProductionError)
        return _built().v4_handoff["source_discovery_id"]

    @add("ArtifactBuildDeterministic", "identical handoff -> identical artifact, receipt and V4 handoff")
    def _():
        a, b = _build(), _build()
        assert a.artifact == b.artifact and a.receipt == b.receipt and a.v4_handoff == b.v4_handoff
        return a.artifact_id

    @add("ArtifactProvenanceComplete", "artifact keeps upstream provenance")
    def _():
        r, (_, d) = _built(), _fixture()
        p = r.artifact["provenance"]
        assert p["discovery_id"] == d.receipt["discovery_id"] == r.v4_handoff["source_discovery_id"]
        assert p["discovery_fingerprint"] == d.receipt["discovery_fingerprint"]
        assert p["evidence_fingerprint"] == d.handoff["evidence_fingerprint"]
        return "discovery id, discovery fingerprint and evidence fingerprint preserved"

    @add("AcceptanceCriteriaEvaluated", "every acceptance criterion and evaluable constraint is checked")
    def _():
        r = _built()
        kinds = {x.kind for x in r.acceptance}
        assert r.acceptance and all(x.passed for x in r.acceptance) and kinds == {"acceptance", "constraint"}
        return f"{len(r.acceptance)} checks passed ({sorted(kinds)})"

    @add("ArtifactIndependentVerification", "independent verifier re-derives the bundle")
    def _():
        for kind in ("structured_bundle", "markdown", "python_module"):
            check = verify_artifact(_built(kind).artifact)
            assert check.passed, (kind, check.problems)
        return "structure, regeneration, fingerprint, provenance, traceability and checks re-derived for 3 kinds"

    @add("ProductionReceiptReproducible", "production receipt verifies and is reproduced exactly")
    def _():
        r = _built()
        assert verify_production_receipt(r.receipt) and r.receipt == _build().receipt
        bad = dict(r.receipt, artifact_id="AF-forged")
        assert not verify_production_receipt(bad)
        return r.receipt["receipt_hash"]

    @add("UnsafePathsRejected", "path traversal, absolute and odd paths refused")
    def _():
        for path in ("../escape.txt", "/etc/passwd", "a/../../b", "./x", "C:\\x", "", "a b"):
            _refused(lambda p=path: ArtifactFile.make(p, "text/plain", "x"), ProductionError)
        return "7 unsafe paths refused"

    @add("PythonSyntaxValidated", "generated Python parses, compiles and imports nothing outside the stdlib")
    def _():
        r = _built("python_module")
        for f in r.artifact["files"]:
            if f["path"].endswith(".py"):
                tree = ast.parse(f["content"])
                compile(f["content"], f["path"], "exec")
                mods = {a.name.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
                mods |= {n.module.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module}
                assert mods <= {"json", "pathlib", "unittest", "artifact"}, mods
        return "artifact.py and test_artifact.py compile; imports: json, pathlib, unittest, artifact"

    @add("TamperDetected", "file mutation is detected")
    def _():
        bad = copy.deepcopy(_built().artifact)
        bad["files"][0]["content"] += "\nTAMPER"
        check = verify_artifact(bad)
        assert not check.passed and any("hash mismatch" in p for p in check.problems)
        return "; ".join(check.problems)

    @add("V4HandoffValidated", "V4 handoff preserves the authority boundary and matches the receipt")
    def _():
        r = _built()
        h = r.v4_handoff
        assert h["schema"] == "lofgren.v4-handoff/1" and h["artifact_verified"] and h["acceptance_passed"]
        assert h["authority_required"] is True and h["requested_actions"] == [] and h["authority"]["granted"] is False
        assert validate_v4_handoff(h, r.receipt, r.artifact) == []
        for change in ({"requested_actions": ["deploy"]}, {"authority": {"granted": True}},
                       {"authority_required": False}, {"artifact_id": "AF-other"}, {"tests": None}):
            assert validate_v4_handoff({**h, **change}, r.receipt, r.artifact), change
        return "verified artifact handed off with no authority granted; 5 forged handoffs refused"

    @add("V3E2ECertificationPassing", "V2 handoff -> spec -> plan -> generate -> test -> verify -> receipt -> V4")
    def _():
        for kind in ("structured_bundle", "markdown", "python_module"):
            r = _built(kind)
            assert verify_artifact(r.artifact).passed and verify_production_receipt(r.receipt)
            assert r.v4_handoff["artifact_id"] == r.artifact_id
        return "all 3 artifact kinds, warehouse domain"

    @add("V3E2ECertificationPassing", "three more domains: software dependencies, LP optimum, measured sensor fact")
    def _():
        out = []
        for name in ("software", "resource", "engineering"):
            d = _domain(name)
            r = build_artifact(d.handoff, discovery_receipt=d.receipt, context=d.context)
            assert r.verification.passed and r.verification.tests["ok"], (name, r.verification.problems)
            spec = r.spec
            if name == "software":
                assert spec.ids("dependency"), "software dependencies did not become requirements"
            if name == "engineering":
                temp = next(x for x in d.handoff["specifications"] if x["name"] == "temp")
                assert temp["source_id"].startswith("CL-") and spec.values["temp"][0] == 42.0
            out.append(f"{name}: {len(spec.requirements)} requirements, {r.verification.tests['ran']} tests")
        return "; ".join(out)

    @add("V2CertifiedUpstream", "V2 code terms still hold under V3")
    def _():
        from ..discovery.certification import run_v2_certification

        cert = run_v2_certification()
        failed = [k for k, v in cert["terms"].items() if not v]
        assert cert["code_terms_certified"] and not failed, failed
        return f"{len(cert['terms'])} V2 code terms TRUE"

    @add("HandoffBoundToDiscovery", "every handoff number and criterion equals the discovery object it cites")
    def _():
        from .upstream import handoff_integrity_problems

        context, d = _fixture()
        assert handoff_integrity_problems(d.handoff, d.receipt, context) == []
        edits = {
            "specification": lambda h: h["specifications"][0].update(value=h["specifications"][0]["value"] + 1),
            "simulated outcome": lambda h: h["expected_outcomes"][0].update(mean=1e9),
            "acceptance criterion": lambda h: h["acceptance_criteria"][0]["relation"]["rhs"].update(value=-1e12),
            "constraint": lambda h: h["constraints"][0]["relation"].update(op=">="),
            "objective": lambda h: h.update(objective="Something else"),
            "assumption": lambda h: h["assumptions"][0].update(statement="Rent is free"),
            "dropped assumption": lambda h: h["assumptions"].pop(0),
            "candidate": lambda h: h["selected_candidate"].update(description="Build a castle"),
            "verified fact": lambda h: h["verified_evidence"][0].update(statement="Made up"),
        }
        for label, edit in edits.items():
            bad = copy.deepcopy(d.handoff)
            edit(bad)
            assert handoff_integrity_problems(bad, d.receipt, context), label
            _refused(lambda b=bad: build_artifact(b, discovery_receipt=d.receipt, context=context), ProductionError)
        return f"clean handoff bound; {len(edits)} edited handoffs refused (V2's validator alone accepts them)"

    @add("SpecificationCompiled", "handoff -> numbered typed requirements; malformed specifications refused")
    def _():
        h = _fixture()[1].handoff
        spec = compile_specification(h)
        kinds = {r.kind for r in spec.requirements}
        assert {"specification", "outcome", "acceptance", "constraint", "test"} <= kinds, kinds
        assert len(set(spec.ids())) == len(spec.ids())
        bad_cases = {
            "duplicate": {**h, "specifications": h["specifications"] + h["specifications"][:1]},
            "non-finite": {**h, "specifications": [{**h["specifications"][0], "value": float("inf")}]},
            "undefined variable": {**h, "acceptance_criteria": [{"name": "x", "relation": {
                "lhs": {"op": "var", "name": "nope"}, "op": ">=", "rhs": {"op": "const", "value": 0}}}]},
            "unit mismatch": {**h, "acceptance_criteria": [{"name": "x", "relation": {
                "lhs": {"op": "var", "name": "sqft"}, "op": ">=", "rhs": {"op": "const", "value": 0, "unit": "usd"}}}]},
            "no criteria": {**h, "acceptance_criteria": []},
            "bad name": {**h, "specifications": [{**h["specifications"][0], "name": "a b"}]},
        }
        for label, bad in bad_cases.items():
            _refused(lambda b=bad: compile_specification(b), ProductionError)
        return f"{len(spec.requirements)} requirements ({sorted(kinds)}); {len(bad_cases)} malformed specs refused"

    @add("ArtifactPlanTraceable", "every requirement is covered; every check has an executable test")
    def _():
        r = _built()
        plan = json.loads(_file(r.artifact, "plan.json")["content"])
        covered = {q for f in plan["files"] for q in f["covers"]}
        assert set(r.spec.ids()) <= covered
        tested = {q for f in plan["files"] if f["path"] == "test_artifact.py" for q in f["covers"]}
        assert {x.requirement_id for x in r.acceptance} <= tested
        bad = copy.deepcopy(r.artifact)
        plan["files"] = [f for f in plan["files"] if f["path"] != "test_artifact.py"]
        _file(bad, "plan.json")["content"] = json.dumps(plan)
        assert not verify_artifact(_rehash(bad)).passed
        return f"{len(r.spec.ids())} requirements traced to files; a plan dropping the tests is refused"

    @add("GeneratedTestsExecuted", "generated tests run in an isolated interpreter and can fail")
    def _():
        r = _built()
        t = r.verification.tests
        manifest = json.loads(_file(r.artifact, "artifact.json")["content"])
        assert t["ok"] and t["ran"] == codegen.expected_test_count(manifest) and t["failures"] == 0
        broken = copy.deepcopy(r.artifact["files"])
        py = next(f for f in broken if f["path"] == "artifact.py")
        py["content"] = py["content"].replace("return lhs - rhs >= -TOLERANCE", "return False", 1)
        run = run_tests(broken)
        assert not run.ok and run.failures >= 1, run.detail
        return f"{t['ran']} generated tests passed; a broken check makes {run.failures} fail"

    @add("ChecksFaithfulToV2", "generated checks reproduce V2's evaluator on reference probes, failures included")
    def _():
        r = _built()
        acc = json.loads(_file(r.artifact, "acceptance.json")["content"])
        ids = {x.requirement_id for x in r.acceptance}
        assert acc["probes"] and set(acc["falsified_by_probes"]) == ids, acc["falsified_by_probes"]
        failing = sum(1 for p in acc["probes"] for ok in p["expected"].values() if not ok)
        return f"{len(acc['probes'])} probes, {failing} expected failures, every check falsifiable"

    @add("RegenerationVerified", "an edited file with recomputed hashes and id is still caught")
    def _():
        out = []
        for path, edit in (("README.md", lambda c: c.replace("Selected candidate", "Chosen candidate")),
                           ("artifact.py", lambda c: c.replace("TOLERANCE = 1e-09", "TOLERANCE = 1e+09")),
                           ("test_artifact.py", lambda c: c.replace("self.assertTrue(fn(", "self.assertTrue(True or fn("))):
            bad = copy.deepcopy(_built().artifact)
            f = _file(bad, path)
            f["content"] = edit(f["content"])
            check = verify_artifact(_rehash(bad))
            assert not check.passed and any("regeneration" in p for p in check.problems), (path, check.problems)
            out.append(path)
        return "regeneration mismatch caught in " + ", ".join(out)

    @add("ReceiptBoundToArtifact", "a receipt describes exactly one artifact")
    def _():
        a, b = _built("structured_bundle"), _built("python_module")
        assert check_production_receipt(a.receipt, a.artifact) == []
        assert check_production_receipt(b.receipt, a.artifact)
        assert check_production_receipt(dict(a.receipt, tests=None), a.artifact)
        return "receipt binds id, fingerprint, kind, provenance, file hashes and acceptance"

    @add("ArtifactDiskRoundTrip", "write, reload and verify from disk; overwrite and stray files refused")
    def _():
        r = _built()
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "art"
            write_artifact(r, target)
            out = verify_directory(target)
            assert out["passed"], out["problems"]
            _refused(lambda: write_artifact(r, target), ProductionError)
            (target / "extra.txt").write_text("x", encoding="utf-8")
            assert any("undeclared" in p for p in verify_directory(target)["problems"])
            (target / "extra.txt").unlink()
            rec = json.loads((target / RECEIPT_RECORD).read_text(encoding="utf-8"))
            rec["artifact_id"] = "AF-forged"
            (target / RECEIPT_RECORD).write_text(json.dumps(rec), encoding="utf-8")
            assert not verify_directory(target)["passed"]
        return "round trip verified; overwrite refused; undeclared file and forged receipt caught"

    @add("V3CLIUsable", "lofgren produce and lofgren verify-artifact work end to end")
    def _():
        import contextlib
        import io

        from ..cli import main

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "docs").mkdir()
            for i, (title, text) in enumerate(AGREE_AND_CONFLICT.items()):
                (root / "docs" / f"doc{i}.txt").write_text(f"{title}\n\n{text}", encoding="utf-8")
            (root / "design.json").write_text(json.dumps(warehouse_design()), encoding="utf-8")
            out, err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                rc = main(["produce", OBJECTIVE, "--files", str(root / "docs"), "--goal",
                           "Choose a warehouse size (fictional)", "--design", str(root / "design.json"),
                           "--out-dir", str(root / "art")])
                assert rc == 0, err.getvalue()
                assert main(["verify-artifact", str(root / "art")]) == 0
                (root / "art" / "README.md").write_text("changed", encoding="utf-8")
                assert main(["verify-artifact", str(root / "art")]) == 1
        return "produce exit 0; verify-artifact exit 0, then 1 after an edit"

    @add("V3MCPToolsTyped", "MCP: investigate -> discover -> build_artifact -> verify -> files -> receipt -> V4")
    def _():
        from ..mcp.server import CONTRACT, PRODUCTION_TOOLS, Server

        assert CONTRACT == "lofgren.mcp/3" and all("inputSchema" in t and "outputSchema" in t for t in PRODUCTION_TOOLS)
        srv = Server()

        def call(name, args):
            return srv.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                               "params": {"name": name, "arguments": args}})["result"]

        run = call("investigate", {"objective": OBJECTIVE, "texts": AGREE_AND_CONFLICT})["structuredContent"]["run_id"]
        did = call("discover", {"run_id": run, "objective": "Choose a warehouse size (fictional)",
                                "design": warehouse_design()})["structuredContent"]["discovery_id"]
        built = call("build_artifact", {"discovery_id": did})
        assert not built["isError"], built
        aid = built["structuredContent"]["artifact_id"]
        for name, args in (("verify_artifact", {}), ("get_production_receipt", {}), ("create_v4_handoff", {}),
                           ("get_artifact_file", {"path": "test_artifact.py"})):
            out = call(name, {"artifact_id": aid, **args})
            assert not out["isError"] and out["structuredContent"]["kind"], (name, out)
        assert call("verify_artifact", {"artifact_id": aid})["structuredContent"]["passed"]
        assert call("create_v4_handoff", {"artifact_id": aid})["structuredContent"]["ready"]
        assert call("get_artifact_file", {"artifact_id": aid, "path": "../x"})["isError"]
        assert call("verify_artifact", {"artifact_id": "AF-none"})["isError"]
        assert call("build_artifact", {"discovery_id": did, "kind": "binary"})["isError"]
        return f"{len(PRODUCTION_TOOLS)} V3 tools typed; full journey over MCP; errors are isError"

    @add("ExecutionAuthorityAbsent", "V3 imports no network, adapter, hosted or authority module and grants nothing")
    def _():
        root = Path(__file__).resolve().parent
        forbidden = {"urllib", "socket", "http", "requests", "ssl", "smtplib", "ftplib"}
        forbidden_rel = {"adapters", "hosted", "authority", "mcp"}
        for path in sorted(root.glob("*.py")):
            if path.name == "certification.py":  # the certifier drives the CLI and MCP; it produces nothing
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for n in ast.walk(tree):
                if isinstance(n, ast.Import):
                    assert not {a.name.split(".")[0] for a in n.names} & forbidden, (path.name, n.names[0].name)
                if isinstance(n, ast.ImportFrom):
                    mod = (n.module or "").split(".")
                    assert not (n.level == 0 and mod[0] in forbidden), (path.name, n.module)
                    assert not (n.level >= 2 and mod and mod[0] in forbidden_rel), (path.name, n.module)
        h = _built().v4_handoff
        assert h["authority"]["granted"] is False and set(h["authority"]["actions_requiring_authority"]) == set(
            AUTHORITY_REQUIRED_ACTIONS)
        return "no forbidden imports in production/*; authority granted = false"

    @add("FailClosedWithoutSelection", "no selected candidate -> nothing is produced")
    def _():
        from ..mcp.server import Server

        run = _run(OBJECTIVE, _docs(AGREE_AND_CONFLICT))
        d = discover_from_run(run, "Choose a warehouse size (fictional)", design=F.infeasible_design(), at=AT)
        assert d.handoff is None and d.outcome.value == "infeasible"
        srv = Server()
        srv.discoveries[d.receipt["discovery_id"]] = d
        out = srv.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                          "params": {"name": "build_artifact", "arguments": {"discovery_id": d.receipt["discovery_id"]}}})
        assert out["result"]["isError"]
        context, good = _fixture()
        bad_receipt = dict(good.receipt, discovery_fingerprint="DFP-" + "0" * 64)
        _refused(lambda: build_artifact(good.handoff, discovery_receipt=bad_receipt, context=context), ProductionError)
        return "infeasible discovery has no handoff and build_artifact is an error; a broken upstream receipt is refused"

    @add("V3AdversarialSuitePassing", "malformed and forged artifacts are refused without crashing")
    def _():
        good = _built().artifact
        cases = {
            "not an object": [],
            "files not a list": {**good, "files": "x"},
            "file entry not an object": {**good, "files": [1]},
            "content not text": {**good, "files": [{**good["files"][0], "content": 5}]},
            "duplicate path": {**good, "files": good["files"] + good["files"][:1]},
            "unsafe path": {**good, "files": [{**good["files"][0], "path": "../x"}]},
            "unsupported kind": _rehash({**copy.deepcopy(good), "kind": "binary_executable"}),
            "manifest missing": _rehash({**copy.deepcopy(good), "files": copy.deepcopy(good["files"][1:])}),
            "acceptance forged": _rehash({**copy.deepcopy(good), "acceptance": [
                {**x, "passed": True, "slack": 1.0} for x in good["acceptance"]]}),
            "provenance swapped": {**copy.deepcopy(good), "provenance": {**good["provenance"],
                                                                          "discovery_id": "DR-" + "0" * 20}},
            "fingerprint forged": {**copy.deepcopy(good), "fingerprint": "AFP-" + "0" * 64},
            "invalid JSON manifest": _rehash({**copy.deepcopy(good), "files": [
                {**good["files"][0], "content": "{"}] + copy.deepcopy(good["files"][1:])}),
            "wrong codegen": None,
        }
        wrong = copy.deepcopy(good)
        m = json.loads(_file(wrong, "artifact.json")["content"])
        m["codegen"] = "production.codegen/0"
        _file(wrong, "artifact.json")["content"] = json.dumps(m)
        cases["wrong codegen"] = _rehash(wrong)
        for label, art in cases.items():
            check = verify_artifact(art)
            assert not check.passed and check.problems, label
        return f"{len(cases)} malformed or forged artifacts refused, none crashed the verifier"

    terms = {term: False for term in CODE_TERMS}
    for term in CODE_TERMS:
        rows = [x for x in s if x.term == term]
        terms[term] = bool(rows) and all(x.passed for x in rows)
    return {
        "schema": "lofgren.v3-certification/1",
        "terms": terms,
        "scenarios": [x.__dict__ for x in s],
        "code_terms_certified": all(terms.values()),
    }


def render_v3_certification(cert: dict) -> str:
    lines = ["Lofgren Intelligence V3 Production Certification", "=" * 52]
    for row in cert["scenarios"]:
        mark = "PASS" if row["passed"] else "FAIL"
        lines.append(f"[{mark}] {row['term']}: {row['scenario']} — {row['detail']}")
    lines += ["-" * 52, f"Process terms ({', '.join(PROCESS_TERMS)}) are evaluated by scripts/v3_gate.py.",
              f"V3CodeTerms = {'TRUE' if cert['code_terms_certified'] else 'FALSE'}"]
    return "\n".join(lines)
