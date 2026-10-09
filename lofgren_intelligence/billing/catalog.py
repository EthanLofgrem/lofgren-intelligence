"""The versioned plan catalog: the one source for what each plan is and grants.

Everything that shows, sells or grants a plan reads it: the pricing page, the
quota defaults, the checkout price allowlist, the Stripe webhook mapping and
the economic gate. `docs/PLAN_CATALOG.json` is generated from it
(`python -m lofgren_intelligence.billing.catalog --write`) and a test keeps the
two equal. Monetary values come from `billing.pricing.PLANS`.

Unknown = false. A value the code or the owner has not decided is the string
`"undecided"`; it is never replaced by a guess, and anything that would need it
(a paid grant, a checkout, the economic gate) fails closed.

Units. The hosted service meters one quantity, the intelligence unit (IU):
`li_reserve_usage` / `li_usage_events.units`. For research and discovery an IU
is a pricing work unit (WU): investigate reserves `plan.estimated_work_units`
and settles `CostLedger.total_units`. Ad hoc tools and build/measure/improve
are a flat IU amount per call. An "entry" (`Plan.entry_limit`) is never
counted, metered or enforced anywhere, so no entry -> unit conversion exists;
see docs/PLAN_CATALOG.md.

Stripe price ids are configuration, never catalog data: each paid plan names
the environment variables that hold its test and live price id, and
`LI_STRIPE_MODE` (`test` | `live`) selects which set is read.
"""

from __future__ import annotations

import dataclasses
import json
import math
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from .pricing import HEAVY_WORK_UNITS, PLANS as PRICING_PLANS

CATALOG_VERSION = "2026-10-06.1"
UNDECIDED = "undecided"

AVAILABLE = "available"
PLANNED = "planned"

STRIPE_MODE_ENV = "LI_STRIPE_MODE"
STRIPE_MODES = ("test", "live")

# Founding Free cohort: activation numbers 1..1000 (li_activate_account).
FOUNDING_FREE_MAX_ACTIVATION = 1000

MCP_REQUESTS_PER_MINUTE_ENV = "LI_MCP_REQUESTS_PER_MINUTE"
MCP_REQUESTS_PER_MINUTE_DEFAULT = 60

INTELLIGENCE_UNIT = {
    "id": "intelligence_unit",
    "name": "intelligence unit",
    "definition": (
        "The single metered quantity of the hosted service (li_usage_events.units). investigate and discover "
        "reserve and settle pricing work units (1 WU = 1 unit, ResearchPlan.estimated_work_units / "
        "CostLedger.total_units); ad hoc tools charge ADHOC_UNIT_COSTS per call; build_artifact charges 10, "
        "measure_outcome and evaluate_improvement 5 per call."
    ),
    "work_unit_ratio": 1.0,
}

ENTRY = {
    "id": "entry",
    "definition": (
        "Plan.entry_limit in billing.pricing. Never counted, metered or enforced by any code path; "
        "the hosted quota does not read it."
    ),
    "units_per_entry": UNDECIDED,
}

QUOTA_WINDOW = {"kind": "rolling", "seconds": 604800, "time_zone": "UTC",
                "description": "rolling 7 days ending now (database now(), UTC); not a calendar week"}


@dataclass(frozen=True)
class Allowance:
    units: float | str  # a number of intelligence units, or UNDECIDED
    window: Mapping[str, Any] | str
    operator_override_env: str | None = None
    source: str = ""
    unenforced_proposal: Mapping[str, Any] | None = None

    @property
    def decided(self) -> bool:
        return (not isinstance(self.units, str) and isinstance(self.window, Mapping)
                and self.window.get("kind") == "rolling" and math.isfinite(float(self.units))
                and float(self.units) >= 0)


@dataclass(frozen=True)
class CatalogPlan:
    id: str
    name: str
    status: str
    audience: str
    price_usd: float
    currency: str
    billing_interval: str  # "none" | "month" | "usage"
    rate_usd_per_work_unit: float | None
    heavy_job_price_usd: float | None
    included_heavy_jobs: int | None
    billable_unit: str
    allowance: Allowance
    heavy_work: Mapping[str, Any]
    concurrency: Mapping[str, Any]
    overage: Mapping[str, Any]
    cancellation: Mapping[str, Any]
    checkout_mode: str | None  # "subscription" | UNDECIDED | None (no checkout)
    stripe_price_env: Mapping[str, str] | None
    pricing_plan_id: str | None
    eligibility: Mapping[str, Any] | None = None
    features: tuple[str, ...] = field(default_factory=tuple)

    def as_dict(self) -> dict[str, Any]:
        d = dataclasses.asdict(self)
        d["features"] = list(self.features)
        return d


