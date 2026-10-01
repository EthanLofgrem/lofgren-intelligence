"""knowledge-map/2: the V1 -> V2 hand-off with provenance, question associations and derivation.

knowledge-map/1 (`kernel.state.export_state`) stays frozen. /2 is a separate, stricter export of the same run:

    questions        the contract's questions and their declared direct dependencies
    claims           every claim, with origin, status, identity version and Claim.question_ids (the legacy
                     single Claim.question_id is not exported: question_ids is the authoritative association), and
                     its assessment: the factors the verifier scored and the policy thresholds it was held to, so a
                     consumer can tell a single-source shortfall from a stale or low-confidence one
    evidence         a manifest: ids, sources, content hashes, times and identity versions (not content or data)
    sources          provenance summaries, including declared derivations
    lineage          how sources derive from one another
    contradictions, unknowns (all, with status), calculations
    findings         with question_ids, derivation and claim_scopes
    receipt          the authoritative V1 receipt this map was exported from: research id, contract, inputs and
                     state hashes, and knowledge_state_hash, the receipt's commitment to every section above
    limitations      what the map cannot establish
    fingerprint      see below

Canonical form
    Objects have text keys (serialized sorted). Integral floats within +-2^53 become ints; NaN, Infinity and larger
    integers are refused. Top-level entity collections carry no order: they are sorted by id (lineage by its
    canonical JSON). Every list inside an entity keeps its order, because order there can mean something
    (Finding.claim_ids is a ranking; Claim.supporting is read first-to-last).

Fingerprint
    map      "KM2-"  + SHA-256 of the canonical map without `fingerprint`. Identifies this exact export, research id
             included; any change to the map changes it.
    content  "KM2C-" + the same, also without the per-run fields `research_id`, `receipt.research_id`,
             `receipt.knowledge_state_hash` and `sources[].retrieved_at`. Re-running identical inputs reproduces it. It does not hide real input
             differences: a source's identity includes its URI, so the same bytes read from another location are
             another source, other evidence ids and another content fingerprint.

`validate_knowledge_map` refuses a map whose schema, research id, fingerprints, dates, numbers, references or
finding derivations do not hold, and, given the receipt, one that does not match it.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
from datetime import date, datetime
from typing import TYPE_CHECKING, Any, Mapping

from ..evidence.types import (
    CLAIM_IDENTITY_SCHEMA,
    CLAIM_IDENTITY_VERSION,
    EVIDENCE_IDENTITY_SCHEMA,
    EVIDENCE_IDENTITY_VERSION,
    FINDING_DERIVATIONS,
    Claim,
    ClaimOrigin,
    ClaimStatus,
    ClaimType,
    EvidenceKind,
    Scope,
    SourceKind,
    to_dict,
)
from ..verification.policies import policy_for
from .receipt import LEGACY_RECEIPT_SCHEMAS, RECEIPT_SCHEMA, verify_receipt

if TYPE_CHECKING:
    from .pipeline import RunResult

MAP_SCHEMA = "lofgren.knowledge-map/2"
FINGERPRINT_ALGORITHM = "sha256/canonical-json-2"
MAP_PREFIX = "KM2-"
CONTENT_PREFIX = "KM2C-"
MAX_DEPTH = 32
_SAFE_INT = 2 ** 53
_RESEARCH_ID = re.compile(r"^RR-[0-9a-f]{20}$")
_HASH = re.compile(r"^[0-9a-f]{64}$")
_FINGERPRINT = {"map": re.compile(r"^KM2-[0-9a-f]{64}$"), "content": re.compile(r"^KM2C-[0-9a-f]{64}$")}

# Section -> id prefix of its entities. Lineage links have no id.
SECTIONS = {"questions": "Q", "claims": "CL", "evidence": "EV", "sources": "SRC", "contradictions": "CX",
            "unknowns": "UNK", "calculations": "CALC", "findings": "F"}
_TOP = {"schema", "research_id", "objective", "mode", "identity_versions", "evidence_standard", "receipt", *SECTIONS,
        "lineage", "limitations", "fingerprint"}
_RECEIPT_REF = ("schema", "research_id", "contract_hash", "inputs_hash", "state_hash", "knowledge_state_hash")
_FACTORS = {"quality", "independent_sources", "recency", "directness", "contradicting_sources", "raw", "calibrated"}
_REQUIREMENTS = {"name", "min_independent_sources", "min_confidence", "requires_observation"}
IDENTITY_VERSIONS = {"claim": {"1": "lofgren.claim-identity/1", str(CLAIM_IDENTITY_VERSION): CLAIM_IDENTITY_SCHEMA},
                     "evidence": {"1": "lofgren.evidence-identity/1",
                                  str(EVIDENCE_IDENTITY_VERSION): EVIDENCE_IDENTITY_SCHEMA}}
_CONFIRMED = {ClaimStatus.VERIFIED.value, ClaimStatus.PARTIALLY_VERIFIED.value}

STANDING_LIMITATIONS = (
    "The evidence manifest lists content hashes, not evidence content or adapter data: a consumer can check that "
    "evidence it holds is the evidence cited, but cannot read it from this map.",
    "Confidence is provisional unless confidence_status says calibrated.",
    "The content fingerprint leaves out research_id and source retrieved_at only. A source's identity includes its "
    "URI, so the same content read from another location is a different source with different evidence ids.",
    "The receipt's inputs_hash covers evidence content hashes but not source identity or URI: two runs can share "
    "an inputs_hash while their source and evidence ids differ. Compare the content fingerprint instead.",
    "Claim.question_ids is the authoritative question association; the legacy single question_id is not exported.",
    "Lineage lists only the derivations V1 detected or sources declared. A source with no lineage entry is not "
    "thereby independent; its independence group is exactly as V1 recorded it.",
    "A finding may use claims gathered for its own question and for the questions it directly declares in "
    "depends_on; claim_scopes records which question each claim came through. Dependencies are not transitive.",
    "The gap question's finding lists every open unknown of the run: it is the run's inventory of what is missing.",
    "Checked against its receipt, every field of this map is bound to the run: the receipt's knowledge_state_hash "
    "commits to the whole exported state. The receipt's research id is an unkeyed hash, so it proves the receipt "
    "was not altered after issue, not who issued it: the binding is only as trustworthy as the receipt (or research "
    "id) the consumer already holds. lofgren.research-receipt/1 receipts carry no commitment and cannot vouch for "
    "a knowledge-map/2.",
    "A claim's assessment records what the verifier scored and the thresholds of its policy. The skeptic pass may "
    "lower a status afterwards; the claim's issues say why. A claim added after verification has no assessment.",
)


class KnowledgeMapError(ValueError):
    """A knowledge-map/2 that is malformed, inconsistent, tampered with, or not the map of the given receipt."""

    def __init__(self, problems: list[str]) -> None:
        self.problems = list(problems)
        super().__init__("; ".join(self.problems))


# ---- canonical form ----------------------------------------------------------

def canonicalize(value: Any, where: str = "knowledge_map", _depth: int = 0) -> Any:
    """Plain JSON with one spelling per value (see the module docstring)."""
    if _depth > MAX_DEPTH:
        raise KnowledgeMapError([f"{where}: nested deeper than {MAX_DEPTH}"])
    if value is None or isinstance(value, (bool, str)):
        return value
    if isinstance(value, int):
        if abs(value) > _SAFE_INT:
            raise KnowledgeMapError([f"{where}: integer beyond +-2^53 cannot round-trip through JSON"])
        return value
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            raise KnowledgeMapError([f"{where}: {value} is not finite"])
        return int(value) if value.is_integer() and abs(value) <= _SAFE_INT else value
    if isinstance(value, (list, tuple)):
        return [canonicalize(v, f"{where}[{i}]", _depth + 1) for i, v in enumerate(value)]
    if isinstance(value, Mapping):
        out = {}
        for k, v in value.items():
            if not isinstance(k, str):
                raise KnowledgeMapError([f"{where}: object keys must be text, got {type(k).__name__}"])
            out[k] = canonicalize(v, f"{where}.{k}", _depth + 1)
        return out
    raise KnowledgeMapError([f"{where}: {type(value).__name__} is not JSON data"])


def canonical_json(data: Any) -> str:
    return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def _sorted_sections(data: dict) -> dict:
    out = dict(data)
    for key in SECTIONS:
        if isinstance(out.get(key), list) and all(isinstance(e, dict) and isinstance(e.get("id"), str)
                                                  for e in out[key]):
            out[key] = sorted(out[key], key=lambda e: e["id"])
    if isinstance(out.get("lineage"), list):
        out["lineage"] = sorted(out["lineage"], key=canonical_json)
    return out


def _digest(prefix: str, data: Any) -> str:
    return prefix + hashlib.sha256(canonical_json(data).encode("utf-8")).hexdigest()


def compute_fingerprints(data: Any) -> dict[str, str]:
    """The map and content fingerprints of a knowledge-map/2, whatever its key or collection order."""
    body = _sorted_sections(canonicalize(_parse(data)))
    body.pop("fingerprint", None)
    content = copy.deepcopy(body)
    content.pop("research_id", None)
    if isinstance(content.get("receipt"), dict):
        content["receipt"].pop("research_id", None)
        content["receipt"].pop("knowledge_state_hash", None)  # it covers retrieval times
    for s in content.get("sources", []) if isinstance(content.get("sources"), list) else []:
        if isinstance(s, dict):
            s.pop("retrieved_at", None)
    return {"algorithm": FINGERPRINT_ALGORITHM, "map": _digest(MAP_PREFIX, body),
            "content": _digest(CONTENT_PREFIX, content)}


def _reject_constant(name: str) -> Any:
    raise KnowledgeMapError([f"knowledge_map: {name} is not a finite number"])


def _parse(data: Any) -> Any:
    if isinstance(data, (bytes, bytearray)):
        data = bytes(data).decode("utf-8")
    if isinstance(data, str):
        try:
            return json.loads(data, parse_constant=_reject_constant)
        except RecursionError:
            raise KnowledgeMapError(["knowledge_map: nested too deeply"]) from None
        except json.JSONDecodeError as exc:
            raise KnowledgeMapError([f"knowledge_map: invalid JSON: {exc.msg} at {exc.pos}"]) from None
    return data


# ---- export ------------------------------------------------------------------

# The receipt-independent part of a map: what the receipt's knowledge_state_hash commits to.
_NOT_KNOWLEDGE_STATE = ("research_id", "receipt", "fingerprint")


def knowledge_state(r: "RunResult") -> dict:
    """The complete semantic state a knowledge-map/2 exports, in canonical form: every section except the research
    id, the receipt reference and the fingerprint (which depend on the receipt). `build_receipt` commits to it."""
    g = r.graph
    std = r.contract.evidence_standard

    def assessment(c) -> dict | None:
        f = r.factors.get(c.id)
        if f is None:
            return None
        pol = policy_for(c.claim_type, std.min_independent_sources, std.min_confidence)
        return {"factors": dict(f.__dict__),
                # A hypothesis is held to no evidence policy: it is never verified.
                "requirements": None if pol.name != c.sufficiency else {
                    "name": pol.name, "min_independent_sources": pol.min_independent_sources,
                    "min_confidence": pol.min_confidence, "requires_observation": pol.requires_observation}}

    def claim(c) -> dict:
        return {"id": c.id, "statement": c.statement, "origin": c.origin.value, "type": c.claim_type.value,
                "status": c.status.value, "confidence": c.confidence,
                "confidence_status": "calibrated" if r.calibrated else "provisional",
                "confidence_method": c.confidence_method, "value": c.value, "unit": c.unit,
                "polarity": c.polarity, "subject": c.subject, "scope": to_dict(c.scope), "policy": c.sufficiency,
                "supporting": list(c.supporting), "contradicting": list(c.contradicting),
                "issues": list(c.issues), "calculation_id": c.calculation_id,
                "question_ids": list(c.question_ids), "identity_version": c.identity_version,
                "assessment": assessment(c)}

    def evidence(e) -> dict:
        return {"id": e.id, "source_id": e.source_id, "kind": e.kind.value, "content_hash": e.content_hash,
                "observed_at": e.observed_at, "valid_from": e.valid_from, "valid_to": e.valid_to,
                "location": to_dict(e.location) if e.location is not None else None,
                "transformations": list(e.transformations), "identity_version": e.identity_version}

    def source(s) -> dict:
        return {"id": s.id, "kind": s.kind.value, "title": s.title, "uri": s.uri, "publisher": s.publisher,
                "published_at": s.published_at, "retrieved_at": s.retrieved_at, "license": s.license,
                "quality": s.quality, "independence_group": s.independence_group,
                "derived_from": list(s.derived_from)}

    return _sorted_sections(canonicalize({
        "schema": MAP_SCHEMA,
        "objective": r.contract.objective,
        "mode": r.contract.mode,
        "identity_versions": copy.deepcopy(IDENTITY_VERSIONS),
        "evidence_standard": {"min_independent_sources": std.min_independent_sources,
                              "min_confidence": std.min_confidence},
        "questions": [{"id": q.id, "text": q.text, "role": q.role, "stage": q.stage, "weight": q.weight,
                       "depends_on": list(q.depends_on), "stop_when": q.stop_when} for q in r.contract.questions],
        "claims": [claim(c) for c in g.claims.values()],
        "evidence": [evidence(e) for e in g.evidence.values()],
        "sources": [source(s) for s in g.sources.values()],
        "lineage": [dict(link.__dict__) for link in r.lineage],
        "contradictions": [to_dict(c) for c in g.contradictions.values()],
        "unknowns": [to_dict(u) for u in r.unknowns],
        "calculations": [to_dict(c) for c in g.calculations.values()],
        "findings": [to_dict(f) for f in r.findings],
        "limitations": list(STANDING_LIMITATIONS),
    }))


def knowledge_state_hash(data: Any) -> str:
    """SHA-256 of the canonical knowledge state: a run's (`knowledge_state(r)`) or a map's (the map without its
    research id, receipt reference and fingerprint), whatever its key or collection order."""
    state = _sorted_sections(canonicalize(_parse(data)))
    for key in _NOT_KNOWLEDGE_STATE:
        state.pop(key, None)
    return hashlib.sha256(canonical_json(state).encode("utf-8")).hexdigest()


def export_knowledge_map(r: "RunResult") -> dict:
    """The knowledge-map/2 of a finished run, in canonical form, fingerprinted."""
    receipt = r.receipt or {}
    if not receipt.get("research_id"):
        raise KnowledgeMapError(["the run has no receipt: only a finished run can be exported"])
    data = {**knowledge_state(r), "research_id": receipt["research_id"],
            "receipt": canonicalize({k: receipt.get(k) for k in _RECEIPT_REF})}
    data["fingerprint"] = compute_fingerprints(data)
    problems = knowledge_map_problems(data, receipt)
    if problems:  # an export that its own validator refuses is a V1 defect: fail closed rather than hand it on
        raise KnowledgeMapError(problems)
    return data


# ---- JSON Schema ---------------------------------------------------------------

# Required keys of each entity, for the published JSON Schema. `validate_knowledge_map` is authoritative: references,
# dates, derivations, fingerprints and the receipt binding cannot be expressed here.
ENTITY_KEYS = {
    "questions": ("id", "text", "role", "stage", "weight", "depends_on", "stop_when"),
    "claims": ("id", "statement", "origin", "type", "status", "confidence", "confidence_status", "confidence_method",
               "value", "unit", "polarity", "subject", "scope", "policy", "supporting", "contradicting", "issues",
               "calculation_id", "question_ids", "identity_version", "assessment"),
    "evidence": ("id", "source_id", "kind", "content_hash", "observed_at", "valid_from", "valid_to", "location",
                 "transformations", "identity_version"),
    "sources": ("id", "kind", "title", "uri", "publisher", "published_at", "retrieved_at", "license", "quality",
                "independence_group", "derived_from"),
    "contradictions": ("id", "claim_a", "claim_b", "reason", "kind", "scope_note", "resolution", "severity"),
    "unknowns": ("id", "description", "question_id", "capability", "source_types", "expected_gain", "est_cost_usd",
                 "needs_approval", "status"),
    "calculations": ("id", "name", "formula", "inputs", "result", "unit", "method"),
    "findings": ("id", "question_id", "question", "answer", "claim_ids", "evidence_ids", "contradiction_ids",
                 "unknown_ids", "scope", "confidence", "confidence_status", "confidence_method", "affects",
                 "next_best_evidence", "issues", "question_ids", "derivation", "claim_scopes"),
}


def json_schema() -> dict:
    """A structural JSON Schema for knowledge-map/2 (shapes only; see `validate_knowledge_map` for the rules)."""
    def entity(key: str) -> dict:
        return {"type": "array", "items": {"type": "object", "required": list(ENTITY_KEYS[key]),
                                           "additionalProperties": False,
                                           "properties": {k: {} for k in ENTITY_KEYS[key]}}}

    hex64 = {"type": "string", "pattern": "^[0-9a-f]{64}$"}
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": "https://lofgren.enterprise/schemas/knowledge-map-2.schema.json",
        "title": "KnowledgeMap2",
        "description": "lofgren.knowledge-map/2: V1 state handed to V2, with provenance, question associations and "
                       "finding derivation. Structural only: validate_knowledge_map enforces references, dates, "
                       "derivations, fingerprints and the receipt binding.",
        "type": "object",
        "required": sorted(_TOP),
        "additionalProperties": False,
        "properties": {
            "schema": {"const": MAP_SCHEMA},
            "research_id": {"type": "string", "pattern": _RESEARCH_ID.pattern},
            "objective": {"type": "string"},
            "mode": {"type": "string"},
            "identity_versions": {"const": IDENTITY_VERSIONS},
            "evidence_standard": {"type": "object", "required": ["min_independent_sources", "min_confidence"],
                                  "additionalProperties": False,
                                  "properties": {"min_independent_sources": {"type": "integer", "minimum": 1},
                                                 "min_confidence": {"type": "number", "minimum": 0, "maximum": 1}}},
            "receipt": {"type": "object", "additionalProperties": False, "required": list(_RECEIPT_REF),
                        "properties": {"schema": {"const": RECEIPT_SCHEMA},
                                       "research_id": {"type": "string", "pattern": _RESEARCH_ID.pattern},
                                       "contract_hash": hex64, "inputs_hash": hex64, "state_hash": hex64,
                                       "knowledge_state_hash": hex64}},
            **{key: entity(key) for key in SECTIONS},
            "lineage": {"type": "array", "items": {"type": "object", "additionalProperties": False,
                                                   "required": ["source", "derived_from", "relation", "detail"],
                                                   "properties": {"source": {"type": "string"},
                                                                  "derived_from": {"type": "string"},
                                                                  "relation": {"enum": ["declared", "syndicated",
                                                                                        "quotes"]},
                                                                  "detail": {"type": "string"}}}},
            "limitations": {"type": "array", "minItems": 1, "items": {"type": "string"}},
            "fingerprint": {"type": "object", "additionalProperties": False,
                            "required": ["algorithm", "map", "content"],
                            "properties": {"algorithm": {"const": FINGERPRINT_ALGORITHM},
                                           "map": {"type": "string", "pattern": _FINGERPRINT["map"].pattern},
                                           "content": {"type": "string",
                                                       "pattern": _FINGERPRINT["content"].pattern}}},
        },
    }


# ---- validation --------------------------------------------------------------

class _Check:
    def __init__(self) -> None:
        self.problems: list[str] = []

    def need(self, cond: bool, where: str, what: str) -> bool:
        if not cond:
            self.problems.append(f"{where}: {what}")
        return cond

    def text(self, v: Any, where: str, required: bool = True, nullable: bool = False) -> bool:
        if v is None and nullable:
            return True
        if not self.need(isinstance(v, str), where, "expected text"):
            return False
        return not required or self.need(bool(v.strip()), where, "must not be empty")

    def num(self, v: Any, where: str, lo: float | None = None, hi: float | None = None,
            nullable: bool = False) -> bool:
        if v is None and nullable:
            return True
        if not self.need(isinstance(v, (int, float)) and not isinstance(v, bool), where, "expected a number"):
            return False
        if lo is not None and not self.need(v >= lo, where, f"{v} is below {lo}"):
            return False
        return hi is None or self.need(v <= hi, where, f"{v} is above {hi}")

    def ident(self, v: Any, prefix: str, where: str) -> bool:
        return self.need(isinstance(v, str) and v.startswith(prefix + "-") and 2 < len(v) <= 128, where,
                         f"expected a {prefix}- id, got {v!r}")

    def ids(self, v: Any, prefix: str, where: str) -> list[str]:
        if not self.need(isinstance(v, list), where, "expected a list of ids"):
            return []
        good = [x for i, x in enumerate(v) if self.ident(x, prefix, f"{where}[{i}]")]
        self.need(len(set(good)) == len(good), where, "lists an id twice")
        return good

    def texts(self, v: Any, where: str) -> None:
        self.need(isinstance(v, list) and all(isinstance(x, str) for x in v), where, "expected a list of text")

    def when(self, v: Any, where: str) -> None:
        """An ISO 8601 date or date-time, or null."""
        if v is None:
            return
        if not self.need(isinstance(v, str) and len(v) <= 64, where, "expected an ISO 8601 date or null"):
            return
        text = v[:-1] + "+00:00" if v.endswith("Z") else v
        try:
            datetime.fromisoformat(text) if "T" in text else date.fromisoformat(text)
        except ValueError:
            self.problems.append(f"{where}: {v!r} is not an ISO 8601 date or date-time")

    def enum(self, v: Any, allowed: set[str], where: str) -> bool:
        return self.need(v in allowed, where, f"{v!r} is not one of {sorted(allowed)}")

    def scope(self, v: Any, where: str) -> None:
        if not self.need(isinstance(v, dict), where, "a scope is an object"):
            return
        self.need(set(v) == {"valid_from", "valid_to", "geography", "lat", "lon"}, where,
                  f"scope keys are valid_from, valid_to, geography, lat, lon; got {sorted(v)}")
        self.when(v.get("valid_from"), f"{where}.valid_from")
        self.when(v.get("valid_to"), f"{where}.valid_to")
        self.text(v.get("geography"), f"{where}.geography", required=False, nullable=True)
        self.num(v.get("lat"), f"{where}.lat", -90, 90, nullable=True)
        self.num(v.get("lon"), f"{where}.lon", -180, 180, nullable=True)


def knowledge_map_problems(data: Any, receipt: Mapping | None = None) -> list[str]:
    """Everything wrong with a knowledge-map/2 (empty when it is valid). With `receipt`, it must be that run's map."""
    try:
        m = _sorted_sections(canonicalize(_parse(data)))
    except KnowledgeMapError as exc:
        return exc.problems
    ck = _Check()
    w = "knowledge_map"
    if not ck.need(isinstance(m, dict), w, "a knowledge map is an object"):
        return ck.problems
    if m.get("schema") != MAP_SCHEMA:
        return [f"{w}.schema: expected {MAP_SCHEMA!r}, got {m.get('schema')!r}"]
    missing, extra = _TOP - set(m), set(m) - _TOP
    ck.need(not missing, w, f"missing keys {sorted(missing)}")
    ck.need(not extra, w, f"keys not defined by {MAP_SCHEMA}: {sorted(extra)}")
    if missing:
        return ck.problems
    rid = m["research_id"]
    ck.need(isinstance(rid, str) and bool(_RESEARCH_ID.match(rid)), f"{w}.research_id",
            "must be a V1 research id (RR-<20 hex>)")
    ck.text(m["objective"], f"{w}.objective")
    ck.text(m["mode"], f"{w}.mode")
    ck.need(m["identity_versions"] == IDENTITY_VERSIONS, f"{w}.identity_versions",
            f"expected {IDENTITY_VERSIONS}")
    std = m["evidence_standard"]
    if ck.need(isinstance(std, dict) and set(std) == {"min_independent_sources", "min_confidence"},
               f"{w}.evidence_standard", "expected {min_independent_sources, min_confidence}"):
        ck.num(std["min_independent_sources"], f"{w}.evidence_standard.min_independent_sources", 1)
        ck.num(std["min_confidence"], f"{w}.evidence_standard.min_confidence", 0, 1)
    ck.need(isinstance(m["limitations"], list) and bool(m["limitations"]) and
            all(isinstance(x, str) and x.strip() for x in m["limitations"]), f"{w}.limitations",
            "expected a non-empty list of text")
    _check_receipt_ref(ck, m["receipt"], rid, m, f"{w}.receipt")
    _check_fingerprint(ck, m, f"{w}.fingerprint")

    index: dict[str, dict[str, dict]] = {}
    for key, prefix in SECTIONS.items():
        rows = m[key]
        index[key] = {}
        if not ck.need(isinstance(rows, list), f"{w}.{key}", "expected a list"):
            continue
        for i, e in enumerate(rows):
            ew = f"{w}.{key}[{i}]"
            if not ck.need(isinstance(e, dict), ew, "expected an object") or not ck.ident(e.get("id"), prefix,
                                                                                          f"{ew}.id"):
                continue
            ck.need(e["id"] not in index[key], ew, f"duplicate id {e['id']}")
            index[key][e["id"]] = e
    if ck.problems:
        return ck.problems
    _check_entities(ck, m, index)
    ref = m["receipt"]
    if isinstance(ref, dict) and isinstance(ref.get("knowledge_state_hash"), str):
        ck.need(ref["knowledge_state_hash"] == knowledge_state_hash(m), f"{w}.receipt.knowledge_state_hash",
                "does not match the map's knowledge state")
    if receipt is not None:
        _check_against_receipt(ck, m, receipt)
    return ck.problems


