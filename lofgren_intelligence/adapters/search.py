"""Web discovery: find evidence the user did not already know about.

    query = (objective, evidence requirement, place, period)
      -> search provider -> canonical URL dedupe -> robots.txt check
      -> fetch page (or fall back to the snippet) -> relevant passages

Search providers are replaceable (SearchProvider). Brave Search is included;
any other API plugs in the same way. The adapter reads public pages only: it
honours robots.txt, never logs in, and caps pages per run.
"""

from __future__ import annotations

import json
import re
import urllib.parse
import urllib.request
import urllib.robotparser
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Callable

from ..evidence.types import Evidence, EvidenceKind, Source, SourceKind, topic_tokens, utcnow
from .base import GatherResult
from .documents import _PassageAdapter, relevance, split_passages, strip_html
from .net import safe_urlopen, validate_public_url

if TYPE_CHECKING:
    from ..intent.compiler import OutcomeContract, Question

USER_AGENT = "lofgren-intelligence/0.2 (+research; respects robots.txt)"
_TRACKING = re.compile(r"^(utm_|fbclid|gclid|mc_|ref$|ref_)")


@dataclass
class SearchResult:
    title: str
    url: str
    snippet: str = ""
    published_at: str | None = None


class SearchProvider(ABC):
    name = "search"

    @abstractmethod
    def search(self, query: str, limit: int = 5) -> list[SearchResult]:
        ...


class BraveSearchProvider(SearchProvider):
    """Brave Search API (https://api.search.brave.com). Needs BRAVE_API_KEY."""

    name = "brave"
    endpoint = "https://api.search.brave.com/res/v1/web/search"

    def __init__(self, api_key: str | None = None, timeout: float = 15.0) -> None:
        import os

        self.api_key = api_key or os.environ.get("BRAVE_API_KEY", "")
        self.timeout = timeout

    def search(self, query: str, limit: int = 5) -> list[SearchResult]:
        url = f"{self.endpoint}?{urllib.parse.urlencode({'q': query, 'count': min(limit, 20)})}"
        req = urllib.request.Request(url, headers={"Accept": "application/json",
                                                   "X-Subscription-Token": self.api_key})
        with safe_urlopen(req, timeout=self.timeout) as resp:
            data = json.loads(resp.read().decode())
        return [SearchResult(r.get("title", ""), r.get("url", ""), strip_html(r.get("description", "")),
                             r.get("page_age"))
                for r in data.get("web", {}).get("results", [])[:limit] if r.get("url")]


def canonical_url(url: str) -> str:
    p = urllib.parse.urlsplit(url.strip())
    query = urllib.parse.urlencode([(k, v) for k, v in urllib.parse.parse_qsl(p.query) if not _TRACKING.match(k)])
    path = p.path.rstrip("/") or "/"
    host = p.netloc.lower().removeprefix("www.")
    return urllib.parse.urlunsplit((p.scheme.lower() or "https", host, path, query, ""))


def classify_source(url: str) -> tuple[str, float]:
    """(source type, prior quality) from the domain. A prior only; verification decides."""
    host = urllib.parse.urlsplit(url).netloc.lower()
    if host.endswith((".gov", ".mil")) or ".gov." in host or host.endswith(".int"):
        return "government", 0.8
    if host.endswith(".edu") or ".ac." in host:
        return "academic", 0.75
    if any(k in host for k in ("wikipedia.org",)):
        return "encyclopedia", 0.55
    if any(k in host for k in ("reddit.", "facebook.", "x.com", "twitter.", "tiktok.", "instagram.")):
        return "social", 0.3
    return "web", 0.5


def build_query(contract: "OutcomeContract", question: "Question") -> str:
    """Q = (objective, requirement, place, constraints) as a compact keyword query."""
    words = topic_tokens(contract.objective)
    place = (contract.location or {}).get("name")
    extra: list[str] = []
    if question.role == "prior":
        extra = ["study", "history"]
    elif question.role == "contradict":
        extra = ["decline", "risk"]
    elif question.stage in ("Blueprint",):
        extra = ["cost", "pricing"]
    elif question.stage in ("Diligence",):
        extra = ["competitors"]
    parts = words[:8] + extra
    if place and place.lower() not in " ".join(parts):
        parts.append(place)
    return " ".join(parts)


@dataclass
class QueryRecord:
    query: str
    question_id: str
    results: int
    kept: int
    at: str = field(default_factory=utcnow)


