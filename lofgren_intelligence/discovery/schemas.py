"""JSON Schemas for the discovery objects, generated from the dataclasses.

The dataclasses stay the single source of truth. `write_schemas` writes one schema per type to
`schemas/discovery/`; a test regenerates them and fails if the committed files drift.
`validate` checks a plain-JSON object against a generated schema using the standard library only
(the subset of JSON Schema these schemas use: type, enum, properties, required,
additionalProperties, items, prefixItems, minItems, maxItems, anyOf, $ref, pattern).

    python -m lofgren_intelligence.discovery.schemas schemas/discovery
"""

from __future__ import annotations

import dataclasses
import json
import re
import sys
import typing
from enum import Enum
from pathlib import Path
from types import UnionType
from typing import Any

from ..evidence.types import Scope
from .expr import Expr, Relation
from .types import ALL_TYPES, SCHEMA_VERSION, DecisionVariable

SCHEMA_BASE = "https://lofgren.enterprise/schemas/discovery/"  # identifier namespace, not a live URL
DIALECT = "https://json-schema.org/draft/2020-12/schema"

_EXPR = {"anyOf": [
    {"type": "object", "properties": {"op": {"const": "const"}, "value": {"type": "number"}, "unit": {"type": "string"}},
     "required": ["op", "value"], "additionalProperties": False},
    {"type": "object", "properties": {"op": {"const": "var"}, "name": {"type": "string"}},
     "required": ["op", "name"], "additionalProperties": False},
    {"type": "object", "properties": {"op": {"enum": ["add", "sub", "mul", "div", "min", "max"]},
                                      "args": {"type": "array", "items": {"$ref": "#/$defs/expr"},
                                               "minItems": 2, "maxItems": 2}},
     "required": ["op", "args"], "additionalProperties": False},
    {"type": "object", "properties": {"op": {"const": "neg"},
                                      "args": {"type": "array", "items": {"$ref": "#/$defs/expr"},
                                               "minItems": 1, "maxItems": 1}},
     "required": ["op", "args"], "additionalProperties": False},
    {"type": "object", "properties": {"op": {"const": "pow"}, "exponent": {"type": "integer"},
                                      "args": {"type": "array", "items": {"$ref": "#/$defs/expr"},
                                               "minItems": 1, "maxItems": 1}},
     "required": ["op", "args", "exponent"], "additionalProperties": False},
]}
_RELATION = {"type": "object", "properties": {"lhs": {"$ref": "#/$defs/expr"}, "op": {"enum": ["<=", ">=", "=="]},
                                              "rhs": {"$ref": "#/$defs/expr"}},
             "required": ["lhs", "op", "rhs"], "additionalProperties": False}


def _dataclass_schema(cls: type, defs: dict) -> dict:
    hints = typing.get_type_hints(cls)
    props, required = {}, []
    for f in dataclasses.fields(cls):
        props[f.name] = _type_schema(hints[f.name], defs)
        if f.default is dataclasses.MISSING and f.default_factory is dataclasses.MISSING:
            required.append(f.name)
    return {"type": "object", "properties": props, "required": required, "additionalProperties": False}


def _type_schema(tp: Any, defs: dict) -> dict:
    origin, args = typing.get_origin(tp), typing.get_args(tp)
    if origin in (typing.Union, UnionType):
        options = [_type_schema(a, defs) for a in args if a is not type(None)]
        schema = options[0] if len(options) == 1 else {"anyOf": options}
        return {"anyOf": [schema, {"type": "null"}]} if type(None) in args else schema
    if origin is tuple:
        return {"type": "array", "prefixItems": [_type_schema(a, defs) for a in args],
                "minItems": len(args), "maxItems": len(args)}
    if origin is list:
        return {"type": "array", "items": _type_schema(args[0], defs) if args else {}}
    if origin is dict or tp is dict:
        return {"type": "object"}
    if isinstance(tp, type) and issubclass(tp, Enum):
        return {"type": "string", "enum": [e.value for e in tp]}
    if tp is Expr:
        defs.setdefault("expr", _EXPR)
        return {"$ref": "#/$defs/expr"}
    if tp is Relation:
        defs.setdefault("expr", _EXPR)
        defs.setdefault("relation", _RELATION)
        return {"$ref": "#/$defs/relation"}
    if tp is Scope:
        defs.setdefault("scope", _dataclass_schema(Scope, defs))
        return {"$ref": "#/$defs/scope"}
    if tp is DecisionVariable:
        defs.setdefault("decision_variable", _dataclass_schema(DecisionVariable, defs))
        return {"$ref": "#/$defs/decision_variable"}
    simple = {str: {"type": "string"}, float: {"type": "number"}, int: {"type": "integer"},
              bool: {"type": "boolean"}}
    if tp in simple:
        return dict(simple[tp])
    raise TypeError(f"no schema mapping for {tp!r}")  # a new field type needs a mapping here


