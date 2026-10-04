"""V4 Execution Intelligence.

V4 is the only layer allowed to perform external side effects. It requires an
explicit identity-bound capability grant and approval record, then executes a
transactional PLAN -> PREFLIGHT -> SNAPSHOT -> AUTHORIZE -> EXECUTE -> VERIFY
-> COMMIT flow. Failed reversible actions roll back.

The default real adapter is a bounded HTTPS JSON webhook. Arbitrary shell,
filesystem mutation, payment, deployment, email, device control and credential
use are not implicit capabilities; they require separately registered adapters
and grants.
"""

from __future__ import annotations

import hashlib
import http.client
import ipaddress
import json
import socket
import ssl
import urllib.parse
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Protocol

ACTION_RECEIPT_SCHEMA = "lofgren.action-receipt/1"
V5_HANDOFF_SCHEMA = "lofgren.v5-handoff/1"
V4_HANDOFF_SCHEMA = "lofgren.v4-handoff/1"
MAX_BODY_BYTES = 256_000
MAX_RESPONSE_BYTES = 512_000


class ExecutionError(RuntimeError):
    pass


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def _hash(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _parse_time(value: str) -> datetime:
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _public_ip(value: str) -> bool:
    ip = ipaddress.ip_address(value)
    return not (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_reserved
                or ip.is_unspecified)


def _validate_https_target(url: str) -> tuple[str, int, str]:
    p = urllib.parse.urlsplit(url)
    if p.scheme.lower() != "https" or not p.hostname or p.username or p.password:
        raise ExecutionError("webhook target must be credential-free HTTPS")
    host = p.hostname.rstrip(".").lower()
    if host in {"localhost", "localhost.localdomain"} or host.endswith(".local"):
        raise ExecutionError("local webhook targets are not allowed")
    port = p.port or 443
    rows = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    ips = []
    for row in rows:
        ip = str(row[4][0])
        if not _public_ip(ip):
            raise ExecutionError("webhook hostname resolves to a non-public address")
        if ip not in ips:
            ips.append(ip)
    if not ips:
        raise ExecutionError("webhook hostname did not resolve")
    path = urllib.parse.urlunsplit(("", "", p.path or "/", p.query, ""))
    return host, port, path


@dataclass(frozen=True)
class ActionRequest:
    artifact_id: str
    artifact_fingerprint: str
    kind: str
    target: str
    payload: dict[str, Any]
    cost_usd: float = 0.0
    reversible: bool = False

    @property
    def action_id(self) -> str:
        body = {
            "artifact_id": self.artifact_id,
            "artifact_fingerprint": self.artifact_fingerprint,
            "kind": self.kind,
            "target": self.target,
            "payload": self.payload,
            "cost_usd": round(float(self.cost_usd), 8),
            "reversible": bool(self.reversible),
        }
        return "ACT-" + _hash(body)[:20]

    @property
    def action_hash(self) -> str:
        return "AH-" + _hash({
            "action_id": self.action_id,
            "target": self.target,
            "payload": self.payload,
            "cost_usd": round(float(self.cost_usd), 8),
        })


@dataclass(frozen=True)
class CapabilityGrant:
    grant_id: str
    subject: str
    action_kind: str
    target_prefix: str
    max_cost_usd: float
    expires_at: str
    reversible_only: bool = False


@dataclass(frozen=True)
class ApprovalRecord:
    approval_id: str
    subject: str
    action_id: str
    action_hash: str
    artifact_id: str
    approved_at: str


@dataclass(frozen=True)
class ExecutionResult:
    status: str
    receipt: dict[str, Any]
    v5_handoff: dict[str, Any] | None


class ExecutionAdapter(Protocol):
    kind: str

    def preflight(self, request: ActionRequest) -> dict[str, Any]: ...
    def snapshot(self, request: ActionRequest) -> dict[str, Any]: ...
    def execute(self, request: ActionRequest, idempotency_key: str) -> dict[str, Any]: ...
    def verify(self, request: ActionRequest, response: dict[str, Any]) -> bool: ...
    def rollback(self, request: ActionRequest, snapshot: dict[str, Any]) -> dict[str, Any]: ...


class InMemoryAdapter:
    """Deterministic transactional adapter used for certification and embedders."""

    kind = "memory"

    def __init__(self, *, fail_verify: bool = False) -> None:
        self.state: dict[str, Any] = {}
        self.fail_verify = fail_verify
        self.executions: dict[str, dict[str, Any]] = {}

    def preflight(self, request: ActionRequest) -> dict[str, Any]:
        return {"ready": True}

    def snapshot(self, request: ActionRequest) -> dict[str, Any]:
        return {"exists": request.target in self.state, "value": self.state.get(request.target)}

    def execute(self, request: ActionRequest, idempotency_key: str) -> dict[str, Any]:
        if idempotency_key in self.executions:
            return dict(self.executions[idempotency_key])
        self.state[request.target] = json.loads(json.dumps(request.payload))
        out = {"ok": True, "target": request.target, "state_hash": _hash(self.state[request.target])}
        self.executions[idempotency_key] = dict(out)
        return out

    def verify(self, request: ActionRequest, response: dict[str, Any]) -> bool:
        return not self.fail_verify and self.state.get(request.target) == request.payload and response.get("ok") is True

    def rollback(self, request: ActionRequest, snapshot: dict[str, Any]) -> dict[str, Any]:
        if snapshot.get("exists"):
            self.state[request.target] = snapshot.get("value")
        else:
            self.state.pop(request.target, None)
        return {"rolled_back": True}


class HTTPSWebhookAdapter:
    """Bounded real-world adapter: one HTTPS JSON POST, no redirects."""

    kind = "https_webhook"

    def __init__(self, timeout: float = 15.0) -> None:
        self.timeout = timeout

    def preflight(self, request: ActionRequest) -> dict[str, Any]:
        host, port, path = _validate_https_target(request.target)
        body = _canonical(request.payload).encode("utf-8")
        if len(body) > MAX_BODY_BYTES:
            raise ExecutionError("webhook body exceeds execution limit")
        return {"ready": True, "host": host, "port": port, "path": path}

    def snapshot(self, request: ActionRequest) -> dict[str, Any]:
        return {"reversible": False, "note": "HTTPS webhook has no generic rollback"}

    def execute(self, request: ActionRequest, idempotency_key: str) -> dict[str, Any]:
        host, port, path = _validate_https_target(request.target)
        ip = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)[0][4][0]
        if not _public_ip(str(ip)):
            raise ExecutionError("resolved webhook address is not public")
        raw = _canonical(request.payload).encode("utf-8")
        sock = socket.create_connection((str(ip), port), self.timeout)
        ctx = ssl.create_default_context()
        tls = ctx.wrap_socket(sock, server_hostname=host)
        conn = http.client.HTTPSConnection(host, port, timeout=self.timeout, context=ctx)
        conn.sock = tls
        try:
            conn.request("POST", path, body=raw, headers={
                "Host": host if port == 443 else f"{host}:{port}",
                "Content-Type": "application/json",
                "Content-Length": str(len(raw)),
                "Idempotency-Key": idempotency_key,
                "User-Agent": "Lofgren-Intelligence/Execution",
                "Connection": "close",
            })
            response = conn.getresponse()
            body = response.read(MAX_RESPONSE_BYTES + 1)
            if len(body) > MAX_RESPONSE_BYTES:
                raise ExecutionError("webhook response exceeds execution limit")
            return {
                "status": response.status,
                "content_type": response.headers.get("Content-Type", ""),
                "body_sha256": hashlib.sha256(body).hexdigest(),
            }
        finally:
            conn.close()

    def verify(self, request: ActionRequest, response: dict[str, Any]) -> bool:
        status = int(response.get("status", 0))
        return 200 <= status < 300

    def rollback(self, request: ActionRequest, snapshot: dict[str, Any]) -> dict[str, Any]:
        return {"rolled_back": False, "reason": "generic HTTPS webhook is irreversible"}


