"""Immutable V1 knowledge-map boundary for Discovery Intelligence.

V2 resolves references against one exact V1 export. knowledge-map/1 exposes
claim evidence IDs but not the evidence/source objects, so this module never
pretends that attachment checks are full provenance verification.
"""
from __future__ import annotations
import copy, hashlib, json
from dataclasses import dataclass
from typing import Any
from .errors import DuplicateId, MalformedInput, PromotionRefused, UnknownReference

SUPPORTED_SCHEMA = "lofgren.knowledge-map/1"
_REFERENCE_SECTIONS = {
    "CL": ("known", "uncertain", "contradicted"),
    "CX": ("contradictions",), "UNK": ("unknowns",),
    "CALC": ("calculations",), "F": ("findings",),
}

def _canonical(value: Any) -> str:
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise MalformedInput(f"knowledge map is not canonical JSON: {exc}", "DiscoveryContext.map") from None

def _require_id(obj: Any, section: str, index: int) -> str:
    if not isinstance(obj, dict):
        raise MalformedInput("entry must be an object", f"DiscoveryContext.{section}[{index}]")
    value = obj.get("id")
    if not isinstance(value, str) or "-" not in value:
        raise MalformedInput("entry needs a typed id", f"DiscoveryContext.{section}[{index}].id")
    return value

@dataclass(frozen=True)
class ResolvedReference:
    id: str
    kind: str
    section: str
    value: dict

class DiscoveryContext:
    """One immutable V1 map. Its fingerprint is not the V1 receipt state_hash."""
    def __init__(self, knowledge_map: dict) -> None:
        if not isinstance(knowledge_map, dict):
            raise MalformedInput("knowledge map must be an object", "DiscoveryContext.map")
        if knowledge_map.get("schema") != SUPPORTED_SCHEMA:
            raise MalformedInput(f"expected {SUPPORTED_SCHEMA!r}, got {knowledge_map.get('schema')!r}",
                                 "DiscoveryContext.schema")
        research_id = knowledge_map.get("research_id")
        if not isinstance(research_id, str) or not research_id.startswith("RR-"):
            raise MalformedInput("knowledge map needs an RR- research_id", "DiscoveryContext.research_id")
        canonical = _canonical(knowledge_map)
        frozen_copy = json.loads(canonical)
        self._canonical_json = canonical
        self._map = frozen_copy
        self.research_id = research_id
        self.knowledge_map_fingerprint = "KMF-" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        self._index: dict[str, ResolvedReference] = {}
        self._evidence_claims: dict[str, set[str]] = {}
        for prefix, sections in _REFERENCE_SECTIONS.items():
            for section in sections:
                entries = frozen_copy.get(section, [])
                if not isinstance(entries, list):
                    raise MalformedInput("section must be a list", f"DiscoveryContext.{section}")
                for i, obj in enumerate(entries):
                    object_id = _require_id(obj, section, i)
                    actual = object_id.split("-", 1)[0]
                    if actual != prefix:
                        raise MalformedInput(f"{object_id!r} has kind {actual}, expected {prefix}",
                                             f"DiscoveryContext.{section}[{i}].id")
                    if object_id in self._index:
                        raise DuplicateId(f"{object_id!r} appears more than once", f"DiscoveryContext.{section}")
                    self._index[object_id] = ResolvedReference(object_id, prefix, section, copy.deepcopy(obj))
                    if prefix == "CL":
                        evidence = obj.get("evidence", [])
                        if not isinstance(evidence, list) or any(not isinstance(e, str) for e in evidence):
                            raise MalformedInput("claim evidence must be a list of ids",
                                                 f"DiscoveryContext.{section}[{i}].evidence")
                        for evidence_id in evidence:
                            if not evidence_id.startswith("EV-"):
                                raise MalformedInput(f"{evidence_id!r} is not an EV- id",
                                                     f"DiscoveryContext.{section}[{i}].evidence")
                            self._evidence_claims.setdefault(evidence_id, set()).add(object_id)

    @property
    def schema(self) -> str:
        return self._map["schema"]

    def snapshot(self) -> dict:
        return copy.deepcopy(self._map)

    def resolve(self, reference_id: str, allowed: tuple[str, ...] = ()) -> ResolvedReference:
        if not isinstance(reference_id, str) or "-" not in reference_id:
            raise UnknownReference(f"{reference_id!r} is not a typed reference", "DiscoveryContext.resolve")
        prefix = reference_id.split("-", 1)[0]
        if prefix == "EV":
            raise UnknownReference("knowledge-map/1 does not export evidence objects; use evidence_claims() only",
                                   "DiscoveryContext.resolve")
        if prefix not in _REFERENCE_SECTIONS:
            if prefix in {"HYP","CHYP","CAND","SIM","SCN","SENS","OPT","OPTR","CONN","GAP","ASM","REQ"}:
                raise PromotionRefused(f"{reference_id!r} is a V2 discovery object, not V1 knowledge",
                                       "DiscoveryContext.resolve")
            raise UnknownReference(f"unsupported reference kind {prefix!r}", "DiscoveryContext.resolve")
        if allowed and prefix not in allowed:
            raise UnknownReference(f"{reference_id!r} is {prefix}; expected one of {list(allowed)}",
                                   "DiscoveryContext.resolve")
        if reference_id not in self._index:
            raise UnknownReference(f"{reference_id!r} does not exist in research run {self.research_id}",
                                   "DiscoveryContext.resolve")
        found = self._index[reference_id]
        return ResolvedReference(found.id, found.kind, found.section, copy.deepcopy(found.value))

    def resolve_many(self, references: list[str], allowed: tuple[str, ...] = ()) -> list[ResolvedReference]:
        if len(references) != len(set(references)):
            raise DuplicateId("reference list contains duplicates", "DiscoveryContext.resolve_many")
        return [self.resolve(ref, allowed) for ref in references]

    def evidence_claims(self, evidence_id: str) -> tuple[str, ...]:
        if not isinstance(evidence_id, str) or not evidence_id.startswith("EV-"):
            raise UnknownReference(f"{evidence_id!r} is not an EV- id", "DiscoveryContext.evidence_claims")
        claims = self._evidence_claims.get(evidence_id)
        if not claims:
            raise UnknownReference(f"{evidence_id!r} is not attached to a claim in research run {self.research_id}",
                                   "DiscoveryContext.evidence_claims")
        return tuple(sorted(claims))

    def assert_same_context(self, other: "DiscoveryContext") -> None:
        if not isinstance(other, DiscoveryContext):
            raise MalformedInput("expected a DiscoveryContext", "DiscoveryContext.assert_same_context")
        if self.research_id != other.research_id or self.knowledge_map_fingerprint != other.knowledge_map_fingerprint:
            raise UnknownReference("objects belong to different V1 knowledge-map contexts", "DiscoveryContext")
