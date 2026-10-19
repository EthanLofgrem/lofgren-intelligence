"""Validate a fail-closed public activation and rollback package.

The package is an operational handoff, not release evidence and not owner
authorization.  It references externally produced receipts by digest; it
never creates signatures or marks a PublicMCP gate term true.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

SCHEMA = "lofgren.activation-package/1"
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
MIGRATION_RE = re.compile(r"^[0-9]{14}_[a-z0-9_]+\.sql$")

REQUIRED_PROOFS = (
    "exact_head_ci",
    "migration_ledger",
    "health_readiness",
    "observability",
    "backup_restore",
    "rollback_rehearsal",
    "remote_client_interop",
    "tenant_isolation",
    "hosted_v1_v6_journey",
)


@dataclass(frozen=True)
class ActivationCheck:
    ready: bool
    checks: dict[str, bool]
    blockers: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": "lofgren.activation-package-check/1",
            "ready_to_request_owner_activation": self.ready,
            "checks": self.checks,
            "blockers": list(self.blockers),
            "PublicMCPReady": False,
            "owner_authorized": False,
        }


def _https_origin(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    parsed = urlsplit(value)
    return (
        parsed.scheme == "https"
        and bool(parsed.hostname)
        and parsed.username is None
        and parsed.password is None
        and parsed.query == ""
        and parsed.fragment == ""
        and parsed.path in ("", "/")
    )


def _receipt_valid(entry: Any, root: Path, candidate_sha: str) -> bool:
    if not isinstance(entry, dict):
        return False
    rel, digest = entry.get("path"), entry.get("sha256")
    if not isinstance(rel, str) or not DIGEST_RE.fullmatch(str(digest or "")):
        return False
    path = (root / rel).resolve()
    try:
        path.relative_to(root.resolve())
    except ValueError:
        return False
    try:
        raw = path.read_bytes()
        payload = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return False
    return (
        hashlib.sha256(raw).hexdigest() == digest
        and isinstance(payload, dict)
        and payload.get("candidate_sha") == candidate_sha
        and payload.get("passed") is True
        and isinstance(payload.get("result_id"), str)
        and bool(payload["result_id"].strip())
    )


def validate_activation_package(package: dict[str, Any], *, package_dir: Path) -> ActivationCheck:
    checks: dict[str, bool] = {}
    blockers: list[str] = []

    checks["schema"] = package.get("schema") == SCHEMA
    candidate = str(package.get("candidate_sha") or "")
    rollback = str(package.get("rollback_sha") or "")
    checks["candidate_sha"] = bool(SHA_RE.fullmatch(candidate))
    checks["rollback_sha"] = bool(SHA_RE.fullmatch(rollback)) and rollback != candidate
    checks["staging_only"] = package.get("environment") == "staging"
    checks["deployment_origin"] = _https_origin(package.get("deployment_origin"))
    checks["deployment_identity"] = package.get("deployed_sha") == candidate

    migrations = package.get("forward_migrations")
    checks["forward_migrations"] = (
        isinstance(migrations, list)
        and len(migrations) == len(set(migrations))
        and all(isinstance(item, str) and MIGRATION_RE.fullmatch(item) for item in migrations)
    )
    compatibility = package.get("rollback_compatibility")
    checks["rollback_compatibility"] = (
        isinstance(compatibility, dict)
        and compatibility.get("application_rollback_safe") is True
        and compatibility.get("database_rollback_required") is False
        and isinstance(compatibility.get("reason"), str)
        and bool(compatibility["reason"].strip())
    )

    proofs = package.get("proofs")
    checks["proof_set_exact"] = isinstance(proofs, dict) and set(proofs) == set(REQUIRED_PROOFS)
    for name in REQUIRED_PROOFS:
        checks[f"proof:{name}"] = (
            isinstance(proofs, dict)
            and _receipt_valid(proofs.get(name), package_dir, candidate)
        )

    commands = package.get("commands")
    checks["activation_command"] = (
        isinstance(commands, dict)
        and isinstance(commands.get("activate"), list)
        and bool(commands["activate"])
        and all(isinstance(item, str) and item.strip() for item in commands["activate"])
    )
    checks["rollback_command"] = (
        isinstance(commands, dict)
        and isinstance(commands.get("rollback"), list)
        and bool(commands["rollback"])
        and all(isinstance(item, str) and item.strip() for item in commands["rollback"])
    )
    checks["billing_fail_closed"] = package.get("billing_enabled") is False
    checks["external_execution_fail_closed"] = package.get("external_execution_enabled") is False
    checks["owner_decision_pending"] = package.get("owner_authorized") is False

    for name, passed in checks.items():
        if not passed:
            blockers.append(name)
    return ActivationCheck(ready=not blockers, checks=checks, blockers=tuple(blockers))
