"""Research receipts: every run can show exactly how its conclusions were made.

A receipt records the contract, the plan, every operation, every source and
evidence hash, the claims and their policies, contradictions, calculations,
lineage, unknowns, findings, model/provider versions, verification settings,
cost and timestamps. Its research ID is the hash of all of that.

Two hashes support reproducibility:
  inputs_hash  contract + evidence content + provider/version + verifier settings
  state_hash   the resulting findings and claim statuses
Re-running with the same inputs_hash must produce the same state_hash.

A third commits the receipt to the run's complete knowledge state (receipt version 2):
  knowledge_state_hash  SHA-256 of the canonical knowledge state that knowledge-map/2 exports (claims with their
                        question associations and assessments, evidence, sources, lineage, contradictions,
                        unknowns, calculations, findings, questions; kernel/knowledge_map.knowledge_state)
It is per run, not reproducible (it covers retrieval times). A knowledge-map/2 is bound to a receipt through it,
so editing any exported field and recomputing the map's own fingerprints still fails against the receipt.

Versions. lofgren.research-receipt/1 receipts (no knowledge_state_hash) keep their meaning and still verify with
verify_receipt; they can never vouch for a knowledge-map/2, which refuses them explicitly. The research id is an
unkeyed hash: it shows that a receipt was not altered after it was issued, not who issued it, so binding is
relative to a receipt (or research id) the consumer already trusts.
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

RECEIPT_SCHEMA = "lofgren.research-receipt/2"
# Still verifiable with verify_receipt, but carrying no knowledge-state commitment.
LEGACY_RECEIPT_SCHEMAS = frozenset({"lofgren.research-receipt/1"})
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


# Provenance labels that public-source adapters (Europe PMC, ClinicalTrials.gov, the operator source manifest)
# put on their evidence. They limit what the evidence may be used for (an abstract is not the full text; a
# registered trial is not evidence of efficacy; posted results were not fetched), so a receipt and a report
# carry them with the evidence instead of leaving them only in the run's graph.
PROVENANCE_LABEL_KEYS = ("source_class", "text_scope", "full_text_fetched", "evidence_label", "results_label",
                         "results_posted", "results_fetched", "selection")


def evidence_labels(data: Any) -> dict:
    """The provenance labels present on one evidence item's data (empty for unlabelled evidence)."""
    if not isinstance(data, dict):
        return {}
    return {k: data[k] for k in PROVENANCE_LABEL_KEYS if k in data}


# Dates a public-source record carries besides the evidence's own observed_at (a registration has no single
# observation date; its registry dates and the retrieval time are what date it).
PROVENANCE_DATE_KEYS = ("publication_date", "start_date", "primary_completion_date", "completion_date")


def evidence_dates(e: Any) -> dict:
    """observed_at, the record's own dates and its retrieval time, where present."""
    data = e.data if isinstance(e.data, dict) else {}
    dates = {"observed_at": e.observed_at} if e.observed_at else {}
    dates.update({k: data[k] for k in PROVENANCE_DATE_KEYS if data.get(k)})
    retrieval = data.get("retrieval") if isinstance(data.get("retrieval"), dict) else {}
    retrieved = retrieval.get("retrieved_at") or data.get("retrieved_at")
    if retrieved:
        dates["retrieved_at"] = retrieved
    return dates


def _receipt_evidence(e: Any) -> dict:
    entry = {"id": e.id, "source_id": e.source_id, "kind": e.kind.value, "content_hash": e.content_hash,
             "observed_at": e.observed_at, "valid_from": e.valid_from, "valid_to": e.valid_to}
    labels = evidence_labels(e.data)
    if labels:
        # Public-source evidence: the labels and the passage itself (public text; content_hash commits to it).
        entry["labels"] = labels
        entry["dates"] = evidence_dates(e)
        entry["passage"] = e.content
    return entry


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
        "evidence": [_receipt_evidence(e) for e in g.evidence.values()],
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
    from .knowledge_map import knowledge_state, knowledge_state_hash  # late import: it imports this module

    body["knowledge_state_hash"] = knowledge_state_hash(knowledge_state(r))
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