@dataclass(frozen=True)
class Catalog:
    version: str
    plans: tuple[CatalogPlan, ...]

    def get(self, plan_id: str | None) -> CatalogPlan | None:
        for p in self.plans:
            if p.id == plan_id:
                return p
        return None

    def replace_plan(self, plan_id: str, **changes: Any) -> "Catalog":
        """A copy with one plan changed (tests and owner what-ifs; the shipped catalog is never mutated)."""
        if self.get(plan_id) is None:
            raise KeyError(plan_id)
        return Catalog(self.version, tuple(dataclasses.replace(p, **changes) if p.id == plan_id else p
                                           for p in self.plans))

    def as_dict(self) -> dict[str, Any]:
        return {
            "catalog_version": self.version,
            "undecided_marker": UNDECIDED,
            "units": {"intelligence_unit": INTELLIGENCE_UNIT, "entry": ENTRY},
            "stripe_mode_env": STRIPE_MODE_ENV,
            "plans": [p.as_dict() for p in self.plans],
        }


def _undecided_allowance(pricing_id: str, override_env: str | None) -> Allowance:
    p = PRICING_PLANS[pricing_id]
    return Allowance(
        units=UNDECIDED,
        window=UNDECIDED,
        operator_override_env=override_env,
        source="owner decision required; an operator override never decides an undecided allowance",
        unenforced_proposal={"value": p.entry_limit, "measure": "entries", "period": p.limit_period,
                             "source": f"billing.pricing.PLANS[{pricing_id!r}].entry_limit (never enforced)"},
    )


_PAID_CONCURRENCY = {
    "requests_per_minute": MCP_REQUESTS_PER_MINUTE_DEFAULT,
    "requests_per_minute_env": MCP_REQUESTS_PER_MINUTE_ENV,
    "concurrent_jobs": UNDECIDED,
}
_PAID_CANCELLATION = {
    "implemented": ("Access follows Stripe: every lifecycle webhook re-reads the subscription and the entitlement is "
                    "paid only while it is active or trialing; otherwise it becomes paid_required with quota 0. "
                    "Account deletion cancels the subscription first."),
    "refund_and_proration": UNDECIDED,
}


def _paid_plan(pricing_id: str, catalog_id: str, audience: str, interval: str, checkout_mode: str,
               features: tuple[str, ...], override_env: str | None = None) -> CatalogPlan:
    p = PRICING_PLANS[pricing_id]
    env = catalog_id.upper()
    return CatalogPlan(
        id=catalog_id,
        name=p.name,
        status=PLANNED,
        audience=audience,
        price_usd=p.monthly_fee,
        currency="USD",
        billing_interval=interval,
        rate_usd_per_work_unit=p.rate,
        heavy_job_price_usd=p.heavy_price,
        included_heavy_jobs=p.included_heavy,
        billable_unit="intelligence_unit",
        allowance=_undecided_allowance(pricing_id, override_env),
        heavy_work={
            "heavy_job_work_units": HEAVY_WORK_UNITS,
            "included_heavy_jobs": p.included_heavy,
            "extra_heavy_job_price_usd": p.heavy_price,
            "conversion_to_allowance": UNDECIDED,
        },
        concurrency=_PAID_CONCURRENCY,
        overage={"policy": UNDECIDED},
        cancellation=_PAID_CANCELLATION,
        checkout_mode=checkout_mode,
        stripe_price_env={"test": f"LI_STRIPE_PRICE_ID_{env}_TEST", "live": f"LI_STRIPE_PRICE_ID_{env}_LIVE"},
        pricing_plan_id=pricing_id,
        features=features,
    )


CATALOG = Catalog(
    version=CATALOG_VERSION,
    plans=(
        CatalogPlan(
            id="founding_free",
            name="Founding Free",
            status=AVAILABLE,
            audience="Accounts 1–1,000",
            price_usd=0.0,
            currency="USD",
            billing_interval="none",
            rate_usd_per_work_unit=None,
            heavy_job_price_usd=None,
            included_heavy_jobs=None,
            billable_unit="intelligence_unit",
            allowance=Allowance(
                units=500.0,
                window=QUOTA_WINDOW,
                operator_override_env="LI_FOUNDER_WEEKLY_UNITS",
                source=("li_activate_account stores 500 on activation (migration 20261004201650_public_mcp.sql); "
                        "the override applies only where an entitlement has no stored quota"),
            ),
            heavy_work={
                "policy": "no separate heavy credits; all work is metered in intelligence units against the allowance",
                "heavy_job_work_units": HEAVY_WORK_UNITS,
                "conversion_to_allowance": "1 work unit = 1 intelligence unit",
            },
            concurrency={
                "requests_per_minute": MCP_REQUESTS_PER_MINUTE_DEFAULT,
                "requests_per_minute_env": MCP_REQUESTS_PER_MINUTE_ENV,
                "concurrent_jobs": "not limited per plan; atomic quota reservations bound total usage",
            },
            overage={"policy": "hard_cap", "detail": "work that would exceed the allowance is refused (QUOTA_EXCEEDED); "
                                                     "nothing is billed"},
            cancellation={"implemented": "no subscription; deleting the account ends access",
                          "refund_and_proration": "not applicable"},
            checkout_mode=None,
            stripe_price_env=None,
            pricing_plan_id=None,
            eligibility={"activation_number_min": 1, "activation_number_max": FOUNDING_FREE_MAX_ACTIVATION,
                         "after_cohort": "paid_required"},
            features=("Research and verification tools", "Evidence and source links",
                      "Weekly usage limit shown by usage_status"),
        ),
        _paid_plan("payg", "payg", "Flexible, occasional use", "usage", UNDECIDED,
                   ("No subscription", "Pay only for estimated, approved work", "Same evidence and traceability")),
        _paid_plan("researcher", "researcher", "For regular researchers", "month", "subscription",
                   ("Everything in Pay as you go", "Lower per-work-unit rate", "Included heavy credits"),
                   # LI_PAID_WEEKLY_UNITS predates the catalog and was paired with LI_PAID_PLAN_ID=researcher.
                   override_env="LI_PAID_WEEKLY_UNITS"),
        _paid_plan("good_idea", "good_idea", "For power users and teams", "month", "subscription",
                   ("Everything in Researcher", "Lowest per-work-unit rate", "Build, act and measure at scale")),
    ),
)


