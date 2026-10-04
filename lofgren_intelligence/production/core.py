"""V3 Production Intelligence.

Consumes a validated V2 -> V3 handoff and produces a deterministic artifact
bundle with provenance, machine-evaluable acceptance results, independent file
verification, a tamper-evident production receipt, and a typed V4 handoff.

V3 builds and verifies artifacts. It never authorizes or executes external
actions; that boundary belongs to V4.
"""

from __future__ import annotations

import ast
import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any, Mapping

from ..discovery.context import DiscoveryContext
from ..discovery.expr import Relation, env_of
from ..discovery.handoff import check_handoff
from ..discovery.receipt import verify_discovery_receipt

PRODUCTION_RECEIPT_SCHEMA = "lofgren.production-receipt/1"
ARTIFACT_SCHEMA = "lofgren.artifact/1"
V4_HANDOFF_SCHEMA = "lofgren.v4-handoff/1"
SUPPORTED_ARTIFACT_KINDS = ("structured_bundle", "markdown", "python_module")
_SAFE_FILE = re.compile(r"^[A-Za-z0-9._/-]{1,240}$")


class ProductionError(ValueError):
    pass


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _file_hash(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _safe_path(path: str) -> str:
    if not isinstance(path, str) or not _SAFE_FILE.fullmatch(path):
        raise ProductionError(f"unsafe artifact path: {path!r}")
    p = PurePosixPath(path)
    if p.is_absolute() or ".." in p.parts or "." in p.parts or not p.parts:
        raise ProductionError(f"unsafe artifact path: {path!r}")
    return str(p)


@dataclass(frozen=True)
class ArtifactFile:
    path: str
    media_type: str
    content: str
    sha256: str

    @classmethod
    def make(cls, path: str, media_type: str, content: str) -> "ArtifactFile":
        path = _safe_path(path)
        if not isinstance(content, str):
            raise ProductionError("artifact content must be text")
        return cls(path, media_type, content, _file_hash(content))

    def as_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "media_type": self.media_type,
            "sha256": self.sha256,
            "content": self.content,
        }


@dataclass(frozen=True)
class AcceptanceResult:
    name: str
    passed: bool
    lhs: float
    rhs: float
    slack: float
    unit: str

    def as_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


@dataclass(frozen=True)
class ArtifactVerification:
    passed: bool
    problems: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return {"passed": self.passed, "problems": list(self.problems)}


@dataclass(frozen=True)
class ProductionResult:
    artifact: dict[str, Any]
    acceptance: tuple[AcceptanceResult, ...]
    verification: ArtifactVerification
    receipt: dict[str, Any]
    v4_handoff: dict[str, Any]

    @property
    def artifact_id(self) -> str:
        return str(self.artifact["artifact_id"])


def _spec_environment(handoff: Mapping[str, Any]) -> dict[str, Any]:
    values: dict[str, Any] = {}
    for spec in handoff.get("specifications", []):
        name = spec.get("name")
        if isinstance(name, str) and name:
            values[name] = (spec.get("value"), spec.get("unit", ""))
    for out in handoff.get("expected_outcomes", []):
        name = out.get("metric")
        if isinstance(name, str) and name:
            values[name] = (out.get("mean"), out.get("unit", ""))
    return values


def evaluate_acceptance(handoff: Mapping[str, Any]) -> tuple[AcceptanceResult, ...]:
    env = env_of(_spec_environment(handoff))
    results: list[AcceptanceResult] = []
    for i, criterion in enumerate(handoff.get("acceptance_criteria", [])):
        relation = Relation.from_json(criterion.get("relation"))
        check = relation.check(env)
        results.append(AcceptanceResult(
            str(criterion.get("name") or f"criterion-{i + 1}"),
            bool(check.satisfied),
            float(check.lhs),
            float(check.rhs),
            float(check.slack),
            str(check.unit),
        ))
    if not results:
        raise ProductionError("V3 handoff has no acceptance criteria")
    return tuple(results)


def _manifest_payload(handoff: Mapping[str, Any], kind: str) -> dict[str, Any]:
    return {
        "schema": ARTIFACT_SCHEMA,
        "kind": kind,
        "objective": handoff["objective"],
        "source_discovery_id": handoff["discovery_receipt_id"],
        "source_discovery_fingerprint": handoff["discovery_fingerprint"],
        "selected_candidate": handoff["selected_candidate"],
        "verified_evidence": handoff.get("verified_evidence", []),
        "hypotheses": handoff.get("hypotheses", []),
        "assumptions": handoff.get("assumptions", []),
        "constraints": handoff.get("constraints", []),
        "specifications": handoff.get("specifications", []),
        "expected_outcomes": handoff.get("expected_outcomes", []),
        "acceptance_criteria": handoff.get("acceptance_criteria", []),
        "test_requirements": handoff.get("test_requirements", []),
        "risks": handoff.get("risks", []),
        "dependencies": handoff.get("dependencies", []),
        "resource_requirements": handoff.get("resource_requirements", {}),
        "cost_estimates": handoff.get("cost_estimates", {}),
    }