def schema_for(name: str) -> dict:
    cls = ALL_TYPES[name]
    defs: dict = {}
    body = _dataclass_schema(cls, defs)
    body["properties"]["version"] = {"const": SCHEMA_VERSION}
    doc = re.sub(r"\s+", " ", (cls.__doc__ or cls.__name__).strip())
    out = {"$schema": DIALECT, "$id": f"{SCHEMA_BASE}{name}.schema.json", "title": cls.__name__,
           "description": doc, **body}
    if defs:
        out["$defs"] = {k: defs[k] for k in sorted(defs)}
    return out


_STR, _OBJ, _ARR = {"type": "string"}, {"type": "object"}, {"type": "array"}
_HASH = {"type": "string", "pattern": "^[0-9a-f]{64}$"}
_NULLABLE_STR = {"anyOf": [_STR, {"type": "null"}]}
_FINGERPRINT = {"type": "string", "pattern": "^DFP-[0-9a-f]{64}$"}
_RECEIPT_ID = {"type": "string", "pattern": "^DR-[0-9a-f]{20}$"}

# Document formats V2 emits (structural schemas; the Python validators are authoritative and stricter).
DOCUMENT_SCHEMAS: dict[str, dict] = {
    "discovery_receipt": {
        "title": "DiscoveryReceipt",
        "description": "lofgren.discovery-receipt/1: what determined a discovery, with a reproducible fingerprint and a "
                       "tamper-evident id. verify_discovery_receipt is authoritative.",
        "type": "object", "additionalProperties": False,
        "required": ["schema", "objective", "evidence", "config", "config_hash", "algorithms", "provider",
                     "prior_art_provider", "seeds", "decision_rule", "outcome", "selected_candidate_id", "objects",
                     "ledger", "verifier", "notes", "started_at", "finished_at", "discovery_fingerprint",
                     "discovery_id"],
        "properties": {
            "schema": {"const": "lofgren.discovery-receipt/1"}, "objective": _STR,
            "evidence": {"type": "object", "required": ["research_id", "knowledge_map_schema", "assurance",
                                                        "knowledge_map_fingerprint"]},
            "config": _OBJ, "config_hash": _HASH, "algorithms": _OBJ, "provider": _OBJ, "prior_art_provider": _OBJ,
            "seeds": _OBJ, "decision_rule": _STR,
            "outcome": {"enum": ["candidate_selected", "insufficient_evidence", "contradicted", "infeasible",
                                 "unsupported", "unknown", "requires_research"]},
            "selected_candidate_id": _NULLABLE_STR, "objects": _OBJ,
            "ledger": {"type": "object", "required": ["rate_usd_per_unit", "total_units", "total_usd", "entries"]},
            "verifier": {"type": "object", "required": ["checked", "issues"]}, "notes": _ARR,
            "started_at": _STR, "finished_at": _STR, "discovery_fingerprint": _FINGERPRINT, "discovery_id": _RECEIPT_ID,
        },
    },
    "v3_handoff": {
        "title": "V3Handoff",
        "description": "lofgren.v3-handoff/1: everything V3 needs to build the selected candidate, typed. "
                       "validate_handoff is authoritative.",
        "type": "object", "additionalProperties": False,
        "required": ["schema", "objective", "selected_candidate", "alternatives", "verified_evidence", "hypotheses",
                     "assumptions", "constraints", "specifications", "expected_outcomes", "acceptance_criteria",
                     "test_requirements", "unresolved_questions", "risks", "dependencies", "resource_requirements",
                     "cost_estimates", "evidence_fingerprint", "discovery_fingerprint", "discovery_receipt_id"],
        "properties": {
            "schema": {"const": "lofgren.v3-handoff/1"}, "objective": _STR,
            "selected_candidate": {"type": "object", "required": ["id", "description", "status", "measures"],
                                   "properties": {"status": {"const": "selected"}}},
            "alternatives": {"type": "array", "items": {"type": "object", "required": ["id", "reason", "measures"]}},
            "verified_evidence": {"type": "array", "items": {
                "type": "object", "required": ["claim_id", "statement", "kind"],
                "properties": {"kind": {"const": "verified_fact"}}}},
            "hypotheses": {"type": "array", "items": {"type": "object",
                                                      "required": ["id", "statement", "status", "kind"],
                                                      "properties": {"kind": {"const": "hypothesis"}}}},
            "assumptions": _ARR, "constraints": _ARR,
            "specifications": {"type": "array", "items": {"type": "object",
                                                          "required": ["name", "value", "unit", "source_id"]}},
            "expected_outcomes": {"type": "array", "items": {"type": "object", "required": ["metric", "mean", "kind"],
                                                             "properties": {"kind": {"const": "simulated"}}}},
            "acceptance_criteria": {"type": "array", "minItems": 1,
                                    "items": {"type": "object", "required": ["name", "relation"]}},
            "test_requirements": _ARR, "unresolved_questions": _ARR, "risks": _ARR, "dependencies": _ARR,
            "resource_requirements": _OBJ, "cost_estimates": _OBJ, "evidence_fingerprint": _OBJ,
            "discovery_fingerprint": _FINGERPRINT, "discovery_receipt_id": _RECEIPT_ID,
        },
    },
}