# ----------------------------------------------------------------------------- lookups

def get_plan(plan_id: str | None, catalog: Catalog | None = None) -> CatalogPlan | None:
    return (catalog or CATALOG).get(plan_id)


def effective_allowance_units(plan: CatalogPlan | None, env: Mapping[str, str] | None = None) -> float | None:
    """The plan's weekly allowance in intelligence units, or None when it is undecided (fail closed).

    An operator override (only where one existed before the catalog) replaces a
    decided value; it never decides an undecided one. A malformed override is
    ignored rather than trusted.
    """
    if plan is None or not plan.allowance.decided:
        return None
    units = float(plan.allowance.units)
    env = os.environ if env is None else env
    name = plan.allowance.operator_override_env
    raw = (env.get(name) or "").strip() if name else ""
    if raw:
        try:
            value = float(raw)
        except ValueError:
            return units
        if math.isfinite(value) and value >= 0:
            return value
    return units


def stripe_mode(env: Mapping[str, str] | None = None) -> str | None:
    env = os.environ if env is None else env
    mode = (env.get(STRIPE_MODE_ENV) or "").strip().lower()
    return mode if mode in STRIPE_MODES else None


def configured_price_id(plan: CatalogPlan, env: Mapping[str, str] | None = None) -> str | None:
    env = os.environ if env is None else env
    mode = stripe_mode(env)
    if mode is None or not plan.stripe_price_env:
        return None
    value = (env.get(plan.stripe_price_env[mode]) or "").strip()
    return value or None


def checkout_plans(catalog: Catalog | None = None, env: Mapping[str, str] | None = None) -> dict[str, str]:
    """plan id -> price id for every plan that may be checked out now.

    Only an `available` plan sold as a subscription, with a decided allowance
    and a price configured for the current Stripe mode, qualifies.
    """
    candidates: dict[str, str] = {}
    for p in (catalog or CATALOG).plans:
        if p.status != AVAILABLE or p.checkout_mode != "subscription":
            continue
        if effective_allowance_units(p, env) is None:
            continue
        price = configured_price_id(p, env)
        if price:
            candidates[p.id] = price
    # A Price ID is an entitlement identity. When two available plans share
    # one, checkout knows the requested plan but the webhook cannot recover it
    # from Stripe's subscription. Remove every ambiguous mapping rather than
    # granting whichever plan happens to appear first.
    prices = list(candidates.values())
    collisions = {price for price in prices if prices.count(price) > 1}
    return {plan_id: price for plan_id, price in candidates.items() if price not in collisions}


def plan_for_price(price_id: str | None, catalog: Catalog | None = None,
                   env: Mapping[str, str] | None = None) -> CatalogPlan | None:
    """The single grantable plan a Stripe price id belongs to, or None.

    Unknown and ambiguously configured price ids both grant nothing.
    """
    if not price_id:
        return None
    cat = catalog or CATALOG
    for plan_id, price in checkout_plans(cat, env).items():
        if price == price_id:
            return cat.get(plan_id)
    return None


# ----------------------------------------------------------------------------- generated document

JSON_PATH = Path(__file__).resolve().parents[2] / "docs" / "PLAN_CATALOG.json"


def catalog_json(catalog: Catalog | None = None) -> str:
    return json.dumps((catalog or CATALOG).as_dict(), indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    text = catalog_json()
    if args == ["--write"]:
        JSON_PATH.write_text(text, encoding="utf-8", newline="\n")
        return 0
    if args == ["--check"]:
        return 0 if JSON_PATH.read_text(encoding="utf-8") == text else 1
    sys.stdout.write(text)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
