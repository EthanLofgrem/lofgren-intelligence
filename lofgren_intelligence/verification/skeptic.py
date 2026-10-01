"""The skeptic: an adversarial pass over claims and evidence only.

It never sees the report or any narrative, only the typed graph, and it looks
for reasons a finding should not be trusted:

  unsupported     the claim's words or numbers are not in the evidence it cites
  circular        its "independent" support includes copies of one source
  stale           the period it describes ended long ago
  unscoped        a number or trend with no stated period
  inference       the claim came from a model, not from a source

Issues are attached to the claim. Serious ones lower its status; none raise it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone

from ..evidence.graph import EvidenceGraph
from ..evidence.types import ClaimOrigin, ClaimStatus, ClaimType, topic_tokens

STALE_YEARS = 3
WORD_COVERAGE = 0.7

_DOWNGRADE = {
    ClaimStatus.VERIFIED: ClaimStatus.PARTIALLY_VERIFIED,
    ClaimStatus.PARTIALLY_VERIFIED: ClaimStatus.SUPPORTED,
}


@dataclass
class SkepticReport:
    issues: dict[str, list[str]] = field(default_factory=dict)  # claim id -> issues
    downgraded: list[str] = field(default_factory=list)

    @property
    def count(self) -> int:
        return sum(len(v) for v in self.issues.values())


def _numbers(text: str) -> set[str]:
    return {n.replace(",", "") for n in re.findall(r"\d[\d,]*(?:\.\d+)?", text)}


def review(graph: EvidenceGraph, now: datetime | None = None) -> SkepticReport:
    now = now or datetime.now(timezone.utc)
    rep = SkepticReport()
    for c in graph.claims.values():
        issues: list[str] = []
        serious = False

        if c.origin == ClaimOrigin.EXTRACTED and c.supporting:
            origin_text = graph.evidence[c.supporting[0]].content
            ev_words = set(topic_tokens(origin_text))
            words = set(c.topic)
            coverage = len(words & ev_words) / len(words) if words else 1.0
            missing_numbers = _numbers(c.statement) - _numbers(origin_text)
            if coverage < WORD_COVERAGE or missing_numbers:
                detail = f"numbers {sorted(missing_numbers)} absent" if missing_numbers else f"word coverage {coverage:.0%}"
                issues.append(f"unsupported: statement not found in its cited evidence ({detail})")
                serious = True

        if c.origin == ClaimOrigin.INFERRED:
            issues.append("inference: produced by a model, not stated by a source")

        srcs = graph.sources_for_claim(c.id)
        ids = {s.id for s in srcs}
        copies = [s for s in srcs if set(s.derived_from) & ids]
        if copies:
            issues.append(f"circular: {len(copies)} supporting source(s) copy another supporting source")

        if c.scope.valid_to:
            try:
                end_year = int(c.scope.valid_to[:4])
                if now.year - end_year >= STALE_YEARS:
                    issues.append(f"stale: describes a period ending {end_year}")
                    serious = serious or c.claim_type in (ClaimType.TREND, ClaimType.QUANTITATIVE)
            except ValueError:
                pass
        elif c.claim_type in (ClaimType.QUANTITATIVE, ClaimType.TREND) and c.origin != ClaimOrigin.OBSERVED:
            issues.append("unscoped: no time period stated for a number or trend")

        if issues:
            c.issues = sorted(set(c.issues) | set(issues))
            rep.issues[c.id] = issues
        if serious and c.status in _DOWNGRADE:
            c.status = _DOWNGRADE[c.status]
            rep.downgraded.append(c.id)
    return rep
