"""Application service used by the remote MCP and HTTP entry point."""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone
from typing import Any

from .. import build_registry
from ..billing.pricing import PLANS, cheapest_plan, estimate, monthly_bill
from ..evidence.types import to_dict
from ..discovery import DiscoveryObjective
from ..discovery.candidates import DesignSpace, evaluate_candidates
from ..discovery.context import DiscoveryContext
from ..discovery.frame import frame_problem
from ..discovery.optimize import optimize, problem_from_json
from ..discovery.pipeline import _prior_art_provider, run_discovery
from ..discovery.prior_art import assess_prior_art
from ..intent.compiler import compile_intent
from ..kernel.pipeline import estimate_run, run_investigation
from ..models.provider import default_provider
from ..orbital.catalog import IMAGING_SATELLITES, fetch_tles
from ..orbital.propagate import PROPAGATOR, find_passes
from ..orbital.tle import parse_tle_text
from ..research.planner import gap_unknowns, plan_research
from .costing import actual_run_cost
from .entitlements import EntitlementError, access_for_run
from .economics import certify_paid_plan
from .security import validate_remote_args
from .snapshots import durable_discovery_snapshot, durable_snapshot, summary
from .store import SupabaseStore, utcnow
from .stripe import cancel_subscription, create_billing_portal, create_checkout


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
        execution_plan = os.environ.get("LI_RUNTIME_PLAN", "payg")
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

    def _discovery_context(self, user_id: str, run_id: str) -> DiscoveryContext:
        snap = self._snapshot(user_id, run_id)
        return DiscoveryContext(snap["knowledge_map2"], snap["receipt"])

    def _discovery_snapshot(self, user_id: str, discovery_id: str) -> dict[str, Any]:
        row = self.store.get_discovery(user_id, discovery_id)
        if not row:
            raise PublicServiceError("unknown discovery_id")
        snap = row.get("snapshot")
        if not isinstance(snap, dict):
            raise PublicServiceError("stored discovery snapshot is invalid")
        return snap

    def discover(self, user_id: str, a: dict[str, Any]) -> dict[str, Any]:
        run_id = str(a["run_id"])
        reserve = float(os.environ.get("LI_PUBLIC_MAX_DISCOVERY_UNITS", "100"))
        decision = access_for_run(self.store, user_id, reserve)
        if not decision.allowed:
            if decision.entitlement.get("kind") == "paid_required":
                raise PaymentRequired(decision.reason)
            raise QuotaExceeded(decision.reason)

        ctx = self._discovery_context(user_id, run_id)
        result = run_discovery(
            ctx,
            str(a["objective"]),
            design=a.get("design"),
            prior_art=a.get("prior_art"),
        )
        snap = durable_discovery_snapshot(result)
        if snap["usage_units"] > reserve:
            raise QuotaExceeded("discovery exceeded the public bounded-work ceiling")

        self.store.save_discovery({
            "user_id": user_id,
            "discovery_id": snap["discovery_id"],
            "research_id": run_id,
            "objective": str(a["objective"]),
            "outcome": snap["summary"]["outcome"],
            "summary": snap["summary"],
            "snapshot": snap,
            "receipt": snap["receipt"],
            "handoff": snap["handoff"],
            "usage_units": snap["usage_units"],
            "created_at": utcnow(),
        })
        self.store.record_usage({
            "id": str(uuid.uuid4()),
            "user_id": user_id,
            "run_id": run_id,
            "operation": "discover",
            "units": snap["usage_units"],
            "known_cost_usd": 0.0,
            "unpriced_components": ["discovery_compute"],
            "created_at": utcnow(),
        })
        return {"kind": "discovery", "contract": "lofgren.mcp/2", **snap["summary"]}

    def find_prior_art(self, user_id: str, a: dict[str, Any]) -> dict[str, Any]:
        ctx = self._discovery_context(user_id, str(a["run_id"]))
        provider, _ = _prior_art_provider({
            k: a[k] for k in ("records", "coverage") if k in a
        })
        assessment, _meta = assess_prior_art(
            ctx,
            str(a["subject"]),
            list(a["queries"]),
            provider,
            domains=a.get("domains", ()),
            time_range=tuple(a.get("time_range", (None, None))),
        )
        return {
            "kind": "prior_art_assessment",
            "id": assessment.id,
            "conclusion": assessment.conclusion.value,
            "statement": assessment.statement,
            "matches": assessment.matches,
            "limitations": assessment.limitations,
            "unsearched_areas": assessment.unsearched_areas,
        }

    def find_discovery_gaps(self, user_id: str, a: dict[str, Any]) -> dict[str, Any]:
        snap = self._discovery_snapshot(user_id, str(a["discovery_id"]))
        return {"kind": "gaps", "discovery_id": a["discovery_id"], "gaps": snap["gaps"]}

    def find_connections(self, user_id: str, a: dict[str, Any]) -> dict[str, Any]:
        snap = self._discovery_snapshot(user_id, str(a["discovery_id"]))
        return {"kind": "connections", "discovery_id": a["discovery_id"], "connections": snap["connections"]}

    def generate_hypotheses(self, user_id: str, a: dict[str, Any]) -> dict[str, Any]:
        snap = self._discovery_snapshot(user_id, str(a["discovery_id"]))
        return {
            "kind": "hypotheses",
            "confidence_kind": "hypothesis",
            "discovery_id": a["discovery_id"],
            "hypotheses": snap["hypotheses"],
            "requirements": snap["requirements"],
        }

    def generate_candidates(self, user_id: str, a: dict[str, Any]) -> dict[str, Any]:
        snap = self._discovery_snapshot(user_id, str(a["discovery_id"]))
        return {
            "kind": "candidates",
            "confidence_kind": "candidate_robustness",
            "discovery_id": a["discovery_id"],
            "candidates": snap["candidates"],
            "decision": snap["decision"],
            "outcome": snap["summary"]["outcome"],
        }

    def _adhoc_discovery(self, user_id: str, a: dict[str, Any]):
        ctx = self._discovery_context(user_id, str(a["run_id"]))
        framed = frame_problem(ctx, ctx.ensure(DiscoveryObjective("Ad hoc analysis", ctx.research_id)))
        if framed.frame is None:
            raise PublicServiceError("the run established nothing to frame")
        space = DesignSpace.from_json({
            "model": a["model"],
            "success": a.get("success"),
            "distributions": a.get("distributions", {}),
            "simulation": {k: a[k] for k in ("seed", "iterations") if k in a},
            "candidates": [{
                "description": "Ad hoc parameter set",
                "parameters": a["parameters"],
            }],
        })
        return evaluate_candidates(ctx, framed, space)

    def simulate_candidate(self, user_id: str, a: dict[str, Any]) -> dict[str, Any]:
        result = self._adhoc_discovery(user_id, a)
        if not result.simulations:
            raise PublicServiceError("the parameters do not give every model input a value")
        return {
            "kind": "simulated",
            "confidence_kind": "simulation_uncertainty",
            "simulation": result.simulations[0].to_dict(),
        }

    def analyze_sensitivity(self, user_id: str, a: dict[str, Any]) -> dict[str, Any]:
        result = self._adhoc_discovery(user_id, a)
        if not result.sensitivities:
            raise PublicServiceError("the parameters do not give every model input a value")
        s = result.sensitivities[0]
        return {
            "kind": "sensitivity",
            "confidence_kind": "candidate_robustness",
            "sensitivity": s.to_dict(),
            "swings": result.sensitivity_tables.get(s.candidate_id, {}),
        }

    def optimize_solution(self, user_id: str, a: dict[str, Any]) -> dict[str, Any]:
        ctx = self._discovery_context(user_id, str(a["run_id"]))
        problem = ctx.ensure(problem_from_json(a["problem"]))
        result = optimize(ctx, problem)
        return {
            "kind": "optimization_result",
            "confidence_kind": "candidate_robustness",
            **result.to_dict(),
        }

    def verify_discovery(self, user_id: str, a: dict[str, Any]) -> dict[str, Any]:
        snap = self._discovery_snapshot(user_id, str(a["discovery_id"]))
        return {
            "kind": "discovery_verification",
            "receipt_intact": snap["receipt_intact"],
            "receipt_object_problems": snap["receipt_object_problems"],
            "issues": snap["verifier_issues"],
            "handoff_problems": snap["handoff_problems"],
        }

    def get_discovery_receipt(self, user_id: str, a: dict[str, Any]) -> dict[str, Any]:
        snap = self._discovery_snapshot(user_id, str(a["discovery_id"]))
        return {
            "kind": "discovery_receipt",
            "intact": snap["receipt_intact"],
            "receipt": snap["receipt"],
        }

    def create_v3_handoff(self, user_id: str, a: dict[str, Any]) -> dict[str, Any]:
        snap = self._discovery_snapshot(user_id, str(a["discovery_id"]))
        if snap["handoff"] is None:
            return {
                "kind": "v3_handoff",
                "ready": False,
                "outcome": snap["summary"]["outcome"],
                "reason": "no candidate was selected",
                "requirements": [x["description"] for x in snap["requirements"]],
            }
        return {"kind": "v3_handoff", "ready": True, "handoff": snap["handoff"]}

    def render_discovery_report(self, user_id: str, a: dict[str, Any]) -> dict[str, Any]:
        snap = self._discovery_snapshot(user_id, str(a["discovery_id"]))
        return {
            "discovery_id": a["discovery_id"],
            "format": "markdown",
            "report": snap["report"],
        }

    def satellite_passes(self, a: dict[str, Any]) -> dict[str, Any]:
        if a.get("tle_text"):
            tles = parse_tle_text(str(a["tle_text"]))
        elif a.get("fetch"):
            tles = fetch_tles([s.norad_id for s in IMAGING_SATELLITES])
        else:
            raise PublicServiceError("provide tle_text or fetch=true")
        start = datetime.now(timezone.utc)
        rows = []
        for tle in tles:
            rows.extend(find_passes(
                tle,
                float(a["lat"]),
                float(a["lon"]),
                start,
                float(a.get("hours", 24)),
                float(a.get("min_elevation_deg", 30)),
            ))
        rows.sort(key=lambda p: p.rise)
        return {
            "propagator": PROPAGATOR,
            "kind": "prediction (not a provider acquisition schedule)",
            "from": start.isoformat(),
            "passes": [p.to_dict() for p in rows],
        }

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
        if os.environ.get("LI_BILLING_ENABLED", "").lower() not in {"1", "true", "yes"}:
            raise PublicServiceError("billing checkout is not enabled")
        gate = certify_paid_plan(self.store.cost_samples())
        if not gate.passed:
            raise PublicServiceError("paid plan has not passed the P95 economic certification gate")
        ent = self.store.get_entitlement(user_id)
        if not ent:
            raise PublicServiceError("entitlement missing")
        if ent.get("kind") == "founding_free":
            raise PublicServiceError("Founding Free accounts do not need checkout")
        if ent.get("kind") != "paid_required":
            raise PublicServiceError("account already has a paid entitlement; use billing portal")
        session = create_checkout(
            user_id,
            success_url=base_url.rstrip("/") + "/billing/success?session_id={CHECKOUT_SESSION_ID}",
            cancel_url=base_url.rstrip("/") + "/billing/cancelled",
        )
        return {"checkout_url": session.get("url"), "session_id": session.get("id")}


    def billing_portal(self, user_id: str, base_url: str) -> dict[str, Any]:
        ent = self.store.get_entitlement(user_id)
        if not ent or ent.get("kind") != "paid":
            raise PublicServiceError("billing portal requires an active paid entitlement")
        customer_id = str(ent.get("stripe_customer_id") or "")
        if not customer_id:
            raise PublicServiceError("paid entitlement is missing its Stripe customer id")
        session = create_billing_portal(
            customer_id,
            return_url=base_url.rstrip("/") + "/",
        )
        return {"portal_url": session.get("url"), "session_id": session.get("id")}


    def export_account_data(self, user_id: str) -> dict[str, Any]:
        account = self.store.get_account(user_id)
        if not account:
            raise PublicServiceError("account is not activated")
        return {
            "account": account,
            "entitlement": self.store.get_entitlement(user_id),
            "runs": self.store.list_runs(user_id),
            "discoveries": self.store.list_discoveries(user_id),
            "usage_events": self.store.list_usage(user_id),
        }

    def delete_account(self, user_id: str, confirmation: str) -> dict[str, Any]:
        expected = "DELETE MY LOFGREN INTELLIGENCE ACCOUNT"
        if confirmation != expected:
            raise PublicServiceError(f"confirmation must exactly equal: {expected}")
        ent = self.store.get_entitlement(user_id)
        if ent and ent.get("stripe_subscription_id"):
            cancel_subscription(str(ent["stripe_subscription_id"]))
        # Deleting auth.users cascades the LI account, runs, tokens and usage
        # through the database foreign keys. If subscription cancellation
        # fails, execution stops before identity/data deletion.
        self.store.delete_auth_user(user_id)
        return {"deleted": True}
