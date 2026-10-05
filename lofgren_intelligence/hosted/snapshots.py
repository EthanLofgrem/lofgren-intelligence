"""Convert an in-memory certified V1 run into a durable read-only snapshot."""

from __future__ import annotations

import json
from typing import Any

from ..evidence.types import to_dict
from ..kernel.knowledge_map import export_knowledge_map
from ..kernel.receipt import verify_receipt
from ..kernel.state import export_state
from ..report.markdown import render_markdown
from ..discovery.errors import UnknownReference


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
    """Freeze a V2 discovery into a tenant-safe, process-independent snapshot.

    Strong V3 verifies the handoff value-by-value against every V2 object named
    by the discovery receipt. Persist those typed objects as part of the durable
    discovery so a later process can reconstruct the exact certified context
    instead of falling back to only the V1 knowledge map.
    """
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
        "context_objects": [
            {"type": type(obj).__name__, "data": obj.to_dict()}
            for obj in result.context.objects()
        ],
        "receipt_intact": verify_discovery_receipt(receipt),
        "receipt_object_problems": objects_match_receipt(receipt, result.context.objects()),
        "handoff": handoff,
        "handoff_problems": handoff_problems,
        "report": render_discovery_markdown(result),
        "stages": dict(result.stages),
        "notes": list(result.notes),
        "usage_units": float(result.ledger.total_units if result.ledger else 0.0),
        "charge_usd": float(result.ledger.total_usd if result.ledger else 0.0),
    }



def restore_discovery_context(base_context: Any, snapshot: dict[str, Any]) -> Any:
    """Reconstruct the exact typed V2 context recorded in a durable snapshot.

    Registration is dependency-aware: objects are instantiated from their
    validated dataclass representation, then registered only after every
    referenced object is available. Unknown or corrupted object types fail
    closed. The reconstructed context must exactly match the object digests in
    the discovery receipt before V3 may consume it.
    """
    from ..discovery import types as discovery_types
    from ..discovery.receipt import objects_match_receipt

    rows = snapshot.get("context_objects")
    if not isinstance(rows, list) or not rows:
        raise ValueError("stored discovery snapshot is missing typed context_objects")

    registry = {
        name: cls for name, cls in vars(discovery_types).items()
        if isinstance(cls, type)
        and issubclass(cls, discovery_types._Obj)
        and cls is not discovery_types._Obj
    }

    pending = []
    for i, row in enumerate(rows):
        if not isinstance(row, dict):
            raise ValueError(f"context_objects[{i}] is not an object")
        type_name = row.get("type")
        data = row.get("data")
        cls = registry.get(type_name)
        if cls is None:
            raise ValueError(f"context_objects[{i}] has unsupported type {type_name!r}")
        if not isinstance(data, dict):
            raise ValueError(f"context_objects[{i}].data is not an object")
        pending.append(cls(**data))

    while pending:
        deferred = []
        progressed = False
        for obj in pending:
            try:
                base_context.ensure(obj)
                progressed = True
            except UnknownReference:
                deferred.append(obj)
        if not deferred:
            break
        if not progressed:
            unresolved = ", ".join(sorted(obj.id for obj in deferred)[:10])
            raise ValueError(f"stored discovery context has unresolved references: {unresolved}")
        pending = deferred

    problems = objects_match_receipt(snapshot.get("receipt") or {}, base_context.objects())
    if problems:
        raise ValueError("stored discovery context does not match its receipt: " + "; ".join(problems[:5]))
    return base_context