def _markdown(payload: Mapping[str, Any]) -> str:
    candidate = payload["selected_candidate"]
    lines = [
        "# Lofgren Intelligence V3 Artifact",
        "",
        f"Objective: {payload['objective']}",
        "",
        f"Selected candidate: {candidate.get('description', candidate.get('id'))}",
        "",
        "## Specifications",
    ]
    for spec in payload.get("specifications", []):
        lines.append(f"- {spec.get('name')}: {spec.get('value')} {spec.get('unit', '')}".rstrip())
    lines += ["", "## Acceptance criteria"]
    for criterion in payload.get("acceptance_criteria", []):
        lines.append(f"- {criterion.get('name', 'criterion')}")
    lines += ["", "## Provenance", f"- Discovery: {payload['source_discovery_id']}",
              f"- Discovery fingerprint: {payload['source_discovery_fingerprint']}", ""]
    return "\n".join(lines)


def _python_module(payload: Mapping[str, Any]) -> str:
    literal = repr(json.loads(_canonical(payload)))
    return (
        '"""Generated by Lofgren Intelligence V3 Production Intelligence.\n'
        "This module is a deterministic, inspectable artifact representation.\n"
        '"""\n\n'
        f"ARTIFACT = {literal}\n\n"
        "def describe():\n"
        "    return ARTIFACT.copy()\n"
    )


def _files(payload: Mapping[str, Any], kind: str) -> tuple[ArtifactFile, ...]:
    manifest = json.dumps(payload, sort_keys=True, indent=2, ensure_ascii=False) + "\n"
    files = [ArtifactFile.make("artifact.json", "application/json", manifest)]
    if kind in {"structured_bundle", "markdown"}:
        files.append(ArtifactFile.make("README.md", "text/markdown", _markdown(payload)))
    if kind in {"structured_bundle", "python_module"}:
        files.append(ArtifactFile.make("artifact.py", "text/x-python", _python_module(payload)))
    return tuple(files)


def verify_artifact(artifact: Mapping[str, Any]) -> ArtifactVerification:
    problems: list[str] = []
    if artifact.get("schema") != ARTIFACT_SCHEMA:
        problems.append("artifact schema mismatch")
    files = artifact.get("files")
    if not isinstance(files, list) or not files:
        problems.append("artifact contains no files")
        files = []
    seen: set[str] = set()
    for i, item in enumerate(files):
        try:
            path = _safe_path(item["path"])
        except Exception as exc:
            problems.append(f"files[{i}] path invalid: {exc}")
            continue
        if path in seen:
            problems.append(f"duplicate file path {path}")
        seen.add(path)
        content = item.get("content")
        if not isinstance(content, str):
            problems.append(f"{path} content is not text")
            continue
        if item.get("sha256") != _file_hash(content):
            problems.append(f"{path} hash mismatch")
        if path.endswith(".json"):
            try:
                json.loads(content)
            except Exception as exc:
                problems.append(f"{path} is invalid JSON: {exc}")
        if path.endswith(".py"):
            try:
                ast.parse(content, filename=path)
            except SyntaxError as exc:
                problems.append(f"{path} is invalid Python: {exc.msg}")

    core = {
        "schema": artifact.get("schema"),
        "artifact_id": artifact.get("artifact_id"),
        "kind": artifact.get("kind"),
        "source_discovery_id": artifact.get("source_discovery_id"),
        "files": [{k: f.get(k) for k in ("path", "media_type", "sha256")} for f in files],
        "acceptance": artifact.get("acceptance"),
    }
    expected = "AF-" + _digest(core)[:20]
    if artifact.get("artifact_id") != expected:
        problems.append("artifact id does not match artifact contents")
    return ArtifactVerification(not problems, tuple(problems))


def _receipt(artifact: Mapping[str, Any], handoff: Mapping[str, Any], verification: ArtifactVerification) -> dict[str, Any]:
    body = {
        "schema": PRODUCTION_RECEIPT_SCHEMA,
        "artifact_id": artifact["artifact_id"],
        "artifact_fingerprint": artifact["fingerprint"],
        "discovery_id": handoff["discovery_receipt_id"],
        "discovery_fingerprint": handoff["discovery_fingerprint"],
        "file_hashes": {f["path"]: f["sha256"] for f in artifact["files"]},
        "acceptance": artifact["acceptance"],
        "verification_passed": verification.passed,
        "algorithms": {
            "producer": "production.bundle/1",
            "acceptance": "discovery.relation/1",
            "verifier": "production.independent/1",
        },
    }
    return {**body, "receipt_hash": "PR-" + _digest(body)}


def verify_production_receipt(receipt: Mapping[str, Any]) -> bool:
    if receipt.get("schema") != PRODUCTION_RECEIPT_SCHEMA:
        return False
    body = dict(receipt)
    supplied = body.pop("receipt_hash", None)
    return supplied == "PR-" + _digest(body)


