"""Prior art: searches, their coverage, and the structured assessment built from them.

A `PriorArtProvider` searches some corpus and declares its coverage (sources, domains, period,
limitations). `assess_prior_art` runs one search per query, records each as a `PriorArt` record,
and concludes one of three things in a `PriorArtAssessment`:

    match_found               matching prior art exists (the matches are listed)
    no_match_within_coverage  nothing matched within the declared coverage; NOT evidence of novelty
    incomplete                part of the requested coverage was not searched; no conclusion

Rules enforced here:

- Coverage gaps are recorded, never assumed away: a requested domain or period the provider does
  not cover, or a query whose search failed, makes the search incomplete.
- Malformed results fail closed (`MalformedInput`); they are not dropped silently.
- Provider metadata (name, version) is returned separately for the receipt; it does not enter the
  domain objects.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..evidence.types import topic_tokens
from .context import DiscoveryContext
from .errors import MalformedInput
from .types import (
    PriorArt,
    PriorArtAssessment,
    PriorArtConclusion,
    calendar_date,
    period,
    prior_art_hit,
    text,
    texts,
    utcnow,
)


@dataclass(frozen=True)
class ProviderCoverage:
    sources: tuple[str, ...]  # what is actually searched
    domains: tuple[str, ...]  # the domains or jurisdictions the sources cover
    time_range: tuple[str | None, str | None]  # the period the sources cover; None is open-ended
    limitations: tuple[str, ...]  # what this corpus cannot show

    def __post_init__(self) -> None:
        if not self.sources or not self.limitations:
            raise MalformedInput("a provider declares its sources and its limitations", "ProviderCoverage")
        texts(list(self.sources), "ProviderCoverage.sources")
        texts(list(self.domains), "ProviderCoverage.domains")
        texts(list(self.limitations), "ProviderCoverage.limitations")
        period(self.time_range, "ProviderCoverage.time_range")


class PriorArtProvider:
    """Interface. `search` returns hits as dicts: title, source, uri, published, summary, matched_terms."""

    name = "provider"
    version = "0"

    def coverage(self) -> ProviderCoverage:
        raise NotImplementedError

    def search(self, query: str, domains: tuple[str, ...], time_range: tuple[str | None, str | None]) -> list[dict]:
        raise NotImplementedError


class FixturePriorArtProvider(PriorArtProvider):
    """Offline and deterministic: matches queries against a fixed list of records.

    A record matches when at least `min_share` of the query's content words appear in its title,
    summary or keywords, and its domains and date fall inside the request.
    """

    name = "fixture"
    version = "1"

    def __init__(self, records: list[dict], coverage: ProviderCoverage, min_share: float = 0.6) -> None:
        if not isinstance(records, list):
            raise MalformedInput("fixture records are a list", "FixturePriorArtProvider.records")
        self._records = []
        for i, r in enumerate(records):
            if not isinstance(r, dict):
                raise MalformedInput("a fixture record is an object", f"FixturePriorArtProvider.records[{i}]")
            body = {k: v for k, v in r.items() if k not in ("domains", "keywords")}
            hit = prior_art_hit(body, f"FixturePriorArtProvider.records[{i}]")
            domains = tuple(texts(r.get("domains", []), f"FixturePriorArtProvider.records[{i}].domains"))
            keywords = tuple(texts(r.get("keywords", []), f"FixturePriorArtProvider.records[{i}].keywords"))
            words = set(topic_tokens(" ".join([hit["title"], hit["summary"], *keywords])))
            self._records.append((hit, domains, words))
        self._coverage = coverage
        self._min_share = min_share

    def coverage(self) -> ProviderCoverage:
        return self._coverage

    def search(self, query: str, domains: tuple[str, ...], time_range: tuple[str | None, str | None]) -> list[dict]:
        terms = set(topic_tokens(query))
        if not terms:
            return []
        start, end = (calendar_date(t, "time_range") for t in time_range)
        found = []
        for hit, record_domains, words in self._records:
            matched = terms & words
            if len(matched) < self._min_share * len(terms):
                continue
            if domains and record_domains and not set(domains) & set(record_domains):
                continue
            published = calendar_date(hit["published"], "published")
            if published and ((start and published < start) or (end and published > end)):
                continue
            found.append((-len(matched), hit["title"], {**hit, "matched_terms": sorted(matched)}))
        return [h for *_, h in sorted(found, key=lambda x: (x[0], x[1]))]


@dataclass(frozen=True)
class ProviderMetadata:
    """For the discovery receipt only."""

    name: str
    version: str
    queries_run: int
    queries_failed: int


def _uncovered(requested: tuple[str, ...], coverage: ProviderCoverage,
               time_range: tuple[str | None, str | None]) -> list[str]:
    gaps = []
    for d in requested:
        if d not in coverage.domains:
            gaps.append(f"domain {d}: not covered by {', '.join(coverage.sources)}")
    req_start, req_end = (calendar_date(t, "time_range") for t in time_range)
    cov_start, cov_end = (calendar_date(t, "coverage.time_range") for t in coverage.time_range)
    if cov_start and (req_start is None or req_start < cov_start):
        gaps.append(f"before {cov_start.isoformat()}: outside the searched sources' coverage")
    if cov_end and (req_end is None or req_end > cov_end):
        gaps.append(f"after {cov_end.isoformat()}: outside the searched sources' coverage")
    return gaps


def assess_prior_art(context: DiscoveryContext, subject: str, queries: list[str], provider: PriorArtProvider,
                     domains: list[str] | tuple[str, ...] = (), time_range: tuple[str | None, str | None] = (None, None),
                     distinctive_features: list[str] | tuple[str, ...] = (), at: str | None = None,
                     ) -> tuple[PriorArtAssessment, ProviderMetadata]:
    """Search, record every search, and conclude only what the coverage supports."""
    at = at or utcnow()
    subject = text(subject, "assess_prior_art.subject")
    queries = texts(list(queries), "assess_prior_art.queries")
    if not queries:
        raise MalformedInput("at least one query is needed", "assess_prior_art.queries")
    domains = tuple(texts(list(domains), "assess_prior_art.domains"))
    time_range = period(time_range, "assess_prior_art.time_range")
    coverage = provider.coverage()
    if not isinstance(coverage, ProviderCoverage):
        raise MalformedInput("a provider's coverage must be a ProviderCoverage", "assess_prior_art.provider")
    unsearched = _uncovered(domains, coverage, time_range)

    searches, matches, failed = [], {}, 0
    for q in queries:
        limitations = list(coverage.limitations)
        try:
            raw = provider.search(q, domains, time_range)
        except Exception as exc:  # a provider failure is a coverage gap, recorded with its type
            failed += 1
            unsearched.append(f"query {q!r}: search failed ({type(exc).__name__})")
            limitations.append(f"search failed ({type(exc).__name__}); no results were obtained")
            raw = []
        if not isinstance(raw, list):
            raise MalformedInput(f"provider returned {type(raw).__name__}, not a list of hits",
                                 f"assess_prior_art.search[{q!r}]")
        hits = [prior_art_hit(h, f"assess_prior_art.search[{q!r}][{i}]") for i, h in enumerate(raw)]
        searches.append(context.ensure(PriorArt(
            q, list(coverage.sources), hits, time_range, list(domains),
            f"{len(hits)} result(s) from {', '.join(coverage.sources)}", limitations, created_at=at)))
        for h in hits:
            matches.setdefault((h["title"], h["uri"]), h)

    ordered = [matches[k] for k in sorted(matches)]
    if ordered:
        conclusion = PriorArtConclusion.MATCH_FOUND
    elif unsearched:
        conclusion = PriorArtConclusion.INCOMPLETE
    else:
        conclusion = PriorArtConclusion.NO_MATCH_WITHIN_COVERAGE
    nearest = [h["title"] for h in sorted(ordered, key=lambda h: (-len(h["matched_terms"]), h["title"]))[:3]]
    assessment = context.ensure(PriorArtAssessment(
        subject, conclusion, queries, [s.id for s in searches], list(coverage.sources), list(domains), time_range,
        ordered, nearest, list(distinctive_features), list(coverage.limitations), unsearched,
        derived_from=[s.id for s in searches], created_at=at))
    return assessment, ProviderMetadata(provider.name, provider.version, len(queries), failed)