def _check_receipt_ref(ck: _Check, ref: Any, rid: Any, m: dict, w: str) -> None:
    if not ck.need(isinstance(ref, dict), w, "expected an object"):
        return
    ck.need(set(ref) == set(_RECEIPT_REF), w, f"expected keys {sorted(_RECEIPT_REF)}, got {sorted(ref)}")
    if ref.get("schema") in LEGACY_RECEIPT_SCHEMAS:
        ck.problems.append(f"{w}.schema: {ref['schema']} predates the knowledge-state commitment; knowledge-map/2 "
                           f"is bound only to {RECEIPT_SCHEMA} receipts")
    else:
        ck.need(ref.get("schema") == RECEIPT_SCHEMA, f"{w}.schema", f"expected {RECEIPT_SCHEMA!r}")
    ck.need(ref.get("research_id") == rid, f"{w}.research_id", "does not match the map's research_id")
    for k in ("contract_hash", "inputs_hash", "state_hash", "knowledge_state_hash"):
        ck.need(isinstance(ref.get(k), str) and bool(_HASH.match(ref[k])), f"{w}.{k}", "expected a SHA-256 hex digest")


def _check_fingerprint(ck: _Check, m: dict, w: str) -> None:
    fp = m["fingerprint"]
    if not ck.need(isinstance(fp, dict) and set(fp) == {"algorithm", "map", "content"}, w,
                   "expected {algorithm, map, content}"):
        return
    ck.need(fp["algorithm"] == FINGERPRINT_ALGORITHM, f"{w}.algorithm", f"expected {FINGERPRINT_ALGORITHM!r}")
    for k, pattern in _FINGERPRINT.items():
        ck.need(isinstance(fp[k], str) and bool(pattern.match(fp[k])), f"{w}.{k}", "malformed fingerprint")
    actual = compute_fingerprints(m)
    for k in ("map", "content"):
        ck.need(fp[k] == actual[k], f"{w}.{k}", "does not match the map: the map was altered after export")


