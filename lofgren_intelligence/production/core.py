"""V3 Production Intelligence.

Consumes a validated V2 -> V3 handoff and produces a deterministic artifact bundle:

    handoff -> specification compiler (numbered requirements) -> artifact plan (requirement -> file)
            -> code and document generation -> executable acceptance tests
            -> independent verifier (structure, regeneration, fingerprint, provenance, traceability,
               re-evaluated checks, generated tests run in an isolated interpreter)
            -> tamper-evident production receipt -> typed V4 handoff (no authority granted)

V3 builds and verifies artifacts. It never deploys, publishes, purchases, sends, changes infrastructure or controls
a device; that boundary belongs to V4.
"""

from __future__ import annotations

import ast
import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Any, Mapping

from ..discovery.context import DiscoveryContext
from ..discovery.errors import DiscoveryError
from ..discovery.expr import Relation, env_of
from ..discovery.handoff import HandoffInvalid, check_handoff
from ..discovery.receipt import verify_discovery_receipt
from . import codegen
from .errors import ProductionError
from .sandbox import RUNNER, run_tests
from .spec import ProductionSpec, compile_specification
from .upstream import handoff_integrity_problems

PRODUCTION_RECEIPT_SCHEMA = "lofgren.production-receipt/1"
ARTIFACT_SCHEMA = "lofgren.artifact/1"
V4_HANDOFF_SCHEMA = "lofgren.v4-handoff/1"
SUPPORTED_ARTIFACT_KINDS = ("structured_bundle", "markdown", "python_module")
# Action classes V3 can never perform; V4 needs a capability grant and authorization for each.
AUTHORITY_REQUIRED_ACTIONS = ("deploy", "publish", "purchase", "send_message", "modify_infrastructure",
                              "control_device", "write_external_repository")
_SAFE_FILE = re.compile(r"^[A-Za-z0-9._/-]{1,240}$")


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
    if p.is_absolute() or ".." in p.parts or "." in p.parts or not p.parts or str(p) != path:
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
        return {"path": self.path, "media_type": self.media_type, "sha256": self.sha256, "content": self.content}


@dataclass(frozen=True)
class AcceptanceResult:
    name: str
    passed: bool
    lhs: float
    rhs: float
    slack: float
    unit: str
    requirement_id: str = ""
    kind: str = "acceptance"

    def as_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


@dataclass(frozen=True)
class ArtifactVerification:
    passed: bool
    problems: tuple[str, ...]
    tests: dict[str, Any] | None = None

    def as_dict(self) -> dict[str, Any]:
        return {"passed": self.passed, "problems": list(self.problems), "tests": self.tests}


@dataclass(frozen=True)
class ProductionResult:
    artifact: dict[str, Any]
    acceptance: tuple[AcceptanceResult, ...]
    verification: ArtifactVerification
    receipt: dict[str, Any]
    v4_handoff: dict[str, Any]
    spec: ProductionSpec | None = field(default=None, compare=False)

    @property
    def artifact_id(self) -> str:
        return str(self.artifact["artifact_id"])


# -- acceptance -----------------------------------------------------------------

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
    """The handoff's acceptance criteria, evaluated over its specifications and simulated outcome means."""
    env = env_of(_spec_environment(handoff))
    results: list[AcceptanceResult] = []
    for i, criterion in enumerate(handoff.get("acceptance_criteria", [])):
        check = Relation.from_json(criterion.get("relation")).check(env)
        results.append(AcceptanceResult(str(criterion.get("name") or f"criterion-{i + 1}"), bool(check.satisfied),
                                        float(check.lhs), float(check.rhs), float(check.slack), str(check.unit)))
    if not results:
        raise ProductionError("V3 handoff has no acceptance criteria")
    return tuple(results)


def evaluate_checks(manifest: Mapping[str, Any]) -> tuple[AcceptanceResult, ...]:
    """Every executable check in a manifest (acceptance criteria and evaluable constraints), via V2's evaluator."""
    env = env_of({k: (v["value"], v["unit"]) for k, v in manifest["values"].items()})
    out = []
    for c in manifest["checks"]:
        r = Relation.from_json(c["relation"]).check(env)
        out.append(AcceptanceResult(c["name"], bool(r.satisfied), float(r.lhs), float(r.rhs), float(r.slack),
                                    str(r.unit), c["requirement_id"], c["kind"]))
    return tuple(out)