def document_schema(name: str) -> dict:
    return {"$schema": DIALECT, "$id": f"{SCHEMA_BASE}{name}.schema.json", **DOCUMENT_SCHEMAS[name]}


def render(name: str) -> str:
    body = document_schema(name) if name in DOCUMENT_SCHEMAS else schema_for(name)
    return json.dumps(body, indent=2, ensure_ascii=False) + "\n"


def write_schemas(out_dir: str | Path) -> list[Path]:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    paths = []
    for name in sorted({*ALL_TYPES, *DOCUMENT_SCHEMAS}):
        p = out / f"{name}.schema.json"
        p.write_text(render(name), encoding="utf-8", newline="\n")
        paths.append(p)
    return paths


# ---- validation --------------------------------------------------------------

_JSON_TYPES = {"object": dict, "array": list, "string": str, "boolean": bool, "null": type(None)}


def _is_type(value: Any, t: str) -> bool:
    if t == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if t == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    return isinstance(value, _JSON_TYPES[t])


def validate(name: str, instance: Any) -> list[str]:
    """Problems found validating `instance` against the schema for `name` (empty when valid)."""
    schema = document_schema(name) if name in DOCUMENT_SCHEMAS else schema_for(name)
    problems: list[str] = []
    _check(instance, schema, schema.get("$defs", {}), name, problems)
    return problems


def _check(value: Any, schema: dict, defs: dict, path: str, out: list[str]) -> None:
    if "$ref" in schema:
        _check(value, defs[schema["$ref"].rsplit("/", 1)[-1]], defs, path, out)
        return
    if "anyOf" in schema:
        if not any(not _probe(value, s, defs, path) for s in schema["anyOf"]):
            out.append(f"{path}: matches none of the allowed forms")
        return
    if "const" in schema and value != schema["const"]:
        out.append(f"{path}: must be {schema['const']!r}")
    if "enum" in schema and value not in schema["enum"]:
        out.append(f"{path}: {value!r} is not one of {schema['enum']}")
    t = schema.get("type")
    if t and not _is_type(value, t):
        out.append(f"{path}: expected {t}")
        return
    if "pattern" in schema and isinstance(value, str) and not re.search(schema["pattern"], value):
        out.append(f"{path}: {value!r} does not match {schema['pattern']}")
    if isinstance(value, dict) and t == "object":
        props = schema.get("properties", {})
        for k in schema.get("required", []):
            if k not in value:
                out.append(f"{path}: missing {k!r}")
        for k, v in value.items():
            if k in props:
                _check(v, props[k], defs, f"{path}.{k}", out)
            elif schema.get("additionalProperties") is False:
                out.append(f"{path}: unexpected {k!r}")
    if isinstance(value, list) and t == "array":
        if "minItems" in schema and len(value) < schema["minItems"]:
            out.append(f"{path}: fewer than {schema['minItems']} items")
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            out.append(f"{path}: more than {schema['maxItems']} items")
        for i, v in enumerate(value):
            prefix = schema.get("prefixItems", [])
            item = prefix[i] if i < len(prefix) else schema.get("items")
            if item is not None:
                _check(v, item, defs, f"{path}[{i}]", out)


def _probe(value: Any, schema: dict, defs: dict, path: str) -> list[str]:
    found: list[str] = []
    _check(value, schema, defs, path, found)
    return found


if __name__ == "__main__":
    for written in write_schemas(sys.argv[1] if len(sys.argv) > 1 else "schemas/discovery"):
        print(written)
