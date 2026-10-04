"""Actual COGS estimation kept separate from customer pricing.

Unknown/unpriced components are surfaced explicitly. Paid-plan economics must
not be certified while material cost components are unpriced.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class CostResult:
    known_cost_usd: float
    unpriced_components: tuple[str, ...]

    @property
    def fully_priced(self) -> bool:
        return not self.unpriced_components


def _rate(name: str) -> float | None:
    raw = os.environ.get(name)
    if raw in (None, ""):
        return None
    value = float(raw)
    if value < 0:
        raise ValueError(f"{name} must be non-negative")
    return value


def actual_run_cost(provider_info: dict[str, Any], ledger: Any) -> CostResult:
    usage = (provider_info or {}).get("usage") or {}
    calls = int(usage.get("calls") or 0)
    input_tokens = int(usage.get("input_tokens") or 0)
    output_tokens = int(usage.get("output_tokens") or 0)
    known = 0.0
    missing: list[str] = []

    provider_name = str((provider_info or {}).get("name") or "")
    if provider_name not in {"", "heuristic"} and calls:
        if input_tokens or output_tokens:
            rin = _rate("LI_MODEL_INPUT_USD_PER_M_TOKENS")
            rout = _rate("LI_MODEL_OUTPUT_USD_PER_M_TOKENS")
            if rin is None or rout is None:
                missing.append("model_token_rates")
            else:
                known += input_tokens / 1_000_000 * rin + output_tokens / 1_000_000 * rout
        else:
            missing.append("model_usage")

    retrieval_calls = 0
    external_known = 0.0
    if ledger is not None:
        for e in ledger.entries:
            if e.kind == "retrieval":
                retrieval_calls += 1
            if e.kind == "external_data":
                ledger_rate = float(getattr(ledger, "rate_usd_per_unit", 0.0) or 0.0)
                external_known += max(0.0, float(e.usd) - float(e.work_units) * ledger_rate)
    known += external_known
    if retrieval_calls:
        rr = _rate("LI_RETRIEVAL_USD_PER_CALL")
        if rr is None:
            missing.append("retrieval_rate")
        else:
            known += retrieval_calls * rr

    infra = _rate("LI_INFRA_USD_PER_RUN")
    if infra is None:
        missing.append("infra_per_run")
    else:
        known += infra

    return CostResult(round(known, 8), tuple(sorted(set(missing))))
