"""Input and network-facing policy for the public MCP surface."""

from __future__ import annotations

import json
from typing import Any

from ..adapters.net import validate_public_url


class PublicInputError(ValueError):
    pass


MAX_OBJECTIVE_CHARS = 8_000
MAX_TEXT_ITEMS = 20
MAX_TEXT_CHARS = 250_000
MAX_URLS = 12
MAX_URL_LENGTH = 2_048
MAX_MCP_BODY_BYTES = 1_000_000


def validate_remote_args(args: dict[str, Any]) -> dict[str, Any]:
    a = dict(args)
    if "files" in a or "tle_path" in a:
        raise PublicInputError("server-local file paths are not accepted by the remote MCP service")
    from ..adapters.public_api import check_query, check_bound
    from ..adapters.manifest import parse_manifest
    for key in ("europepmc", "trials"):
        if a.get(key) is not None:
            a[key] = check_query(a[key], key)
    if "max_records" in a:
        a["max_records"] = check_bound(a["max_records"], "max_records", 20)
    if a.get("sources_manifest") is not None:
        if not isinstance(a["sources_manifest"], dict):
            raise PublicInputError("remote sources_manifest must be an inline object, never a file path")
        raw = json.dumps(a["sources_manifest"], sort_keys=True, allow_nan=False).encode("utf-8")
        manifest = parse_manifest(raw)
        if len(manifest.sources) > MAX_URLS:
            raise PublicInputError("too many manifest URLs")
    objective = a.get("objective") or a.get("claim")
    if objective is not None and len(str(objective)) > MAX_OBJECTIVE_CHARS:
        raise PublicInputError("objective/claim is too large")
    texts = a.get("texts") or {}
    if not isinstance(texts, dict):
        raise PublicInputError("texts must be an object of title -> text")
    if len(texts) > MAX_TEXT_ITEMS:
        raise PublicInputError("too many inline documents")
    total = sum(len(str(k)) + len(str(v)) for k, v in texts.items())
    if total > MAX_TEXT_CHARS:
        raise PublicInputError("inline document payload is too large")
    urls = a.get("urls") or []
    if not isinstance(urls, list) or len(urls) > MAX_URLS:
        raise PublicInputError("too many URLs")
    for url in urls:
        if len(str(url)) > MAX_URL_LENGTH:
            raise PublicInputError("URL is too long")
        validate_public_url(str(url))
    if a.get("search") not in (None, "brave"):
        raise PublicInputError("unsupported search provider")
    if float(a.get("max_spend_usd", 5.0)) < 0:
        raise PublicInputError("max_spend_usd must be non-negative")
    return a
