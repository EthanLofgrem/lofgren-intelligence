"""Economic certification for activating paid public access."""

from __future__ import annotations

import math
import os
from dataclasses import dataclass
from typing import Any

from ..billing.catalog import AVAILABLE, CatalogPlan, effective_allowance_units

# The fewest real, fully-priced samples a paid plan can be certified on. The
# operator may raise it with LI_ECON_MIN_SAMPLES but never lower it.
MIN_SAMPLES_FLOOR = 100


@dataclass(frozen=True)
class EconomicGate:
    passed: bool
    sample_count: int
    p95_cost_per_unit: float | None
    max_cost_per_unit: float | None
    net_monthly_revenue: float | None
    target_margin: float
    reasons: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "sample_count": self.sample_count,
            "p95_cost_per_unit": self.p95_cost_per_unit,
            "max_cost_per_unit": self.max_cost_per_unit,
            "net_monthly_revenue": self.net_monthly_revenue,
            "target_margin": self.target_margin,
            "reasons": list(self.reasons),
        }


def _required_float(name: str, reasons: list[str]) -> float | None:
    raw = os.environ.get(name)
    if raw in (None, ""):
        reasons.append(f"missing:{name}")
        return None
    value = float(raw)
    if value < 0:
        reasons.append(f"invalid:{name}")
        return None
    return value


def min_samples_required(reasons: list[str] | None = None) -> int:
    """Samples needed for paid-plan certification: never below MIN_SAMPLES_FLOOR.

    LI_ECON_MIN_SAMPLES can only raise the floor; a lower value is ignored. A
    non-integer value fails closed (recorded in `reasons`) and the floor applies.
    """
    raw = os.environ.get("LI_ECON_MIN_SAMPLES")
    if raw in (None, ""):
        return MIN_SAMPLES_FLOOR
    try:
        requested = int(str(raw).strip())
    except ValueError:
        if reasons is not None:
            reasons.append("invalid:LI_ECON_MIN_SAMPLES")
        return MIN_SAMPLES_FLOOR
    return max(MIN_SAMPLES_FLOOR, requested)


def _p95(values: list[float]) -> float:
    ordered = sorted(values)
    idx = max(0, math.ceil(0.95 * len(ordered)) - 1)
    return ordered[idx]


def _plan_reasons(plan: CatalogPlan, monthly_fee: float | None, weekly_units: float | None) -> list[str]:
    """Extra refusals when certifying a specific catalog plan; they only ever add reasons.

    The gate's economics must describe the plan actually being sold: an
    unavailable plan, an undecided allowance, a non-monthly plan or gate inputs
    that differ from the catalog price/allowance all fail closed.
    """
    out: list[str] = []
    if plan.status != AVAILABLE:
        out.append(f"plan_not_available:{plan.id}")
    if plan.billing_interval != "month":
        out.append(f"unsupported_billing_interval:{plan.id}")
    allowance = effective_allowance_units(plan)
    if allowance is None:
        out.append(f"undecided_allowance:{plan.id}")
    elif weekly_units is not None and abs(weekly_units - allowance) > 1e-9:
        out.append("mismatch:LI_PAID_WEEKLY_UNITS")
    if monthly_fee is not None and abs(monthly_fee - plan.price_usd) > 1e-9:
        out.append("mismatch:LI_PAID_MONTHLY_USD")
    return out


def certify_paid_plan(samples: list[dict[str, Any]], plan: CatalogPlan | None = None) -> EconomicGate:
    """Fail closed unless real, fully-priced samples support the plan margin.

    With `plan`, the catalog plan being sold must also be available, decided
    and identical to the gate's price and allowance inputs.
    """
    reasons: list[str] = []
    min_samples = min_samples_required(reasons)
    target_margin = float(os.environ.get("LI_TARGET_GROSS_MARGIN", "0.65"))
    monthly_fee = _required_float("LI_PAID_MONTHLY_USD", reasons)
    weekly_units = _required_float("LI_PAID_WEEKLY_UNITS", reasons)
    fee_pct = _required_float("LI_PAYMENT_FEE_PERCENT", reasons)
    fee_fixed = _required_float("LI_PAYMENT_FEE_FIXED_USD", reasons)
    if plan is not None:
        reasons.extend(_plan_reasons(plan, monthly_fee, weekly_units))

    if not 0 < target_margin < 1:
        reasons.append("invalid:LI_TARGET_GROSS_MARGIN")

    priced: list[float] = []
    for row in samples:
        missing = row.get("unpriced_components") or []
        units = float(row.get("units") or 0)
        cost = float(row.get("known_cost_usd") or 0)
        if missing:
            reasons.append("unpriced_sample")
            continue
        if units > 0 and cost >= 0:
            priced.append(cost / units)

    if len(priced) < min_samples:
        reasons.append(f"insufficient_samples:{len(priced)}/{min_samples}")

    p95 = _p95(priced) if priced else None
    net_revenue: float | None = None
    max_cost: float | None = None
    if None not in (monthly_fee, weekly_units, fee_pct, fee_fixed) and weekly_units and monthly_fee is not None:
        assert fee_pct is not None and fee_fixed is not None
        gross = monthly_fee
        net_revenue = gross - gross * fee_pct - fee_fixed
        monthly_units = weekly_units * 52 / 12
        if net_revenue <= 0 or monthly_units <= 0:
            reasons.append("nonpositive_plan_economics")
        else:
            revenue_per_unit = net_revenue / monthly_units
            max_cost = revenue_per_unit * (1 - target_margin)
            if p95 is not None and p95 > max_cost:
                reasons.append("p95_cost_exceeds_margin_ceiling")

    # Deduplicate while preserving stable order.
    reasons = list(dict.fromkeys(reasons))
    passed = not reasons and p95 is not None and max_cost is not None
    return EconomicGate(
        passed=passed,
        sample_count=len(priced),
        p95_cost_per_unit=None if p95 is None else round(p95, 8),
        max_cost_per_unit=None if max_cost is None else round(max_cost, 8),
        net_monthly_revenue=None if net_revenue is None else round(net_revenue, 4),
        target_margin=target_margin,
        reasons=tuple(reasons),
    )
