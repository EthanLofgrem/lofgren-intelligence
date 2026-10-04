"""Discovery ledger and receipt (`lofgren.discovery-receipt/1`).

The ledger records every discovery operation with its kind, actor, work units and result count; the receipt total
is the sum of its entries. Kinds: search, retrieval, verification, model, hypothesis_generation, simulation,
optimization, tool, external_api, licensed_data, compute.

The receipt records what determined the result: the objective; the V1 evidence it rests on (research id, the
knowledge map's schema, assurance level and fingerprints, and, for a receipt-bound knowledge-map/2, the V1
receipt's state, inputs and knowledge-state hashes); the config and its hash; algorithm and model versions; the
provider and prior-art provider; seeds; the decision rule and outcome; a digest of every discovery object; the
ledger; and timestamps.

    discovery_fingerprint  "DFP-" + SHA-256 of everything above except timestamps. Identical inputs reproduce it.
    discovery_id           "DR-" + the first 20 hex of SHA-256 of the whole receipt. Any change to any field
                           breaks `verify_discovery_receipt`.

Object digests leave out each object's `created_at` and a simulation's measured `runtime_s`: they record when, and
how fast, not what.
"""

from __future__ import annotations

import hashlib
from typing import Any, Iterable, Mapping

from ..kernel.knowledge_map import canonical_json, canonicalize
from ..kernel.ledger import CostLedger
from .context import DiscoveryContext
from .errors import MalformedInput, ReceiptTampered
from .types import _Obj

RECEIPT_SCHEMA = "lofgren.discovery-receipt/1"
LEDGER_KINDS = ("search", "retrieval", "verification", "model", "hypothesis_generation", "simulation",
                "optimization", "tool", "external_api", "licensed_data", "compute")
_UNTIMED = ("started_at", "finished_at")
_VOLATILE_FIELDS = ("created_at", "runtime_s")


class DiscoveryLedger(CostLedger):
    """A cost ledger that accepts only discovery operation kinds and never negative work or cost."""

    def record(self, stage: str, kind: str, actor: str, work_units: float = 0.0, external_usd: float = 0.0,
               detail: str = "", evidence_items: int = 0):
        if kind not in LEDGER_KINDS:
            raise MalformedInput(f"ledger kind {kind!r} is not one of {LEDGER_KINDS}", "DiscoveryLedger.record")
        if work_units < 0 or external_usd < 0:
            raise MalformedInput("work units and costs are never negative", "DiscoveryLedger.record")
        return super().record(stage, kind, actor, work_units, external_usd, detail, evidence_items)


def _sha256(data: Any) -> str:
    return hashlib.sha256(canonical_json(canonicalize(data)).encode("utf-8")).hexdigest()


def object_digest(obj: _Obj) -> str:
    content = {k: v for k, v in obj.to_dict().items() if k not in _VOLATILE_FIELDS}
    return _sha256(content)


def evidence_fingerprint(context: DiscoveryContext) -> dict:
    """What V1 evidence a discovery rests on, as precisely as the context can vouch for it."""
    receipt = context.receipt or {}
    return {"research_id": context.research_id, "knowledge_map_schema": context.schema,
            "assurance": context.assurance.level.value, "knowledge_map_fingerprint": context.knowledge_map_fingerprint,
            "content_fingerprint": context.content_fingerprint, "state_hash": receipt.get("state_hash"),
            "inputs_hash": receipt.get("inputs_hash"), "knowledge_state_hash": receipt.get("knowledge_state_hash")}


def build_discovery_receipt(*, objective: str, context: DiscoveryContext, config: Mapping, algorithms: Mapping,
                            provider: Mapping, prior_art_provider: Mapping, seeds: Mapping, decision_rule: str,
                            outcome: str, selected_candidate_id: str | None, objects: Iterable[_Obj],
                            ledger: DiscoveryLedger, verifier: Mapping, notes: Iterable[str], started_at: str,
                            finished_at: str) -> dict:
    objects = sorted(objects, key=lambda o: o.id)
    ids = [o.id for o in objects]
    if len(ids) != len(set(ids)):
        raise MalformedInput("an object appears twice in the receipt", "build_discovery_receipt")
    config = canonicalize(dict(config))
    body = {
        "schema": RECEIPT_SCHEMA,
        "objective": objective,
        "evidence": evidence_fingerprint(context),
        "config": config,
        "config_hash": _sha256(config),
        "algorithms": canonicalize(dict(algorithms)),
        "provider": canonicalize(dict(provider)),
        "prior_art_provider": canonicalize(dict(prior_art_provider)),
        "seeds": canonicalize(dict(seeds)),
        "decision_rule": decision_rule,
        "outcome": outcome,
        "selected_candidate_id": selected_candidate_id,
        "objects": {o.id: {"type": type(o).__name__, "digest": object_digest(o)} for o in objects},
        "ledger": canonicalize(ledger.to_json()),
        "verifier": canonicalize(dict(verifier)),
        "notes": sorted(set(notes)),
        "started_at": started_at,
        "finished_at": finished_at,
    }
    body["discovery_fingerprint"] = "DFP-" + _sha256({k: v for k, v in body.items() if k not in _UNTIMED})
    body["discovery_id"] = "DR-" + _sha256(body)[:20]
    return body


def discovery_fingerprint(receipt: Mapping) -> str:
    body = {k: v for k, v in receipt.items() if k not in (*_UNTIMED, "discovery_fingerprint", "discovery_id")}
    return "DFP-" + _sha256(body)


def verify_discovery_receipt(receipt: Any) -> bool:
    """True only if the receipt is well formed and no field changed since it was issued."""
    if not isinstance(receipt, Mapping) or receipt.get("schema") != RECEIPT_SCHEMA:
        return False
    try:
        if receipt.get("discovery_fingerprint") != discovery_fingerprint(receipt):
            return False
        body = {k: v for k, v in receipt.items() if k != "discovery_id"}
        if receipt.get("discovery_id") != "DR-" + _sha256(body)[:20]:
            return False
        ledger = receipt["ledger"]
        total = round(sum(e["usd"] for e in ledger["entries"]), 6)
        return abs(total - ledger["total_usd"]) <= 1e-6
    except (KeyError, TypeError, ValueError, MalformedInput):
        return False


def check_discovery_receipt(receipt: Any) -> Mapping:
    if not verify_discovery_receipt(receipt):
        raise ReceiptTampered("the discovery receipt is malformed or was altered after it was issued",
                              "discovery_receipt")
    return receipt


def objects_match_receipt(receipt: Mapping, objects: Iterable[_Obj]) -> list[str]:
    """Problems between a receipt and the objects it claims to record (missing, extra or changed)."""
    recorded = receipt.get("objects", {})
    have = {o.id: o for o in objects}
    problems = [f"{oid} is recorded but missing" for oid in sorted(set(recorded) - set(have))]
    problems += [f"{oid} is not recorded" for oid in sorted(set(have) - set(recorded))]
    problems += [f"{oid} changed after the receipt was issued" for oid in sorted(set(recorded) & set(have))
                 if recorded[oid]["digest"] != object_digest(have[oid])]
    return problems
