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
