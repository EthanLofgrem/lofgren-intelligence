"""Shared behaviour for free public-source APIs (Europe PMC, ClinicalTrials.gov, operator URLs).

Every request goes through the SSRF-protected fetcher (`net.safe_urlopen`), so the public-address checks,
redirect re-validation and the response-size limit apply unchanged. On top of that this module adds:

- a per-request timeout, bounded retries with exponential backoff (429 and 5xx, timeouts and connection
  errors only) and a minimum interval between requests (a simple rate limit);
- explicit retrieval outcomes. A retrieval is ``complete``, ``partial`` (some data arrived, then a page
  failed or records were malformed) or ``provider_unavailable`` (nothing usable arrived). The outcome is
  stored on the adapter, written into each evidence item's ``data["retrieval"]`` and turned into notes, which
  the pipeline records as open unknowns. A failure is never silent and an absent signal is never read as a
  negative one: a flag such as ``retracted`` is True only when the provider says so.

No API keys, no paid endpoints and no request bodies: only public GETs.
"""

from __future__ import annotations

import http.client
import json
import re
import time
import urllib.parse
import urllib.request
from abc import abstractmethod
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable

from ..evidence.types import Evidence, EvidenceKind, Source, topic_tokens, utcnow
from .base import Adapter, GatherResult
from .net import UnsafeURL, safe_urlopen

if TYPE_CHECKING:
    from ..intent.compiler import OutcomeContract, Question

USER_AGENT = "lofgren-intelligence/0.7 (+research; free public sources; no keys)"

OUTCOME_COMPLETE = "complete"
OUTCOME_PARTIAL = "partial"
OUTCOME_UNAVAILABLE = "provider_unavailable"
OUTCOMES = (OUTCOME_COMPLETE, OUTCOME_PARTIAL, OUTCOME_UNAVAILABLE)

# Hard ceilings: a caller may ask for less, never for more.
MAX_RECORDS_CEILING = 100
MAX_PAGES_CEILING = 10
MAX_QUERY_CHARS = 500
MAX_RETRIES_CEILING = 4
MAX_TIMEOUT_S = 60.0

_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


class ProviderUnavailable(Exception):
    """A request failed for good (after any retries). `attempts` counts the requests made."""

    def __init__(self, reason: str, attempts: int = 1) -> None:
        super().__init__(reason)
        self.reason = reason
        self.attempts = attempts


def check_query(query: Any, what: str = "query") -> str:
    """A bounded, printable search query. Fails closed."""
    if not isinstance(query, str) or not query.strip():
        raise ValueError(f"{what} must be non-empty text")
    query = query.strip()
    if len(query) > MAX_QUERY_CHARS:
        raise ValueError(f"{what} is longer than {MAX_QUERY_CHARS} characters")
    if _CONTROL.search(query):
        raise ValueError(f"{what} contains control characters")
    return query


