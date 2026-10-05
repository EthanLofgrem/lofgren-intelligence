"""Convert an in-memory certified V1 run into a durable read-only snapshot."""

from __future__ import annotations

import json
from typing import Any

from ..evidence.types import to_dict
from ..kernel.knowledge_map import export_knowledge_map
from ..kernel.receipt import verify_receipt
from ..kernel.state import export_state
from ..report.markdown import render_markdown


def durable_snapshot(result: Any) -> dict[str, Any]:
    q_index = {q.id: i + 1 for i, q in enumerate(result.contract.questions)}
    findings = []
    for f in result.findings:
        d = to_dict(f)
        d["question_index"] = q_index.get(f.question_id)
        d["claims"] = [
            {
                "id": c.id,
                "statement": c.statement,
                "status": c.status.value,
                "confidence": c.confidence,
                "type": c.claim_type.value,
                "policy": c.sufficiency,
                "scope": to_dict(c.scope),
                "issues": list(c.issues),
            }
            for c in (result.graph.claims[i] for i in f.claim_ids)
        ]
        findings.append(d)

    contradictions = [
        {
            **to_dict(cx),
            "claim_a_statement": result.graph.claims[cx.claim_a].statement,
            "claim_b_statement": result.graph.claims[cx.claim_b].statement,
        }
        for cx in result.graph.contradictions.values()
    ]
    unknowns = [to_dict(u) for u in result.unknowns if u.status == "open"]
    traces = {cid: result.graph.trace(cid) for cid in result.graph.claims}
    receipt = json.loads(json.dumps(result.receipt, default=str))
    return {
        "run_id": receipt.get("research_id"),
        "completed": bool(result.completed),
        "stopped_reason": result.stopped_reason,
        "findings": findings,
        "contradictions": contradictions,
        "unknowns": unknowns,
        "traces": traces,
        "receipt": receipt,
        "receipt_intact": verify_receipt(receipt),
        "state": export_state(result),
        "knowledge_map2": export_knowledge_map(result),
        "report": render_markdown(result),
        "usage_units": float(result.ledger.total_units if result.ledger else 0.0),
        "charge_usd": float(result.charge.total_usd if result.charge else 0.0),
        "provider_info": json.loads(json.dumps(result.provider_info, default=str)),
    }


def summary(snapshot: dict[str, Any]) -> dict[str, Any]:
    return {
        "run_id": snapshot["run_id"],
        "completed": snapshot["completed"],
        "stopped_reason": snapshot["stopped_reason"],
        "findings": [
            {
                "id": f["id"],
                "question_index": f.get("question_index"),
                "question": f["question"],
                "answer": f["answer"],
                "confidence": f["confidence"],
                "claims": len(f.get("claim_ids") or []),
                "contradictions": len(f.get("contradiction_ids") or []),
                "issues": f.get("issues") or [],
            }
            for f in snapshot["findings"]
        ],
        "contradictions": len(snapshot["contradictions"]),
        "unknowns": [
            {"id": u["id"], "description": u["description"], "expected_gain": u["expected_gain"]}
            for u in snapshot["unknowns"]
        ],
        "charge_usd": snapshot["charge_usd"],
        "confidence_status": "provisional",
    }



