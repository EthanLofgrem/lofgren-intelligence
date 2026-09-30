"""Cost ledger: every operation a run performs, what it consumed, what it cost.

Kinds: retrieval (a source call), model (a reasoning call), compute
(verification, calculations), external_data (licensed data passed through),
report. The research planner can then compare information gained per dollar
across sources, and the user can see exactly where the money went.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field


@dataclass
class LedgerEntry:
    seq: int
    stage: str
    kind: str  # retrieval | model | compute | external_data | report
    actor: str  # adapter id, provider name or module
    work_units: float
    usd: float
    detail: str = ""
    evidence_items: int = 0  # what the operation produced


@dataclass
class CostLedger:
    rate_usd_per_unit: float
    entries: list[LedgerEntry] = field(default_factory=list)

    def record(self, stage: str, kind: str, actor: str, work_units: float = 0.0, external_usd: float = 0.0,
               detail: str = "", evidence_items: int = 0) -> LedgerEntry:
        entry = LedgerEntry(len(self.entries) + 1, stage, kind, actor, work_units,
                            round(work_units * self.rate_usd_per_unit + external_usd, 6), detail, evidence_items)
        self.entries.append(entry)
        return entry

    @property
    def total_units(self) -> float:
        return sum(e.work_units for e in self.entries)

    @property
    def total_usd(self) -> float:
        return round(sum(e.usd for e in self.entries), 6)

    def by_kind(self) -> dict[str, dict[str, float]]:
        out: dict[str, dict[str, float]] = {}
        for e in self.entries:
            row = out.setdefault(e.kind, {"operations": 0, "work_units": 0.0, "usd": 0.0})
            row["operations"] += 1
            row["work_units"] += e.work_units
            row["usd"] = round(row["usd"] + e.usd, 6)
        return out

    def yield_by_actor(self) -> dict[str, dict[str, float]]:
        """Evidence items per work unit, per source: the input to value-per-cost planning."""
        out: dict[str, dict[str, float]] = {}
        for e in self.entries:
            if e.kind != "retrieval":
                continue
            row = out.setdefault(e.actor, {"work_units": 0.0, "evidence_items": 0})
            row["work_units"] += e.work_units
            row["evidence_items"] += e.evidence_items
        for row in out.values():
            row["items_per_unit"] = round(row["evidence_items"] / row["work_units"], 3) if row["work_units"] else 0.0
        return out

    def to_json(self) -> dict:
        return {"rate_usd_per_unit": self.rate_usd_per_unit, "total_units": self.total_units,
                "total_usd": self.total_usd, "by_kind": self.by_kind(),
                "entries": [asdict(e) for e in self.entries]}
