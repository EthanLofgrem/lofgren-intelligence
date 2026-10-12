"""Fail-closed, term-by-term public release evidence ledger.

This module does not create evidence or signatures.  It turns the existing
PublicMCP gate result into an auditable ledger and adds an explicit freshness
check.  Missing or malformed timestamps are UNKNOWN, therefore not proven.
"""

from __future__ import annotations

import json
import hashlib
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from lofgren_intelligence.hosted.certification import PublicMCPGate, TERMS

LEDGER_SCHEMA = "lofgren.public-evidence-ledger/1"
DEFAULT_MAX_AGE_SECONDS = 7 * 24 * 60 * 60

_OWNER_BY_TERM = {
    "CoreV1Certified": "intelligence",
    "V1BoundaryCertified": "intelligence",
    "V2Certified": "intelligence",
    "V3Certified": "intelligence",
    "V4Certified": "intelligence",
    "V5Certified": "intelligence",
    "V6Certified": "intelligence",
    "FullV1V6RemoteIntegrationCertified": "hosted-journey",
    "CurrentMCPProtocolCertified": "mcp",
    "InstalledWheelMCPCertified": "release",
    "OAuthPKCECertified": "identity",
    "DurableRunsCertified": "data",
    "DurableLifecycleCertified": "worker",
    "TenantIsolationCertified": "security",
    "HumanActionApprovalCertified": "authority",
    "FoundingFreeExactly1000Certified": "entitlements",
    "User1001PaidRequiredCertified": "billing",
    "QuotaEnforcementCertified": "economics",
    "RateLimitCertified": "security",
    "SSRFSafe": "security",
    "InputLimitsCertified": "security",
    "ActualCOGSMeteringCertified": "economics",
    "PaidPlanEconomicsCertified": "economics-owner",
    "StripeSandboxJourneyCertified": "billing",
    "WebhookSecurityCertified": "billing",
    "DatabaseMigrationCertified": "data",
    "MigrationLedgerAligned": "operations",
    "RemoteClientInteropCertified": "mcp-clients",
    "HealthReadinessCertified": "operations",
    "ObservabilityCertified": "operations",
    "BackupRestoreCertified": "operations",
    "RollbackCertified": "operations",
    "SecretsBoundaryCertified": "security-owner",
    "ExactHeadCIPassing": "release",
    "ExactSHAPinned": "release",
    "DeployedSHAMatches": "deployment",
}


def _parse_utc(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.endswith("Z"):
        return None
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def _artifact_payload(entry: dict[str, Any], root: Path) -> dict[str, Any] | None:
    rel = entry.get("path")
    if not isinstance(rel, str):
        return None
    path = (root / rel).resolve()
    try:
        path.relative_to(root.resolve())
    except ValueError:
        return None
    try:
        raw = path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != entry.get("sha256"):
            return None
        payload = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def assemble_evidence_ledger(
    *,
    gate: PublicMCPGate,
    manifest: dict[str, Any],
    manifest_dir: Path,
    evaluated_at: datetime | None = None,
    max_age_seconds: int = DEFAULT_MAX_AGE_SECONDS,
) -> dict[str, Any]:
    """Build a deterministic ledger; freshness can only reduce proof status."""
    if max_age_seconds <= 0:
        raise ValueError("max_age_seconds must be positive")
    now = (evaluated_at or datetime.now(timezone.utc)).astimezone(timezone.utc)
    entries = manifest.get("artifacts")
    by_term: dict[str, list[dict[str, Any]]] = {}
    if isinstance(entries, list):
        for entry in entries:
            if isinstance(entry, dict):
                by_term.setdefault(str(entry.get("term") or ""), []).append(entry)

    rows: list[dict[str, Any]] = []
    proven = 0
    for term in TERMS:
        matches = by_term.get(term, [])
        entry = matches[0] if len(matches) == 1 else None
        payload = _artifact_payload(entry, manifest_dir) if entry is not None else None
        issued = _parse_utc(payload.get("issued_at")) if payload is not None else None
        age_seconds = (now - issued).total_seconds() if issued is not None else None
        fresh = (
            issued is not None
            and age_seconds is not None
            and 0 <= age_seconds <= max_age_seconds
        )
        authenticated = bool(gate.values.get(term))
        status = "PROVEN" if authenticated and fresh and len(matches) == 1 else "BLOCKED"
        if status == "PROVEN":
            proven += 1
        if len(matches) != 1:
            blocker = "missing evidence" if not matches else "duplicate evidence"
        elif not authenticated:
            blocker = gate.reasons.get(term, "gate rejected evidence")
        elif payload is None:
            blocker = "artifact unavailable or digest changed after gate evaluation"
        elif issued is None:
            blocker = "signed artifact issued_at missing or malformed"
        elif age_seconds is not None and age_seconds < 0:
            blocker = "signed artifact is future-dated"
        elif not fresh:
            blocker = f"signed artifact is stale ({int(age_seconds or 0)}s)"
        else:
            blocker = None
        rows.append(
            {
                "term": term,
                "status": status,
                "owner": _OWNER_BY_TERM[term],
                "proof": entry.get("path") if entry is not None else None,
                "key_id": entry.get("key_id") if entry is not None else None,
                "issued_at": payload.get("issued_at") if payload is not None else None,
                "age_seconds": int(age_seconds) if age_seconds is not None else None,
                "blocker": blocker,
            }
        )

    candidate_sha = str(manifest.get("expected_sha") or "")
    return {
        "schema": LEDGER_SCHEMA,
        "candidate_sha": candidate_sha,
        "evaluated_at": now.isoformat().replace("+00:00", "Z"),
        "freshness_limit_seconds": max_age_seconds,
        "proven": proven,
        "total": len(TERMS),
        "PublicMCPReady": proven == len(TERMS) and gate.ready,
        "terms": rows,
    }


def render_markdown(ledger: dict[str, Any]) -> str:
    lines = [
        "# Public release evidence ledger",
        "",
        f"- Candidate SHA: `{ledger['candidate_sha']}`",
        f"- Evaluated: {ledger['evaluated_at']}",
        f"- Proven: {ledger['proven']}/{ledger['total']}",
        f"- PublicMCPReady: {'TRUE' if ledger['PublicMCPReady'] else 'FALSE'}",
        "",
        "| Term | Status | Owner | Proof | Blocker |",
        "| --- | --- | --- | --- | --- |",
    ]
    for row in ledger["terms"]:
        values = [
            row["term"],
            row["status"],
            row["owner"],
            row["proof"] or "—",
            row["blocker"] or "—",
        ]
        lines.append("| " + " | ".join(str(value).replace("|", "\\|") for value in values) + " |")
    return "\n".join(lines) + "\n"

