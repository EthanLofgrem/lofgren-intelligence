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
