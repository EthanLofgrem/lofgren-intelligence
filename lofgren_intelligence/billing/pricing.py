"""Pricing: work units, job classes and plans.

Base unit u = $0.0312. Every job is metered in work units (WU) and falls into
a class. Classes below "heavy" are priced as class_multiplier x plan rate;
a heavy job is the full loop and is priced per plan (or covered by an
included heavy credit). Jobs larger than one heavy unit are "projects",
priced as whole heavy units after an estimate the user approves.

Monthly bill:
    Bill = F + r * n + p * max(0, h - k) + data_cost * (1 + markup)
      F = plan fee, r = plan rate, n = standard units used,
      p = extra heavy price, h = heavy jobs, k = included heavy jobs.

Every price here must satisfy price * (1 - target_margin) >= cost to serve.
Measure real cost per WU during beta before locking these numbers.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

BASE_UNIT_USD = 0.0312
HEAVY_WORK_UNITS = 400  # one heavy job = 400 base units = $12.48 pay-as-you-go
DATA_MARKUP = 0.20  # margin on licensed third-party data passed through

# class name -> (max work units for the class, price multiplier in base units)
JOB_CLASSES: tuple[tuple[str, float, int], ...] = (
    ("standard", 1, 1),
    ("verified", 4, 4),
    ("deep", 40, 40),
    ("heavy", HEAVY_WORK_UNITS, HEAVY_WORK_UNITS),
)


@dataclass(frozen=True)
class Plan:
    id: str
    name: str
    monthly_fee: float
    rate: float  # $ per base unit for standard / verified / deep jobs
    heavy_price: float  # $ per heavy job beyond included credits
    included_heavy: int
    entry_limit: int
    limit_period: str  # "week" | "month"
    stages: tuple[str, ...] = field(default_factory=tuple)  # empty = all available
    max_class: str = "project"
    free_heavy_trials: int = 0


PLANS: dict[str, Plan] = {
    "free": Plan("free", "Free", 0.0, 0.0, 0.0, 0, 25, "week",
                 stages=("intent", "plan", "sense", "research", "verify", "report"),
                 max_class="verified", free_heavy_trials=1),
    "payg": Plan("payg", "Pay as you go", 0.0, BASE_UNIT_USD, 12.48, 0, 500, "week"),
    "researcher": Plan("researcher", "Researcher", 49.99, BASE_UNIT_USD / 2, 9.36, 4, 400, "week"),
    "good_idea": Plan("good_idea", "Good Idea", 79.99, 0.005, 6.24, 6, 20_000, "month"),
}

_CLASS_ORDER = [c[0] for c in JOB_CLASSES] + ["project"]


def classify(work_units: float) -> str:
    for name, max_units, _ in JOB_CLASSES:
        if work_units <= max_units:
            return name
    return "project"


@dataclass
class Estimate:
    plan: str
    job_class: str
    work_units: float
    platform_usd: float
    data_usd: float
    heavy_units: int  # heavy credits or heavy charges this job consumes
    allowed: bool
    reason: str = ""

    @property
    def total_usd(self) -> float:
        return round(self.platform_usd + self.data_usd, 4)

    def as_dict(self) -> dict:
        return {**self.__dict__, "total_usd": self.total_usd}


def estimate(plan_id: str, work_units: float, data_cost_usd: float = 0.0,
             heavy_credits_left: int | None = None) -> Estimate:
    plan = PLANS[plan_id]
    job_class = classify(work_units)
    data = round(data_cost_usd * (1 + DATA_MARKUP), 4)
    if _CLASS_ORDER.index(job_class) > _CLASS_ORDER.index(plan.max_class):
        allowed = plan.free_heavy_trials > 0 and job_class == "heavy"
        return Estimate(plan_id, job_class, work_units, 0.0, data, 1 if allowed else 0, allowed,
                        "" if allowed else f"{plan.name} plan runs jobs up to '{plan.max_class}'; upgrade to run '{job_class}'")
    if job_class in ("heavy", "project"):
        units = max(1, math.ceil(work_units / HEAVY_WORK_UNITS))
        credits = plan.included_heavy if heavy_credits_left is None else heavy_credits_left
        paid = max(0, units - credits)
        return Estimate(plan_id, job_class, work_units, round(paid * plan.heavy_price, 4), data, units, True)
    multiplier = next(m for n, _, m in JOB_CLASSES if n == job_class)
    return Estimate(plan_id, job_class, work_units, round(multiplier * plan.rate, 4), data, 0, True)


def monthly_bill(plan_id: str, standard_units: float, heavy_jobs: int, data_cost_usd: float = 0.0) -> float:
    """Bill = F + r*n + p*max(0, h-k) + data*(1+markup)."""
    p = PLANS[plan_id]
    return round(p.monthly_fee + p.rate * standard_units
                 + p.heavy_price * max(0, heavy_jobs - p.included_heavy)
                 + data_cost_usd * (1 + DATA_MARKUP), 2)


def max_cost_to_serve(price_usd: float, target_margin: float = 0.5) -> float:
    """The most a job may cost you to run at this price and margin."""
    return price_usd * (1 - target_margin)


def cheapest_plan(standard_units: float, heavy_jobs: int, data_cost_usd: float = 0.0) -> str:
    paid = [p for p in PLANS if p != "free"]
    return min(paid, key=lambda p: monthly_bill(p, standard_units, heavy_jobs, data_cost_usd))
