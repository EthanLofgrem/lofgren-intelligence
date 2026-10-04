"""Offline certification for V3 Production Intelligence."""

from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import Callable

from ..certification import AGREE_AND_CONFLICT, OBJECTIVE, _docs, _run
from ..discovery.context import DiscoveryContext
from ..discovery.fixtures import AT, warehouse_design
from ..discovery.pipeline import run_discovery
from ..kernel.knowledge_map import export_knowledge_map
from .core import (
    ProductionError,
    build_artifact,
    verify_artifact,
    verify_production_receipt,
)

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


def _fixture():
    v1 = _run(OBJECTIVE, _docs(AGREE_AND_CONFLICT))
    context = DiscoveryContext(export_knowledge_map(v1), v1.receipt)
    discovery = run_discovery(
        context,
        "Choose a fictional warehouse size",
        design=warehouse_design(),
        at=AT,
    )
    if discovery.handoff is None:
        raise AssertionError(f"fixture produced no V3 handoff: {discovery.outcome.value}")
    return context, discovery


def _one(term: str, scenario: str, fn: Callable[[], str]) -> Scenario:
    try:
        return Scenario(term, scenario, True, fn())
    except Exception as exc:
        return Scenario(term, scenario, False, f"{type(exc).__name__}: {exc}")


def _build(kind: str = "structured_bundle"):
    context, discovery = _fixture()
    return build_artifact(
        discovery.handoff,
        discovery_receipt=discovery.receipt,
        context=context,
        kind=kind,
    )


def run_v3_certification() -> dict:
    scenarios: list[Scenario] = []

    scenarios.append(_one("V2HandoffValidated", "validated V2 handoff builds", lambda: (
        _build().v4_handoff["source_discovery_id"]
    )))

    def deterministic():
        context, discovery = _fixture()
        a = build_artifact(discovery.handoff, discovery_receipt=discovery.receipt, context=context)
        b = build_artifact(discovery.handoff, discovery_receipt=discovery.receipt, context=context)
        assert a.artifact["fingerprint"] == b.artifact["fingerprint"]
        assert a.artifact_id == b.artifact_id
        assert a.receipt["receipt_hash"] == b.receipt["receipt_hash"]
        return a.artifact_id
    scenarios.append(_one("ArtifactBuildDeterministic", "identical handoff -> identical artifact", deterministic))

    def provenance():
        r = _build()
        p = r.artifact["provenance"]
        assert p["discovery_id"] == r.v4_handoff["source_discovery_id"]
        assert p["discovery_fingerprint"]
        assert p["evidence_fingerprint"]
        return "discovery and evidence fingerprints preserved"
    scenarios.append(_one("ArtifactProvenanceComplete", "artifact keeps upstream provenance", provenance))

    def acceptance():
        r = _build()
        assert r.acceptance and all(x.passed for x in r.acceptance)
        return f"{len(r.acceptance)} machine-evaluable criteria passed"
    scenarios.append(_one("AcceptanceCriteriaEvaluated", "typed acceptance criteria execute", acceptance))

    def independent():
        r = _build()
        check = verify_artifact(r.artifact)
        assert check.passed, check.problems
        return "independent file/hash/syntax verification passed"
    scenarios.append(_one("ArtifactIndependentVerification", "independent verifier re-checks bundle", independent))

    def receipt():
        r = _build()
        assert verify_production_receipt(r.receipt)
        return r.receipt["receipt_hash"]
    scenarios.append(_one("ProductionReceiptReproducible", "production receipt verifies", receipt))

    def unsafe():
        from .core import ArtifactFile
        try:
            ArtifactFile.make("../escape.txt", "text/plain", "x")
        except ProductionError:
            return "path traversal refused"
        raise AssertionError("unsafe path accepted")
    scenarios.append(_one("UnsafePathsRejected", "path traversal refused", unsafe))

    def python_ok():
        r = _build("python_module")
        py = next(f for f in r.artifact["files"] if f["path"].endswith(".py"))
        compile(py["content"], py["path"], "exec")
        return "generated Python parses and compiles"
    scenarios.append(_one("PythonSyntaxValidated", "generated Python is syntactically valid", python_ok))

    def tamper():
        r = _build()
        bad = copy.deepcopy(r.artifact)
        bad["files"][0]["content"] += "\nTAMPER"
        check = verify_artifact(bad)
        assert not check.passed
        return "; ".join(check.problems)
    scenarios.append(_one("TamperDetected", "file mutation is detected", tamper))

    def v4():
        r = _build()
        h = r.v4_handoff
        assert h["schema"] == "lofgren.v4-handoff/1"
        assert h["artifact_verified"] is True
        assert h["acceptance_passed"] is True
        assert h["authority_required"] is True
        assert h["requested_actions"] == []
        return "verified artifact handed off with no authority granted"
    scenarios.append(_one("V4HandoffValidated", "V4 handoff preserves authority boundary", v4))

    def e2e():
        for kind in ("structured_bundle", "markdown", "python_module"):
            r = _build(kind)
            assert verify_artifact(r.artifact).passed
            assert verify_production_receipt(r.receipt)
            assert r.v4_handoff["artifact_id"] == r.artifact_id
        return "V2 handoff -> build -> test -> verify -> receipt -> V4 handoff for all supported kinds"
    scenarios.append(_one("V3E2ECertificationPassing", "complete bounded V3 journey", e2e))

    terms = {term: False for term in CODE_TERMS}
    for term in CODE_TERMS:
        rows = [x for x in scenarios if x.term == term]
        terms[term] = bool(rows) and all(x.passed for x in rows)

    return {
        "schema": "lofgren.v3-certification/1",
        "terms": terms,
        "scenarios": [x.__dict__ for x in scenarios],
        "code_terms_certified": all(terms.values()),
    }


def render_v3_certification(cert: dict) -> str:
    lines = ["Lofgren Intelligence V3 Production Certification", "=" * 52]
    for row in cert["scenarios"]:
        mark = "PASS" if row["passed"] else "FAIL"
        lines.append(f"[{mark}] {row['term']}: {row['scenario']} — {row['detail']}")
    lines += ["-" * 52, f"V3CodeTerms = {'TRUE' if cert['code_terms_certified'] else 'FALSE'}"]
    return "\n".join(lines)