# -- generation -----------------------------------------------------------------

def _manifest_payload(handoff: Mapping[str, Any], kind: str, spec: ProductionSpec) -> dict[str, Any]:
    payload = {
        "schema": ARTIFACT_SCHEMA,
        "kind": kind,
        "codegen": codegen.CODEGEN_VERSION,
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
        "requirements": spec.as_list(),
        "values": {k: {"value": v, "unit": u} for k, (v, u) in spec.values.items()},
        "checks": [{"requirement_id": rid, "kind": k, "name": n, "relation": rel} for rid, k, n, rel in spec.checks],
    }
    return json.loads(_canonical(payload))


def _file_meta(files: list[Mapping[str, Any]]) -> list[dict[str, Any]]:
    return [{k: f.get(k) for k in ("path", "media_type", "sha256")} for f in files]


def _artifact_id(schema: Any, kind: Any, source: Any, file_meta: list, acceptance: Any) -> str:
    return "AF-" + _digest({"schema": schema, "artifact_id": "PENDING", "kind": kind, "source_discovery_id": source,
                            "files": file_meta, "acceptance": acceptance})[:20]


def _fingerprint(kind: Any, source: Any, payload: Any, file_meta: list, acceptance: Any) -> str:
    return "AFP-" + _digest({"kind": kind, "source_discovery_id": source, "payload": payload, "files": file_meta,
                             "acceptance": acceptance})


# -- independent verification ------------------------------------------------------

def verify_artifact(artifact: Mapping[str, Any], *, run_generated_tests: bool = True) -> ArtifactVerification:
    """Independently re-check an artifact.

    Structure (paths, hashes, JSON and Python syntax, identity); then, from the manifest alone: the recompiled
    specification, byte-for-byte regeneration of every file, the fingerprint, provenance, plan traceability and the
    re-evaluated checks; finally the generated tests, run in a separate isolated interpreter.
    """
    problems: list[str] = []
    if not isinstance(artifact, Mapping):
        return ArtifactVerification(False, ("artifact is not an object",))
    if artifact.get("schema") != ARTIFACT_SCHEMA:
        problems.append("artifact schema mismatch")
    files = artifact.get("files")
    if not isinstance(files, list) or not files:
        problems.append("artifact contains no files")
        files = []
    seen: set[str] = set()
    for i, item in enumerate(files):
        if not isinstance(item, Mapping):
            problems.append(f"files[{i}] is not an object")
            continue
        try:
            path = _safe_path(item.get("path"))
        except ProductionError as exc:
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
            except ValueError as exc:
                problems.append(f"{path} is invalid JSON: {exc}")
        if path.endswith(".py"):
            try:
                ast.parse(content, filename=path)
            except SyntaxError as exc:
                problems.append(f"{path} is invalid Python: {exc.msg}")
    files = [f for f in files if isinstance(f, Mapping)]
    meta = _file_meta(files)
    try:
        expected_id = _artifact_id(artifact.get("schema"), artifact.get("kind"), artifact.get("source_discovery_id"),
                                   meta, artifact.get("acceptance"))
    except (TypeError, ValueError):
        expected_id = None
    if artifact.get("artifact_id") != expected_id:
        problems.append("artifact id does not match artifact contents")
    if problems:
        return ArtifactVerification(False, tuple(problems))

    tests = None
    try:
        problems += _verify_semantics(artifact, files, meta)
        if not problems and artifact["kind"] in codegen.EXECUTABLE_KINDS and run_generated_tests:
            manifest = json.loads(next(f["content"] for f in files if f["path"] == "artifact.json"))
            run = run_tests(files)
            tests = run.as_dict()
            expected = codegen.expected_test_count(manifest)
            if not run.ok:
                problems.append(f"generated tests failed: {run.detail}")
            elif run.ran != expected:
                problems.append(f"generated tests ran {run.ran} tests, expected {expected}")
    except (DiscoveryError, ProductionError, KeyError, TypeError, ValueError, AttributeError) as exc:
        problems.append(f"artifact cannot be re-derived: {type(exc).__name__}: {exc}")
    return ArtifactVerification(not problems, tuple(problems), tests)


