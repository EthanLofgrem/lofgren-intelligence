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
