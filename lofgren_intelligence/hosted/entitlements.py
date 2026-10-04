"""Public entitlement and quota policy."""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

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


def founder_weekly_units() -> float:
    return float(os.environ.get("LI_FOUNDER_WEEKLY_UNITS", "500"))


def paid_weekly_units() -> float:
    return float(os.environ.get("LI_PAID_WEEKLY_UNITS", "2000"))


def access_for_run(store: SupabaseStore, user_id: str, estimated_units: float = 0.0) -> AccessDecision:
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

    quota = float(ent.get("quota_units_per_week") or (
        founder_weekly_units() if kind == "founding_free" else paid_weekly_units()
    ))
    since = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
    used = store.usage_units_since(user_id, since)
    if used + max(0.0, estimated_units) > quota:
        return AccessDecision(False, "weekly intelligence-unit quota reached", ent, used, quota)
    return AccessDecision(True, "", ent, used, quota)
