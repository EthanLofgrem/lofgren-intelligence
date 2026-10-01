"""Immutable V1 knowledge-map boundary for Discovery Intelligence.

V2 reads V1 only through `kernel.state.export_state` (schema `lofgren.knowledge-map/1`). A
`DiscoveryContext` holds exactly one such map: validated, canonicalized, fingerprinted and frozen.

Reference integrity
    `resolve` / `resolve_many` look up V1 ids (CL, CX, UNK, CALC, F) in this map only. A dangling
    id raises `UnknownReference`; a V2 id offered as V1 knowledge raises `PromotionRefused`.
    knowledge-map/1 exports evidence ids on claims but not evidence or source objects, so EV ids
    can only be confirmed as attachments (`evidence_claims`), never resolved as provenance.

    `register` admits a V2 object only when every reference it makes resolves in this map with the
    right kind, or to a V2 object registered in this same context, and when the V1 facts it
    restates still match the map. Otherwise it fails closed (`UnknownReference`, `MalformedInput`,
    `PromotionRefused`, `ContextMismatch`).

Fingerprint
    `knowledge_map_fingerprint` = "KMF-" + SHA-256 of the canonical JSON of the map (rules in
    `canonicalize` / `canonical_map`). It identifies the exported map this run consumed. It is NOT
    V1's receipt `state_hash` and proves nothing about V1's internal evidence state.

Compatibility rule for knowledge-map/1
    Only the schema string "lofgren.knowledge-map/1" is accepted; any other version is refused until
    V2 supports it. Within /1 an exporter may add fields (additive compatibility): V2 requires the
    fields it reads (`REQUIRED_FIELDS`), type-checks every field it recognizes, ignores the rest and
    lists them in `limitations`. Ignored fields are still covered by the fingerprint. Recognized
    optional fields that are absent take the documented `OPTIONAL_DEFAULTS`, and every default used
    is listed in `limitations` too.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
from dataclasses import dataclass, fields
from types import MappingProxyType
from typing import Any, Iterator, Mapping

from ..evidence.types import Scope
from .errors import (
    ContextMismatch,
    DuplicateId,
    InputTooLarge,
    MalformedInput,
    NonFiniteValue,
    PromotionRefused,
    UnitMismatch,
    UnknownReference,
)
from .expr import Unit
from .types import (
    Assumption,
    Candidate,
    Constraint,
    CounterHypothesis,
    DiscoveryObjective,
    EvidenceRequirement,
    Gap,
    Hypothesis,
    KnownFact,
    MissingEvidence,
    PriorArt,
    PriorArtAssessment,
    ProblemFrame,
    Uncertainty,
    V2_IDEA_PREFIXES,
    _Obj,
    scope as check_scope,
)

SUPPORTED_SCHEMA = "lofgren.knowledge-map/1"
FINGERPRINT_PREFIX = "KMF-"
FINGERPRINT_ALGORITHM = "sha256/canonical-json-1"
MAX_MAP_BYTES = 20_000_000
MAX_DEPTH = 32
_SAFE_INT = 2 ** 53
_RESEARCH_ID = re.compile(r"^RR-[0-9a-f]{20}$")

# Section -> id prefix. Entity collections carry no order; the canonical form sorts them by id.
SECTION_PREFIXES = {"questions": "Q", "known": "CL", "uncertain": "CL", "contradicted": "CL",
                    "contradictions": "CX", "unknowns": "UNK", "calculations": "CALC", "findings": "F"}
# What `resolve` exposes (the V1 kinds V2 may cite).
_REFERENCE_SECTIONS = {"CL": ("known", "uncertain", "contradicted"), "CX": ("contradictions",),
                       "UNK": ("unknowns",), "CALC": ("calculations",), "F": ("findings",)}
_TOP_REQUIRED = {"schema", "research_id", "objective", "mode", *SECTION_PREFIXES}
_TOP_KNOWN = _TOP_REQUIRED | {"rule"}

_CLAIM_REQUIRED = {"id", "statement", "status", "confidence", "scope", "evidence"}
_CLAIM_OPTIONAL = {"type": None, "confidence_status": "provisional", "value": None, "unit": "", "policy": "",
                   "issues": [], "calculation_id": None}
REQUIRED_FIELDS = {
    "questions": {"id"}, "known": _CLAIM_REQUIRED, "uncertain": _CLAIM_REQUIRED,
    "contradicted": _CLAIM_REQUIRED, "contradictions": {"id", "claim_a", "claim_b"},
    "unknowns": {"id", "description"}, "calculations": {"id"}, "findings": {"id"},
}
OPTIONAL_DEFAULTS = {
    "questions": {"text": "", "role": "", "stage": "", "depends_on": []},
    "known": _CLAIM_OPTIONAL, "uncertain": _CLAIM_OPTIONAL,
    "contradicted": {**_CLAIM_OPTIONAL, "contradictions": []},
    # An unexported kind is treated as a conflict: surfacing a possible conflict is the safe side.
    "contradictions": {"kind": "incompatible", "reason": "", "scope_note": "", "resolution": "", "severity": 1.0},
    "unknowns": {"question_id": None, "capability": "", "source_types": [], "expected_gain": 0.0,
                 "est_cost_usd": 0.0, "needs_approval": False, "status": "open"},
    "calculations": {"name": "", "formula": "", "inputs": [], "result": None, "unit": "", "method": ""},
    "findings": {"question_id": None, "question": "", "answer": "", "claim_ids": [], "evidence_ids": [],
                 "contradiction_ids": [], "unknown_ids": [], "scope": None, "confidence": None,
                 "confidence_status": "provisional", "confidence_method": "", "affects": [],
                 "next_best_evidence": "", "issues": []},
}
CLAIM_STATUSES = {"known": {"verified"}, "uncertain": {"partially_verified", "supported", "insufficient_evidence"},
                  "contradicted": {"contested"}}
V1_KIND_NAMES = {"CL": "claim", "CX": "contradiction", "UNK": "unknown", "CALC": "calculation", "F": "finding",
                 "Q": "question", "EV": "evidence"}

STANDING_LIMITATIONS = (
    "knowledge_map_fingerprint identifies the exported knowledge-map/1 consumed by this run; it is not V1's "
    "receipt state_hash and does not prove equivalence to V1's internal evidence state.",
    "knowledge-map/1 lists evidence ids per claim but not the evidence or source records: provenance, source "
    "independence, lineage and licences of EV ids cannot be inspected from V2.",
    "knowledge-map/1 does not say why a claim fell short of its policy beyond its status and issues, so a "
    "single-source shortfall cannot be told apart from other policy shortfalls.",
)


class NonFiniteMapValue(NonFiniteValue, MalformedInput):
    """NaN or Infinity in a knowledge map: both a non-finite value and a malformed map."""


# ---- canonical form ----------------------------------------------------------

def canonicalize(value: Any, where: str = "knowledge_map", _depth: int = 0) -> Any:
    """Plain JSON with one spelling per value.

    - objects: text keys only (sorted when serialized); arrays keep their order
    - numbers: NaN/Infinity rejected; a float with an integral value within ±2^53 becomes an int
      (14000000.0 and 14000000 are the same); larger integers are rejected as unrepresentable
    - only null, booleans, numbers, text, arrays and objects are accepted
    """
    if _depth > MAX_DEPTH:
        raise InputTooLarge(f"nested deeper than {MAX_DEPTH}", where)
    if value is None or isinstance(value, (bool, str)):
        return value
    if isinstance(value, int):
        if abs(value) > _SAFE_INT:
            raise InputTooLarge("integer beyond ±2^53 cannot round-trip through JSON numbers", where)
        return value
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            raise NonFiniteMapValue(f"{value} is not finite", where)
        if value.is_integer() and abs(value) <= _SAFE_INT:
            return int(value)
        return value
    if isinstance(value, (list, tuple)):
        return [canonicalize(v, f"{where}[{i}]", _depth + 1) for i, v in enumerate(value)]
    if isinstance(value, Mapping):
        out = {}
        for k, v in value.items():
            if not isinstance(k, str):
                raise MalformedInput(f"object keys must be text, got {type(k).__name__}", where)
            out[k] = canonicalize(v, f"{where}.{k}", _depth + 1)
        return out
    raise MalformedInput(f"{type(value).__name__} is not JSON data", where)


def _reject_constant(name: str) -> Any:
    raise NonFiniteMapValue(f"{name} is not a finite number", "knowledge_map")


def _parse(knowledge_map: Any) -> Any:
    if isinstance(knowledge_map, (bytes, bytearray)):
        knowledge_map = bytes(knowledge_map).decode("utf-8")
    if isinstance(knowledge_map, str):
        if len(knowledge_map.encode("utf-8")) > MAX_MAP_BYTES:
            raise InputTooLarge(f"knowledge map larger than {MAX_MAP_BYTES} bytes", "knowledge_map")
        try:
            return json.loads(knowledge_map, parse_constant=_reject_constant)
        except RecursionError:
            raise InputTooLarge("knowledge map nested too deeply", "knowledge_map") from None
        except json.JSONDecodeError as exc:
            raise MalformedInput(f"invalid JSON: {exc.msg}", f"knowledge_map[{exc.pos}]") from None
    return knowledge_map


def canonical_map(knowledge_map: Any) -> dict:
    """Validate a knowledge-map/1 and return its canonical form (entity collections sorted by id)."""
    data = canonicalize(_parse(knowledge_map))
    _validate(data)
    for key in SECTION_PREFIXES:
        data[key] = sorted(data[key], key=lambda e: e["id"])
    return data


def canonical_json(data: Any) -> str:
    return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def knowledge_map_fingerprint(knowledge_map: Any) -> str:
    text = canonical_json(canonical_map(knowledge_map))
    return FINGERPRINT_PREFIX + hashlib.sha256(text.encode("utf-8")).hexdigest()


# ---- validation --------------------------------------------------------------

def _need(cond: bool, what: str, where: str) -> None:
    if not cond:
        raise MalformedInput(what, where)


def _str(value: Any, where: str, required: bool = True) -> None:
    _need(isinstance(value, str), "expected text", where)
    if required:
        _need(bool(value.strip()), "must not be empty", where)


def _id(value: Any, prefix: str, where: str) -> None:
    _need(isinstance(value, str) and value.startswith(prefix + "-") and len(value) <= 128,
          f"expected a {prefix}- id, got {value!r}", where)


def _ids(value: Any, prefix: str, where: str) -> None:
    _need(isinstance(value, list), "expected a list of ids", where)
    for i, v in enumerate(value):
        _id(v, prefix, f"{where}[{i}]")


def _texts(value: Any, where: str) -> None:
    _need(isinstance(value, list) and all(isinstance(x, str) for x in value), "expected a list of text", where)


def _num(value: Any, where: str, lo: float | None = None, hi: float | None = None, optional: bool = False) -> None:
    if value is None and optional:
        return
    _need(isinstance(value, (int, float)) and not isinstance(value, bool), "expected a number", where)
    if lo is not None and hi is not None:
        _need(lo <= value <= hi, f"{value} outside [{lo}, {hi}]", where)
    elif lo is not None:
        _need(value >= lo, f"{value} is below {lo}", where)


def _validate(data: Any) -> None:
    w = "knowledge_map"
    _need(isinstance(data, dict), "a knowledge map is an object", w)
    if data.get("schema") != SUPPORTED_SCHEMA:
        raise MalformedInput(f"expected schema {SUPPORTED_SCHEMA!r}, got {data.get('schema')!r}; other versions "
                             "are refused until V2 supports them", f"{w}.schema")
    missing = _TOP_REQUIRED - set(data)
    _need(not missing, f"missing keys {sorted(missing)}", w)
    _need(isinstance(data["research_id"], str) and bool(_RESEARCH_ID.match(data["research_id"])),
          "research_id must be a V1 research id (RR-<20 hex>)", f"{w}.research_id")
    _str(data["objective"], f"{w}.objective")
    _str(data["mode"], f"{w}.mode")
    if "rule" in data:
        _str(data["rule"], f"{w}.rule", required=False)
    seen: dict[str, str] = {}
    for key, prefix in SECTION_PREFIXES.items():
        items = data[key]
        _need(isinstance(items, list), "expected a list", f"{w}.{key}")
        for i, e in enumerate(items):
            ew = f"{w}.{key}[{i}]"
            _need(isinstance(e, dict), "expected an object", ew)
            absent = REQUIRED_FIELDS[key] - set(e)
            _need(not absent, f"missing required fields {sorted(absent)}", ew)
            _id(e["id"], prefix, f"{ew}.id")
            if e["id"] in seen:
                raise DuplicateId(f"{e['id']} appears in both {seen[e['id']]} and {key}", ew)
            seen[e["id"]] = key
            _VALIDATORS[key](e, ew, key)


def _claim(e: dict, w: str, category: str) -> None:
    _str(e["statement"], f"{w}.statement")
    _str(e["status"], f"{w}.status")
    _need(e["status"] in CLAIM_STATUSES[category],
          f"status {e['status']!r} does not belong in '{category}' (expected {sorted(CLAIM_STATUSES[category])})",
          f"{w}.status")
    _num(e["confidence"], f"{w}.confidence", 0, 1)
    check_scope(e["scope"], f"{w}.scope")
    _ids(e["evidence"], "EV", f"{w}.evidence")
    if "confidence_status" in e:
        _need(e["confidence_status"] in ("provisional", "calibrated"), "confidence_status is provisional or "
              "calibrated", f"{w}.confidence_status")
    if "value" in e:
        _num(e["value"], f"{w}.value", optional=True)
    for k in ("unit", "policy"):
        if k in e:
            _str(e[k], f"{w}.{k}", required=False)
    if "type" in e:
        _str(e["type"], f"{w}.type")
    if "issues" in e:
        _texts(e["issues"], f"{w}.issues")
    if e.get("calculation_id") is not None:
        _id(e["calculation_id"], "CALC", f"{w}.calculation_id")
    if "contradictions" in e:
        _ids(e["contradictions"], "CX", f"{w}.contradictions")


def _contradiction(e: dict, w: str, _: str) -> None:
    _id(e["claim_a"], "CL", f"{w}.claim_a")
    _id(e["claim_b"], "CL", f"{w}.claim_b")
    _need(e["claim_a"] != e["claim_b"], "a claim cannot contradict itself", w)
    if "kind" in e:
        _need(isinstance(e["kind"], str) and e["kind"] in ("incompatible", "scope_mismatch"),
              "kind is incompatible or scope_mismatch", f"{w}.kind")
    if "severity" in e:
        _num(e["severity"], f"{w}.severity", 0, 1)
    for k in ("reason", "scope_note", "resolution"):
        if k in e:
            _str(e[k], f"{w}.{k}", required=False)


def _unknown(e: dict, w: str, _: str) -> None:
    _str(e["description"], f"{w}.description")
    if "capability" in e:
        _str(e["capability"], f"{w}.capability", required=False)
    if "status" in e:
        _need(e["status"] == "open", "knowledge-map/1 exports open unknowns only", f"{w}.status")
    if "expected_gain" in e:
        _num(e["expected_gain"], f"{w}.expected_gain", 0, 1)
    if "est_cost_usd" in e:
        _num(e["est_cost_usd"], f"{w}.est_cost_usd", 0)
    if "needs_approval" in e:
        _need(isinstance(e["needs_approval"], bool), "needs_approval is true or false", f"{w}.needs_approval")
    if "source_types" in e:
        _texts(e["source_types"], f"{w}.source_types")
    if e.get("question_id") is not None:
        _id(e["question_id"], "Q", f"{w}.question_id")


def _calculation(e: dict, w: str, _: str) -> None:
    for k in ("name", "formula", "unit", "method"):
        if k in e:
            _str(e[k], f"{w}.{k}", required=False)
    if "result" in e:
        _num(e["result"], f"{w}.result", optional=True)
    if "inputs" in e:
        _need(isinstance(e["inputs"], list), "inputs is a list", f"{w}.inputs")


def _finding(e: dict, w: str, _: str) -> None:
    if e.get("question_id") is not None:
        _id(e["question_id"], "Q", f"{w}.question_id")
    for k, prefix in (("claim_ids", "CL"), ("evidence_ids", "EV"), ("contradiction_ids", "CX"),
                      ("unknown_ids", "UNK")):
        if k in e:
            _ids(e[k], prefix, f"{w}.{k}")
    if e.get("scope") is not None:
        check_scope(e["scope"], f"{w}.scope")
    if "confidence" in e:
        _num(e["confidence"], f"{w}.confidence", 0, 1, optional=True)


def _question(e: dict, w: str, _: str) -> None:
    if "text" in e:
        _str(e["text"], f"{w}.text", required=False)
    if "depends_on" in e:
        _ids(e["depends_on"], "Q", f"{w}.depends_on")


_VALIDATORS = {"questions": _question, "known": _claim, "uncertain": _claim, "contradicted": _claim,
               "contradictions": _contradiction, "unknowns": _unknown, "calculations": _calculation,
               "findings": _finding}


def _freeze(value: Any) -> Any:
    if isinstance(value, dict):
        return MappingProxyType({k: _freeze(v) for k, v in value.items()})
    if isinstance(value, list):
        return tuple(_freeze(v) for v in value)
    return value


def thaw(value: Any) -> Any:
    """A mutable deep copy of frozen map data."""
    if isinstance(value, Mapping):
        return {k: thaw(v) for k, v in value.items()}
    if isinstance(value, tuple):
        return [thaw(v) for v in value]
    return value


# ---- references --------------------------------------------------------------

@dataclass(frozen=True)
class ResolvedReference:
    """A V1 id found in this map. `value` is a copy of the entry as exported; `kind` is its id prefix."""

    id: str
    kind: str
    section: str
    value: dict


# Reference rules for registered objects: field -> accepted targets.
# V1 targets: "claim", "claim:known", "claim:uncertain" (uncertain or contested), "contradiction",
# "unknown", "calculation", "finding", "question", "evidence". V2 targets are classes. "any" accepts
# any id that resolves in this context.
_RULES: dict[type, dict[str, tuple]] = {
    DiscoveryObjective: {},
    KnownFact: {"claim_id": ("claim:known",)},
    Uncertainty: {"claim_id": ("claim:uncertain",)},
    MissingEvidence: {"unknown_id": ("unknown",)},
    ProblemFrame: {"objective_id": (DiscoveryObjective,), "known_ids": (KnownFact, "claim:known"),
                   "uncertain_ids": (Uncertainty, "claim:uncertain"), "contradiction_ids": ("contradiction",),
                   "missing_ids": (MissingEvidence, "unknown")},
    PriorArt: {},
    PriorArtAssessment: {"search_ids": (PriorArt,)},
    Gap: {"evidence_ids": ("any",)},
    Assumption: {},
    Constraint: {"source_fact_id": (KnownFact, "claim:known"), "source_assumption_id": (Assumption,)},
    EvidenceRequirement: {},
    Hypothesis: {"supporting_claim_ids": ("claim",), "contradicting_claim_ids": ("claim",),
                 "parent_ids": (Hypothesis,), "candidate_ids": (Candidate,),
                 "predicted_observations": (EvidenceRequirement,), "falsification_criteria": (EvidenceRequirement,),
                 "originating": ("any",)},
    Candidate: {"hypothesis_ids": (Hypothesis,), "evidence_support": ("claim",), "evidence_against": ("claim",),
                "constraints_satisfied": (Constraint,), "constraints_violated": (Constraint,),
                "prior_art_assessment_id": (PriorArtAssessment,)},
}
_RULES[CounterHypothesis] = {**_RULES[Hypothesis], "counters": (Hypothesis,)}


class DiscoveryContext:
    """One immutable V1 map plus an append-only registry of V2 objects built against it.

    Its fingerprint is not the V1 receipt state_hash.
    """

    def __init__(self, knowledge_map: Any) -> None:
        canonical = canonical_map(knowledge_map)
        text = canonical_json(canonical)
        if len(text.encode("utf-8")) > MAX_MAP_BYTES:
            raise InputTooLarge(f"knowledge map larger than {MAX_MAP_BYTES} bytes", "knowledge_map")
        put = object.__setattr__
        put(self, "_canonical_json", text)
        put(self, "_fingerprint", FINGERPRINT_PREFIX + hashlib.sha256(text.encode("utf-8")).hexdigest())
        put(self, "_map", _freeze(canonical))
        put(self, "_objects", {})
        views: dict[str, tuple] = {}
        index: dict[str, tuple[str, Mapping]] = {}
        evidence: dict[str, set[str]] = {}
        defaulted: dict[tuple[str, str], list[str]] = {}
        extensions: dict[str, set[str]] = {}
        known_fields = {k: REQUIRED_FIELDS[k] | set(OPTIONAL_DEFAULTS[k]) for k in SECTION_PREFIXES}
        for key in SECTION_PREFIXES:
            rows = []
            for e in canonical[key]:
                view = dict(e)
                for name, default in OPTIONAL_DEFAULTS[key].items():
                    if name not in view:
                        view[name] = copy.deepcopy(default)
                        defaulted.setdefault((key, name), []).append(e["id"])
                extra = set(e) - known_fields[key]
                if extra:
                    extensions.setdefault(key, set()).update(extra)
                frozen = _freeze(view)
                rows.append(frozen)
                index[e["id"]] = (key, frozen)
                if key in CLAIM_STATUSES:
                    for ev in e["evidence"]:
                        evidence.setdefault(ev, set()).add(e["id"])
            views[key] = tuple(rows)
        put(self, "_views", MappingProxyType(views))
        put(self, "_index", MappingProxyType(index))
        put(self, "_evidence_claims", MappingProxyType({k: tuple(sorted(v)) for k, v in evidence.items()}))
        top_extra = sorted(set(canonical) - _TOP_KNOWN)
        notes = list(STANDING_LIMITATIONS)
        if top_extra:
            notes.append(f"Ignored top-level fields not defined by knowledge-map/1: {', '.join(top_extra)}.")
        for key in sorted(extensions):
            notes.append(f"Ignored {key} fields not defined by knowledge-map/1: {', '.join(sorted(extensions[key]))}.")
        for (key, name), ids in sorted(defaulted.items()):
            shown = ", ".join(ids[:5]) + (f" and {len(ids) - 5} more" if len(ids) > 5 else "")
            notes.append(f"{key}: '{name}' not exported for {shown}; V2 uses "
                         f"{OPTIONAL_DEFAULTS[key][name]!r}.")
        notes.extend(self._internal_gaps())
        put(self, "_limitations", tuple(notes))

    def __setattr__(self, name: str, value: Any) -> None:
        raise MalformedInput("a discovery context is immutable", f"DiscoveryContext.{name}")

    # -- the map -------------------------------------------------------------
    @property
    def schema(self) -> str:
        return self._map["schema"]

    @property
    def research_id(self) -> str:
        return self._map["research_id"]

    @property
    def objective(self) -> str:
        return self._map["objective"]

    @property
    def mode(self) -> str:
        return self._map["mode"]

    @property
    def knowledge_map_fingerprint(self) -> str:
        return self._fingerprint

    @property
    def fingerprint_algorithm(self) -> str:
        return FINGERPRINT_ALGORITHM

    @property
    def knowledge_map(self) -> Mapping:
        """The canonical map exactly as received (entity collections sorted), frozen."""
        return self._map

    def snapshot(self) -> dict:
        """A mutable copy of the canonical map. Changing it never changes the context."""
        return thaw(self._map)

    def canonical_json(self) -> str:
        return self._canonical_json

    @property
    def limitations(self) -> tuple[str, ...]:
        """What this context cannot establish from the map, and every default it applied. Stated, never inferred."""
        return self._limitations

    def claims(self, category: str) -> tuple[Mapping, ...]:
        """Claims in one section, with documented defaults applied for unexported optional fields."""
        if category not in CLAIM_STATUSES:
            raise MalformedInput(f"category is one of {sorted(CLAIM_STATUSES)}", "DiscoveryContext.claims")
        return self._views[category]

    def entities(self, key: str) -> tuple[Mapping, ...]:
        if key not in SECTION_PREFIXES:
            raise MalformedInput(f"collection is one of {sorted(SECTION_PREFIXES)}", "DiscoveryContext.entities")
        return self._views[key]

    def scope_of(self, record: Mapping) -> Scope | None:
        return None if record.get("scope") is None else Scope(**thaw(record["scope"]))

    def _internal_gaps(self) -> Iterator[str]:
        """References inside the map that point outside it. V2 cannot resolve these ids."""
        dangling: dict[str, set[str]] = {}

        def note(owner: str, ref: str | None) -> None:
            if ref and ref not in self._index:
                dangling.setdefault(owner, set()).add(ref)

        v = self._views
        for cx in v["contradictions"]:
            note(cx["id"], cx["claim_a"])
            note(cx["id"], cx["claim_b"])
        for cat in CLAIM_STATUSES:
            for c in v[cat]:
                for x in c.get("contradictions", ()):
                    note(c["id"], x)
                note(c["id"], c["calculation_id"])
        for f in v["findings"]:
            for ref in (f["question_id"], *f["claim_ids"], *f["contradiction_ids"], *f["unknown_ids"]):
                note(f["id"], ref)
        for u in v["unknowns"]:
            note(u["id"], u["question_id"])
        for q in v["questions"]:
            for d in q["depends_on"]:
                note(q["id"], d)
        for owner in sorted(dangling):
            yield (f"{owner} references {', '.join(sorted(dangling[owner]))}, which the knowledge map does not "
                   "export; V2 cannot resolve or cite these ids.")

    # -- V1 references -----------------------------------------------------------
    def resolve(self, reference_id: str, allowed: tuple[str, ...] = ()) -> ResolvedReference:
        """Look up a V1 id (CL, CX, UNK, CALC, F). `allowed` restricts the accepted prefixes."""
        if not isinstance(reference_id, str) or "-" not in reference_id:
            raise UnknownReference(f"{reference_id!r} is not a typed reference", "DiscoveryContext.resolve")
        prefix = reference_id.split("-", 1)[0]
        if prefix == "EV":
            raise UnknownReference("knowledge-map/1 does not export evidence objects; use evidence_claims() only",
                                   "DiscoveryContext.resolve")
        if prefix not in _REFERENCE_SECTIONS:
            if prefix in V2_IDEA_PREFIXES:
                raise PromotionRefused(f"{reference_id!r} is a V2 discovery object, not V1 knowledge",
                                       "DiscoveryContext.resolve")
            raise UnknownReference(f"unsupported reference kind {prefix!r}", "DiscoveryContext.resolve")
        if allowed and prefix not in allowed:
            raise UnknownReference(f"{reference_id!r} is {prefix}; expected one of {list(allowed)}",
                                   "DiscoveryContext.resolve")
        found = self._index.get(reference_id)
        if found is None:
            raise UnknownReference(f"{reference_id!r} does not exist in research run {self.research_id}",
                                   "DiscoveryContext.resolve")
        section = found[0]
        return ResolvedReference(reference_id, prefix, section, thaw(self._raw(reference_id, section)))

    def _raw(self, reference_id: str, section: str) -> Mapping:
        return next(e for e in self._map[section] if e["id"] == reference_id)

    def resolve_many(self, references: list[str], allowed: tuple[str, ...] = ()) -> list[ResolvedReference]:
        if len(references) != len(set(references)):
            raise DuplicateId("reference list contains duplicates", "DiscoveryContext.resolve_many")
        return [self.resolve(ref, allowed) for ref in references]

    def evidence_claims(self, evidence_id: str) -> tuple[str, ...]:
        """The claims that list an EV id. This confirms attachment only, never provenance."""
        if not isinstance(evidence_id, str) or not evidence_id.startswith("EV-"):
            raise UnknownReference(f"{evidence_id!r} is not an EV- id", "DiscoveryContext.evidence_claims")
        claims = self._evidence_claims.get(evidence_id)
        if not claims:
            raise UnknownReference(f"{evidence_id!r} is not attached to a claim in research run {self.research_id}",
                                   "DiscoveryContext.evidence_claims")
        return claims

    def assert_same_context(self, other: "DiscoveryContext") -> None:
        if not isinstance(other, DiscoveryContext):
            raise MalformedInput("expected a DiscoveryContext", "DiscoveryContext.assert_same_context")
        if self.research_id != other.research_id or self.knowledge_map_fingerprint != other.knowledge_map_fingerprint:
            raise UnknownReference("objects belong to different V1 knowledge-map contexts", "DiscoveryContext")

    # -- references from V2 objects ---------------------------------------------
    def reference(self, ref: Any, accept: tuple = ("any",), where: str = "reference") -> Mapping | _Obj:
        """Resolve any id a V2 object may cite: a V1 entity in this map (its view, defaults applied), an EV id
        attached to a claim here, or a V2 object registered here. Kind rules as in `_RULES`."""
        if not isinstance(ref, str) or not ref:
            raise MalformedInput("expected a non-empty id", where)
        prefix = ref.split("-", 1)[0]
        if prefix == "EV":
            if ref not in self._evidence_claims:
                raise UnknownReference(f"{ref} is not attached to a claim in this knowledge map", where)
            self._check_kind("evidence", None, ref, accept, where)
            return MappingProxyType({"id": ref, "claims": self._evidence_claims[ref]})
        if prefix in V1_KIND_NAMES:
            found = self._index.get(ref)
            if found is None:
                raise UnknownReference(f"{ref} is not in knowledge map {self.research_id} "
                                       f"({self._fingerprint[:16]})", where)
            section, view = found
            self._check_kind(V1_KIND_NAMES[prefix], section if section in CLAIM_STATUSES else None, ref, accept,
                             where)
            return view
        obj = self._objects.get(ref)
        if obj is None:
            raise UnknownReference(f"{ref} is not a V2 object registered in this context ({self.research_id}); "
                                   "objects built elsewhere cannot be cited", where)
        if "any" not in accept and not any(isinstance(a, type) and isinstance(obj, a) for a in accept):
            raise MalformedInput(f"{ref} is a {type(obj).__name__}; expected {_names(accept)}", where)
        return obj

    @staticmethod
    def _check_kind(kind: str, category: str | None, ref: str, accept: tuple, where: str) -> None:
        if "any" in accept:
            return
        for a in accept:
            if not isinstance(a, str):
                continue
            want, _, want_category = a.partition(":")
            if want != kind:
                continue
            if not want_category or category == want_category or (
                    want_category == "uncertain" and category == "contradicted"):
                return
            if want_category == "known":
                raise PromotionRefused(f"{ref} is {category} in this knowledge map, not known; it cannot stand as "
                                       "a known fact", where)
        raise MalformedInput(f"{ref} is a V1 {category or kind}; expected {_names(accept)}", where)

    # -- registry ------------------------------------------------------------
    def check(self, obj: _Obj) -> None:
        """Validate every reference `obj` makes against this context, without registering it."""
        if not isinstance(obj, _Obj):
            raise MalformedInput(f"only discovery objects can be registered, got {type(obj).__name__}",
                                 "DiscoveryContext")
        name = type(obj).__name__
        rules = _RULES.get(type(obj))
        if rules is None:
            raise MalformedInput(f"{name} cannot be registered in a step 2 context yet", "DiscoveryContext")
        for field_name, accept in rules.items():
            value = getattr(obj, field_name)
            refs = value if isinstance(value, list) else [value]
            for i, ref in enumerate(refs):
                if ref is None:
                    continue
                suffix = f"[{i}]" if isinstance(value, list) else ""
                self.reference(ref, accept, f"{obj.id}:{name}.{field_name}{suffix}")
        for i, ref in enumerate(obj.derived_from):
            self.reference(ref, ("any",), f"{obj.id}:{name}.derived_from[{i}]")
        _SEMANTIC.get(type(obj), lambda *_: None)(self, obj)

    def register(self, obj: _Obj) -> _Obj:
        """Add an object after checking it. An id can be registered once."""
        if isinstance(obj, _Obj) and obj.id in self._objects:
            raise DuplicateId(f"{obj.id} is already registered in this context", "DiscoveryContext.register")
        self.check(obj)
        self._objects[obj.id] = obj
        return obj

    def ensure(self, obj: _Obj) -> _Obj:
        """Register `obj`, or return the registered object with the same id and content."""
        existing = self._objects.get(obj.id) if isinstance(obj, _Obj) else None
        if existing is None:
            return self.register(obj)
        if _content(existing) != _content(obj):
            raise DuplicateId(f"{obj.id} is registered with different content", "DiscoveryContext.ensure")
        return existing

    def get(self, object_id: str) -> _Obj:
        obj = self._objects.get(object_id)
        if obj is None:
            raise UnknownReference(f"{object_id} is not registered in this context", "DiscoveryContext.get")
        return obj

    def __contains__(self, object_id: object) -> bool:
        return object_id in self._objects

    def objects(self, kind: type | None = None) -> tuple[_Obj, ...]:
        return tuple(o for _, o in sorted(self._objects.items()) if kind is None or isinstance(o, kind))


def _names(accept: tuple) -> str:
    return " or ".join(a.__name__ if isinstance(a, type) else a for a in accept)


def _content(obj: _Obj) -> dict:
    d = obj.to_dict()
    d.pop("created_at", None)
    return d


# ---- semantic checks: registered objects must agree with the map ---------------

def _same(a: Any, b: Any) -> bool:
    return canonicalize(a) == canonicalize(b)


def _plain_scope(s: Scope) -> dict:
    return {f.name: getattr(s, f.name) for f in fields(Scope)}


def _scope_of(view: Mapping) -> dict:
    return _plain_scope(check_scope(thaw(view["scope"]), "scope"))


def _match(expected: dict, actual: dict, where: str) -> None:
    for k in expected:
        if not _same(expected[k], actual[k]):
            raise ContextMismatch(f"{k} does not match the knowledge map ({actual[k]!r} != {expected[k]!r})",
                                  f"{where}.{k}")


def _check_objective(ctx: DiscoveryContext, obj: DiscoveryObjective) -> None:
    if obj.research_id != ctx.research_id:
        raise ContextMismatch(f"objective rests on {obj.research_id}, but this context holds {ctx.research_id}",
                              "DiscoveryObjective.research_id")


def _check_known_fact(ctx: DiscoveryContext, obj: KnownFact) -> None:
    rec = ctx._index[obj.claim_id][1]
    _match({"statement": rec["statement"], "value": rec["value"], "unit": rec["unit"], "policy": rec["policy"],
            "confidence": rec["confidence"], "scope": _scope_of(rec)},
           {"statement": obj.statement, "value": obj.value, "unit": obj.unit, "policy": obj.policy,
            "confidence": obj.confidence, "scope": _plain_scope(obj.scope)}, f"{obj.id}:KnownFact")


def _check_uncertainty(ctx: DiscoveryContext, obj: Uncertainty) -> None:
    rec = ctx._index[obj.claim_id][1]
    _match({"statement": rec["statement"], "confidence": rec["confidence"], "scope": _scope_of(rec)},
           {"statement": obj.statement, "confidence": obj.confidence, "scope": _plain_scope(obj.scope)},
           f"{obj.id}:Uncertainty")


def _check_missing(ctx: DiscoveryContext, obj: MissingEvidence) -> None:
    rec = ctx._index[obj.unknown_id][1]
    _match({"description": rec["description"], "capability": rec["capability"],
            "expected_gain": rec["expected_gain"], "est_cost_usd": rec["est_cost_usd"],
            "needs_approval": rec["needs_approval"], "sources": list(rec["source_types"])},
           {"description": obj.description, "capability": obj.capability, "expected_gain": obj.expected_gain,
            "est_cost_usd": obj.est_cost_usd, "needs_approval": obj.needs_approval, "sources": obj.sources},
           f"{obj.id}:MissingEvidence")


def _check_constraint(ctx: DiscoveryContext, obj: Constraint) -> None:
    if obj.source_fact_id and obj.source_assumption_id:
        raise MalformedInput("a constraint rests on one source: a known fact or an assumption", f"{obj.id}:Constraint")
    if obj.source_assumption_id:
        asm = ctx.get(obj.source_assumption_id)
        if asm.name and asm.name in obj.variable_units and \
                Unit.parse(asm.unit) != Unit.parse(obj.variable_units[asm.name]):
            raise UnitMismatch(f"assumption {asm.id} sets {asm.name} in {asm.unit!r}, but the constraint uses "
                               f"{obj.variable_units[asm.name]!r}", f"{obj.id}:Constraint.variable_units")


def _check_assessment(ctx: DiscoveryContext, obj: PriorArtAssessment) -> None:
    for sid in obj.search_ids:
        search = ctx.get(sid)
        if search.query not in obj.queries or not set(search.sources_searched) <= set(obj.sources_searched):
            raise MalformedInput(f"{sid} searched something this assessment does not record",
                                 f"{obj.id}:PriorArtAssessment.search_ids")


_SEMANTIC = {DiscoveryObjective: _check_objective, KnownFact: _check_known_fact, Uncertainty: _check_uncertainty,
             MissingEvidence: _check_missing, Constraint: _check_constraint, PriorArtAssessment: _check_assessment}