def _validate_authority(
    v4_handoff: dict[str, Any],
    request: ActionRequest,
    grant: CapabilityGrant,
    approval: ApprovalRecord,
    *,
    subject: str,
    now: datetime,
    spent_usd: float,
) -> None:
    if v4_handoff.get("schema") != V4_HANDOFF_SCHEMA:
        raise ExecutionError("invalid V4 handoff")
    if v4_handoff.get("artifact_verified") is not True or v4_handoff.get("acceptance_passed") is not True:
        raise ExecutionError("artifact is not verified for execution")
    if request.artifact_id != v4_handoff.get("artifact_id"):
        raise ExecutionError("action artifact does not match V4 handoff")
    if request.artifact_fingerprint != v4_handoff.get("artifact_fingerprint"):
        raise ExecutionError("action fingerprint does not match V4 handoff")
    if grant.subject != subject or approval.subject != subject:
        raise ExecutionError("grant/approval subject does not match authenticated subject")
    if grant.action_kind != request.kind:
        raise ExecutionError("capability grant does not allow this action kind")
    if not request.target.startswith(grant.target_prefix):
        raise ExecutionError("target is outside capability grant")
    if now.astimezone(timezone.utc) >= _parse_time(grant.expires_at):
        raise ExecutionError("capability grant expired")
    if spent_usd + request.cost_usd > grant.max_cost_usd + 1e-9:
        raise ExecutionError("action exceeds capability budget")
    if grant.reversible_only and not request.reversible:
        raise ExecutionError("capability grant permits reversible actions only")
    if approval.action_id != request.action_id or approval.action_hash != request.action_hash:
        raise ExecutionError("approval does not bind to this exact action")
    if approval.artifact_id != request.artifact_id:
        raise ExecutionError("approval artifact mismatch")
    if _parse_time(approval.approved_at) > now.astimezone(timezone.utc):
        raise ExecutionError("approval is future-dated")


