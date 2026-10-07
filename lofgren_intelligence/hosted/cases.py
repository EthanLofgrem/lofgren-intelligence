"""Intelligence Case charters: content, versions, hashes and approval bindings.

A Case Charter is the scoped, user-reviewed description of a serious research
objective: the objective, the clarification answers (including explicit
unknowns), the evidence sources, and the budget. The service stores every
revision as a new version (li_case_charters) and authority to run research
comes only from a browser-recorded approval of one exact version and content
hash (li_case_approvals). Nothing a client sends -- in particular no
`approved: true` flag -- is authority.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
from typing import Any

from ..intent.clarification import clarify_objective, requires_clarification, to_dict as clarification_to_dict

CHARTER_SCHEMA = "lofgren.case-charter/1"

SOURCE_KEYS = ("texts", "urls", "search", "lat", "lon", "fetch_orbits", "imagery")

STATUS_FOR_CLARIFICATION = {
    "CLARIFICATION_REQUIRED": "clarifying",
    "READY_FOR_SCOPE_APPROVAL": "ready_for_approval",
}


class CaseInputError(ValueError):
    """A case argument is malformed (the service turns it into a typed refusal)."""


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    return value if math.isfinite(value) and value >= 0 else default


def default_budget() -> dict[str, float]:
    return {
        "max_spend_usd": _env_float("LI_CASE_DEFAULT_MAX_SPEND_USD", 5.0),
        "max_units": _env_float("LI_CASE_DEFAULT_MAX_UNITS", 100.0),
    }


def approval_ttl_seconds() -> int:
    return int(_env_float("LI_CASE_APPROVAL_TTL_MINUTES", 60.0) * 60) or 60


def normalize_budget(raw: dict[str, Any] | None, base: dict[str, Any] | None = None) -> dict[str, float]:
    out = dict(base or default_budget())
    for key in ("max_spend_usd", "max_units"):
        if raw is None or raw.get(key) is None:
            continue
        try:
            value = float(raw[key])
        except (TypeError, ValueError):
            raise CaseInputError(f"{key} must be a number") from None
        if not math.isfinite(value) or value < 0:
            raise CaseInputError(f"{key} must be a finite number >= 0")
        out[key] = value
    return {"max_spend_usd": float(out["max_spend_usd"]), "max_units": float(out["max_units"])}


def normalize_sources(raw: dict[str, Any] | None, base: dict[str, Any] | None = None) -> dict[str, Any]:
    out = dict(base or {})
    for key in SOURCE_KEYS:
        if raw is not None and raw.get(key) is not None:
            out[key] = raw[key]
    return {k: out[k] for k in SOURCE_KEYS if k in out}


def merge_answers(previous: dict[str, Any] | None, new: dict[str, Any] | None) -> dict[str, Any]:
    """Merge raw answers. An earlier answer -- an explicit unknown included -- is kept unless replaced."""
    merged = {str(k): v for k, v in (previous or {}).items()}
    for key, value in (new or {}).items():
        if value is None or (isinstance(value, str) and not value.strip()):
            continue
        merged[str(key)] = value
    return merged


def build_charter(
    objective: str,
    answers: dict[str, Any],
    budget: dict[str, float],
    sources: dict[str, Any],
) -> dict[str, Any]:
    """The charter document for one version (no case id or version inside, so content is hashable)."""
    clarification = clarification_to_dict(clarify_objective(objective, answers))
    return {
        "schema": CHARTER_SCHEMA,
        "objective": clarification["objective"],
        "status": clarification["status"],
        "round": clarification["round"],
        "requires_approval": requires_clarification(objective),
        "domain": clarification["domain"],
        "consequence": clarification["consequence"],
        "classification": clarification["classification"],
        "safety_notice": clarification["safety_notice"],
        "exclusions": clarification["exclusions"],
        "questions": clarification["questions"],
        "accepted_answers": clarification["accepted_answers"],
        "critical_unknowns": clarification["critical_unknowns"],
        "case_charter": clarification["case_charter"],
        "budget": dict(budget),
        "sources": dict(sources),
    }


def canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, default=str)


def content_hash(case_id: str, charter: dict[str, Any], answers: dict[str, Any]) -> str:
    """SHA-256 over the case id, the charter and the raw answers.

    The case id is inside the hash so that an approval of one case can never match
    another case whose charter happens to have identical content.
    """
    body = {"case_id": str(case_id), "charter": charter, "answers": answers}
    return hashlib.sha256(canonical(body).encode("utf-8")).hexdigest()


def approval_scope(charter: dict[str, Any]) -> dict[str, Any]:
    """What an approval authorizes: the exact objective, accepted scope, unknowns and sources."""
    return {
        "objective": charter["objective"],
        "accepted_answers": charter.get("accepted_answers") or {},
        "critical_unknowns": charter.get("critical_unknowns") or [],
        "excluded_scope": ((charter.get("case_charter") or {}).get("excluded_scope") or []),
        "sources": charter.get("sources") or {},
    }


def client_view(case: dict[str, Any], row: dict[str, Any], approval_url: str | None) -> dict[str, Any]:
    charter = row["charter"]
    return {
        "kind": "intelligence_case",
        "case_id": str(case["id"]),
        "case_status": case.get("status"),
        "charter_version": int(row["version"]),
        "content_hash": row["content_hash"],
        "status": charter["status"],
        "objective": charter["objective"],
        "round": charter.get("round"),
        "domain": charter.get("domain"),
        "consequence": charter.get("consequence"),
        "safety_notice": charter.get("safety_notice"),
        "exclusions": charter.get("exclusions") or [],
        "questions": charter.get("questions") or [],
        "accepted_answers": charter.get("accepted_answers") or {},
        "critical_unknowns": charter.get("critical_unknowns") or [],
        "case_charter": charter.get("case_charter"),
        "budget": charter.get("budget"),
        "sources": sorted((charter.get("sources") or {}).keys()),
        "approval_required": True,
        "approval_url": approval_url,
    }
