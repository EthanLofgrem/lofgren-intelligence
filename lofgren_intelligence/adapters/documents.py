"""Document and web-page adapters (capability: "text")."""

from __future__ import annotations

import html
import re
import urllib.request
from pathlib import Path
from typing import TYPE_CHECKING

from ..evidence.types import Evidence, EvidenceKind, Source, SourceKind, topic_tokens
from .base import Adapter, GatherResult
from .net import safe_urlopen, validate_public_url

if TYPE_CHECKING:
    from ..intent.compiler import OutcomeContract, Question

TEXT_SUFFIXES = {".txt", ".md", ".markdown", ".csv", ".json", ".html", ".htm", ".rst"}


def split_passages(text: str, max_chars: int = 900) -> list[str]:
    """Split text into paragraph-sized passages."""
    paras = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    out: list[str] = []
    for p in paras:
        p = re.sub(r"\s+", " ", p)
        while len(p) > max_chars:
            cut = p.rfind(". ", 0, max_chars)
            cut = cut + 1 if cut > 200 else max_chars
            out.append(p[:cut].strip())
            p = p[cut:].strip()
        if p:
            out.append(p)
    return out


def relevance(passage: str, query_tokens: set[str]) -> int:
    return len(set(topic_tokens(passage)) & query_tokens)


def strip_html(raw: str) -> str:
    raw = re.sub(r"(?is)<(script|style|noscript).*?>.*?</\1>", " ", raw)
    raw = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</h\d>|</li>", "\n\n", raw)
    raw = re.sub(r"<[^>]+>", " ", raw)
    return html.unescape(re.sub(r"[ \t]+", " ", raw))


class _PassageAdapter(Adapter):
    capabilities = frozenset({"text"})
    top_k = 6
    min_overlap = 2

    def _documents(self) -> list[tuple[Source, str]]:
        raise NotImplementedError

    def gather(self, question: "Question", contract: "OutcomeContract") -> GatherResult:
        result = GatherResult()
        docs = self._documents()
        if not docs:
            result.notes.append(f"{self.id}: no documents supplied")
            return result
        query = set(topic_tokens(contract.objective)) | set(topic_tokens(question.text))
        scored: list[tuple[int, Source, str]] = []
        for source, text in docs:
            for passage in split_passages(text):
                score = relevance(passage, query)
                if score >= self.min_overlap:
                    scored.append((score, source, passage))
        scored.sort(key=lambda t: -t[0])
        for score, source, passage in scored[: self.top_k]:
            ev = Evidence(
                source_id=source.id,
                kind=EvidenceKind.DOCUMENT,
                content=passage,
                data={"relevance": score},
                observed_at=source.published_at,
                transformations=[f"passage selected by {self.id} (overlap={score})"],
            )
            result.items.append((source, ev))
        if not result.items:
            result.notes.append(f"{self.id}: no passage was relevant to '{question.text}'")
        return result


class DocumentAdapter(_PassageAdapter):
    """User-supplied files and inline texts. The user's own material."""

    id = "documents"
    license = "user-provided"
    description = "Files and text the user supplies (txt, md, csv, json, html)."

    def __init__(self, paths: list[str | Path] | None = None, texts: dict[str, str] | None = None,
                 quality: float = 0.6) -> None:
        self.paths = [Path(p) for p in (paths or [])]
        self.texts = dict(texts or {})
        self.quality = quality

    def available(self) -> bool:
        return bool(self.paths or self.texts)

    def _documents(self) -> list[tuple[Source, str]]:
        docs: list[tuple[Source, str]] = []
        for path in self.paths:
            files = sorted(path.rglob("*")) if path.is_dir() else [path]
            for f in files:
                if f.is_file() and f.suffix.lower() in TEXT_SUFFIXES:
                    raw = f.read_text(errors="replace")
                    text = strip_html(raw) if f.suffix.lower() in (".html", ".htm") else raw
                    src = Source(kind=SourceKind.USER_FILE, title=f.name, uri=f.resolve().as_uri(),
                                 license=self.license, quality=self.quality)
                    docs.append((src, text))
        for title, text in self.texts.items():
            src = Source(kind=SourceKind.DOCUMENT, title=title, uri=f"inline:{title}",
                         license=self.license, quality=self.quality)
            docs.append((src, text))
        return docs


class WebPageAdapter(_PassageAdapter):
    """Fetches specific public web pages the user or planner names.

    It reads pages; it does not crawl, log in, or bypass access controls.
    """

    id = "web_pages"
    license = "per-site terms"
    description = "Public web pages fetched by URL (read-only, no crawling)."

    def __init__(self, urls: list[str] | None = None, quality: float = 0.5, timeout: float = 15.0) -> None:
        self.urls = [validate_public_url(u) for u in (urls or [])]
        self.quality = quality
        self.timeout = timeout
        self._cache: dict[str, str] = {}

    def available(self) -> bool:
        return bool(self.urls)

    def _fetch(self, url: str) -> str:
        if url not in self._cache:
            req = urllib.request.Request(url, headers={"User-Agent": "lofgren-intelligence/0.1 (research)"})
            with safe_urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read(2_000_000).decode(resp.headers.get_content_charset() or "utf-8", "replace")
            self._cache[url] = strip_html(raw)
        return self._cache[url]

    def _documents(self) -> list[tuple[Source, str]]:
        docs: list[tuple[Source, str]] = []
        for url in self.urls:
            try:
                text = self._fetch(url)
            except Exception:  # network failures are gaps, not crashes
                continue
            host = re.sub(r"^https?://([^/]+).*$", r"\1", url)
            docs.append((Source(kind=SourceKind.WEB, title=url, uri=url, publisher=host,
                                quality=self.quality), text))
        return docs