def _receipt(body: dict[str, Any]) -> dict[str, Any]:
    return {**body, "receipt_hash": "AR-" + _hash(body)}


def verify_action_receipt(receipt: dict[str, Any]) -> bool:
    if receipt.get("schema") != ACTION_RECEIPT_SCHEMA:
        return False
    body = dict(receipt)
    supplied = body.pop("receipt_hash", None)
    return supplied == "AR-" + _hash(body)


def execute_authorized(
    v4_handoff: dict[str, Any],
    request: ActionRequest,
    grant: CapabilityGrant,
    approval: ApprovalRecord,
    adapter: ExecutionAdapter,
    *,
    subject: str,
    now: datetime,
    spent_usd: float = 0.0,
) -> ExecutionResult:
    """Execute one exact approved action and return a reproducible receipt."""
    _validate_authority(v4_handoff, request, grant, approval, subject=subject, now=now, spent_usd=spent_usd)
    if adapter.kind != request.kind:
        raise ExecutionError("adapter kind does not match action kind")

    preflight = adapter.preflight(request)
    if preflight.get("ready") is not True:
        raise ExecutionError("adapter preflight refused action")
    snapshot = adapter.snapshot(request)
    idempotency_key = request.action_hash
    response = adapter.execute(request, idempotency_key)
    verified = bool(adapter.verify(request, response))

    rollback = None
    status = "committed"
    if not verified:
        status = "failed"
        if request.reversible:
            rollback = adapter.rollback(request, snapshot)
            status = "rolled_back" if rollback.get("rolled_back") else "rollback_failed"

    body = {
        "schema": ACTION_RECEIPT_SCHEMA,
        "action_id": request.action_id,
        "action_hash": request.action_hash,
        "artifact_id": request.artifact_id,
        "artifact_fingerprint": request.artifact_fingerprint,
        "subject": subject,
        "grant_id": grant.grant_id,
        "approval_id": approval.approval_id,
        "kind": request.kind,
        "target": request.target,
        "cost_usd": round(float(request.cost_usd), 8),
        "reversible": bool(request.reversible),
        "preflight": preflight,
        "snapshot_hash": "SN-" + _hash(snapshot),
        "response": response,
        "verified": verified,
        "rollback": rollback,
        "status": status,
        "executed_at": now.astimezone(timezone.utc).isoformat(),
    }
    receipt = _receipt(body)
    if not verify_action_receipt(receipt):
        raise ExecutionError("action receipt failed self-verification")

    v5 = None
    if status == "committed":
        v5 = {
            "schema": V5_HANDOFF_SCHEMA,
            "action_id": request.action_id,
            "action_receipt_hash": receipt["receipt_hash"],
            "artifact_id": request.artifact_id,
            "objective": v4_handoff.get("objective", ""),
            "expected_outcomes": v4_handoff.get("expected_outcomes", []),
            "measurement_required": True,
        }
    return ExecutionResult(status, receipt, v5)
