"""Shared fixtures. All sample data is fictional and labeled as such."""

from __future__ import annotations

from datetime import datetime, timezone

from lofgren_intelligence.orbital import TLE

EPOCH = datetime(2026, 9, 30, tzinfo=timezone.utc)
PHOENIX = (33.4484, -112.0740)

# A sun-synchronous orbit like Sentinel-2's (elements are synthetic).
SUN_SYNC = TLE("SENTINEL-2A", 40697, EPOCH, 98.5692, 340.0, 0.0001, 90.0, 270.0, 14.30818)

TEXTS = {
    "journal (fictional)": (
        "Industrial construction in the Phoenix metro increased sharply in 2026. Developers completed "
        "14 million square feet of industrial warehouse space in the Phoenix metro during the year."
    ),
    "permits (fictional)": (
        "Permit records show industrial construction in the Phoenix metro increased in 2026. "
        "Completed industrial warehouse space in the Phoenix metro reached 14.2 million square feet in 2026."
    ),
    "broker (fictional)": (
        "Industrial construction in the Phoenix metro decreased in 2026 as developers paused new starts. "
        "Industrial vacancy in the Phoenix metro rose to 11 percent in 2026."
    ),
}


# --- plan catalog test fixtures -------------------------------------------------
# The shipped catalog grants no paid plan (every paid allowance is undecided), so
# webhook/checkout mechanics are exercised against a copy in which "researcher"
# is open with a hypothetical allowance. This is a test fixture, not a decision.
TEST_PRICE_ID = "price_test_researcher"
TEST_PAID_UNITS = 2000.0
STRIPE_TEST_ENV = {"LI_STRIPE_MODE": "test", "LI_STRIPE_PRICE_ID_RESEARCHER_TEST": TEST_PRICE_ID}


def open_researcher_catalog(units: float = TEST_PAID_UNITS):
    import dataclasses

    from lofgren_intelligence.billing.catalog import AVAILABLE, CATALOG, QUOTA_WINDOW

    plan = CATALOG.get("researcher")
    allowance = dataclasses.replace(plan.allowance, units=float(units), window=QUOTA_WINDOW)
    return CATALOG.replace_plan("researcher", status=AVAILABLE, allowance=allowance)


def active_researcher(_sub=None):
    return {"status": "active", "price_ids": [TEST_PRICE_ID]}
