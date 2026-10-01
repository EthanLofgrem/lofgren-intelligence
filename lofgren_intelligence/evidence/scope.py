"""Temporal and geographic scope for claims.

"Warehouse vacancy rose" is not one fact: it is a fact about a place and a
period. Scope is inferred from, in order of strength:
  1. the evidence's own validity window and location (sensors, orbits, imagery)
  2. explicit dates and years in the statement
  3. the place named in the statement, or the contract's place when the
     statement names it
Anything not established stays None; unknown scope is reported, not guessed.
"""

from __future__ import annotations

import re

from .types import Evidence, Scope

_MONTHS = ["january", "february", "march", "april", "may", "june", "july", "august", "september",
           "october", "november", "december"]
_ISO_DATE = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")
_MONTH_YEAR = re.compile(r"\b(" + "|".join(_MONTHS) + r")\s+(\d{4})\b", re.I)
_YEAR = re.compile(r"\b(19[5-9]\d|20\d{2})\b")
_YEAR_RANGE = re.compile(r"\b(19[5-9]\d|20\d{2})\s*(?:-|–|to|through)\s*(19[5-9]\d|20\d{2})\b")
_PLACE = re.compile(r"\b(?:in|across|near|around|throughout)\s+(?:the\s+)?([A-Z][a-zA-Z]+(?:\s+[A-Z][a-zA-Z]+){0,3})")
_MONTH_END = [31, 29, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31]


def _date_only(stamp: str | None) -> str | None:
    return stamp[:10] if stamp else None


def period_from_text(text: str) -> tuple[str | None, str | None]:
    m = _ISO_DATE.search(text)
    if m:
        d = m.group(0)
        return d, d
    m = _MONTH_YEAR.search(text)
    if m:
        month = _MONTHS.index(m.group(1).lower()) + 1
        year = m.group(2)
        return f"{year}-{month:02d}-01", f"{year}-{month:02d}-{_MONTH_END[month - 1]:02d}"
    m = _YEAR_RANGE.search(text)
    if m:
        return f"{m.group(1)}-01-01", f"{m.group(2)}-12-31"
    years = sorted({int(y) for y in _YEAR.findall(text)})
    if years:
        return f"{years[0]}-01-01", f"{years[-1]}-12-31"
    return None, None


def place_from_text(text: str, known_place: str | None = None) -> str | None:
    if known_place and known_place.lower() in text.lower():
        return known_place
    m = _PLACE.search(text)
    if m and m.group(1).lower() not in _MONTHS:
        return m.group(1).strip()
    return None


def infer_scope(statement: str, evidence: Evidence | None = None, contract_location: dict | None = None) -> Scope:
    scope = Scope()
    if evidence is not None and (evidence.valid_from or evidence.valid_to):
        scope.valid_from = _date_only(evidence.valid_from)
        scope.valid_to = _date_only(evidence.valid_to or evidence.valid_from)
    else:
        scope.valid_from, scope.valid_to = period_from_text(statement)
    known = (contract_location or {}).get("name")
    if evidence is not None and evidence.location is not None:
        loc = evidence.location
        scope.lat, scope.lon = loc.lat, loc.lon
        scope.geography = loc.name or (f"{loc.lat:.4f},{loc.lon:.4f}" if loc.lat is not None else None)
    if scope.geography is None:
        scope.geography = place_from_text(statement, known)
    return scope


def merge_scopes(scopes: list[Scope]) -> Scope:
    """The span a set of claims covers together."""
    froms = [s.valid_from for s in scopes if s.valid_from]
    tos = [s.valid_to for s in scopes if s.valid_to]
    places = {s.geography for s in scopes if s.geography}
    return Scope(
        valid_from=min(froms) if froms else None,
        valid_to=max(tos) if tos else None,
        geography=places.pop() if len(places) == 1 else ("; ".join(sorted(places)) if places else None),
    )
