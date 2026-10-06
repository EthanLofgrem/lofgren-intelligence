"""Operator-selected public URLs, listed in a source manifest (capability: "text").

A manifest is a small JSON file naming the public pages an operator chose as evidence:

    {"schema": "lofgren.sources-manifest/1",
     "sources": [{"url": "https://example.org/report.html",
                  "title": "optional", "publisher": "optional",
                  "sha256": "optional: expected sha256 of the response body",
                  "note": "optional"}]}

Every page is fetched once through the SSRF-protected fetcher (with the shared timeout, retry and rate-limit
behaviour) and labelled ``operator_selected``: the operator picked it, nothing here vouches for it. Each
evidence item records the retrieval time, the sha256 of the exact bytes read and the sha256 of the manifest
file. A page that cannot be read, has an unsupported content type or does not match the manifest's expected
hash is reported as a note, never silently dropped. The manifest is bounded (file size, number of sources,
field lengths) and fails closed on anything it does not recognise.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import re
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from ..evidence.types import Source, SourceKind, utcnow
from .documents import _PassageAdapter, strip_html
from .net import UnsafeURL, _public_ip
from .public_api import OUTCOME_COMPLETE, OUTCOME_UNAVAILABLE, ProviderUnavailable, PublicHTTPClient

MANIFEST_SCHEMA = "lofgren.sources-manifest/1"
MAX_MANIFEST_BYTES = 256_000
MAX_MANIFEST_SOURCES = 25
MAX_URL_CHARS = 2048
SELECTION_LABEL = "operator_selected"
_ENTRY_FIELDS = {"url": MAX_URL_CHARS, "title": 300, "publisher": 200, "sha256": 64, "note": 500}
_TOP_FIELDS = {"schema", "description", "sources"}
_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_TEXT_TYPES = ("text/html", "text/plain", "text/csv", "text/markdown", "application/json", "application/xhtml+xml")


class ManifestError(ValueError):
    pass


def check_public_url_shape(url: str) -> str:
    """The checks that need no DNS: scheme, host, no credentials, no local names, no non-public IP literals.

    The full check (DNS resolution to public addresses, pinned connection, redirects re-checked) runs again
    in the fetcher on every request; this only rejects a bad manifest before anything is fetched.
    """
    p = urllib.parse.urlsplit(url)
    if p.scheme.lower() not in {"http", "https"}:
        raise UnsafeURL("only http and https URLs are allowed")
    if not p.hostname:
        raise UnsafeURL("URL hostname is required")
    if p.username or p.password:
        raise UnsafeURL("credentials in URLs are not allowed")
    host = p.hostname.rstrip(".").lower()
    if host in {"localhost", "localhost.localdomain"} or host.endswith((".local", ".localhost", ".internal")):
        raise UnsafeURL("local hostnames are not allowed")
    try:
        p.port
    except ValueError:
        raise UnsafeURL("invalid port") from None
    literal = host.strip("[]")
    try:
        ipaddress.ip_address(literal)
    except ValueError:
        return url
    if not _public_ip(literal):
        raise UnsafeURL("non-public IP addresses are not allowed")
    return url


@dataclass
class SourceManifest:
    sources: list[dict[str, str]]
    sha256: str
    path: str = ""
    description: str = ""


def parse_manifest(raw: bytes, path: str = "") -> SourceManifest:
    if len(raw) > MAX_MANIFEST_BYTES:
        raise ManifestError(f"source manifest is larger than {MAX_MANIFEST_BYTES} bytes")
    try:
        data = json.loads(raw.decode("utf-8"))
    except ValueError as exc:
        raise ManifestError(f"source manifest is not valid UTF-8 JSON: {exc}") from None
    if not isinstance(data, dict):
        raise ManifestError("source manifest must be a JSON object")
    unknown = set(data) - _TOP_FIELDS
    if unknown:
        raise ManifestError(f"source manifest has unknown fields: {sorted(unknown)}")
    if data.get("schema") != MANIFEST_SCHEMA:
        raise ManifestError(f"source manifest schema must be {MANIFEST_SCHEMA!r}")
    description = data.get("description", "")
    if not isinstance(description, str) or len(description) > 1000:
        raise ManifestError("description must be text of at most 1000 characters")
    sources = data.get("sources")
    if not isinstance(sources, list) or not sources:
        raise ManifestError("source manifest needs a non-empty 'sources' list")
    if len(sources) > MAX_MANIFEST_SOURCES:
        raise ManifestError(f"source manifest lists more than {MAX_MANIFEST_SOURCES} sources")
    out: list[dict[str, str]] = []
    seen: set[str] = set()
    for i, entry in enumerate(sources):
        where = f"sources[{i}]"
        if not isinstance(entry, dict):
            raise ManifestError(f"{where} must be an object")
        extra = set(entry) - set(_ENTRY_FIELDS)
        if extra:
            raise ManifestError(f"{where} has unknown fields: {sorted(extra)}")
        for key, limit in _ENTRY_FIELDS.items():
            if key in entry and (not isinstance(entry[key], str) or len(entry[key]) > limit
                                 or re.search(r"[\x00-\x1f\x7f]", entry[key])):
                raise ManifestError(f"{where}.{key} must be text of at most {limit} characters")
        url = entry.get("url", "").strip()
        if not url:
            raise ManifestError(f"{where}.url is required")
        try:
            check_public_url_shape(url)
        except UnsafeURL as exc:
            raise ManifestError(f"{where}.url refused: {exc}") from None
        if url in seen:
            raise ManifestError(f"{where}.url is listed twice")
        seen.add(url)
        if "sha256" in entry and not _HEX64.match(entry["sha256"]):
            raise ManifestError(f"{where}.sha256 must be 64 lowercase hex characters")
        out.append({k: v.strip() for k, v in entry.items()} | {"url": url})
    return SourceManifest(out, hashlib.sha256(raw).hexdigest(), path, description)


def load_manifest(path: str | Path) -> SourceManifest:
    p = Path(path)
    if not p.is_file():
        raise ManifestError(f"source manifest not found: {path}")
    if p.stat().st_size > MAX_MANIFEST_BYTES:
        raise ManifestError(f"source manifest is larger than {MAX_MANIFEST_BYTES} bytes")
    return parse_manifest(p.read_bytes(), str(p))


def _charset(ctype: str) -> str:
    m = re.search(r"charset=([\w.-]+)", ctype or "", re.I)
    return m.group(1) if m else "utf-8"


@dataclass
class _Fetched:
    entry: dict[str, str]
    index: int
    outcome: str = OUTCOME_COMPLETE
    text: str = ""
    sha256: str = ""
    bytes: int = 0
    content_type: str = ""
    retrieved_at: str = ""
    error: str = ""
    labels: dict[str, Any] = field(default_factory=dict)


class OperatorSourcesAdapter(_PassageAdapter):
    """Public URLs an operator listed in a manifest, each labelled operator_selected with date and hash."""

    id = "operator_sources"
    license = "per-site terms (operator selected)"
    description = "Operator-selected public URLs from a source manifest (labelled, hashed, read-only)."

    def __init__(self, manifest: SourceManifest | str | Path, *, client: PublicHTTPClient | None = None,
                 clock: Callable[[], str] = utcnow, quality: float = 0.5) -> None:
        self.manifest = manifest if isinstance(manifest, SourceManifest) else load_manifest(manifest)
        self.client = client or PublicHTTPClient(self.id, min_interval_s=0.5)
        self._clock = clock
        self.quality = quality
        self.fetched: list[_Fetched] | None = None
        self._labels_by_source: dict[str, tuple[dict, list[str]]] = {}
        self._reported = False

    def available(self) -> bool:
        return bool(self.manifest.sources)

    def _fetch_one(self, index: int, entry: dict[str, str]) -> _Fetched:
        f = _Fetched(entry, index, retrieved_at=self._clock())
        try:
            body, ctype = self.client.get(entry["url"], accept="text/html, text/plain;q=0.9, application/json;q=0.5")
        except ProviderUnavailable as exc:
            f.outcome, f.error = OUTCOME_UNAVAILABLE, exc.reason
            return f
        f.sha256, f.bytes, f.content_type = hashlib.sha256(body).hexdigest(), len(body), ctype.split(";")[0].strip()
        if f.content_type and f.content_type.lower() not in _TEXT_TYPES:
            f.outcome, f.error = OUTCOME_UNAVAILABLE, f"unsupported content type {f.content_type!r}"
            return f
        if entry.get("sha256") and entry["sha256"] != f.sha256:
            f.outcome = OUTCOME_UNAVAILABLE
            f.error = f"content sha256 {f.sha256} does not match the manifest's {entry['sha256']}"
            return f
        try:
            raw = body.decode(_charset(ctype), "replace")
        except LookupError:  # an unknown charset name
            raw = body.decode("utf-8", "replace")
        f.text = strip_html(raw) if "html" in f.content_type.lower() or not f.content_type else raw
        return f

    def fetch_all(self) -> list[_Fetched]:
        if self.fetched is None:
            self.fetched = [self._fetch_one(i, e) for i, e in enumerate(self.manifest.sources)]
        return self.fetched

    def _documents(self) -> list[tuple[Source, str]]:
        docs: list[tuple[Source, str]] = []
        for f in self.fetch_all():
            if f.outcome != OUTCOME_COMPLETE:
                continue
            url = f.entry["url"]
            host = urllib.parse.urlsplit(url).hostname or url
            src = Source(kind=SourceKind.WEB, title=f.entry.get("title") or url, uri=url,
                         publisher=f.entry.get("publisher") or host, retrieved_at=f.retrieved_at,
                         license=self.license, quality=self.quality)
            data = {"source_class": SELECTION_LABEL, "selection": SELECTION_LABEL, "url": url,
                    "retrieved_at": f.retrieved_at, "content_sha256": f.sha256, "content_bytes": f.bytes,
                    "content_type": f.content_type, "manifest_sha256": self.manifest.sha256,
                    "manifest_entry": f.index,
                    "manifest_hash_checked": bool(f.entry.get("sha256")),
                    "retrieval": {"provider": self.id, "outcome": f.outcome, "retrieved_at": f.retrieved_at}}
            steps = [f"{SELECTION_LABEL}: listed in source manifest {self.manifest.sha256[:12]} (entry {f.index})",
                     f"retrieved {url} at {f.retrieved_at}; sha256 {f.sha256}"]
            self._labels_by_source[src.id] = (data, steps)
            docs.append((src, f.text))
        return docs

    def _labels(self, source: Source) -> tuple[dict, list[str]]:
        data, steps = self._labels_by_source.get(source.id, ({}, []))
        return dict(data), list(steps)

    def _retrieval_notes(self) -> list[str]:
        if self._reported:  # run-level facts: reported once
            return []
        self._reported = True
        return [f"{self.id}: provider_unavailable for {f.entry['url']} — {f.error}"
                for f in self.fetch_all() if f.outcome != OUTCOME_COMPLETE]
