"""JSON Schemas for the open evidence protocol, generated from the typed state.

The dataclasses are the single source of truth; `lofgren schemas --out schemas/`
writes one schema per type so other languages and tools can interoperate.
"""

from __future__ import annotations

import dataclasses
import json
import re
import typing
from enum import Enum
from pathlib import Path

from .evidence.types import (
    Calculation,
    Claim,
    Contradiction,
    Evidence,
    Finding,
    Location,
    Scope,
    Source,
    Unknown,
)

SCHEMA_BASE = "https://lofgren.enterprise/schemas/"  # identifier namespace, not a live URL
TYPES = {"source": Source, "evidence": Evidence, "claim": Claim, "contradiction": Contradiction,
         "calculation": Calculation, "unknown": Unknown, "finding": Finding, "scope": Scope, "location": Location}
_REFS = {cls: name for name, cls in TYPES.items()}


def _type_schema(tp: typing.Any) -> dict:
    origin = typing.get_origin(tp)
    args = typing.get_args(tp)
    if origin in (typing.Union, getattr(__import__("types"), "UnionType", None)):
        options = [_type_schema(a) for a in args if a is not type(None)]
        schema = options[0] if len(options) == 1 else {"anyOf": options}
        if type(None) in args:
            schema = {"anyOf": [schema, {"type": "null"}]}
        return schema
    if origin in (list, tuple, set, frozenset):
        return {"type": "array", "items": _type_schema(args[0]) if args else {}}
    if origin is dict or tp is dict:
        return {"type": "object"}
    if isinstance(tp, type) and issubclass(tp, Enum):
        return {"type": "string", "enum": [e.value for e in tp]}
    if tp in _REFS:
        return {"$ref": f"{_REFS[tp]}.schema.json"}
    return {str: {"type": "string"}, float: {"type": "number"}, int: {"type": "integer"},
            bool: {"type": "boolean"}}.get(tp, {})


def schema_for(name: str) -> dict:
    cls = TYPES[name]
    hints = typing.get_type_hints(cls)
    props, required = {}, []
    for f in dataclasses.fields(cls):
        props[f.name] = _type_schema(hints[f.name])
        if f.default is dataclasses.MISSING and f.default_factory is dataclasses.MISSING:  # type: ignore[misc]
            required.append(f.name)
    doc = re.sub(r"\s+", " ", (cls.__doc__ or name).strip())
    return {"$schema": "https://json-schema.org/draft/2020-12/schema", "$id": f"{SCHEMA_BASE}{name}.schema.json",
            "title": cls.__name__, "description": doc, "type": "object", "properties": props, "required": required}


def write_schemas(out_dir: str | Path) -> list[Path]:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    paths = []
    for name in TYPES:
        p = out / f"{name}.schema.json"
        p.write_text(json.dumps(schema_for(name), indent=2) + "\n")
        paths.append(p)
    return paths


def validate_shape(name: str, obj: dict) -> list[str]:
    """Minimal structural check (required keys and basic types) without third-party libraries."""
    schema = schema_for(name)
    problems = [f"missing '{k}'" for k in schema["required"] if k not in obj]
    for k, v in obj.items():
        spec = schema["properties"].get(k)
        if spec is None:
            problems.append(f"unexpected '{k}'")
            continue
        t = spec.get("type")
        checks = {"string": str, "number": (int, float), "integer": int, "boolean": bool, "array": list, "object": dict}
        if t in checks and v is not None and not isinstance(v, checks[t]):
            problems.append(f"'{k}' should be {t}")
    return problems