def durable_discovery_snapshot(result: Any) -> dict[str, Any]:
    """Freeze a V2 discovery into a tenant-safe, process-independent snapshot."""
    from ..discovery.handoff import validate_handoff
    from ..discovery.receipt import objects_match_receipt, verify_discovery_receipt
    from ..discovery.report import discovery_summary, render_discovery_markdown

    receipt = json.loads(json.dumps(result.receipt, default=str))
    handoff = None if result.handoff is None else json.loads(json.dumps(result.handoff, default=str))
    handoff_problems = [] if handoff is None else validate_handoff(handoff, receipt, result.context)

    return {
        "discovery_id": receipt.get("discovery_id"),
        "research_id": result.context.research_id,
        "summary": discovery_summary(result),
        "prior_art": [x.to_dict() for x in result.prior_art],
        "gaps": [] if result.gaps is None else [x.to_dict() for x in result.gaps.gaps],
        "connections": [] if result.connections is None else [x.to_dict() for x in result.connections.connections],
        "hypotheses": [x.to_dict() for x in result.hypotheses.all],
        "requirements": [x.to_dict() for x in result.requirements],
        "candidates": [x.to_dict() for x in result.candidates.candidates],
        "simulations": [x.to_dict() for x in result.candidates.simulations],
        "sensitivities": [x.to_dict() for x in result.candidates.sensitivities],
        "optimization": None if result.candidates.optimization is None else result.candidates.optimization.to_dict(),
        "decision": None if result.decision is None else result.decision.to_dict(),
        "findings": [x.to_dict() for x in result.findings],
        "verifier_issues": [x.__dict__ for x in result.verifier.issues],
        "receipt": receipt,
        "receipt_intact": verify_discovery_receipt(receipt),
        "receipt_object_problems": objects_match_receipt(receipt, result.context.objects()),
        "context_objects": export_context_objects(result.context),
        "handoff": handoff,
        "handoff_problems": handoff_problems,
        "report": render_discovery_markdown(result),
        "stages": dict(result.stages),
        "notes": list(result.notes),
        "usage_units": float(result.ledger.total_units if result.ledger else 0.0),
        "charge_usd": float(result.ledger.total_usd if result.ledger else 0.0),
    }


# -- the discovery context ------------------------------------------------------------
#
# A discovery registers its V2 objects (objective, assumptions, candidates, scenarios, simulations, decision ...)
# in a DiscoveryContext built over the V1 knowledge map, and its receipt records each one's digest. A context
# rebuilt from the V1 run alone holds none of them, so V3 correctly refuses the handoff ("recorded but missing").
# The snapshot therefore keeps every registered object, and restore_discovery_context re-registers them into a
# fresh context over the same V1 map, re-running every reference and semantic check. Nothing is trusted: V3 still
# compares the restored objects against the receipt's digests.

CONTEXT_OBJECTS_SCHEMA = "lofgren.hosted.discovery-context-objects/1"


class SnapshotRestoreError(ValueError):
    """A stored discovery snapshot cannot be restored faithfully."""


def export_context_objects(context: Any) -> dict[str, Any]:
    from ..discovery.types import ALL_TYPES

    names = {cls: name for name, cls in ALL_TYPES.items()}
    objects = []
    for obj in context.objects():
        name = names.get(type(obj))
        if name is None:
            raise SnapshotRestoreError(f"{obj.id}: {type(obj).__name__} has no durable form")
        objects.append({"type": name, "object": obj.to_dict()})
    return json.loads(json.dumps({"schema": CONTEXT_OBJECTS_SCHEMA, "research_id": context.research_id,
                                  "objects": objects}))


def restore_discovery_context(base: Any, stored: Any) -> Any:
    """Re-register a snapshot's objects into `base`, a fresh context over the discovery's V1 map.

    Objects are registered as soon as everything they cite is present; any other refusal is final."""
    from ..discovery.errors import DiscoveryError, UnknownReference
    from ..discovery.types import from_dict

    if not isinstance(stored, dict) or stored.get("schema") != CONTEXT_OBJECTS_SCHEMA:
        raise SnapshotRestoreError("the discovery snapshot does not carry its context objects; run discover again")
    if stored.get("research_id") != base.research_id:
        raise SnapshotRestoreError("the discovery snapshot belongs to another research run")
    if base.objects():
        raise SnapshotRestoreError("a discovery context can only be restored into a fresh context")
    try:
        pending = [from_dict(x["type"], dict(x["object"])) for x in stored.get("objects") or []]
    except (DiscoveryError, KeyError, TypeError) as exc:
        raise SnapshotRestoreError(f"stored discovery object is invalid: {exc}") from None
    while pending:
        waiting, last = [], None
        for obj in pending:
            try:
                base.register(obj)
            except UnknownReference as exc:
                waiting.append(obj)
                last = exc
            except DiscoveryError as exc:
                raise SnapshotRestoreError(f"stored discovery object {obj.id} is refused: {exc}") from None
        if len(waiting) == len(pending):
            raise SnapshotRestoreError(f"stored discovery objects cite what the snapshot lacks: {last}")
        pending = waiting
    return base