def check_bound(value: Any, what: str, ceiling: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{what} must be an integer")
    if not 1 <= value <= ceiling:
        raise ValueError(f"{what} must be between 1 and {ceiling}")
    return value


class PublicHTTPClient:
    """GETs through `safe_urlopen` with a timeout, bounded retries, backoff and a minimum request interval.

    `opener`, `sleep` and `monotonic` are injectable so tests never touch the network or the wall clock.
    """

    def __init__(self, name: str, *, timeout: float = 15.0, max_retries: int = 2, backoff_s: float = 0.5,
                 max_backoff_s: float = 4.0, min_interval_s: float = 0.35,
                 opener: Callable[..., Any] | None = None, sleep: Callable[[float], None] = time.sleep,
                 monotonic: Callable[[], float] = time.monotonic) -> None:
        if not 0 < float(timeout) <= MAX_TIMEOUT_S:
            raise ValueError(f"timeout must be in (0, {MAX_TIMEOUT_S}] seconds")
        if isinstance(max_retries, bool) or not isinstance(max_retries, int) or not 0 <= max_retries <= MAX_RETRIES_CEILING:
            raise ValueError(f"max_retries must be an integer in [0, {MAX_RETRIES_CEILING}]")
        self.name = name
        self.timeout = float(timeout)
        self.max_retries = max_retries
        self.backoff_s = max(0.0, float(backoff_s))
        self.max_backoff_s = max(0.0, float(max_backoff_s))
        self.min_interval_s = max(0.0, float(min_interval_s))
        self._open = opener or safe_urlopen
        self._sleep = sleep
        self._monotonic = monotonic
        self._last: float | None = None
        self.requests = 0  # requests actually sent (for tests and the retrieval record)

    def _throttle(self) -> None:
        if self._last is not None and self.min_interval_s:
            wait = self.min_interval_s - (self._monotonic() - self._last)
            if wait > 0:
                self._sleep(wait)
        self._last = self._monotonic()

    def _backoff(self, attempt: int, retry_after: str | None) -> float | None:
        """Seconds to wait before the next attempt, or None when the server asks for more than the bound."""
        if retry_after:
            try:
                wanted = float(retry_after.strip())
            except ValueError:
                wanted = None  # an HTTP-date: fall back to our own backoff
            if wanted is not None:
                if wanted > self.max_backoff_s:
                    return None
                return max(0.0, wanted)
        return min(self.max_backoff_s, self.backoff_s * (2 ** attempt))

    def get(self, url: str, accept: str = "application/json") -> tuple[bytes, str]:
        """(body, content type). Raises ProviderUnavailable when the request fails for good."""
        last_error = "no request made"
        for attempt in range(self.max_retries + 1):
            self._throttle()
            self.requests += 1
            retry_after: str | None = None
            req = urllib.request.Request(url, headers={"Accept": accept, "User-Agent": USER_AGENT})
            try:
                with self._open(req, timeout=self.timeout) as resp:
                    status = int(resp.status)
                    if status == 200:
                        # read() without a size enforces the fetcher's response limit and raises past it
                        return resp.read(), str(resp.headers.get("Content-Type", "") or "")
                    retry_after = resp.headers.get("Retry-After")
                    last_error = f"HTTP {status}"
                    retryable = status == 429 or 500 <= status <= 599
            except UnsafeURL as exc:  # the SSRF guard refused: never retried
                raise ProviderUnavailable(f"refused by the public-fetch guard: {exc}", attempt + 1) from None
            except ValueError as exc:  # response over the size limit, bad encoding: not transient
                raise ProviderUnavailable(f"unusable response: {exc}", attempt + 1) from None
            except (OSError, http.client.HTTPException) as exc:  # timeouts, resets, TLS, protocol errors
                last_error = f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__
                retryable = True
            if not retryable or attempt >= self.max_retries:
                break
            wait = self._backoff(attempt, retry_after)
            if wait is None:
                last_error += f"; Retry-After {retry_after}s exceeds the {self.max_backoff_s:g}s bound"
                break
            self._sleep(wait)
        raise ProviderUnavailable(f"{last_error} after {attempt + 1} attempt(s)", attempt + 1)

    def get_json(self, url: str) -> Any:
        body, ctype = self.get(url)
        if ctype and "json" not in ctype.lower():
            raise ProviderUnavailable(f"expected JSON, got content type {ctype!r}", 1)
        try:
            return json.loads(body.decode("utf-8"))
        except ValueError as exc:
            raise ProviderUnavailable(f"malformed JSON: {exc}", 1) from None


def iso_date(value: Any) -> str | None:
    """YYYY-MM-DD when the value is a valid full date, else None (partial dates are kept elsewhere as text)."""
    from datetime import date

    if isinstance(value, str) and len(value) == 10:
        try:
            date.fromisoformat(value)
            return value
        except ValueError:
            return None
    return None


def text_list(value: Any, cap: int = 50, item_chars: int = 300) -> list[str]:
    """A list of non-empty strings from a provider field that may be a string, a list or absent."""
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        return []
    out = [v.strip()[:item_chars] for v in value if isinstance(v, str) and v.strip()]
    return out[:cap]


@dataclass
class Retrieval:
    """What one bounded retrieval from a provider produced, and how it ended."""

    provider: str
    query: str
    endpoint: str
    retrieved_at: str
    outcome: str = OUTCOME_COMPLETE
    pages: int = 0
    requests: int = 0
    records: int = 0
    malformed: int = 0
    hit_count: int | None = None
    truncated: bool = False  # the provider reports more matches than the bound let us fetch
    errors: list[str] = field(default_factory=list)

    def as_data(self) -> dict[str, Any]:
        return {"provider": self.provider, "query": self.query, "endpoint": self.endpoint,
                "retrieved_at": self.retrieved_at, "outcome": self.outcome, "pages": self.pages,
                "requests": self.requests, "records": self.records, "malformed": self.malformed,
                "hit_count": self.hit_count, "truncated": self.truncated, "errors": list(self.errors)}

    def notes(self, adapter_id: str) -> list[str]:
        out: list[str] = []
        if self.outcome == OUTCOME_UNAVAILABLE:
            out.append(f"{adapter_id}: provider_unavailable for '{self.query}' — {'; '.join(self.errors) or 'no data'}")
        elif self.outcome == OUTCOME_PARTIAL:
            out.append(f"{adapter_id}: partial retrieval for '{self.query}' ({self.records} records kept) — "
                       f"{'; '.join(self.errors)}")
        if self.truncated:
            out.append(f"{adapter_id}: bounded at {self.records} of {self.hit_count} matching records for "
                       f"'{self.query}'; the rest were not read")
        return out


class PublicRecordAdapter(Adapter):
    """Base for adapters that retrieve one bounded result set per run and select from it per question.

    The provider is queried once (lazily, on the first gather) with the operator's query; each research
    question then receives the records whose text overlaps the objective and the question. Subclasses
    implement `_retrieve` (pagination and parsing) and `_to_evidence` (one record -> Source + Evidence).
    """

    capabilities = frozenset({"text"})
    top_k = 8
    min_overlap = 1
    default_page_size = 25
    page_size_ceiling = 100

    def __init__(self, query: str, max_records: int = 20, *, page_size: int | None = None, max_pages: int = 5,
                 client: PublicHTTPClient | None = None, endpoint: str | None = None,
                 clock: Callable[[], str] = utcnow) -> None:
        self.query = check_query(query, f"{self.id} query")
        self.max_records = check_bound(max_records, "max_records", MAX_RECORDS_CEILING)
        # never ask for more per page than the run may keep
        self.page_size = min(check_bound(page_size or self.default_page_size, "page_size", self.page_size_ceiling),
                             self.max_records)
        self.max_pages = check_bound(max_pages, "max_pages", MAX_PAGES_CEILING)
        self.client = client or self._default_client()
        if endpoint is not None:
            self.endpoint = endpoint
        self._clock = clock
        self.retrieval: Retrieval | None = None
        self.records: list[dict[str, Any]] = []
        self.excluded: list[dict[str, Any]] = []  # records kept for audit but not used as evidence
        self._reported = False

    endpoint: str = ""

    def _default_client(self) -> PublicHTTPClient:
        return PublicHTTPClient(self.id)

    def available(self) -> bool:
        return bool(self.query)

    def url(self, params: dict[str, Any]) -> str:
        return f"{self.endpoint}?{urllib.parse.urlencode(params)}"

    @abstractmethod
    def _retrieve(self, retrieval: Retrieval) -> list[dict[str, Any]]:
        """Fetch bounded pages, fill `retrieval`, return parsed records (dicts)."""

    @abstractmethod
    def _to_evidence(self, record: dict[str, Any], retrieval: Retrieval) -> tuple[Source, Evidence]:
        ...

    def _exclude(self, record: dict[str, Any]) -> str | None:
        """A reason to keep a record out of the evidence graph, or None."""
        return None

    @staticmethod
    def _search_text(record: dict[str, Any]) -> str:
        return " ".join(str(record.get(k) or "") for k in ("title", "abstract", "journal"))

    def retrieve(self) -> Retrieval:
        if self.retrieval is None:
            retrieval = Retrieval(self.id, self.query, self.endpoint, self._clock())
            start = self.client.requests
            try:
                records = self._retrieve(retrieval)
            except ProviderUnavailable as exc:  # raised before any page arrived
                records = []
                retrieval.errors.append(exc.reason)
            retrieval.requests = self.client.requests - start
            retrieval.records = len(records)
            if not records and (retrieval.errors or retrieval.pages == 0 or retrieval.malformed):
                retrieval.outcome = OUTCOME_UNAVAILABLE
            elif retrieval.errors or retrieval.malformed:
                retrieval.outcome = OUTCOME_PARTIAL
            kept: list[dict[str, Any]] = []
            for r in records:
                reason = self._exclude(r)
                if reason:
                    self.excluded.append({**r, "excluded_reason": reason})
                else:
                    kept.append(r)
            self.records = kept
            self.retrieval = retrieval
        return self.retrieval

    def gather(self, question: "Question", contract: "OutcomeContract") -> GatherResult:
        result = GatherResult()
        retrieval = self.retrieve()
        if not self._reported:  # run-level facts: reported once (the pipeline records each note as an unknown)
            self._reported = True
            result.notes.extend(retrieval.notes(self.id))
            for r in self.excluded:
                result.notes.append(f"{self.id}: excluded {r.get('label') or r.get('title')} — "
                                    f"{r['excluded_reason']}")
        wanted = set(topic_tokens(contract.objective)) | set(topic_tokens(question.text))
        scored = []
        for i, record in enumerate(self.records):
            score = len(set(topic_tokens(self._search_text(record))) & wanted)
            if score >= self.min_overlap:
                scored.append((-score, i, record))
        scored.sort(key=lambda t: (t[0], t[1]))
        for neg, _, record in scored[: self.top_k]:
            source, ev = self._to_evidence(record, retrieval)
            ev.transformations.append(f"selected by {self.id} for this question (overlap={-neg})")
            result.items.append((source, ev))
        if retrieval.outcome != OUTCOME_UNAVAILABLE and not result.items:
            result.notes.append(f"{self.id}: none of the {len(self.records)} retrieved records was relevant to "
                                f"'{question.text}'")
        return result