def _verify_semantics(artifact: Mapping[str, Any], files: list[Mapping[str, Any]], meta: list) -> list[str]:
    problems: list[str] = []
    kind = artifact.get("kind")
    if kind not in SUPPORTED_ARTIFACT_KINDS:
        return [f"unsupported artifact kind {kind!r}"]
    by_path = {f["path"]: f for f in files}
    if "artifact.json" not in by_path:
        return ["artifact.json (the manifest) is missing"]
    manifest = json.loads(by_path["artifact.json"]["content"])
    if not isinstance(manifest, dict):
        return ["the manifest is not an object"]
    if manifest.get("schema") != ARTIFACT_SCHEMA or manifest.get("kind") != kind:
        problems.append("manifest schema or kind does not match the artifact")
    if manifest.get("codegen") != codegen.CODEGEN_VERSION:
        return problems + [f"manifest was generated by {manifest.get('codegen')!r}, not {codegen.CODEGEN_VERSION}"]

    spec = compile_specification(manifest)
    if [r.as_dict() for r in spec.requirements] != manifest["requirements"]:
        problems.append("manifest requirements do not match its recompiled specification")
    if {k: {"value": v, "unit": u} for k, (v, u) in spec.values.items()} != manifest["values"]:
        problems.append("manifest values do not match its specifications")
    if [[rid, k, n, rel] for rid, k, n, rel in spec.checks] != [
            [c["requirement_id"], c["kind"], c["name"], c["relation"]] for c in manifest["checks"]]:
        problems.append("manifest checks do not match its recompiled specification")
    if problems:
        return problems

    expected = codegen.generate(manifest, kind)
    if [p for p, _, _ in expected] != [f["path"] for f in files]:
        problems.append(f"files {[f['path'] for f in files]} != regenerated {[p for p, _, _ in expected]}")
    for path, media, content in expected:
        f = by_path.get(path)
        if f is not None and (f.get("content") != content or f.get("media_type") != media):
            problems.append(f"{path} differs from its regeneration from the manifest")

    if artifact.get("source_discovery_id") != manifest.get("source_discovery_id"):
        problems.append("artifact source discovery differs from the manifest")
    prov = artifact.get("provenance") or {}
    if (prov.get("discovery_id"), prov.get("discovery_fingerprint")) != (
            manifest.get("source_discovery_id"), manifest.get("source_discovery_fingerprint")):
        problems.append("provenance does not match the manifest")
    if not prov.get("evidence_fingerprint"):
        problems.append("provenance has no evidence fingerprint")
    if artifact.get("fingerprint") != _fingerprint(kind, artifact.get("source_discovery_id"), manifest, meta,
                                                   artifact.get("acceptance")):
        problems.append("artifact fingerprint does not match its contents")

    results = [r.as_dict() for r in evaluate_checks(manifest)]
    if results != artifact.get("acceptance"):
        problems.append("recorded acceptance results differ from re-evaluated checks")
    failed = [r["requirement_id"] for r in results if not r["passed"]]
    if failed:
        problems.append(f"checks do not hold: {failed}")

    plan = json.loads(by_path["plan.json"]["content"]) if "plan.json" in by_path else {"files": []}
    covered = {req for f in plan["files"] for req in f["covers"]}
    missing = sorted(set(spec.ids()) - covered)
    if missing:
        problems.append(f"requirements not covered by any file: {missing}")
    runner_file = "test_artifact.py" if kind in codegen.EXECUTABLE_KINDS else "acceptance.json"
    executable = {req for f in plan["files"] if f["path"] == runner_file for req in f["covers"]}
    untested = sorted({c["requirement_id"] for c in manifest["checks"]} - executable)
    if untested:
        problems.append(f"checks without an executable test in {runner_file}: {untested}")
    return problems


# -- receipt and V4 handoff ----------------------------------------------------------

