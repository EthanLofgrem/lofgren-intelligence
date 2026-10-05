"""Signed, evidence-backed, fail-closed public MCP release gate.

Every release term must be backed by a SHA-bound artifact whose bytes are
hashed in the manifest and signed by an operator-trusted Ed25519 verifier.
The trust policy is supplied separately from the release candidate.

Unknown, unsigned, revoked, malformed, stale, duplicated, mismatched or
unverifiable evidence is false.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

SHA_RE = re.compile(r"^[0-9a-f]{40}$")
ARTIFACT_SCHEMA = "lofgren.public-evidence/1"
MANIFEST_SCHEMA = "lofgren.public-release-evidence/1"
TRUST_SCHEMA = "lofgren.evidence-trust/1"

TERMS = (
    "CoreV1Certified",
    "V1BoundaryCertified",
    "V2Certified",
    "V3Certified",
    "V4Certified",
    "V5Certified",
    "V6Certified",
    "FullV1V6RemoteIntegrationCertified",
    "CurrentMCPProtocolCertified",
    "InstalledWheelMCPCertified",
    "OAuthPKCECertified",
    "DurableRunsCertified",
    "DurableLifecycleCertified",
    "TenantIsolationCertified",
    "HumanActionApprovalCertified",
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
    "MigrationLedgerAligned",
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


@dataclass(frozen=True)
class PublicMCPGate:
    values: dict[str, bool]
    evidence: dict[str, Any]
    reasons: dict[str, str]

    @property
    def passed_count(self) -> int:
        return sum(1 for value in self.values.values() if value)

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


def _sha256_bytes(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _policy_key(policy: dict[str, Any], key_id: str) -> tuple[dict[str, Any] | None, str | None]:
    if not isinstance(policy, dict) or policy.get("schema") != TRUST_SCHEMA:
        return None, "trust policy schema mismatch"
    keys = policy.get("keys")
    if not isinstance(keys, dict):
        return None, "trust policy keys missing"
    key = keys.get(key_id)
    if not isinstance(key, dict):
        return None, f"untrusted key id {key_id!r}"
    if key.get("revoked") is True:
        return None, f"verifier key {key_id!r} is revoked"
    return key, None


def _authenticate_artifact(
    *,
    entry: dict[str, Any],
    payload: dict[str, Any],
    raw: bytes,
    policy: dict[str, Any],
) -> tuple[bool, str]:
    key_id = str(entry.get("key_id") or "")
    signature_text = str(entry.get("signature") or "")
    if not key_id or not signature_text:
        return False, "artifact signature metadata missing"
    key, error = _policy_key(policy, key_id)
    if error:
        return False, error
    assert key is not None

    term = str(entry.get("term") or "")
    allowed = key.get("terms")
    if not isinstance(allowed, list) or term not in allowed:
        return False, f"verifier key {key_id!r} is not authorized for {term}"
    if payload.get("producer") != key.get("producer"):
        return False, "artifact producer does not match trust policy"
    if payload.get("verifier") != key.get("verifier"):
        return False, "artifact verifier does not match trust policy"

    try:
        public_key = base64.b64decode(str(key["public_key"]), validate=True)
        signature = base64.b64decode(signature_text, validate=True)
        Ed25519PublicKey.from_public_bytes(public_key).verify(signature, raw)
    except (KeyError, ValueError, InvalidSignature) as exc:
        return False, f"artifact signature invalid: {type(exc).__name__}"

    details = payload.get("details")
    if not isinstance(details, dict):
        return False, "artifact details missing"

    mode = key.get("mode")
    if mode == "automated":
        source = key.get("source")
        if not isinstance(source, str) or not source:
            return False, "automated verifier source missing from trust policy"
        if details.get("source") != source:
            return False, "automated evidence source does not match trust policy"
        if not isinstance(details.get("result_id"), str) or not details["result_id"].strip():
            return False, "automated evidence result_id missing"
    elif mode == "manual":
        approval = details.get("approval")
        if not isinstance(approval, dict):
            return False, "manual approval record missing"
        required = {
            "approved": True,
            "sha": payload.get("sha"),
            "term": term,
            "approver": key.get("verifier"),
        }
        for field, expected in required.items():
            if approval.get(field) != expected:
                return False, f"manual approval field {field!r} mismatch"
        if not isinstance(approval.get("reason"), str) or not approval["reason"].strip():
            return False, "manual approval reason missing"
    else:
        return False, "verifier mode must be automated or manual"

    return True, "signature and trust policy verified"


def _load_artifact(
    entry: dict[str, Any],
    root: Path,
    expected_sha: str,
    trust_policy: dict[str, Any],
) -> tuple[bool, str, dict[str, Any] | None]:
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

    raw = path.read_bytes()
    actual_digest = _sha256_bytes(raw)
    if not re.fullmatch(r"[0-9a-f]{64}", digest) or actual_digest != digest:
        return False, f"artifact digest mismatch: {rel}", None
    try:
        payload = json.loads(raw.decode("utf-8"))
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
    if not isinstance(payload.get("producer"), str) or not str(payload["producer"]).strip():
        return False, "artifact producer missing", payload
    if not isinstance(payload.get("verifier"), str) or not str(payload["verifier"]).strip():
        return False, "artifact verifier missing", payload

    trusted, reason = _authenticate_artifact(entry=entry, payload=payload, raw=raw, policy=trust_policy)
    if not trusted:
        return False, reason, payload
    return True, reason, payload


def evaluate_public_mcp_manifest(
    manifest: dict[str, Any],
    *,
    manifest_dir: Path,
    trust_policy: dict[str, Any],
    local_head: str | None = None,
) -> PublicMCPGate:
    values = {term: False for term in TERMS}
    reasons = {term: "missing evidence" for term in TERMS}

    if manifest.get("schema") != MANIFEST_SCHEMA:
        return PublicMCPGate(values, manifest, {term: "manifest schema mismatch" for term in TERMS})
    if not isinstance(trust_policy, dict) or trust_policy.get("schema") != TRUST_SCHEMA:
        return PublicMCPGate(values, manifest, {term: "trust policy schema mismatch" for term in TERMS})

    expected_sha = str(manifest.get("expected_sha") or "")
    if not SHA_RE.fullmatch(expected_sha):
        return PublicMCPGate(values, manifest, {term: "invalid expected SHA" for term in TERMS})

    entries = manifest.get("artifacts")
    if not isinstance(entries, list):
        return PublicMCPGate(values, manifest, {term: "artifact list missing" for term in TERMS})

    seen: set[str] = set()
    payloads: dict[str, dict[str, Any]] = {}
    for raw_entry in entries:
        if not isinstance(raw_entry, dict):
            continue
        term = str(raw_entry.get("term") or "")
        if term not in TERMS:
            continue
        if term in seen:
            reasons[term] = "duplicate evidence for term"
            values[term] = False
            continue
        seen.add(term)
        ok, reason, payload = _load_artifact(raw_entry, manifest_dir, expected_sha, trust_policy)
        values[term] = ok
        reasons[term] = reason
        if payload is not None:
            payloads[term] = payload

    if local_head is None or local_head != expected_sha:
        values["ExactSHAPinned"] = False
        reasons["ExactSHAPinned"] = f"local HEAD {local_head or 'unknown'} != expected {expected_sha}"

    deployment = payloads.get("DeployedSHAMatches")
    if deployment is not None:
        deployed_sha = (deployment.get("details") or {}).get("deployed_sha")
        if deployed_sha != expected_sha:
            values["DeployedSHAMatches"] = False
            reasons["DeployedSHAMatches"] = "deployment artifact does not identify the exact release SHA"

    ci = payloads.get("ExactHeadCIPassing")
    if ci is not None:
        details = ci.get("details") or {}
        if details.get("tested_sha") != expected_sha or details.get("conclusion") != "success":
            values["ExactHeadCIPassing"] = False
            reasons["ExactHeadCIPassing"] = "CI artifact does not prove success on the exact release SHA"

    integrated = payloads.get("FullV1V6RemoteIntegrationCertified")
    if integrated is not None:
        details = integrated.get("details") or {}
        required = {
            "oauth_authenticated": True,
            "tenant_isolation": True,
            "durable_restart_retrieval": True,
            "v1_research_remote": True,
            "v2_discovery_remote": True,
            "v3_artifact_remote": True,
            "v4_browser_approval": True,
            "v4_execution_remote": True,
            "v5_outcome_remote": True,
            "v6_improvement_remote": True,
            "all_receipts_verified": True,
        }
        for field, expected in required.items():
            if details.get(field) is not expected:
                values["FullV1V6RemoteIntegrationCertified"] = False
                reasons["FullV1V6RemoteIntegrationCertified"] = f"integration artifact missing {field}=true"
                break

    return PublicMCPGate(values=values, evidence=manifest, reasons=reasons)
