"""Public entitlement and quota policy."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from ..billing.catalog import Catalog, effective_allowance_units, get_plan
from .store import SupabaseStore


class EntitlementError(RuntimeError):
    pass


@dataclass(frozen=True)
class AccessDecision:
    allowed: bool
    reason: str
    entitlement: dict
    used_units: float
    quota_units: float


def founder_weekly_units(catalog: Catalog | None = None) -> float:
    """Founding Free weekly allowance: the catalog value, or its LI_FOUNDER_WEEKLY_UNITS operator override."""
    units = effective_allowance_units(get_plan("founding_free", catalog))
    if units is None:  # the shipped catalog decides it; a catalog that does not fails closed
        return 0.0
    return units


def paid_weekly_units(plan_id: str | None, catalog: Catalog | None = None) -> float | None:
    """A paid plan's weekly allowance from the catalog, or None when the plan is unknown or undecided."""
    plan = get_plan(plan_id, catalog)
    if plan is None or plan.id == "founding_free":
        return None
    return effective_allowance_units(plan)


def access_for_run(store: SupabaseStore, user_id: str, estimated_units: float = 0.0,
                   catalog: Catalog | None = None) -> AccessDecision:
    ent = store.get_entitlement(user_id)
    if not ent:
        account = store.get_account(user_id)
        if not account:
            raise EntitlementError("account is not activated")
        ent = store.get_entitlement(user_id)
    if not ent:
        raise EntitlementError("entitlement is missing")

    kind = str(ent.get("kind") or "")
    active = bool(ent.get("active"))
    if kind == "paid_required" or not active:
        return AccessDecision(False, "paid entitlement required", ent, 0.0, 0.0)

    # A paid entitlement is honoured only for a catalog plan whose allowance is
    # decided; an unknown or undecided plan fails closed (unknown = false).
    if kind != "founding_free" and paid_weekly_units(ent.get("plan_id"), catalog) is None:
        return AccessDecision(False, "plan allowance is undecided", ent, 0.0, 0.0)

    # An explicit stored quota, including 0, is authoritative; only a missing
    # value falls back to the catalog allowance. (0 used to fall through to the
    # default, so zeroing a quota silently granted the full plan.)
    stored = ent.get("quota_units_per_week")
    if stored is not None:
        quota = float(stored)
    elif kind == "founding_free":
        quota = founder_weekly_units(catalog)
    else:
        quota = float(paid_weekly_units(ent.get("plan_id"), catalog) or 0.0)
    since = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
    used = store.usage_units_since(user_id, since)
    if used + max(0.0, estimated_units) > quota:
        return AccessDecision(False, "weekly intelligence-unit quota reached", ent, used, quota)
    return AccessDecision(True, "", ent, used, quota)