def _receipt(artifact: Mapping[str, Any], handoff: Mapping[str, Any], verification: ArtifactVerification,
             spec: ProductionSpec) -> dict[str, Any]:
    body = {
        "schema": PRODUCTION_RECEIPT_SCHEMA,
        "artifact_id": artifact["artifact_id"],
        "artifact_fingerprint": artifact["fingerprint"],
        "kind": artifact["kind"],
        "discovery_id": handoff["discovery_receipt_id"],
        "discovery_fingerprint": handoff["discovery_fingerprint"],
        "evidence_fingerprint": handoff["evidence_fingerprint"],
        "file_hashes": {f["path"]: f["sha256"] for f in artifact["files"]},
        "requirements": spec.ids(),
        "acceptance": artifact["acceptance"],
        "tests": verification.tests,
        "verification_passed": verification.passed,
        "algorithms": {
            "specification": "production.spec/1",
            "producer": "production.bundle/2",
            "codegen": codegen.CODEGEN_VERSION,
            "acceptance": "discovery.relation/1",
            "verifier": "production.independent/2",
            "tests": RUNNER,
        },
    }
    return {**body, "receipt_hash": "PR-" + _digest(body)}


def verify_production_receipt(receipt: Mapping[str, Any]) -> bool:
    """True when the receipt is intact (its hash matches its contents)."""
    if not isinstance(receipt, Mapping) or receipt.get("schema") != PRODUCTION_RECEIPT_SCHEMA:
        return False
    body = dict(receipt)
    supplied = body.pop("receipt_hash", None)
    try:
        return supplied == "PR-" + _digest(body)
    except (TypeError, ValueError):
        return False


def check_production_receipt(receipt: Mapping[str, Any], artifact: Mapping[str, Any]) -> list[str]:
    """Problems binding a receipt to an artifact (empty when the receipt is intact and describes this artifact)."""
    if not verify_production_receipt(receipt):
        return ["production receipt is not intact"]
    problems = []
    prov = artifact.get("provenance") or {}
    files = artifact.get("files") or []
    expected = {
        "artifact_id": artifact.get("artifact_id"),
        "artifact_fingerprint": artifact.get("fingerprint"),
        "kind": artifact.get("kind"),
        "discovery_id": prov.get("discovery_id"),
        "discovery_fingerprint": prov.get("discovery_fingerprint"),
        "evidence_fingerprint": prov.get("evidence_fingerprint"),
        "file_hashes": {f.get("path"): f.get("sha256") for f in files if isinstance(f, Mapping)},
        "acceptance": artifact.get("acceptance"),
    }
    for key, value in expected.items():
        if receipt.get(key) != value:
            problems.append(f"receipt {key} does not match the artifact")
    if receipt.get("verification_passed") is not True:
        problems.append("receipt does not record a passed verification")
    return problems


def validate_v4_handoff(h: Mapping[str, Any], receipt: Mapping[str, Any],
                        artifact: Mapping[str, Any] | None = None) -> list[str]:
    """Problems with a V3 -> V4 handoff (empty when valid). It must grant nothing and match its receipt."""
    if not isinstance(h, Mapping) or h.get("schema") != V4_HANDOFF_SCHEMA:
        return [f"not a {V4_HANDOFF_SCHEMA} handoff"]
    problems = []
    if h.get("authority_required") is not True:
        problems.append("authority_required must be true")
    if h.get("requested_actions") != []:
        problems.append("V3 may not request actions; V4 plans them under authorization")
    authority = h.get("authority") or {}
    if authority.get("granted") is not False:
        problems.append("V3 cannot grant authority")
    if sorted(authority.get("actions_requiring_authority") or []) != sorted(AUTHORITY_REQUIRED_ACTIONS):
        problems.append("the authority boundary is incomplete")
    if h.get("artifact_verified") is not True or h.get("acceptance_passed") is not True:
        problems.append("only a verified artifact whose checks pass can be handed to V4")
    if not verify_production_receipt(receipt):
        problems.append("production receipt is not intact")
    else:
        pairs = {"artifact_id": "artifact_id", "artifact_fingerprint": "artifact_fingerprint",
                 "production_receipt_hash": "receipt_hash", "source_discovery_id": "discovery_id",
                 "artifact_kind": "kind"}
        for hk, rk in pairs.items():
            if h.get(hk) != receipt.get(rk):
                problems.append(f"{hk} does not match the production receipt")
        files = h.get("files") if isinstance(h.get("files"), list) else []
        if {f.get("path"): f.get("sha256") for f in files if isinstance(f, Mapping)} != receipt.get("file_hashes"):
            problems.append("file hashes do not match the production receipt")
        if h.get("tests") != receipt.get("tests"):
            problems.append("test results do not match the production receipt")
    if artifact is not None:
        problems += check_production_receipt(receipt, artifact)
    return problems


