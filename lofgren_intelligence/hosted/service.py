"""Application service used by the remote MCP and HTTP entry point."""

from __future__ import annotations

import os
import uuid
from typing import Any

from .. import build_registry
from ..billing.pricing import PLANS, cheapest_plan, estimate, monthly_bill
from ..evidence.types import to_dict
from ..intent.compiler import compile_intent
from ..kernel.pipeline import estimate_run, run_investigation
from ..models.provider import default_provider
from ..research.planner import gap_unknowns, plan_research
from .costing import actual_run_cost
from .entitlements import EntitlementError, access_for_run
from .security import validate_remote_args
from .snapshots import durable_snapshot, summary
from .store import SupabaseStore, utcnow
from .stripe import create_checkout


class PublicServiceError(RuntimeError):
    pass


class PaymentRequired(PublicServiceError):
    pass


class QuotaExceeded(PublicServiceError):
    pass


class PublicService:
    def __init__(self, store: SupabaseStore | None = None) -> None:
        self.store = store or SupabaseStore()

    @staticmethod
    def _location(a: dict[str, Any]) -> dict[str, Any] | None:
        if a.get("lat") is not None and a.get("lon") is not None:
            return {"lat": float(a["lat"]), "lon": float(a["lon"]), "name": None}
        return None

    @staticmethod
    def _registry(a: dict[str, Any]):
        # Local file paths and arbitrary local sensors are deliberately absent from remote mode.
        return build_registry(
            texts=a.get("texts"),
            urls=a.get("urls"),
            fetch_orbits=bool(a.get("fetch_orbits")),
            imagery=bool(a.get("imagery")),
            search=a.get("search"),
        )

    def activate(self, user_id: str, email: str | None = None) -> dict[str, Any]:
        return self.store.activate_account(user_id, email)

    def account_status(self, user_id: str) -> dict[str, Any]:
        account = self.store.get_account(user_id)
        ent = self.store.get_entitlement(user_id)
        if not account:
            raise PublicServiceError("account is not activated")
        return {
            "activation_number": account.get("activation_number"),
            "entitlement": ent,
            "founding_free": bool(ent and ent.get("kind") == "founding_free"),
        }

    def usage_status(self, user_id: str) -> dict[str, Any]:
        decision = access_for_run(self.store, user_id, 0.0)
        return {
            "allowed": decision.allowed,
            "reason": decision.reason,
            "used_units_7d": decision.used_units,
            "quota_units_7d": decision.quota_units,
            "remaining_units_7d": max(0.0, decision.quota_units - decision.used_units),
            "kind": decision.entitlement.get("kind"),
            "plan_id": decision.entitlement.get("plan_id"),
        }

    def compile_objective(self, a: dict[str, Any]) -> dict[str, Any]:
        a = validate_remote_args(a)
        c = compile_intent(
            a["objective"],
            max_spend_usd=float(a.get("max_spend_usd", 5.0)),
            location=self._location(a),
        )
        return to_dict(c)

    def plan_research(self, a: dict[str, Any]) -> dict[str, Any]:
        a = validate_remote_args(a)
        c = compile_intent(
            a["objective"],
            max_spend_usd=float(a.get("max_spend_usd", 5.0)),
            location=self._location(a),
        )
        plan = plan_research(c, self._registry(a))
        plan_id = a.get("plan", "payg")
        est = estimate_run(plan, plan_id)
        return {
            "estimate": est.as_dict(),
            "spend_cap_usd": c.max_spend_usd,
            "estimated_work_units": plan.estimated_work_units,
            "tasks": [t.__dict__ for t in plan.tasks],
            "unknowns": [to_dict(u) for u in gap_unknowns(plan, c, PLANS[plan_id].rate)],
        }

    def _run(self, user_id: str, objective: str, a: dict[str, Any]) -> dict[str, Any]:
        a = validate_remote_args(a)
        c = compile_intent(
            objective,
            max_spend_usd=float(a.get("max_spend_usd", 5.0)),
            location=self._location(a),
        )
        plan = plan_research(c, self._registry(a))
        decision = access_for_run(self.store, user_id, float(plan.estimated_work_units))
        if not decision.allowed:
            if decision.entitlement.get("kind") == "paid_required":
                raise PaymentRequired(decision.reason)
            raise QuotaExceeded(decision.reason)

        # Public safety ceiling independent of user-supplied max_spend.
        public_cap = float(os.environ.get("LI_PUBLIC_MAX_ESTIMATED_USD_PER_RUN", "5.0"))
        execution_plan = a.get("plan", "payg")
        est = estimate_run(plan, execution_plan)
        if est.total_usd > public_cap:
            raise QuotaExceeded("run exceeds the public per-run cost ceiling")

        result = run_investigation(
            c,
            self._registry(a),
            default_provider(),
            execution_plan,
            approved=False,  # public V1 never executes external actions
        )
        snap = durable_snapshot(result)
        cost = actual_run_cost(result.provider_info, result.ledger)
        self.store.save_run(
            {
                "run_id": snap["run_id"],
                "user_id": user_id,
                "objective": objective,
                "status": "complete" if result.completed else "stopped",
                "summary": summary(snap),
                "snapshot": snap,
                "receipt": snap["receipt"],
                "knowledge_state": snap["knowledge_map2"],
                "report": snap["report"],
                "usage_units": snap["usage_units"],
                "known_cost_usd": cost.known_cost_usd,
                "unpriced_components": list(cost.unpriced_components),
                "created_at": utcnow(),
            }
        )
        self.store.record_usage(
            {
                "id": str(uuid.uuid4()),
                "user_id": user_id,
                "run_id": snap["run_id"],
                "operation": "investigate",
                "units": snap["usage_units"],
                "known_cost_usd": cost.known_cost_usd,
                "unpriced_components": list(cost.unpriced_components),
                "created_at": utcnow(),
            }
        )
        out = summary(snap)
        out["known_cost_usd"] = cost.known_cost_usd
        out["cost_fully_priced"] = cost.fully_priced
        out["unpriced_cost_components"] = list(cost.unpriced_components)
        return out

    def investigate(self, user_id: str, a: dict[str, Any]) -> dict[str, Any]:
        return self._run(user_id, a["objective"], a)

    def verify_claim(self, user_id: str, a: dict[str, Any]) -> dict[str, Any]:
        return self._run(user_id, f"Verify the claim: {a['claim']}", a)

    def _snapshot(self, user_id: str, run_id: str) -> dict[str, Any]:
        row = self.store.get_run(user_id, run_id)
        if not row:
            raise PublicServiceError("unknown run_id")
        snap = row.get("snapshot")
        if not isinstance(snap, dict):
            raise PublicServiceError("stored run snapshot is invalid")
        return snap

    def get_finding(self, user_id: str, a: dict[str, Any]) -> dict[str, Any]:
        snap = self._snapshot(user_id, a["run_id"])
        if a.get("finding_id"):
            found = next((x for x in snap["findings"] if x.get("id") == a["finding_id"]), None)
        elif a.get("question_index"):
            found = next((x for x in snap["findings"] if x.get("question_index") == int(a["question_index"])), None)
        else:
            found = None
        if not found:
            raise PublicServiceError("give a valid finding_id or question_index")
        return found

    def find_contradictions(self, user_id: str, a: dict[str, Any]) -> dict[str, Any]:
        return {"contradictions": self._snapshot(user_id, a["run_id"])["contradictions"]}

    def find_gaps(self, user_id: str, a: dict[str, Any]) -> dict[str, Any]:
        return {"unknowns": self._snapshot(user_id, a["run_id"])["unknowns"]}

    def trace_claim(self, user_id: str, a: dict[str, Any]) -> dict[str, Any]:
        traces = self._snapshot(user_id, a["run_id"])["traces"]
        if a.get("claim_id") not in traces:
            raise PublicServiceError("unknown claim_id")
        return traces[a["claim_id"]]

    def get_receipt(self, user_id: str, a: dict[str, Any]) -> dict[str, Any]:
        snap = self._snapshot(user_id, a["run_id"])
        return {"intact": snap["receipt_intact"], "receipt": snap["receipt"]}

    def export_state(self, user_id: str, a: dict[str, Any]) -> dict[str, Any]:
        return self._snapshot(user_id, a["run_id"])["state"]

    def export_knowledge_map2(self, user_id: str, a: dict[str, Any]) -> dict[str, Any]:
        return self._snapshot(user_id, a["run_id"])["knowledge_map2"]

    def render_report(self, user_id: str, a: dict[str, Any]) -> dict[str, Any]:
        snap = self._snapshot(user_id, a["run_id"])
        return {"run_id": a["run_id"], "format": "markdown", "report": snap["report"]}

    def pricing(self, a: dict[str, Any]) -> dict[str, Any]:
        d: dict[str, Any] = {"plans": {k: p.__dict__ for k, p in PLANS.items()}}
        if a.get("standard_units") is not None:
            su = float(a["standard_units"])
            hj = int(a.get("heavy_jobs", 0))
            d["bills"] = {k: monthly_bill(k, su, hj) for k in PLANS if k != "free"}
            d["cheapest"] = cheapest_plan(su, hj)
        d["example_estimate"] = estimate("payg", 40).as_dict()
        d["notice"] = "Published pricing is provisional until P95 actual COGS is fully measured."
        return d

    def checkout(self, user_id: str, base_url: str) -> dict[str, Any]:
        ent = self.store.get_entitlement(user_id)
        if not ent:
            raise PublicServiceError("entitlement missing")
        if ent.get("kind") == "founding_free":
            raise PublicServiceError("Founding Free accounts do not need checkout")
        session = create_checkout(
            user_id,
            success_url=base_url.rstrip("/") + "/billing/success?session_id={CHECKOUT_SESSION_ID}",
            cancel_url=base_url.rstrip("/") + "/billing/cancelled",
        )
        return {"checkout_url": session.get("url"), "session_id": session.get("id")}