def _check_entities(ck: _Check, m: dict, index: dict[str, dict[str, dict]]) -> None:
    w = "knowledge_map"
    questions, claims, evidence, sources = index["questions"], index["claims"], index["evidence"], index["sources"]
    contradictions, unknowns, calculations = index["contradictions"], index["unknowns"], index["calculations"]

    def ref(v: Any, table: dict, prefix: str, where: str) -> None:
        if ck.ident(v, prefix, where):
            ck.need(v in table, where, f"{v} is not in this map")

    def refs(v: Any, table: dict, prefix: str, where: str) -> list[str]:
        good = ck.ids(v, prefix, where)
        for i, x in enumerate(good):
            ck.need(x in table, f"{where}[{i}]", f"{x} is not in this map")
        return good

    for qid, q in questions.items():
        qw = f"{w}.questions[{qid}]"
        ck.text(q.get("text"), f"{qw}.text")
        ck.text(q.get("role"), f"{qw}.role")
        ck.text(q.get("stage"), f"{qw}.stage", required=False)
        ck.text(q.get("stop_when"), f"{qw}.stop_when", required=False)
        ck.num(q.get("weight"), f"{qw}.weight", 0)
        deps = ck.ids(q.get("depends_on"), "Q", f"{qw}.depends_on")
        ck.need(qid not in deps, f"{qw}.depends_on", "a question cannot depend on itself")
        # A declared dependency outside the contract grants nothing; it is reported, not followed.
        for d in deps:
            ck.need(d in questions, f"{qw}.depends_on", f"{d} is not in this map")

    statuses = {s.value for s in ClaimStatus}
    for cid, c in claims.items():
        cw = f"{w}.claims[{cid}]"
        ck.text(c.get("statement"), f"{cw}.statement")
        ck.enum(c.get("origin"), {o.value for o in ClaimOrigin}, f"{cw}.origin")
        ck.enum(c.get("type"), {t.value for t in ClaimType}, f"{cw}.type")
        ck.enum(c.get("status"), statuses, f"{cw}.status")
        ck.need(not (c.get("origin") == ClaimOrigin.HYPOTHESIS.value and c.get("status") in _CONFIRMED),
                f"{cw}.status", "a hypothesis is never verified")
        ck.num(c.get("confidence"), f"{cw}.confidence", 0, 1)
        ck.enum(c.get("confidence_status"), {"provisional", "calibrated"}, f"{cw}.confidence_status")
        ck.num(c.get("value"), f"{cw}.value", nullable=True)
        ck.enum(c.get("polarity"), {1, -1}, f"{cw}.polarity")
        for k in ("unit", "subject", "policy", "confidence_method"):
            ck.text(c.get(k), f"{cw}.{k}", required=False)
        ck.texts(c.get("issues"), f"{cw}.issues")
        ck.scope(c.get("scope"), f"{cw}.scope")
        sup = refs(c.get("supporting"), evidence, "EV", f"{cw}.supporting")
        con = refs(c.get("contradicting"), evidence, "EV", f"{cw}.contradicting")
        ck.need(not set(sup) & set(con), cw, "evidence both supports and contradicts the claim")
        if c.get("calculation_id") is not None:
            ref(c["calculation_id"], calculations, "CALC", f"{cw}.calculation_id")
        qids = refs(c.get("question_ids"), questions, "Q", f"{cw}.question_ids")
        ck.need(qids == sorted(qids), f"{cw}.question_ids", "must be sorted")
        ck.enum(str(c.get("identity_version")), set(IDENTITY_VERSIONS["claim"]), f"{cw}.identity_version")
        _check_assessment(ck, c, f"{cw}.assessment")
        _check_claim_identity(ck, c, cw)

    for eid, e in evidence.items():
        ew = f"{w}.evidence[{eid}]"
        ref(e.get("source_id"), sources, "SRC", f"{ew}.source_id")
        ck.enum(e.get("kind"), {k.value for k in EvidenceKind}, f"{ew}.kind")
        ck.need(isinstance(e.get("content_hash"), str) and bool(_HASH.match(e["content_hash"])),
                f"{ew}.content_hash", "expected a SHA-256 hex digest")
        for k in ("observed_at", "valid_from", "valid_to"):
            ck.when(e.get(k), f"{ew}.{k}")
        loc = e.get("location")
        if loc is not None and ck.need(isinstance(loc, dict) and set(loc) == {"lat", "lon", "name"},
                                       f"{ew}.location", "expected {lat, lon, name} or null"):
            ck.num(loc["lat"], f"{ew}.location.lat", -90, 90, nullable=True)
            ck.num(loc["lon"], f"{ew}.location.lon", -180, 180, nullable=True)
        ck.texts(e.get("transformations"), f"{ew}.transformations")
        ck.enum(str(e.get("identity_version")), set(IDENTITY_VERSIONS["evidence"]), f"{ew}.identity_version")

    for sid, s in sources.items():
        sw = f"{w}.sources[{sid}]"
        ck.enum(s.get("kind"), {k.value for k in SourceKind}, f"{sw}.kind")
        ck.text(s.get("title"), f"{sw}.title")
        for k in ("uri", "publisher", "license", "independence_group"):
            ck.text(s.get(k), f"{sw}.{k}", required=False)
        ck.when(s.get("published_at"), f"{sw}.published_at")
        ck.when(s.get("retrieved_at"), f"{sw}.retrieved_at")
        ck.num(s.get("quality"), f"{sw}.quality", 0, 1)
        derived = refs(s.get("derived_from"), sources, "SRC", f"{sw}.derived_from")
        ck.need(sid not in derived, f"{sw}.derived_from", "a source cannot derive from itself")

    lineage = m["lineage"]
    if ck.need(isinstance(lineage, list), f"{w}.lineage", "expected a list"):
        for i, link in enumerate(lineage):
            lw = f"{w}.lineage[{i}]"
            if not ck.need(isinstance(link, dict) and set(link) == {"source", "derived_from", "relation", "detail"},
                           lw, "expected {source, derived_from, relation, detail}"):
                continue
            ref(link["source"], sources, "SRC", f"{lw}.source")
            ref(link["derived_from"], sources, "SRC", f"{lw}.derived_from")
            ck.enum(link["relation"], {"declared", "syndicated", "quotes"}, f"{lw}.relation")
            ck.text(link["detail"], f"{lw}.detail", required=False)

    for xid, x in contradictions.items():
        xw = f"{w}.contradictions[{xid}]"
        ref(x.get("claim_a"), claims, "CL", f"{xw}.claim_a")
        ref(x.get("claim_b"), claims, "CL", f"{xw}.claim_b")
        ck.need(x.get("claim_a") != x.get("claim_b"), xw, "a claim cannot contradict itself")
        ck.enum(x.get("kind"), {"incompatible", "scope_mismatch"}, f"{xw}.kind")
        ck.num(x.get("severity"), f"{xw}.severity", 0, 1)
        for k in ("reason", "scope_note", "resolution"):
            ck.text(x.get(k), f"{xw}.{k}", required=False)

    for uid, u in unknowns.items():
        uw = f"{w}.unknowns[{uid}]"
        ck.text(u.get("description"), f"{uw}.description")
        if u.get("question_id") is not None:
            ref(u["question_id"], questions, "Q", f"{uw}.question_id")
        ck.text(u.get("capability"), f"{uw}.capability", required=False)
        ck.texts(u.get("source_types"), f"{uw}.source_types")
        ck.num(u.get("expected_gain"), f"{uw}.expected_gain", 0, 1)
        ck.num(u.get("est_cost_usd"), f"{uw}.est_cost_usd", 0)
        ck.need(isinstance(u.get("needs_approval"), bool), f"{uw}.needs_approval", "expected true or false")
        ck.text(u.get("status"), f"{uw}.status")

    for kid, k in calculations.items():
        kw = f"{w}.calculations[{kid}]"
        for f in ("name", "formula"):
            ck.text(k.get(f), f"{kw}.{f}")
        for f in ("unit", "method"):
            ck.text(k.get(f), f"{kw}.{f}", required=False)
        ck.num(k.get("result"), f"{kw}.result")
        if ck.need(isinstance(k.get("inputs"), list), f"{kw}.inputs", "expected a list"):
            for i, item in enumerate(k["inputs"]):
                if ck.need(isinstance(item, dict), f"{kw}.inputs[{i}]", "expected an object") and \
                        item.get("evidence_id") is not None:
                    ref(item["evidence_id"], evidence, "EV", f"{kw}.inputs[{i}].evidence_id")

    for fid, f in index["findings"].items():
        _check_finding(ck, f, f"{w}.findings[{fid}]", questions, claims, evidence, contradictions, unknowns, refs)


