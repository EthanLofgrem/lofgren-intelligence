"""Confidence calibration.

A confidence of 0.8 must mean the claim turns out right about 80% of the
time. The calibrator records (raw confidence, was it right?) pairs as real
outcomes come in, and maps future raw scores through the observed hit rate
of their bin, shrunk toward the raw score while evidence is thin.

With no recorded outcomes it returns the raw score unchanged, and reports say
so: an uncalibrated number is a starting estimate, not a promise.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Calibrator:
    bins: int = 10
    prior_strength: float = 5.0  # pseudo-observations pulling toward the raw score
    records: list[tuple[float, bool]] = field(default_factory=list)

    @property
    def calibrated(self) -> bool:
        return len(self.records) >= self.bins * 3

    def _bin(self, p: float) -> int:
        return min(int(p * self.bins), self.bins - 1)

    def record(self, raw: float, correct: bool) -> None:
        if not 0.0 <= raw <= 1.0:
            raise ValueError("confidence must be in [0, 1]")
        self.records.append((raw, bool(correct)))

    def calibrate(self, raw: float) -> float:
        if not self.records:
            return raw
        b = self._bin(raw)
        hits = [ok for p, ok in self.records if self._bin(p) == b]
        n = len(hits)
        return (sum(hits) + self.prior_strength * raw) / (n + self.prior_strength)

    def expected_calibration_error(self) -> float | None:
        """Mean gap between stated confidence and observed accuracy, weighted by bin size."""
        if not self.records:
            return None
        total = len(self.records)
        err = 0.0
        for b in range(self.bins):
            group = [(p, ok) for p, ok in self.records if self._bin(p) == b]
            if group:
                conf = sum(p for p, _ in group) / len(group)
                acc = sum(ok for _, ok in group) / len(group)
                err += len(group) / total * abs(conf - acc)
        return err

    def save(self, path: str | Path) -> None:
        Path(path).write_text(json.dumps({"bins": self.bins, "prior_strength": self.prior_strength,
                                          "records": self.records}))

    @classmethod
    def load(cls, path: str | Path) -> "Calibrator":
        data = json.loads(Path(path).read_text())
        return cls(data["bins"], data["prior_strength"], [(float(p), bool(o)) for p, o in data["records"]])


def brier_score(records: list[tuple[float, bool]]) -> float | None:
    """Mean squared error of stated confidence against what happened (0 is perfect)."""
    if not records:
        return None
    return sum((p - float(ok)) ** 2 for p, ok in records) / len(records)


def reliability_table(records: list[tuple[float, bool]], bins: int = 10) -> list[dict]:
    """Stated confidence vs observed accuracy per bin: the data behind a reliability curve."""
    rows = []
    for b in range(bins):
        lo, hi = b / bins, (b + 1) / bins
        group = [(p, ok) for p, ok in records if lo <= p < hi or (b == bins - 1 and p == 1.0)]
        if group:
            rows.append({"bin": f"{lo:.1f}-{hi:.1f}", "count": len(group),
                         "stated": round(sum(p for p, _ in group) / len(group), 3),
                         "observed": round(sum(ok for _, ok in group) / len(group), 3)})
    return rows


class PredictionLog:
    """Append-only JSONL log of every confidence the system states.

    Each line: {claim_id, statement, confidence, raw, method, run_id, at, outcome}
    `outcome` starts null and is filled when the claim is later checked against
    reality (Outcome Intelligence, V5). Resolved lines feed the Calibrator.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def _read(self) -> list[dict]:
        if not self.path.exists():
            return []
        return [json.loads(line) for line in self.path.read_text().splitlines() if line.strip()]

    def append(self, entries: list[dict]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a") as fh:
            for e in entries:
                fh.write(json.dumps({**e, "outcome": e.get("outcome")}) + "\n")

    def resolve(self, claim_id: str, correct: bool) -> int:
        rows = self._read()
        n = 0
        for r in rows:
            if r["claim_id"] == claim_id and r.get("outcome") is None:
                r["outcome"] = bool(correct)
                n += 1
        self.path.write_text("".join(json.dumps(r) + "\n" for r in rows))
        return n

    def resolved(self) -> list[tuple[float, bool]]:
        return [(float(r["confidence"]), bool(r["outcome"])) for r in self._read() if r.get("outcome") is not None]

    def pending(self) -> list[dict]:
        return [r for r in self._read() if r.get("outcome") is None]

    def calibrator(self) -> Calibrator:
        """Fit on the raw (pre-calibration) scores, so calibration never compounds."""
        cal = Calibrator()
        for r in self._read():
            if r.get("outcome") is not None:
                cal.record(float(r.get("raw", r["confidence"])), bool(r["outcome"]))
        return cal

    def summary(self) -> dict:
        res = self.resolved()
        cal = self.calibrator()
        return {"stated": len(self._read()), "resolved": len(res), "brier": brier_score(res),
                "ece": cal.expected_calibration_error(), "reliability": reliability_table(res),
                "calibrated": cal.calibrated}