def build_artifact(
    handoff: Mapping[str, Any],
    *,
    discovery_receipt: Mapping[str, Any],
    context: DiscoveryContext,
    kind: str = "structured_bundle",
) -> ProductionResult:
    """Build, test and independently verify one bounded V3 artifact."""
    if kind not in SUPPORTED_ARTIFACT_KINDS:
        raise ProductionError(f"unsupported artifact kind {kind!r}; expected one of {SUPPORTED_ARTIFACT_KINDS}")
    if not verify_discovery_receipt(discovery_receipt):
        raise ProductionError("discovery receipt is not intact")
    check_handoff(handoff, discovery_receipt, context)

    acceptance = evaluate_acceptance(handoff)
    if not all(x.passed for x in acceptance):
        failed = ", ".join(x.name for x in acceptance if not x.passed)
        raise ProductionError(f"acceptance preconditions failed: {failed}")

    payload = _manifest_payload(handoff, kind)
    files = _files(payload, kind)
    file_meta = [{"path": f.path, "media_type": f.media_type, "sha256": f.sha256} for f in files]
    core = {
        "schema": ARTIFACT_SCHEMA,
        "artifact_id": None,
        "kind": kind,
        "source_discovery_id": handoff["discovery_receipt_id"],
        "files": file_meta,
        "acceptance": [x.as_dict() for x in acceptance],
    }
    # ID commits to all stable artifact metadata. Calculate with the field omitted,
    # then verify with the same canonical rule below.
    id_core = dict(core)
    id_core["artifact_id"] = "PENDING"
    artifact_id = "AF-" + _digest(id_core)[:20]
    core["artifact_id"] = artifact_id
    # Recompute the public id rule over the final structure with its id normalized.
    normalized = dict(core)
    normalized["artifact_id"] = "PENDING"
    artifact_id = "AF-" + _digest(normalized)[:20]
    core["artifact_id"] = artifact_id

    artifact = {
        **core,
        "fingerprint": "AFP-" + _digest({
            "kind": kind,
            "source_discovery_id": handoff["discovery_receipt_id"],
            "payload": payload,
            "files": file_meta,
            "acceptance": core["acceptance"],
        }),
        "files": [f.as_dict() for f in files],
        "provenance": {
            "discovery_id": handoff["discovery_receipt_id"],
            "discovery_fingerprint": handoff["discovery_fingerprint"],
            "evidence_fingerprint": handoff["evidence_fingerprint"],
        },
    }

    # verify_artifact normalizes artifact_id when checking.
    verification = _verify_with_normalized_id(artifact)
    if not verification.passed:
        raise ProductionError("artifact verification failed: " + "; ".join(verification.problems))
    receipt = _receipt(artifact, handoff, verification)
    if not verify_production_receipt(receipt):
        raise ProductionError("production receipt failed self-verification")

    v4 = {
        "schema": V4_HANDOFF_SCHEMA,
        "artifact_id": artifact_id,
        "artifact_fingerprint": artifact["fingerprint"],
        "production_receipt_hash": receipt["receipt_hash"],
        "objective": handoff["objective"],
        "requested_actions": [],
        "authority_required": True,
        "artifact_verified": True,
        "acceptance_passed": True,
        "source_discovery_id": handoff["discovery_receipt_id"],
        "risks": handoff.get("risks", []),
        "dependencies": handoff.get("dependencies", []),
    }
    return ProductionResult(artifact, acceptance, verification, receipt, v4)


def _verify_with_normalized_id(artifact: Mapping[str, Any]) -> ArtifactVerification:
    problems: list[str] = []
    if artifact.get("schema") != ARTIFACT_SCHEMA:
        problems.append("artifact schema mismatch")
    files = artifact.get("files")
    if not isinstance(files, list) or not files:
        problems.append("artifact contains no files")
        files = []
    seen: set[str] = set()
    for i, item in enumerate(files):
        try:
            path = _safe_path(item["path"])
        except Exception as exc:
            problems.append(f"files[{i}] path invalid: {exc}")
            continue
        if path in seen:
            problems.append(f"duplicate file path {path}")
        seen.add(path)
        content = item.get("content")
        if not isinstance(content, str):
            problems.append(f"{path} content is not text")
            continue
        if item.get("sha256") != _file_hash(content):
            problems.append(f"{path} hash mismatch")
        if path.endswith(".json"):
            try:
                json.loads(content)
            except Exception as exc:
                problems.append(f"{path} is invalid JSON: {exc}")
        if path.endswith(".py"):
            try:
                ast.parse(content, filename=path)
            except SyntaxError as exc:
                problems.append(f"{path} is invalid Python: {exc.msg}")

    normalized = {
        "schema": artifact.get("schema"),
        "artifact_id": "PENDING",
        "kind": artifact.get("kind"),
        "source_discovery_id": artifact.get("source_discovery_id"),
        "files": [{k: f.get(k) for k in ("path", "media_type", "sha256")} for f in files],
        "acceptance": artifact.get("acceptance"),
    }
    expected = "AF-" + _digest(normalized)[:20]
    if artifact.get("artifact_id") != expected:
        problems.append("artifact id does not match artifact contents")
    return ArtifactVerification(not problems, tuple(problems))