# -- the pipeline --------------------------------------------------------------------

def build_artifact(handoff: Mapping[str, Any], *, discovery_receipt: Mapping[str, Any], context: DiscoveryContext,
                   kind: str = "structured_bundle") -> ProductionResult:
    """Compile, plan, generate, test and independently verify one V3 artifact from a validated V2 handoff."""
    if kind not in SUPPORTED_ARTIFACT_KINDS:
        raise ProductionError(f"unsupported artifact kind {kind!r}; expected one of {SUPPORTED_ARTIFACT_KINDS}")
    if not verify_discovery_receipt(discovery_receipt):
        raise ProductionError("discovery receipt is not intact")
    try:
        check_handoff(handoff, discovery_receipt, context)
    except HandoffInvalid as exc:
        raise ProductionError(str(exc)) from exc
    upstream = handoff_integrity_problems(handoff, discovery_receipt, context)
    if upstream:
        raise ProductionError("V3 handoff does not match its discovery: " + "; ".join(upstream[:5]))

    spec = compile_specification(handoff)
    acceptance = evaluate_acceptance(handoff)
    if not all(x.passed for x in acceptance):
        raise ProductionError("acceptance preconditions failed: " + ", ".join(x.name for x in acceptance
                                                                              if not x.passed))
    manifest = _manifest_payload(handoff, kind, spec)
    checks = evaluate_checks(manifest)
    if not all(x.passed for x in checks):
        raise ProductionError("checks failed: " + ", ".join(f"{x.requirement_id} {x.name}" for x in checks
                                                             if not x.passed))
    files = [ArtifactFile.make(p, media, content) for p, media, content in codegen.generate(manifest, kind)]
    meta = _file_meta([f.as_dict() for f in files])
    results = [x.as_dict() for x in checks]
    source = handoff["discovery_receipt_id"]
    artifact = {
        "schema": ARTIFACT_SCHEMA,
        "artifact_id": _artifact_id(ARTIFACT_SCHEMA, kind, source, meta, results),
        "kind": kind,
        "source_discovery_id": source,
        "files": [f.as_dict() for f in files],
        "acceptance": results,
        "fingerprint": _fingerprint(kind, source, manifest, meta, results),
        "provenance": {
            "discovery_id": source,
            "discovery_fingerprint": handoff["discovery_fingerprint"],
            "evidence_fingerprint": handoff["evidence_fingerprint"],
        },
    }

    verification = verify_artifact(artifact)
    if not verification.passed:
        raise ProductionError("artifact verification failed: " + "; ".join(verification.problems))
    receipt = _receipt(artifact, handoff, verification, spec)
    if check_production_receipt(receipt, artifact):
        raise ProductionError("production receipt failed self-verification")

    v4 = {
        "schema": V4_HANDOFF_SCHEMA,
        "artifact_id": artifact["artifact_id"],
        "artifact_fingerprint": artifact["fingerprint"],
        "artifact_kind": kind,
        "production_receipt_hash": receipt["receipt_hash"],
        "objective": handoff["objective"],
        "files": [{"path": f.path, "sha256": f.sha256} for f in files],
        "tests": verification.tests,
        "requested_actions": [],
        "authority_required": True,
        "authority": {"granted": False, "actions_requiring_authority": list(AUTHORITY_REQUIRED_ACTIONS)},
        "artifact_verified": True,
        "acceptance_passed": True,
        "source_discovery_id": source,
        "open_verification_work": [r.detail for r in spec.requirements if r.kind in ("test", "dependency")],
        "risks": handoff.get("risks", []),
        "dependencies": handoff.get("dependencies", []),
    }
    problems = validate_v4_handoff(v4, receipt, artifact)
    if problems:
        raise ProductionError("V4 handoff failed validation: " + "; ".join(problems))
    return ProductionResult(artifact, checks, verification, receipt, v4, spec)
