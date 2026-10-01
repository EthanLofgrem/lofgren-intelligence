"""Research receipts: every run can show exactly how its conclusions were made.

A receipt records the contract, the plan, every operation, every source and
evidence hash, the claims and their policies, contradictions, calculations,
lineage, unknowns, findings, model/provider versions, verification settings,
cost and timestamps. Its research ID is the hash of all of that.

Two hashes support reproducibility:
  inputs_hash  contract + evidence content + provider/version + verifier settings
  state_hash   the resulting findings and claim statuses
Re-running with the same inputs_hash must produce the same state_hash.
"""

from __future__ import annotations

import copy
import hashlib
import json
from typing import TYPE_CHECKING, Any

from ..evidence.types import to_dict
from ..models.provider import EXTRACTION_TEMPLATE_VERSION
from ..verification.engine import JACCARD_THRESHOLD, OVERLAP_THRESHOLD, VALUE_TOLERANCE, WEIGHTS

if TYPE_CHECKING:
    from .pipeline import RunResult

RECEIPT_SCHEMA = "lofgren.research-receipt/1"
VERIFIER_SETTINGS = {
    "weights": WEIGHTS,
    "overlap_threshold": OVERLAP_THRESHOLD,
    "jaccard_threshold": JACCARD_THRESHOLD,
    "value_tolerance": VALUE_TOLERANCE,
    "policies": "v1.1",
    "skeptic": "v1.1",
}


def canonical_hash(obj: Any) -> str:
    blob = json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(blob.encode()).hexdigest()


def build_receipt(r: "RunResult") -> dict:
    from .. import __version__
    from ..report.markdown import render_markdown

    g = r.graph
    contract = to_dict(r.contract)
    used_evidence = {e for c in g.claims.values() for e in c.supporting} | \
                    {i.get("evidence_id") for calc in g.calculations.values() for i in calc.inputs}
    provider = {k: v for k, v in r.provider_info.items() if k != "usage"}
    body = {
        "schema": RECEIPT_SCHEMA,
        "engine": {"name": "lofgren-intelligence", "version": __version__, "capability_generation": "V1"},
        "provider": provider,
        "provider_usage": r.provider_info.get("usage", {}),
        "extraction_template": EXTRACTION_TEMPLATE_VERSION,
        "verifier": {**VERIFIER_SETTINGS, "calibrated": r.calibrated},
        "objective": r.contract.objective,
        "contract": contract,
        "contract_hash": canonical_hash(contract),
        "plan": {
            "tasks": [t.__dict__ for t in r.plan.tasks],
            "capability_gaps": [gap.__dict__ for gap in r.plan.gaps],
        },
        "operations": r.ledger.to_json()["entries"] if r.ledger else [],
        "sources": [{"id": s.id, "kind": s.kind.value, "title": s.title, "uri": s.uri, "publisher": s.publisher,
                     "license": s.license, "published_at": s.published_at, "retrieved_at": s.retrieved_at,
                     "independence_group": s.independence_group, "derived_from": s.derived_from}
                    for s in g.sources.values()],
        "evidence": [{"id": e.id, "source_id": e.source_id, "kind": e.kind.value, "content_hash": e.content_hash,
                      "observed_at": e.observed_at, "valid_from": e.valid_from, "valid_to": e.valid_to}
                     for e in g.evidence.values()],
        "rejected_evidence": sorted(e for e in g.evidence if e not in used_evidence),
        "claims": [{"id": c.id, "statement": c.statement, "origin": c.origin.value, "type": c.claim_type.value,
                    "status": c.status.value, "confidence": c.confidence, "method": c.confidence_method,
                    "policy": c.sufficiency, "scope": to_dict(c.scope), "supporting": c.supporting,
                    "contradicting": c.contradicting, "issues": c.issues, "calculation_id": c.calculation_id}
                   for c in g.claims.values()],
        "contradictions": [to_dict(c) for c in g.contradictions.values()],
        "calculations": [to_dict(c) for c in g.calculations.values()],
        "lineage": [link.__dict__ for link in r.lineage],
        "unknowns": [to_dict(u) for u in r.unknowns],
        "findings": [to_dict(f) for f in r.findings],
        "cost": {"estimate": r.estimate.as_dict() if r.estimate else None,
                 "charge": r.charge.as_dict() if r.charge else None,
                 "ledger_units": r.ledger.total_units if r.ledger else 0.0},
        "stopped_reason": r.stopped_reason,
        "started_at": r.started_at,
        "finished_at": r.finished_at,
    }
    # The receipt is a record, not a view: copy it, so later changes to the run's graph, plan or ledger (which the
    # body would otherwise share lists and dicts with) can never alter an issued receipt.
    body = copy.deepcopy(body)
    body["inputs_hash"] = canonical_hash({
        "contract": body["contract_hash"],
        "evidence": sorted(e["content_hash"] for e in body["evidence"]),
        "provider": [provider.get("name"), provider.get("version")],
        "template": EXTRACTION_TEMPLATE_VERSION,
        "verifier": VERIFIER_SETTINGS,
    })
    body["state_hash"] = canonical_hash({
        "claims": sorted((c["id"], c["status"], c["confidence"]) for c in body["claims"]),
        "findings": sorted((f["question_id"], f["answer"], tuple(f["claim_ids"])) for f in body["findings"]),
        "contradictions": sorted(c["id"] for c in body["contradictions"]),
    })
    body["report_hash"] = hashlib.sha256(render_markdown(r, include_receipt=False).encode()).hexdigest()
    body["research_id"] = "RR-" + canonical_hash(body)[:20]
    return body


def verify_receipt(receipt: dict) -> bool:
    """True if the receipt has not been altered since it was issued."""
    body = {k: v for k, v in receipt.items() if k != "research_id"}
    return receipt.get("research_id") == "RR-" + canonical_hash(body)[:20]
