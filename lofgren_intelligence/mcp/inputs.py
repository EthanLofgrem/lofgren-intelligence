"""Input validation for the local stdio MCP server.

Every `tools/call` is checked against the tool's advertised `inputSchema`
before a handler runs, then against the size limits the hosted service uses,
then (for tools that read the local disk) against the local file policy.
Standard library only, like the server itself.

Local file access. The stdio server is a trusted local tool: it runs as the
user who started it, and `files`, `tle_path` and `sources_manifest` name paths
on that user's disk. It is not a sandbox. It does refuse anything that is not a
regular file (devices, pipes, sockets), file types the document reader cannot
use, oversized files, and directory walks that would read more than the
document limit. Set `LOFGREN_MCP_FILES_ROOT` to a directory to confine every
local path to that directory (paths are resolved, so `..` and symlinks cannot
leave it).
"""

from __future__ import annotations

import math
import os
import stat
from pathlib import Path
from typing import Any, Iterable

from ..adapters.manifest import MAX_MANIFEST_BYTES

# The same limits the hosted service applies (hosted/security.py), so a request
# the local server accepts is never wildly larger than a hosted one.
MAX_OBJECTIVE_CHARS = 8_000
MAX_DOCUMENTS = 20
MAX_TEXT_CHARS = 250_000
MAX_URLS = 12
MAX_URL_LENGTH = 2_048
MAX_PATH_CHARS = 4_096
MAX_TLE_TEXT_CHARS = 20_000
MAX_PASS_HOURS = 168.0
# Local public-source queries may read up to 100 records (the adapter ceiling);
# the hosted service caps the same field at 20.
LOCAL_MAX_RECORDS = 100
# A directory named in `files` is walked; stop after this many entries.
MAX_WALK_ENTRIES = 10_000
MAX_LOCAL_FILE_BYTES = MAX_TEXT_CHARS
MAX_TLE_FILE_BYTES = 2_000_000

FILES_ROOT_ENV = "LOFGREN_MCP_FILES_ROOT"

# Mirrors adapters/documents.py TEXT_SUFFIXES: the reader skips everything else.
DOCUMENT_SUFFIXES = frozenset({".txt", ".md", ".markdown", ".csv", ".json", ".html", ".htm", ".rst"})


class InputInvalid(ValueError):
    """A tool argument that does not satisfy the schema or a limit. The message is safe to return."""


_TYPES: dict[str, tuple[type, ...]] = {
    "string": (str,),
    "number": (int, float),
    "integer": (int,),
    "boolean": (bool,),
    "object": (dict,),
    "array": (list,),
    "null": (type(None),),
}


def _is_type(value: Any, name: str) -> bool:
    if name in ("number", "integer") and isinstance(value, bool):
        return False
    if name == "integer" and isinstance(value, float):
        return value.is_integer() and math.isfinite(value)
    if name == "number" and isinstance(value, float) and not math.isfinite(value):
        return False
    return isinstance(value, _TYPES[name])


def schema_errors(schema: dict[str, Any], value: Any, where: str = "arguments") -> list[str]:
    """Problems with `value` under the JSON Schema subset the server's tool schemas use.

    Supported keywords: type, enum, const, required, properties,
    additionalProperties, maxProperties, items, minItems, maxItems, minLength,
    maxLength, minimum, maximum, exclusiveMinimum, exclusiveMaximum.
    """
    errors: list[str] = []
    expected = schema.get("type")
    if expected is not None:
        names = [expected] if isinstance(expected, str) else list(expected)
        if not any(_is_type(value, n) for n in names):
            return [f"{where} must be of type {' or '.join(names)}"]
    if "enum" in schema and value not in schema["enum"]:
        errors.append(f"{where} must be one of {schema['enum']}")
    if "const" in schema and value != schema["const"]:
        errors.append(f"{where} must be {schema['const']!r}")
    if isinstance(value, str):
        if "minLength" in schema and len(value) < schema["minLength"]:
            errors.append(f"{where} must be at least {schema['minLength']} characters")
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            errors.append(f"{where} must be at most {schema['maxLength']} characters")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            errors.append(f"{where} must be >= {schema['minimum']}")
        if "maximum" in schema and value > schema["maximum"]:
            errors.append(f"{where} must be <= {schema['maximum']}")
        if "exclusiveMinimum" in schema and value <= schema["exclusiveMinimum"]:
            errors.append(f"{where} must be > {schema['exclusiveMinimum']}")
        if "exclusiveMaximum" in schema and value >= schema["exclusiveMaximum"]:
            errors.append(f"{where} must be < {schema['exclusiveMaximum']}")
    if isinstance(value, list):
        if "minItems" in schema and len(value) < schema["minItems"]:
            errors.append(f"{where} must have at least {schema['minItems']} items")
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            errors.append(f"{where} must have at most {schema['maxItems']} items")
        if isinstance(schema.get("items"), dict):
            for i, item in enumerate(value):
                errors += schema_errors(schema["items"], item, f"{where}[{i}]")
    if isinstance(value, dict):
        for key in schema.get("required", ()):
            if key not in value:
                errors.append(f"{where}.{key} is required")
        if "maxProperties" in schema and len(value) > schema["maxProperties"]:
            errors.append(f"{where} must have at most {schema['maxProperties']} entries")
        props = schema.get("properties", {})
        extra = schema.get("additionalProperties", True)
        for key, item in value.items():
            if key in props:
                errors += schema_errors(props[key], item, f"{where}.{key}")
            elif extra is False:
                errors.append(f"{where}.{key} is not an accepted argument")
            elif isinstance(extra, dict):
                errors += schema_errors(extra, item, f"{where}.{key}")
    return errors


