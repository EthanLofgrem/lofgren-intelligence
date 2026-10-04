"""Evidence-backed, fail-closed public MCP release gate.

A Boolean supplied by a caller is never sufficient. Every release term must be
backed by an artifact file whose bytes are hashed in the manifest and whose
payload binds the result to the exact release SHA.

Unknown, malformed, stale, duplicated, mismatched or unverifiable evidence is
false.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

SHA_RE = re.compile(r"^[0-9a-f]{40}$")

TERMS = (
    "CoreV1Certified",
    "V1BoundaryCertified",
    "V2Certified",
    "V2RemoteIntegrationCertified",
    "CurrentMCPProtocolCertified",
    "InstalledWheelMCPCertified",
    "OAuthPKCECertified",
    "DurableRunsCertified",
    "TenantIsolationCertified",
    "FoundingFreeExactly1000Certified",
    "User1001PaidRequiredCertified",
    "QuotaEnforcementCertified",
    "RateLimitCertified",
    "SSRFSafe",
    "InputLimitsCertified",
    "ActualCOGSMeteringCertified",
    "PaidPlanEconomicsCertified",
    "StripeSandboxJourneyCertified",
    "WebhookSecurityCertified",
    "DatabaseMigrationCertified",
    "RemoteClientInteropCertified",
    "HealthReadinessCertified",
    "ObservabilityCertified",
    "BackupRestoreCertified",
    "RollbackCertified",
    "SecretsBoundaryCertified",
    "ExactHeadCIPassing",
    "ExactSHAPinned",
    "DeployedSHAMatches",
)

ARTIFACT_SCHEMA = "lofgren.public-evidence/1"
MANIFEST_SCHEMA = "lofgren.public-release-evidence/1"


@dataclass(frozen=True)
class PublicMCPGate:
    values: dict[str, bool]
    evidence: dict[str, Any]
    reasons: dict[str, str]

    @property
    def passed_count(self) -> int:
        return sum(1 for v in self.values.values() if v)

    @property
    def total(self) -> int:
        return len(TERMS)

    @property
    def ready(self) -> bool:
        return self.passed_count == self.total

    def as_dict(self) -> dict[str, Any]:
        return {
            "terms": self.values,
            "reasons": self.reasons,
            "true": self.passed_count,
            "total": self.total,
            "PublicMCPReady": self.ready,
        }


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _load_artifact(entry: dict[str, Any], root: Path, expected_sha: str) -> tuple[bool, str, dict[str, Any] | None]:
    term = str(entry.get("term") or "")
    rel = entry.get("path")
    digest = str(entry.get("sha256") or "")
    if not rel or not isinstance(rel, str):
        return False, "artifact path missing", None
    path = (root / rel).resolve()
    try:
        path.relative_to(root.resolve())
    except ValueError:
        return False, "artifact path escapes manifest directory", None
    if not path.is_file():
        return False, f"artifact file missing: {rel}", None
    actual_digest = _sha256(path)
    if not re.fullmatch(r"[0-9a-f]{64}", digest) or actual_digest != digest:
        return False, f"artifact digest mismatch: {rel}", None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        return False, f"artifact JSON invalid: {exc}", None
    if not isinstance(payload, dict):
        return False, "artifact must be a JSON object", None
    if payload.get("schema") != ARTIFACT_SCHEMA:
        return False, "artifact schema mismatch", payload
    if payload.get("term") != term:
        return False, "artifact term mismatch", payload
    if payload.get("sha") != expected_sha:
        return False, "artifact SHA mismatch", payload
    if payload.get("passed") is not True:
        return False, "artifact did not pass", payload
    producer = payload.get("producer")
    verifier = payload.get("verifier")
    if not isinstance(producer, str) or not producer.strip():
        return False, "artifact producer missing", payload
    if not isinstance(verifier, str) or not verifier.strip():
        return False, "artifact verifier missing", payload
    return True, "verified artifact", payload


def evaluate_public_mcp_manifest(manifest: dict[str, Any], *, manifest_dir: Path, local_head: str | None = None) -> PublicMCPGate:
    values = {term: False for term in TERMS}
    reasons = {term: "missing evidence" for term in TERMS}

    if manifest.get("schema") != MANIFEST_SCHEMA:
        return PublicMCPGate(values, manifest, {term: "manifest schema mismatch" for term in TERMS})

    expected_sha = str(manifest.get("expected_sha") or "")
    if not SHA_RE.fullmatch(expected_sha):
        return PublicMCPGate(values, manifest, {term: "invalid expected SHA" for term in TERMS})

    entries = manifest.get("artifacts")
    if not isinstance(entries, list):
        return PublicMCPGate(values, manifest, {term: "artifact list missing" for term in TERMS})

    seen: set[str] = set()
    payloads: dict[str, dict[str, Any]] = {}
    for raw in entries:
        if not isinstance(raw, dict):
            continue
        term = str(raw.get("term") or "")
        if term not in TERMS:
            continue
        if term in seen:
            reasons[term] = "duplicate evidence for term"
            values[term] = False
            continue
        seen.add(term)
        ok, reason, payload = _load_artifact(raw, manifest_dir, expected_sha)
        values[term] = ok
        reasons[term] = reason
        if payload is not None:
            payloads[term] = payload

    # ExactSHAPinned requires both artifact evidence and the actual checkout HEAD
    # when the gate is executed in CI/fresh clone.
    if local_head is None or local_head != expected_sha:
        values["ExactSHAPinned"] = False
        reasons["ExactSHAPinned"] = f"local HEAD {local_head or 'unknown'} != expected {expected_sha}"

    deployment = payloads.get("DeployedSHAMatches")
    if deployment is not None:
        deployed_sha = ((deployment.get("details") or {}).get("deployed_sha"))
        if deployed_sha != expected_sha:
            values["DeployedSHAMatches"] = False
            reasons["DeployedSHAMatches"] = "deployment artifact does not identify the exact release SHA"

    ci = payloads.get("ExactHeadCIPassing")
    if ci is not None:
        details = ci.get("details") or {}
        if details.get("tested_sha") != expected_sha:
            values["ExactHeadCIPassing"] = False
            reasons["ExactHeadCIPassing"] = "CI artifact tested a different SHA"
        if details.get("conclusion") != "success":
            values["ExactHeadCIPassing"] = False
            reasons["ExactHeadCIPassing"] = "CI artifact conclusion is not success"

    integrated = payloads.get("V2RemoteIntegrationCertified")
    if integrated is not None:
        details = integrated.get("details") or {}
        required = {
            "oauth_authenticated": True,
            "durable_restart_retrieval": True,
            "tenant_isolation": True,
            "v2_tools_remote": True,
            "receipt_verified": True,
        }
        for key, expected in required.items():
            if details.get(key) is not expected:
                values["V2RemoteIntegrationCertified"] = False
                reasons["V2RemoteIntegrationCertified"] = f"integration artifact missing {key}=true"
                break

    return PublicMCPGate(values=values, evidence=manifest, reasons=reasons)