def _check_claim_identity(ck: _Check, c: dict, cw: str) -> None:
    """A claim's id must be the id of what the record says: statement, subject, value, unit, polarity and scope
    under identity version 2 (statement only under version 1). The id is bound to the receipt, so these fields are
    too."""
    if ck.problems and any(p.startswith(cw) for p in ck.problems):
        return  # already malformed; an identity error would only repeat it
    try:
        expected = Claim(c["statement"], value=c["value"], unit=c["unit"], polarity=c["polarity"],
                         subject=c["subject"], scope=Scope(**c["scope"]), identity_version=c["identity_version"]).id
    except (TypeError, ValueError) as exc:
        ck.problems.append(f"{cw}: cannot recompute the claim id: {exc}")
        return
    ck.need(expected == c["id"], f"{cw}.id", f"does not match the claim it describes (expected {expected})")


def _check_assessment(ck: _Check, c: dict, aw: str) -> None:
    a = c.get("assessment")
    if a is None or not ck.need(isinstance(a, dict) and set(a) == {"factors", "requirements"}, aw,
                                "expected {factors, requirements} or null"):
        return
    f = a["factors"]
    if ck.need(isinstance(f, dict) and set(f) == _FACTORS, f"{aw}.factors", f"expected {sorted(_FACTORS)}"):
        for k in ("quality", "recency", "directness", "raw", "calibrated"):
            ck.num(f[k], f"{aw}.factors.{k}", 0, 1)
        for k in ("independent_sources", "contradicting_sources"):
            ck.need(isinstance(f[k], int) and not isinstance(f[k], bool) and f[k] >= 0, f"{aw}.factors.{k}",
                    "expected a count")
    req = a["requirements"]
    if req is None:
        ck.need(c.get("origin") == ClaimOrigin.HYPOTHESIS.value, f"{aw}.requirements",
                "only a hypothesis is held to no evidence policy")
    elif ck.need(isinstance(req, dict) and set(req) == _REQUIREMENTS, f"{aw}.requirements",
                 f"expected {sorted(_REQUIREMENTS)} or null"):
        ck.need(req["name"] == c.get("policy"), f"{aw}.requirements.name", "is not the claim's policy")
        ck.num(req["min_independent_sources"], f"{aw}.requirements.min_independent_sources", 1)
        ck.num(req["min_confidence"], f"{aw}.requirements.min_confidence", 0, 1)
        ck.need(isinstance(req["requires_observation"], bool), f"{aw}.requirements.requires_observation",
                "expected true or false")


