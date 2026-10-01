"""Source lineage: two pages repeating one press release are one confirmation.

After collection, sources are merged into one independence group when:
  * a source declares it is derived from another (derived_from)
  * two passages from different sources are near-duplicates (syndication)
  * a passage credits another collected source ("according to <publisher>")

The verifier counts independent groups, so merging here directly lowers the
confidence of claims that only looked independently confirmed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .graph import EvidenceGraph

SHINGLE_WORDS = 5
NEAR_DUPLICATE = 0.6


def shingles(text: str, k: int = SHINGLE_WORDS) -> set[tuple[str, ...]]:
    words = re.findall(r"[a-z0-9]+", text.lower())
    if len(words) < k:
        return {tuple(words)} if words else set()
    return {tuple(words[i:i + k]) for i in range(len(words) - k + 1)}


def similarity(a: str, b: str) -> float:
    sa, sb = shingles(a), shingles(b)
    return len(sa & sb) / len(sa | sb) if sa and sb else 0.0


@dataclass
class LineageLink:
    source: str
    derived_from: str
    relation: str  # declared | syndicated | quotes
    detail: str = ""


class _UnionFind:
    def __init__(self) -> None:
        self.parent: dict[str, str] = {}

    def find(self, x: str) -> str:
        self.parent.setdefault(x, x)
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, a: str, b: str) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[max(ra, rb)] = min(ra, rb)


def resolve_lineage(graph: EvidenceGraph) -> list[LineageLink]:
    links: list[LineageLink] = []
    uf = _UnionFind()
    for s in graph.sources.values():
        uf.find(s.independence_group)
        for parent_id in s.derived_from:
            if parent_id in graph.sources:
                uf.union(s.independence_group, graph.sources[parent_id].independence_group)
                links.append(LineageLink(s.id, parent_id, "declared"))

    evidence = list(graph.evidence.values())
    for i, a in enumerate(evidence):
        for b in evidence[i + 1:]:
            sa, sb = graph.sources[a.source_id], graph.sources[b.source_id]
            if uf.find(sa.independence_group) == uf.find(sb.independence_group):
                continue
            sim = similarity(a.content, b.content)
            if sim >= NEAR_DUPLICATE:
                # the later retrieval (or later publication) is treated as the copy
                copy, orig = (sb, sa) if (sb.published_at or sb.retrieved_at) >= (sa.published_at or sa.retrieved_at) else (sa, sb)
                uf.union(copy.independence_group, orig.independence_group)
                if orig.id not in copy.derived_from:
                    copy.derived_from.append(orig.id)
                links.append(LineageLink(copy.id, orig.id, "syndicated", f"passage similarity {sim:.2f}"))

    for ev in evidence:
        src = graph.sources[ev.source_id]
        text = ev.content.lower()
        for other in graph.sources.values():
            if other.id == src.id or not other.publisher:
                continue
            if uf.find(src.independence_group) == uf.find(other.independence_group):
                continue
            if f"according to {other.publisher.lower()}" in text or f"{other.publisher.lower()} reported" in text:
                uf.union(src.independence_group, other.independence_group)
                if other.id not in src.derived_from:
                    src.derived_from.append(other.id)
                links.append(LineageLink(src.id, other.id, "quotes", other.publisher))

    for s in graph.sources.values():
        s.independence_group = uf.find(s.independence_group)
    for link in links:
        graph._edge(link.source, link.derived_from, f"derived_from:{link.relation}")
    return links
