"""Application service used by the remote MCP and HTTP entry point."""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone
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
from ..production import build_artifact as produce_artifact, verify_artifact, verify_production_receipt
from ..execution import (
    ActionRequest, ApprovalRecord, CapabilityGrant, HTTPSWebhookAdapter,
    execute_authorized, verify_action_receipt,
)
from ..outcome import Measurement, evaluate_outcome, verify_outcome_receipt
from ..improvement import (
    EvaluationDataset, ImprovementProposal, MetricObservation, SafetyConstraint,
    evaluate_improvement, verify_improvement_receipt,
)
from .costing import actual_run_cost
from .entitlements import EntitlementError, access_for_run
from .economics import certify_paid_plan
from .security import validate_remote_args
from .snapshots import durable_discovery_snapshot, durable_snapshot, restore_discovery_context, summary
from .store import SupabaseStore, utcnow
from .stripe import cancel_subscription, create_billing_portal, create_checkout


MAX_PASS_HOURS = 168.0
MAX_PASS_TLES = 20
MAX_TLE_TEXT_CHARS = 20_000


class PublicServiceError(RuntimeError):
    pass


class PaymentRequired(PublicServiceError):
    pass


class QuotaExceeded(PublicServiceError):
    pass


class PublicService:
    def __init__(self, store: SupabaseStore | None = None) -> None:
        self.store = store or SupabaseStore()

    def _reserve_usage(self, user_id: str, operation: str, units: float) -> str:
        ent = self.store.get_entitlement(user_id)
        if not ent:
            raise PublicServiceError("entitlement missing")
        if ent.get("kind") == "paid_required" or ent.get("active") is False:
            raise PaymentRequired("active entitlement is required")
        reservation_id = str(uuid.uuid4())
        if not self.store.reserve_usage(reservation_id, user_id, operation, float(units)):
            raise QuotaExceeded("rolling usage quota would be exceeded")
        return reservation_id

    def _finalize_usage(
        self,
        reservation_id: str,
        *,
        run_id: str | None,
        actual_units: float,
        known_cost_usd: float = 0.0,
        unpriced_components: list[str] | None = None,
    ) -> None:
        if not self.store.finalize_usage(
            reservation_id,
            run_id,
            float(actual_units),
            float(known_cost_usd),
            list(unpriced_components or []),
        ):
            self.store.release_usage(reservation_id)
            raise QuotaExceeded("actual work exceeded the reserved usage allowance")

    def _require_open_quota(self, user_id: str) -> None:
        """Refuse unreserved compute when entitlement is inactive or weekly quota is exhausted."""
        decision = access_for_run(self.store, user_id, 0.0)
        if not decision.allowed:
            if decision.entitlement.get("kind") == "paid_required":
                raise PaymentRequired(decision.reason)
            raise QuotaExceeded(decision.reason)
        if decision.used_units >= decision.quota_units:
            raise QuotaExceeded("weekly intelligence-unit quota reached")

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
        reservation_id = self._reserve_usage(
            user_id, "investigate", float(plan.estimated_work_units)
        )

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
        self._finalize_usage(
            reservation_id,
            run_id=snap["run_id"],
            actual_units=snap["usage_units"],
            known_cost_usd=cost.known_cost_usd,
            unpriced_components=list(cost.unpriced_components),
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
        reservation_id = self._reserve_usage(user_id, "discover", reserve)

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
        self._finalize_usage(
            reservation_id,
            run_id=run_id,
            actual_units=snap["usage_units"],
            known_cost_usd=0.0,
            unpriced_components=["discovery_compute"],
        )
        return {"kind": "discovery", "contract": "lofgren.mcp/2", **snap["summary"]}

    def find_prior_art(self, user_id: str, a: dict[str, Any]) -> dict[str, Any]:
        self._require_open_quota(user_id)
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
        self._require_open_quota(user_id)
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
        self._require_open_quota(user_id)
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

    def build_artifact(self, user_id: str, a: dict[str, Any]) -> dict[str, Any]:
        discovery_id = str(a["discovery_id"])
        reservation_id = self._reserve_usage(user_id, "build_artifact", 10.0)
        row = self.store.get_discovery(user_id, discovery_id)
        if not row:
            raise PublicServiceError("unknown discovery_id")
        snap = row.get("snapshot")
        if not isinstance(snap, dict) or not snap.get("handoff"):
            raise PublicServiceError("discovery has no validated V3 handoff")
        context = restore_discovery_context(\n            self._discovery_context(user_id, str(row["research_id"])), snap\n        )\n        result = produce_artifact(
            snap["handoff"],
            discovery_receipt=snap["receipt"],
            context=context,
            kind=str(a.get("kind") or "structured_bundle"),
        )
        self.store.save_artifact({
            "user_id": user_id,
            "artifact_id": result.artifact_id,
            "discovery_id": discovery_id,
            "kind": result.artifact["kind"],
            "artifact": result.artifact,
            "receipt": result.receipt,
            "v4_handoff": result.v4_handoff,
            "created_at": utcnow(),
        })
        self._finalize_usage(
            reservation_id,
            run_id=row["research_id"],
            actual_units=10.0,
            unpriced_components=["artifact_compute"],
        )
        return {
            "kind": "artifact",
            "artifact_id": result.artifact_id,
            "artifact_kind": result.artifact["kind"],
            "fingerprint": result.artifact["fingerprint"],
            "verified": result.verification.passed,
            "receipt_hash": result.receipt["receipt_hash"],
            "v4_authority_required": result.v4_handoff["authority_required"],
        }

    def get_artifact(self, user_id: str, a: dict[str, Any]) -> dict[str, Any]:
        row = self.store.get_artifact(user_id, str(a["artifact_id"]))
        if not row:
            raise PublicServiceError("unknown artifact_id")
        check = verify_artifact(row["artifact"])
        return {
            "artifact": row["artifact"],
            "verified": check.passed,
            "verification_problems": list(check.problems),
            "receipt_intact": verify_production_receipt(row["receipt"]),
            "receipt": row["receipt"],
        }

    @staticmethod
    def _action_request(data: dict[str, Any]) -> ActionRequest:
        return ActionRequest(
            str(data["artifact_id"]), str(data["artifact_fingerprint"]),
            str(data["kind"]), str(data["target"]), dict(data["payload"]),
            float(data.get("cost_usd", 0)), bool(data.get("reversible", False)),
        )

    def propose_action(self, user_id: str, a: dict[str, Any], base_url: str) -> dict[str, Any]:
        artifact = self.store.get_artifact(user_id, str(a["artifact_id"]))
        if not artifact:
            raise PublicServiceError("unknown artifact_id")
        kind = str(a.get("kind") or "https_webhook")
        if kind != "https_webhook":
            raise PublicServiceError("public V4 currently permits only the bounded https_webhook adapter")
        cost = float(a.get("cost_usd", 0))
        max_cost = float(os.environ.get("LI_PUBLIC_MAX_ACTION_COST_USD", "0"))
        if cost < 0 or cost > max_cost + 1e-9:
            raise PublicServiceError("action cost exceeds the public action ceiling")
        request = ActionRequest(
            str(artifact["artifact_id"]),
            str(artifact["artifact"]["fingerprint"]),
            kind,
            str(a["target"]),
            dict(a.get("payload") or {}),
            cost,
            False,
        )
        # Preflight performs URL/network safety validation but sends no action.
        HTTPSWebhookAdapter().preflight(request)
        request_data = {
            "artifact_id": request.artifact_id,
            "artifact_fingerprint": request.artifact_fingerprint,
            "kind": request.kind,
            "target": request.target,
            "payload": request.payload,
            "cost_usd": request.cost_usd,
            "reversible": request.reversible,
            "action_hash": request.action_hash,
        }
        self.store.save_action({
            "user_id": user_id,
            "action_id": request.action_id,
            "artifact_id": request.artifact_id,
            "request": request_data,
            "status": "awaiting_approval",
            "grant_record": None,
            "approval_record": None,
            "receipt": None,
            "v5_handoff": None,
            "created_at": utcnow(),
        })
        return {
            "kind": "action_proposal",
            "action_id": request.action_id,
            "action_hash": request.action_hash,
            "status": "awaiting_human_approval",
            "approval_url": base_url.rstrip("/") + "/actions/" + request.action_id,
        }

    def action_status(self, user_id: str, a: dict[str, Any]) -> dict[str, Any]:
        row = self.store.get_action(user_id, str(a["action_id"]))
        if not row:
            raise PublicServiceError("unknown action_id")
        return {
            "action_id": row["action_id"],
            "status": row["status"],
            "request": row["request"],
            "approved_at": row.get("approved_at"),
            "executed_at": row.get("executed_at"),
            "receipt_intact": bool(row.get("receipt") and verify_action_receipt(row["receipt"])),
        }

    def approve_action(self, user_id: str, action_id: str) -> dict[str, Any]:
        row = self.store.get_action(user_id, action_id)
        if not row:
            raise PublicServiceError("unknown action_id")
        if row["status"] == "approved":
            return {"action_id": action_id, "status": "approved"}
        if row["status"] != "awaiting_approval":
            raise PublicServiceError("action is not awaiting approval")
        request = self._action_request(row["request"])
        now = datetime.now(timezone.utc)
        expires = now + timedelta(minutes=10)
        grant = CapabilityGrant(
            "GRANT-" + str(uuid.uuid4()), user_id, request.kind, request.target,
            request.cost_usd, expires.isoformat(), False,
        )
        approval = ApprovalRecord(
            "APR-" + str(uuid.uuid4()), user_id, request.action_id, request.action_hash,
            request.artifact_id, now.isoformat(),
        )
        updated = dict(row)
        updated.update({
            "status": "approved",
            "grant_record": grant.__dict__,
            "approval_record": approval.__dict__,
            "approved_at": now.isoformat(),
        })
        self.store.save_action(updated)
        return {
            "action_id": action_id,
            "status": "approved",
            "expires_at": grant.expires_at,
            "action_hash": request.action_hash,
        }

    def execute_action(self, user_id: str, a: dict[str, Any]) -> dict[str, Any]:
        row = self.store.get_action(user_id, str(a["action_id"]))
        if not row:
            raise PublicServiceError("unknown action_id")
        if row["status"] == "executed" and row.get("receipt"):
            return {
                "action_id": row["action_id"], "status": "executed",
                "receipt_intact": verify_action_receipt(row["receipt"]),
                "receipt": row["receipt"],
            }
        if row["status"] != "approved" or not row.get("grant_record") or not row.get("approval_record"):
            raise PublicServiceError("action requires explicit browser approval")
        artifact = self.store.get_artifact(user_id, row["artifact_id"])
        if not artifact:
            raise PublicServiceError("artifact disappeared before execution")
        request = self._action_request(row["request"])
        grant = CapabilityGrant(**row["grant_record"])
        approval = ApprovalRecord(**row["approval_record"])
        result = execute_authorized(
            artifact["v4_handoff"], request, grant, approval, HTTPSWebhookAdapter(),
            subject=user_id, now=datetime.now(timezone.utc), spent_usd=0,
        )
        status = "executed" if result.status == "committed" else result.status
        updated = dict(row)
        updated.update({
            "status": status,
            "receipt": result.receipt,
            "v5_handoff": result.v5_handoff,
            "executed_at": utcnow(),
        })
        self.store.save_action(updated)
        return {
            "action_id": row["action_id"],
            "status": status,
            "receipt_intact": verify_action_receipt(result.receipt),
            "receipt": result.receipt,
            "v5_ready": result.v5_handoff is not None,
        }

    def measure_outcome(self, user_id: str, a: dict[str, Any]) -> dict[str, Any]:
        reservation_id = self._reserve_usage(user_id, "measure_outcome", 5.0)
        action = self.store.get_action(user_id, str(a["action_id"]))
        if not action or action.get("status") != "executed" or not action.get("v5_handoff"):
            raise PublicServiceError("a committed verified action is required before measurement")
        measurements = [
            Measurement(
                str(m["metric"]), float(m["value"]), str(m["unit"]),
                str(m["observed_at"]), str(m["source"]), m.get("observation_id"),
            )
            for m in list(a.get("measurements") or [])
        ]
        result = evaluate_outcome(
            action["v5_handoff"], measurements,
            causal_design=a.get("causal_design"),
        )
        outcome_id = str(result.receipt["receipt_hash"])
        self.store.save_outcome({
            "user_id": user_id,
            "outcome_id": outcome_id,
            "action_id": action["action_id"],
            "receipt": result.receipt,
            "v6_handoff": result.v6_handoff,
            "created_at": utcnow(),
        })
        self._finalize_usage(
            reservation_id,
            run_id=None,
            actual_units=5.0,
            unpriced_components=["outcome_compute"],
        )
        return {
            "kind": "outcome",
            "outcome_id": outcome_id,
            "status": result.receipt["status"],
            "causal_standing": result.receipt["causal"]["standing"],
            "receipt_intact": verify_outcome_receipt(result.receipt),
            "v6_improvement_allowed": result.v6_handoff["improvement_allowed"],
        }

    def get_outcome(self, user_id: str, a: dict[str, Any]) -> dict[str, Any]:
        row = self.store.get_outcome(user_id, str(a["outcome_id"]))
        if not row:
            raise PublicServiceError("unknown outcome_id")
        return {
            "receipt_intact": verify_outcome_receipt(row["receipt"]),
            "receipt": row["receipt"],
            "v6_handoff": row["v6_handoff"],
        }

    def evaluate_improvement(self, user_id: str, a: dict[str, Any]) -> dict[str, Any]:
        reservation_id = self._reserve_usage(user_id, "evaluate_improvement", 5.0)
        outcome = self.store.get_outcome(user_id, str(a["outcome_id"]))
        if not outcome:
            raise PublicServiceError("unknown outcome_id")
        p = dict(a["proposal"])
        ds = dict(p["evaluation_dataset"])
        metric = dict(p["primary_metric"])
        constraints = tuple(
            SafetyConstraint(
                str(x["metric"]), float(x["baseline"]), float(x["candidate"]),
                float(x["max_regression"]),
            )
            for x in list(p.get("safety_constraints") or [])
        )
        proposal = ImprovementProposal(
            str(p["proposal_id"]), str(p["baseline_id"]), str(p["candidate_id"]),
            str(p["change_summary"]),
            EvaluationDataset(
                str(ds["dataset_id"]), str(ds["content_hash"]), int(ds["sample_count"]),
                bool(ds.get("held_out", True)),
            ),
            MetricObservation(
                str(metric["metric"]), float(metric["baseline"]), float(metric["candidate"]),
                str(metric.get("direction") or "higher_is_better"),
            ),
            float(p["min_gain"]),
            constraints,
            True,
        )
        result = evaluate_improvement(
            outcome["v6_handoff"], proposal,
            minimum_samples=int(os.environ.get("LI_PUBLIC_MIN_IMPROVEMENT_SAMPLES", "30")),
        )
        improvement_id = str(result.receipt["receipt_hash"])
        self.store.save_improvement({
            "user_id": user_id,
            "improvement_id": improvement_id,
            "outcome_id": outcome["outcome_id"],
            "receipt": result.receipt,
            "next_cycle": result.next_cycle,
            "created_at": utcnow(),
        })
        self._finalize_usage(
            reservation_id,
            run_id=None,
            actual_units=5.0,
            unpriced_components=["improvement_compute"],
        )
        return {
            "kind": "improvement",
            "improvement_id": improvement_id,
            "decision": result.decision,
            "receipt_intact": verify_improvement_receipt(result.receipt),
            "human_review_required": True,
            "mutation_performed": False,
        }

    def get_improvement(self, user_id: str, a: dict[str, Any]) -> dict[str, Any]:
        row = self.store.get_improvement(user_id, str(a["improvement_id"]))
        if not row:
            raise PublicServiceError("unknown improvement_id")
        return {
            "receipt_intact": verify_improvement_receipt(row["receipt"]),
            "receipt": row["receipt"],
            "next_cycle": row["next_cycle"],
        }

    def satellite_passes(self, user_id: str, a: dict[str, Any]) -> dict[str, Any]:
        self._require_open_quota(user_id)
        lat, lon = float(a["lat"]), float(a["lon"])
        hours = float(a.get("hours", 24))
        min_el = float(a.get("min_elevation_deg", 30))
        if not (-90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0):
            raise PublicServiceError("lat must be in [-90, 90] and lon in [-180, 180]")
        if not (0.0 < hours <= MAX_PASS_HOURS):
            raise PublicServiceError(f"hours must be in (0, {MAX_PASS_HOURS:g}]")
        if not (0.0 <= min_el <= 90.0):
            raise PublicServiceError("min_elevation_deg must be in [0, 90]")
        if a.get("tle_text"):
            if len(str(a["tle_text"])) > MAX_TLE_TEXT_CHARS:
                raise PublicServiceError("tle_text is too large")
            tles = parse_tle_text(str(a["tle_text"]))
        elif a.get("fetch"):
            tles = fetch_tles([s.norad_id for s in IMAGING_SATELLITES])
        else:
            raise PublicServiceError("provide tle_text or fetch=true")
        if len(tles) > MAX_PASS_TLES:
            raise PublicServiceError(f"at most {MAX_PASS_TLES} element sets per call")
        start = datetime.now(timezone.utc)
        rows = []
        for tle in tles:
            rows.extend(find_passes(
                tle,
                lat,
                lon,
                start,
                hours,
                min_el,
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
            "artifacts": self.store.list_artifacts(user_id),
            "actions": self.store.list_actions(user_id),
            "outcomes": self.store.list_outcomes(user_id),
            "improvements": self.store.list_improvements(user_id),
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