def _check_finding(ck: _Check, f: dict, fw: str, questions: dict, claims: dict, evidence: dict,
                   contradictions: dict, unknowns: dict, refs) -> None:
    ck.text(f.get("question"), f"{fw}.question")
    ck.text(f.get("answer"), f"{fw}.answer")
    ck.num(f.get("confidence"), f"{fw}.confidence", 0, 1)
    ck.enum(f.get("confidence_status"), {"provisional", "calibrated"}, f"{fw}.confidence_status")
    for k in ("confidence_method", "next_best_evidence"):
        ck.text(f.get(k), f"{fw}.{k}", required=False)
    ck.texts(f.get("affects"), f"{fw}.affects")
    ck.texts(f.get("issues"), f"{fw}.issues")
    ck.scope(f.get("scope"), f"{fw}.scope")
    qid = f.get("question_id")
    if not ck.ident(qid, "Q", f"{fw}.question_id") or not ck.need(qid in questions, f"{fw}.question_id",
                                                                  f"{qid} is not in this map"):
        return
    claim_ids = refs(f.get("claim_ids"), claims, "CL", f"{fw}.claim_ids")
    evidence_ids = refs(f.get("evidence_ids"), evidence, "EV", f"{fw}.evidence_ids")
    cx_ids = refs(f.get("contradiction_ids"), contradictions, "CX", f"{fw}.contradiction_ids")
    refs(f.get("unknown_ids"), unknowns, "UNK", f"{fw}.unknown_ids")
    qids = refs(f.get("question_ids"), questions, "Q", f"{fw}.question_ids")
    ck.need(qids == sorted(qids) and qid in qids, f"{fw}.question_ids", "must be sorted and include question_id")
    derivation = f.get("derivation")
    if not ck.enum(derivation, set(FINDING_DERIVATIONS), f"{fw}.derivation"):
        return
    scopes = f.get("claim_scopes")
    if not ck.need(isinstance(scopes, dict), f"{fw}.claim_scopes", "expected an object"):
        return
    # The questions this finding may draw on: the named ones for a synthesis, otherwise its own question and the
    # questions it directly declares in depends_on (never transitively).
    if derivation == "synthesis":
        allowed = set(qids)
        ck.need(len(qids) >= 2, f"{fw}.question_ids", "a synthesis names at least two questions")
    else:
        allowed = {qid} | (set(questions[qid].get("depends_on") or []) & set(questions))
        ck.need(set(qids) <= allowed, f"{fw}.question_ids", f"names {sorted(set(qids) - allowed)} without a "
                                                             "declared dependency")
    if derivation == "direct":
        ck.need(qids == [qid] and scopes == {}, fw, "a direct finding names only its own question and no scopes")
    else:
        ck.need(set(scopes) == set(claim_ids), f"{fw}.claim_scopes", "a derived finding scopes every claim it uses")
    for cid in claim_ids:
        c = claims.get(cid)
        if c is None:
            continue
        via = scopes.get(cid, [qid])
        if not ck.need(isinstance(via, list) and bool(via) and all(isinstance(x, str) for x in via),
                       f"{fw}.claim_scopes[{cid}]", "expected a non-empty list of question ids"):
            continue
        ck.need(set(via) <= set(qids), f"{fw}.claim_scopes[{cid}]", "names a question the finding does not")
        ck.need(set(via) <= set(c.get("question_ids") or []), f"{fw}.claim_scopes[{cid}]",
                f"says {cid} came through {via}, but it was gathered for {c.get('question_ids')}")
        ck.need(bool(set(c.get("question_ids") or []) & allowed), f"{fw}.claim_ids",
                f"{cid} was gathered for {c.get('question_ids')}, outside the questions this finding may use")
    for xid in cx_ids:
        x = contradictions.get(xid)
        if x is None:
            continue
        ends = [claims.get(x.get("claim_a")), claims.get(x.get("claim_b"))]
        ck.need(any(e is not None and set(e.get("question_ids") or []) & allowed for e in ends),
                f"{fw}.contradiction_ids", f"{xid} lies between claims outside the questions this finding may use")
    cited = {e for cid in claim_ids if cid in claims for e in claims[cid].get("supporting") or []}
    ck.need(set(evidence_ids) <= cited, f"{fw}.evidence_ids", "lists evidence none of its claims rests on")