def check_limits(args: dict[str, Any]) -> None:
    """Aggregate limits a per-field schema cannot express (hosted parity)."""
    texts = args.get("texts") or {}
    total = sum(len(str(k)) + len(str(v)) for k, v in texts.items())
    if total > MAX_TEXT_CHARS:
        raise InputInvalid(f"inline documents total {total} characters; the limit is {MAX_TEXT_CHARS}")


def _files_root() -> Path | None:
    raw = os.environ.get(FILES_ROOT_ENV, "").strip()
    return Path(raw).resolve() if raw else None


def _inside(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _regular_file(path: Path, what: str, max_bytes: int, root: Path | None) -> Path:
    resolved = path.resolve()
    if root is not None and not _inside(resolved, root):
        raise InputInvalid(f"{what} {str(path)!r} is outside {FILES_ROOT_ENV}")
    try:
        info = resolved.stat()
    except OSError:
        raise InputInvalid(f"{what} {str(path)!r} does not exist or cannot be read") from None
    if not stat.S_ISREG(info.st_mode):
        raise InputInvalid(f"{what} {str(path)!r} is not a regular file")
    if info.st_size > max_bytes:
        raise InputInvalid(f"{what} {str(path)!r} is {info.st_size} bytes; the limit is {max_bytes}")
    return resolved


def _walk(directory: Path) -> Iterable[Path]:
    seen = 0
    for entry in sorted(directory.rglob("*")):
        seen += 1
        if seen > MAX_WALK_ENTRIES:
            raise InputInvalid(f"directory {str(directory)!r} has more than {MAX_WALK_ENTRIES} entries")
        if entry.suffix.lower() in DOCUMENT_SUFFIXES:
            yield entry


def check_local_paths(args: dict[str, Any]) -> None:
    """Apply the local file policy to `files`, `tle_path` and a `sources_manifest` path."""
    root = _files_root()
    files = args.get("files") or []
    documents = len(args.get("texts") or {})
    total_bytes = 0
    for name in files:
        path = Path(name)
        resolved = path.resolve()
        if root is not None and not _inside(resolved, root):
            raise InputInvalid(f"file {name!r} is outside {FILES_ROOT_ENV}")
        if resolved.is_dir():
            candidates = list(_walk(resolved))
        else:
            if path.suffix.lower() not in DOCUMENT_SUFFIXES:
                raise InputInvalid(f"file {name!r} is not a supported document type "
                                   f"({', '.join(sorted(DOCUMENT_SUFFIXES))})")
            candidates = [path]
        for candidate in candidates:
            if candidate.is_dir():
                continue
            checked = _regular_file(candidate, "file", MAX_LOCAL_FILE_BYTES, root)
            documents += 1
            if documents > MAX_DOCUMENTS:
                raise InputInvalid(f"more than {MAX_DOCUMENTS} documents (files and texts together)")
            total_bytes += checked.stat().st_size
            if total_bytes > MAX_TEXT_CHARS:
                raise InputInvalid(f"local documents total more than {MAX_TEXT_CHARS} bytes")
    if args.get("tle_path"):
        _regular_file(Path(args["tle_path"]), "tle_path", MAX_TLE_FILE_BYTES, root)
    manifest = args.get("sources_manifest")
    if isinstance(manifest, str) and manifest:
        _regular_file(Path(manifest), "sources_manifest", MAX_MANIFEST_BYTES, root)
