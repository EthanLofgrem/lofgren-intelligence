"""Fail-closed public MCP release gate.

This gate intentionally combines code, database, payment, deployment and
interoperability evidence. Unknown is false. Passing unit tests alone can never
make a public release ready.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


TERMS = (
    "CoreV1Certified",
    "V1BoundaryCertified",
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
    "ExactSHAPinned",
    "DeployedSHAMatches",
)


@dataclass(frozen=True)
class PublicMCPGate:
    values: dict[str, bool]
    evidence: dict[str, Any]

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
            "true": self.passed_count,
            "total": self.total,
            "PublicMCPReady": self.ready,
        }


def evaluate_public_mcp(evidence: dict[str, Any]) -> PublicMCPGate:
    values: dict[str, bool] = {}
    for term in TERMS:
        # Strict identity to True prevents strings, 1, or optimistic status
        # labels from silently satisfying a release condition.
        values[term] = evidence.get(term) is True

    expected = evidence.get("expected_sha")
    deployed = evidence.get("deployed_sha")
    tested = evidence.get("tested_sha")
    if not expected or tested != expected:
        values["ExactSHAPinned"] = False
    if not expected or deployed != expected:
        values["DeployedSHAMatches"] = False

    return PublicMCPGate(values=values, evidence=evidence)