def _check_against_receipt(ck: _Check, m: dict, receipt: Mapping) -> None:
    w = "knowledge_map.receipt"
    if not ck.need(isinstance(receipt, Mapping) and verify_receipt(dict(receipt)), w,
                   "the receipt given is not intact"):
        return
    if receipt.get("schema") != RECEIPT_SCHEMA:
        ck.problems.append(f"{w}: a {receipt.get('schema')} receipt carries no knowledge_state_hash, so it cannot "
                           f"vouch for a knowledge-map/2; only {RECEIPT_SCHEMA} receipts can")
        return
    for k in _RECEIPT_REF:
        ck.need(m["receipt"].get(k) == receipt.get(k), f"{w}.{k}", "does not match the receipt")
    # The authoritative commitment: the receipt's hash of the complete knowledge state, recomputed from this map.
    ck.need(knowledge_state_hash(m) == receipt.get("knowledge_state_hash"), "knowledge_map",
            "its knowledge state is not the state the receipt committed to")
    ck.need(m["research_id"] == receipt.get("research_id"), "knowledge_map.research_id",
            "is not the research id of the receipt: this is another run's map")
    ck.need(m["objective"] == receipt.get("objective"), "knowledge_map.objective", "does not match the receipt")

    def by_id(rows: Any) -> dict:
        return {r["id"]: r for r in canonicalize(rows or []) if isinstance(r, dict) and "id" in r}

    def same(section: str, rows: Any, fields: dict[str, str] | None) -> None:
        """Every entity of `section` must be in the receipt with the same values. `fields` maps map field ->
        receipt field; None compares whole records."""
        theirs, ours = by_id(rows), {e["id"]: e for e in m[section]}
        if not ck.need(set(theirs) == set(ours), f"knowledge_map.{section}",
                       f"ids differ from the receipt (only in map: {sorted(set(ours) - set(theirs))[:3]}, only in "
                       f"receipt: {sorted(set(theirs) - set(ours))[:3]})"):
            return
        for i, e in ours.items():
            r = theirs[i]
            diff = sorted(k for k in e if e[k] != r.get(k)) if fields is None else                 sorted(k for k, rk in fields.items() if e.get(k) != r.get(rk))
            if fields is None and set(r) != set(e):
                diff = sorted(set(diff) | (set(r) ^ set(e)))
            ck.need(not diff, f"knowledge_map.{section}[{i}]", f"{', '.join(diff)} differ from the receipt")

    contract = canonicalize(receipt.get("contract") or {})
    ck.need(m["mode"] == contract.get("mode"), "knowledge_map.mode", "does not match the receipt's contract")
    ck.need(m["evidence_standard"] == contract.get("evidence_standard"), "knowledge_map.evidence_standard",
            "does not match the receipt's contract")
    same("questions", contract.get("questions"), {k: k for k in ENTITY_KEYS["questions"]})
    same("claims", receipt.get("claims"), {
        "statement": "statement", "origin": "origin", "type": "type", "status": "status", "confidence": "confidence",
        "confidence_method": "method", "policy": "policy", "scope": "scope", "supporting": "supporting",
        "contradicting": "contradicting", "issues": "issues", "calculation_id": "calculation_id"})
    calibrated = bool((receipt.get("verifier") or {}).get("calibrated"))
    for c in m["claims"]:
        ck.need(c["confidence_status"] == ("calibrated" if calibrated else "provisional"),
                f"knowledge_map.claims[{c['id']}].confidence_status", "does not match the receipt's verifier")
    same("evidence", receipt.get("evidence"), {k: k for k in ("source_id", "kind", "content_hash", "observed_at",
                                                              "valid_from", "valid_to")})
    same("sources", receipt.get("sources"), {k: k for k in ("kind", "title", "uri", "publisher", "published_at",
                                                            "retrieved_at", "license", "independence_group",
                                                            "derived_from")})
    for section in ("contradictions", "calculations", "unknowns", "findings"):
        same(section, receipt.get(section), None)
    ck.need(sorted(canonicalize(receipt.get("lineage") or []), key=canonical_json) == m["lineage"],
            "knowledge_map.lineage", "differs from the receipt")


def validate_knowledge_map(data: Any, receipt: Mapping | None = None) -> dict:
    """The canonical form of a valid knowledge-map/2. Raises KnowledgeMapError listing every problem."""
    problems = knowledge_map_problems(data, receipt)
    if problems:
        raise KnowledgeMapError(problems)
    return _sorted_sections(canonicalize(_parse(data)))