class WebSearchAdapter(_PassageAdapter):
    id = "web_search"
    license = "per-site terms"
    description = "Discovers public web pages through a search provider (robots.txt respected)."

    def __init__(self, provider: SearchProvider, results_per_query: int = 5, max_pages: int = 20,
                 fetch_pages: bool = True, fetch: Callable[[str], str] | None = None,
                 robots_allowed: Callable[[str], bool] | None = None, timeout: float = 15.0) -> None:
        self.provider = provider
        self.results_per_query = results_per_query
        self.max_pages = max_pages
        self.fetch_pages = fetch_pages
        self.timeout = timeout
        self._fetch = fetch or self._http_fetch
        self._robots_allowed = robots_allowed or self._robots_check
        self._robots: dict[str, urllib.robotparser.RobotFileParser | None] = {}
        self.seen: set[str] = set()
        self.history: list[QueryRecord] = []
        self.cost_per_call_usd = 0.0

    def available(self) -> bool:
        return True

    # -- network -------------------------------------------------------------
    def _http_fetch(self, url: str) -> str:
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with safe_urlopen(req, timeout=self.timeout) as resp:
            ctype = resp.headers.get("Content-Type", "")
            if "html" not in ctype and "text" not in ctype:
                raise ValueError(f"unsupported content type {ctype}")
            raw = resp.read(2_000_000).decode(resp.headers.get_content_charset() or "utf-8", "replace")
        return strip_html(raw)

    def _robots_check(self, url: str) -> bool:
        validate_public_url(url)
        p = urllib.parse.urlsplit(url)
        base = f"{p.scheme}://{p.netloc}"
        if base not in self._robots:
            rp = urllib.robotparser.RobotFileParser()
            rp.set_url(f"{base}/robots.txt")
            try:
                req = urllib.request.Request(f"{base}/robots.txt", headers={"User-Agent": USER_AGENT})
                with safe_urlopen(req, timeout=self.timeout) as resp:
                    rp.parse(resp.read(512_000).decode(resp.headers.get_content_charset() or "utf-8", "replace").splitlines())
                self._robots[base] = rp
            except Exception:
                self._robots[base] = None  # unreachable robots.txt: treat as allowed
        rp = self._robots[base]
        return True if rp is None else rp.can_fetch(USER_AGENT, url)

    # -- gather --------------------------------------------------------------
    def gather(self, question: "Question", contract: "OutcomeContract") -> GatherResult:
        result = GatherResult()
        query = build_query(contract, question)
        try:
            hits = self.provider.search(query, self.results_per_query)
        except Exception as exc:
            result.notes.append(f"web_search: search provider '{self.provider.name}' unavailable ({exc})")
            self.history.append(QueryRecord(query, question.id, 0, 0))
            return result
        wanted = set(topic_tokens(contract.objective)) | set(topic_tokens(question.text))
        kept = 0
        for hit in hits:
            try:
                validate_public_url(hit.url)
            except ValueError:
                result.notes.append(f"web_search: unsafe result URL refused: {hit.url}")
                continue
            url = canonical_url(hit.url)
            if url in self.seen or len(self.seen) >= self.max_pages:
                continue
            self.seen.add(url)
            if not self._robots_allowed(hit.url):
                result.notes.append(f"web_search: robots.txt disallows {url}; not read")
                continue
            source_type, quality = classify_source(url)
            host = urllib.parse.urlsplit(url).netloc
            text, how = hit.snippet, "search snippet only"
            if self.fetch_pages:
                try:
                    text, how = self._fetch(hit.url), "page fetched"
                except Exception:
                    quality *= 0.8  # we could not read the page itself
            src = Source(kind=SourceKind.WEB, title=hit.title or url, uri=url, publisher=host,
                         published_at=hit.published_at, license=self.license, quality=round(quality, 3))
            passages = sorted(((relevance(p, wanted), p) for p in split_passages(text)), key=lambda t: -t[0])
            for score, passage in passages[:3]:
                if score < self.min_overlap:
                    continue
                result.items.append((src, Evidence(
                    source_id=src.id, kind=EvidenceKind.DOCUMENT, content=passage,
                    data={"relevance": score, "source_type": source_type, "query": query},
                    observed_at=hit.published_at,
                    transformations=[f"discovered via {self.provider.name} search '{query}'", how],
                )))
                kept += 1
        self.history.append(QueryRecord(query, question.id, len(hits), kept))
        if not kept:
            result.notes.append(f"web_search: no relevant passage found for '{query}'")
        return result
