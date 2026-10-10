"""Application service used by the remote MCP and HTTP entry point."""

from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import secrets
import urllib.error
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from .. import build_registry
from ..billing.catalog import CATALOG, checkout_plans
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
from ..intent.clarification import clarify_objective as clarify_case_objective, requires_clarification, to_dict as clarification_to_dict
from ..kernel.ledger import CostLedger
from ..kernel.receipt import verify_receipt
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
from . import cases as case_model
from . import jobs as job_model
from .costing import actual_run_cost
from .entitlements import EntitlementError, access_for_run
from .economics import certify_paid_plan
from .security import PublicInputError, validate_remote_args
from .snapshots import durable_discovery_snapshot, durable_snapshot, restore_discovery_context, summary
from .store import SupabaseStore, utcnow
from .stripe import cancel_subscription, create_billing_portal, create_checkout


# Intelligence units charged per call of an ad hoc tool. These tools do bounded
# compute against an existing run (or, for satellite_passes, against supplied or
# fetched element sets) and persist nothing, so they are charged a flat,
# documented amount per successful call through the same atomic
# li_reserve_usage -> li_finalize_usage path as investigate/discover. A call that
# fails releases its reservation and is not charged. Operators may override a
# cost with LI_UNITS_<TOOL> (for example LI_UNITS_SIMULATE_CANDIDATE=8); a
# value that is not a finite number >= 0 is ignored. See docs/PUBLIC_MCP.md.
ADHOC_UNIT_COSTS: dict[str, float] = {
    "find_prior_art": 2.0,
    "simulate_candidate": 5.0,
    "analyze_sensitivity": 5.0,
    "optimize_solution": 5.0,
    "satellite_passes": 1.0,
}


def adhoc_unit_cost(operation: str) -> float:
    units = ADHOC_UNIT_COSTS[operation]
    raw = os.environ.get("LI_UNITS_" + operation.upper(), "").strip()
    if raw:
        try:
            value = float(raw)
        except ValueError:
            return units
        if math.isfinite(value) and value >= 0:
            return value
    return units


MAX_PASS_HOURS = 168.0
MAX_PASS_TLES = 20
MAX_TLE_TEXT_CHARS = 20_000


class PublicServiceError(RuntimeError):
    """A refusal the public caller may read.

    The message is user-facing and must never carry stored data, schema names,
    object ids from a failed integrity check or a traceback. `code` is a stable
    machine-readable reason that the MCP surface puts in front of the message.
    """

    code = "REQUEST_REFUSED"


class PaymentRequired(PublicServiceError):
    code = "PAYMENT_REQUIRED"


class QuotaExceeded(PublicServiceError):
    code = "QUOTA_EXCEEDED"


class DiscoveryStateInvalid(PublicServiceError):
    """Stored V2 discovery state failed to restore or failed its integrity checks."""

    code = "DISCOVERY_STATE_INVALID"


class RunStateInvalid(PublicServiceError):
    """A stored V1 research run could not be reloaded as a discovery context."""

    code = "RUN_STATE_INVALID"


class CaseNotFound(PublicServiceError):
    """No such case for this account (another account's case is reported the same way)."""

    code = "CASE_NOT_FOUND"


class CaseInvalid(PublicServiceError):
    """The case request is malformed, or the stored charter failed its integrity check."""

    code = "CASE_INVALID"


class CaseVersionConflict(PublicServiceError):
    """The charter version the caller expected is not the latest version."""

    code = "CASE_VERSION_CONFLICT"


class CaseNotReady(PublicServiceError):
    """The charter still has open clarification questions and cannot be approved."""

    code = "CASE_NOT_READY"


class CaseApprovalRequired(PublicServiceError):
    """No valid browser approval exists for the latest charter version."""

    code = "CASE_APPROVAL_REQUIRED"


class CaseApprovalConsumed(PublicServiceError):
    """The approval was already used to start research (under another idempotency key)."""

    code = "CASE_APPROVAL_CONSUMED"


class CaseBudgetExceeded(PublicServiceError):
    """The planned research needs more than the approved charter budget."""

    code = "CASE_BUDGET_EXCEEDED"


class CaseRunFailed(PublicServiceError):
    """The research started from this approval failed; a fresh approval is needed to retry."""

    code = "CASE_RUN_FAILED"


class JobNotFound(PublicServiceError):
    """No such research job for this account (another account's job is reported the same way)."""

    code = "JOB_NOT_FOUND"


class JobInvalid(PublicServiceError):
    """The research job request is malformed (for example an unusable idempotency key)."""

    code = "JOB_INVALID"


class AsyncRequired(PublicServiceError):
    """The research is not bounded well under the request time limit; use start_research."""

    code = "ASYNC_REQUIRED"


_LOG = logging.getLogger("lofgren_intelligence.public")

# The synchronous investigate path runs inside one HTTP request, and the hosting
# function is capped at 60 seconds (vercel.json maxDuration). Only plans bounded
# well under that run inline: at most LI_SYNC_MAX_NETWORK_TASKS source calls that
# leave the process (each network fetch has a 15 s timeout) and at most
# LI_SYNC_MAX_WORK_UNITS estimated work units. Anything larger is refused with
# ASYNC_REQUIRED and goes through start_research (a durable job). See
# docs/WORKER_RUNTIME.md.
SYNC_MAX_WORK_UNITS = 40.0
SYNC_MAX_NETWORK_TASKS = 2
LOCAL_ADAPTERS = frozenset({"documents"})


class AccountDeletionPending(PublicServiceError):
    """Deletion must wait: research or usage of the account has not settled yet. Nothing was deleted."""

    code = "ACCOUNT_DELETION_PENDING"


class _ResearchAttempt:
    """One execution of the shared research core: its provider and ledger outlive a failure."""

    def __init__(self, execution_plan: str) -> None:
        self.execution_plan = execution_plan
        self.provider = default_provider()
        self.ledger = CostLedger(PLANS[execution_plan].rate)
        self.result: Any = None
        self.snap: dict[str, Any] | None = None
        self.cost: Any = None


DISCOVERY_STATE_INVALID_MESSAGE = (
    "the stored discovery state is invalid or incomplete and cannot be used; "
    "run discover again to create a new discovery"
)

# Keys every durable discovery snapshot carries (see snapshots.durable_discovery_snapshot).
# A snapshot missing any of them is truncated or tampered and is refused as a whole.
REQUIRED_DISCOVERY_KEYS = (
    "discovery_id", "summary", "gaps", "connections", "hypotheses", "requirements",
    "candidates", "decision", "receipt", "context_objects", "receipt_intact",
    "receipt_object_problems", "verifier_issues", "handoff", "handoff_problems", "report",
)


class PublicService:
    def __init__(
        self,
        store: SupabaseStore | None = None,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.store = store or SupabaseStore()
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    # ---- usage: reserve -> work -> settle --------------------------------------------------
    #
    # Every metered call follows one accounting rule set:
    #   * a refusal before any work releases the reservation (li_release_usage);
    #   * a failure after work incurred cost charges the units/cost known so far;
    #   * work that ran is charged even when li_finalize_usage refuses (actual units
    #     overran the quota, the reservation expired) or fails: the actual usage is
    #     recorded on the reservation as an 'unsettled' marker (li_mark_usage_unsettled)
    #     and settled exactly once (li_settle_usage, via reconcile_usage). The usage event
    #     id is the reservation id, so a retry never charges twice and a saved result is
    #     never discarded because settlement failed.

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

    def _settle_work(
        self,
        reservation_id: str,
        *,
        run_id: str | None,
        actual_units: float,
        known_cost_usd: float = 0.0,
        unpriced_components: list[str] | None = None,
    ) -> str:
        """Charge work that already ran. Returns 'settled' or 'unsettled' (marker awaits reconcile).

        Never raises: the work's result must not be lost because accounting failed.
        """
        args = (run_id, float(actual_units), float(known_cost_usd), list(unpriced_components or []))
        try:
            if self.store.finalize_usage(reservation_id, *args):
                return "settled"
        except Exception:
            _LOG.warning("usage finalize failed for reservation %s; recording a reconciliation marker",
                         reservation_id)
        try:
            self.store.mark_usage_unsettled(reservation_id, *args)
        except Exception:
            _LOG.error("could not record the usage reconciliation marker for reservation %s", reservation_id)
            return "unsettled"
        try:
            if self.reconcile_usage(reservation_id) in ("settled", "already_settled"):
                return "settled"
        except Exception:
            _LOG.warning("usage settlement for reservation %s is pending reconciliation", reservation_id)
        return "unsettled"

    def reconcile_usage(self, reservation_id: str) -> str:
        """Settle one unsettled reservation exactly once (idempotent; safe to retry).

        Returns 'settled' when this call charged it, 'already_settled' when an earlier
        call (or the original finalize) did, 'not_unsettled' when nothing is pending
        and 'missing' for an unknown reservation.
        """
        return self.store.settle_usage(str(reservation_id))

    def reconcile_unsettled_usage(self, limit: int = 100) -> dict[str, Any]:
        """Settle every pending reconciliation marker; one failure does not stop the rest."""
        outcomes: dict[str, str] = {}
        for row in self.store.list_unsettled_usage(limit):
            rid = str(row["id"])
            try:
                outcomes[rid] = self.reconcile_usage(rid)
            except Exception:
                outcomes[rid] = "error"
        return {"reconciled": outcomes}

    def _release_quietly(self, reservation_id: str) -> None:
        """Release a reservation on a failed call without masking the original error.

        If the release itself cannot reach the store, the database expires the
        reservation after one hour (li_reserve_usage), so capacity is never lost.
        """
        try:
            self.store.release_usage(reservation_id)
        except Exception:
            pass

    def _require_open_quota(self, user_id: str) -> None:
        """Refuse unreserved compute when entitlement is inactive or weekly quota is exhausted."""
        decision = access_for_run(self.store, user_id, 0.0)
        if not decision.allowed:
            if decision.entitlement.get("kind") == "paid_required":
                raise PaymentRequired(decision.reason)
            raise QuotaExceeded(decision.reason)
        if decision.used_units >= decision.quota_units:
            raise QuotaExceeded("weekly intelligence-unit quota reached")

    def _metered(self, user_id: str, operation: str, run_id: str | None, work: Callable[[], Any]) -> Any:
        """Run one ad hoc call under an atomic usage reservation.

        reserve (li_reserve_usage) -> work -> settle. A refused or crashed call
        persists nothing and has no measured partial cost, so its reservation is
        released (li_release_usage) and it is not charged. The open-quota check
        stays in front of the reservation so an inactive entitlement is refused first.
        """
        self._require_open_quota(user_id)
        units = adhoc_unit_cost(operation)
        reservation_id = self._reserve_usage(user_id, operation, units)
        try:
            out = work()
        except BaseException:
            self._release_quietly(reservation_id)
            raise
        self._settle_work(
            reservation_id,
            run_id=run_id,
            actual_units=units,
            unpriced_components=[f"{operation}_compute"],
        )
        return out

    @staticmethod
    def _location(a: dict[str, Any]) -> dict[str, Any] | None:
        if a.get("lat") is not None and a.get("lon") is not None:
            return {"lat": float(a["lat"]), "lon": float(a["lon"]), "name": None}
        return None

    @staticmethod
    def _registry(a: dict[str, Any]):
        # Local file paths and arbitrary local sensors are deliberately absent from remote mode.
        registry = build_registry(
            texts=a.get("texts"),
            urls=a.get("urls"),
            fetch_orbits=bool(a.get("fetch_orbits")),
            imagery=bool(a.get("imagery")),
            search=a.get("search"),
        )
        from ..adapters.europepmc import EuropePMCAdapter
        from ..adapters.clinicaltrials import ClinicalTrialsAdapter
        from ..adapters.manifest import OperatorSourcesAdapter, parse_manifest
        from ..adapters.public_api import PublicHTTPClient
        for key, adapter in (("europepmc", EuropePMCAdapter), ("trials", ClinicalTrialsAdapter)):
            if a.get(key):
                registry.register(adapter(a[key], max_records=a.get("max_records", 20), max_pages=2,
                    client=PublicHTTPClient(key, timeout=15, max_retries=1, min_interval_s=1.2)))
        if a.get("sources_manifest") is not None:
            manifest = parse_manifest(json.dumps(a["sources_manifest"], sort_keys=True, allow_nan=False).encode("utf-8"))
            registry.register(OperatorSourcesAdapter(manifest,
                client=PublicHTTPClient("operator_sources", timeout=15, max_retries=1, min_interval_s=1.2)))
        return registry


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

    # ---- Intelligence Cases: clarification, versioned charters, browser approval ----------
    #
    # A serious objective (requires_clarification) is researched only through a case:
    # clarify_objective opens it and records each answer set as a new charter version;
    # the account owner approves one exact version + content hash on the /cases/{id}
    # browser page (never through MCP); investigate consumes that approval once and runs
    # with the approved charter's objective, sources and budget -- not the client's.

    class _ReplayStart(Exception):
        """The approval was already consumed under the caller's idempotency key."""

        def __init__(self, approval: dict[str, Any]) -> None:
            super().__init__("replay")
            self.approval = approval

    @staticmethod
    def _approval_url(base_url: str, case_id: str) -> str:
        return (base_url or "").rstrip("/") + "/cases/" + str(case_id)

    def _expired(self, approval: dict[str, Any]) -> bool:
        raw = str(approval.get("expires_at") or "")
        try:
            expires = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return True  # an unreadable expiry is not a valid approval
        if expires.tzinfo is None:
            return True
        return expires <= self._clock()

    def _case(self, user_id: str, case_id: Any) -> dict[str, Any]:
        cid = str(case_id or "").strip()
        try:
            cid = str(uuid.UUID(cid))
        except ValueError:
            raise CaseNotFound("unknown case_id") from None
        row = self.store.get_case(user_id, cid)
        if not row or str(row.get("user_id")) != str(user_id) or str(row.get("id")) != cid:
            raise CaseNotFound("unknown case_id")
        return row

    def _latest_charter(self, user_id: str, case_id: str) -> dict[str, Any]:
        row = self.store.get_case_charter(user_id, case_id)
        if (not row or str(row.get("user_id")) != str(user_id) or str(row.get("case_id")) != str(case_id)
                or not isinstance(row.get("charter"), dict)):
            raise CaseInvalid("the stored case charter is missing; open a new case")
        if case_model.content_hash(case_id, row["charter"], row.get("answers") or {}) != row.get("content_hash"):
            raise CaseInvalid("the stored case charter failed its integrity check; open a new case")
        return row

    def _case_view(self, case: dict[str, Any], row: dict[str, Any], base_url: str) -> dict[str, Any]:
        ready = row["charter"].get("status") == "READY_FOR_SCOPE_APPROVAL"
        view = case_model.client_view(case, row, self._approval_url(base_url, case["id"]) if ready else None)
        if ready:
            view["message"] = (
                "The Case Charter is ready. The account owner must sign in at approval_url and approve "
                "this exact charter version; then call investigate with this case_id. Approval is not "
                "an MCP call, and any edit to the charter needs a fresh approval."
            )
        else:
            view["message"] = (
                "Answer the open questions (an answer, 'unknown', 'skip', 'ask me later' or "
                "'use a reasonable default' are all valid) by calling clarify_objective again with this "
                "case_id and expected_version."
            )
        return view

    def clarify_objective(self, user_id: str, a: dict[str, Any], base_url: str = "") -> dict[str, Any]:
        """Open an Intelligence Case, or record a revised answer set as a new charter version."""
        a = validate_remote_args(a)
        answers_in = a.get("answers")
        if answers_in is not None and not isinstance(answers_in, dict):
            raise CaseInvalid("answers must be an object of question key -> answer")
        budget_in = a.get("budget")
        if budget_in is not None and not isinstance(budget_in, dict):
            raise CaseInvalid("budget must be an object with max_spend_usd and/or max_units")
        try:
            if a.get("case_id") in (None, ""):
                return self._open_case(user_id, a, answers_in, budget_in, base_url)
            return self._revise_case(user_id, a, answers_in, budget_in, base_url)
        except case_model.CaseInputError as exc:
            raise CaseInvalid(str(exc)) from None

    def _open_case(self, user_id: str, a: dict[str, Any], answers_in: dict[str, Any] | None,
                   budget_in: dict[str, Any] | None, base_url: str) -> dict[str, Any]:
        objective = str(a.get("objective") or "").strip()
        if not objective:
            raise CaseInvalid("objective is required")
        answers = case_model.merge_answers(None, answers_in)
        budget = case_model.normalize_budget(budget_in)
        sources = case_model.normalize_sources(a)
        charter = case_model.build_charter(objective, answers, budget, sources)
        case_id = str(uuid.uuid4())
        digest = case_model.content_hash(case_id, charter, answers)
        status = case_model.STATUS_FOR_CLARIFICATION[charter["status"]]
        self.store.create_case(case_id, user_id, charter["objective"], status, digest, charter, answers)
        return self._case_view(
            {"id": case_id, "user_id": user_id, "status": status},
            {"version": 1, "content_hash": digest, "charter": charter},
            base_url,
        )

    def _revise_case(self, user_id: str, a: dict[str, Any], answers_in: dict[str, Any] | None,
                     budget_in: dict[str, Any] | None, base_url: str) -> dict[str, Any]:
        case = self._case(user_id, a["case_id"])
        latest = self._latest_charter(user_id, case["id"])
        if a.get("expected_version") is None:
            raise CaseVersionConflict(
                f"expected_version is required to revise a case (the latest version is {latest['version']})"
            )
        try:
            expected = int(a["expected_version"])
        except (TypeError, ValueError):
            raise CaseInvalid("expected_version must be an integer") from None
        if expected != int(latest["version"]):
            raise CaseVersionConflict(
                f"charter version {expected} is not the latest (version {latest['version']}); "
                "read the case again and resubmit"
            )
        previous = latest["charter"]
        objective = str(a.get("objective") or "").strip()
        if objective and objective != previous["objective"]:
            raise CaseInvalid("a case's objective cannot change; open a new case for a different objective")
        answers = case_model.merge_answers(latest.get("answers") or {}, answers_in)
        budget = case_model.normalize_budget(budget_in, previous.get("budget"))
        sources = case_model.normalize_sources(a, previous.get("sources"))
        charter = case_model.build_charter(previous["objective"], answers, budget, sources)
        digest = case_model.content_hash(case["id"], charter, answers)
        if digest == latest["content_hash"]:
            view = self._case_view(case, latest, base_url)
            view["unchanged"] = True
            return view
        status = case_model.STATUS_FOR_CLARIFICATION[charter["status"]]
        version = self.store.revise_case(case["id"], user_id, expected, status, digest, charter, answers)
        if version == -1:
            raise CaseVersionConflict("another revision of this case was saved first; read it again and resubmit")
        if version <= 0:
            raise CaseNotFound("unknown case_id")
        return self._case_view(
            {**case, "status": status},
            {"version": version, "content_hash": digest, "charter": charter},
            base_url,
        )

    def _approval_state(self, user_id: str, case: dict[str, Any], row: dict[str, Any]) -> dict[str, Any]:
        approval = self.store.latest_case_approval(user_id, case["id"])
        if not approval or str(approval.get("user_id")) != str(user_id):
            return {"state": "none"}
        if approval.get("consumed_at"):
            state = "consumed"
        elif approval.get("revoked_at"):
            state = "revoked"
        elif (int(approval.get("charter_version") or 0) != int(row["version"])
              or approval.get("content_hash") != row["content_hash"]):
            state = "stale"
        elif self._expired(approval):
            state = "expired"
        else:
            state = "live"
        return {
            "state": state,
            "approval_id": str(approval.get("id")),
            "charter_version": approval.get("charter_version"),
            "expires_at": approval.get("expires_at"),
            "run_id": approval.get("run_id"),
            "run_status": approval.get("run_status"),
        }

    def list_owned_cases(self, user_id: str) -> dict[str, Any]:
        """Bounded workspace index; ownership never comes from request parameters."""
        rows = self.store.list_cases(user_id, limit=101)
        owned = [row for row in rows if str(row.get("user_id")) == str(user_id)]
        return {"cases": [{key: row[key] for key in ("id", "objective", "status", "created_at")
                           if key in row} for row in owned[:100]],
                "truncated": len(owned) > 100, "limit": 100}

    def case_status(self, user_id: str, a: dict[str, Any], base_url: str = "") -> dict[str, Any]:
        case = self._case(user_id, a.get("case_id"))
        row = self._latest_charter(user_id, case["id"])
        view = self._case_view(case, row, base_url)
        view["approval"] = self._approval_state(user_id, case, row)
        return view

    def case_charter_details(self, user_id: str, case_id: str, base_url: str = "") -> dict[str, Any]:
        """Everything the browser approval page shows: the exact charter the owner approves."""
        case = self._case(user_id, case_id)
        row = self._latest_charter(user_id, case["id"])
        view = self._case_view(case, row, base_url)
        view["approval"] = self._approval_state(user_id, case, row)
        view["charter"] = row["charter"]
        view["approval_ttl_minutes"] = case_model.approval_ttl_seconds() // 60
        # The same owner check and store boundary used by MCP protect browser
        # history. Only project public fields: never return approval tokens,
        # internal payloads or worker credentials from an event row.
        events = self.store.list_case_events(user_id, case["id"])
        owned = [event for event in events
                 if str(event.get("user_id")) == str(user_id)
                 and str(event.get("case_id")) == str(case["id"])]
        view["history"] = {
            "events": [{key: event[key] for key in
                        ("kind", "created_at", "charter_version", "run_id") if key in event}
                       for event in owned[-200:]],
            "truncated": len(owned) > 200,
            "limit": 200,
        }
        return view

    def approve_case_charter(
        self, user_id: str, case_id: str, charter_version: Any, content_hash: Any,
    ) -> dict[str, Any]:
        """Record the signed-in owner's approval of one exact charter version (browser only)."""
        case = self._case(user_id, case_id)
        row = self._latest_charter(user_id, case["id"])
        try:
            version = int(charter_version)
        except (TypeError, ValueError):
            raise CaseInvalid("charter_version must be an integer") from None
        if version != int(row["version"]) or str(content_hash or "") != row["content_hash"]:
            raise CaseVersionConflict("the charter changed since this page loaded; review the latest version")
        charter = row["charter"]
        if charter.get("status") != "READY_FOR_SCOPE_APPROVAL":
            raise CaseNotReady("the charter still has open questions; answer them before approving")
        now = self._clock()
        expires = now + timedelta(seconds=case_model.approval_ttl_seconds())
        token = secrets.token_urlsafe(32)  # one-time consumption key; only its hash is stored
        budget = case_model.normalize_budget(charter.get("budget"))
        approval = {
            "id": str(uuid.uuid4()),
            "case_id": str(case["id"]),
            "user_id": user_id,
            "charter_version": version,
            "content_hash": row["content_hash"],
            "scope": case_model.approval_scope(charter),
            "budget_usd": budget["max_spend_usd"],
            "budget_units": budget["max_units"],
            "expires_at": expires.isoformat(),
            "token_hash": hashlib.sha256(token.encode("utf-8")).hexdigest(),
        }
        if not self.store.approve_case_charter(approval):
            raise CaseVersionConflict("approval refused: the charter changed or the approval window closed")
        return {
            "kind": "case_approval",
            "case_id": approval["case_id"],
            "approval_id": approval["id"],
            "charter_version": version,
            "content_hash": approval["content_hash"],
            "budget": budget,
            "expires_at": approval["expires_at"],
            "status": "approved",
        }

    def _authorized_case(
        self, user_id: str, a: dict[str, Any], base_url: str,
    ) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
        """The case, its latest charter and the approval that authorizes research on it."""
        case = self._case(user_id, a.get("case_id"))
        row = self._latest_charter(user_id, case["id"])
        charter = row["charter"]
        objective = str(a.get("objective") or "").strip()
        if objective and objective != charter["objective"]:
            raise CaseInvalid("objective does not match the case charter")
        url = self._approval_url(base_url, case["id"])
        if charter.get("status") != "READY_FOR_SCOPE_APPROVAL":
            raise CaseNotReady("the case still has open questions; answer them with clarify_objective first")
        approval = self.store.latest_case_approval(user_id, case["id"])
        if (not approval or str(approval.get("user_id")) != str(user_id)
                or str(approval.get("case_id")) != str(case["id"])):
            raise CaseApprovalRequired(f"approve charter version {row['version']} in the browser: {url}")
        if (approval.get("revoked_at") or int(approval.get("charter_version") or 0) != int(row["version"])
                or approval.get("content_hash") != row["content_hash"]):
            raise CaseApprovalRequired(
                f"the charter changed after it was approved; approve version {row['version']} in the browser: {url}"
            )
        budget = case_model.normalize_budget(charter.get("budget"))
        if (abs(float(approval.get("budget_usd", -1)) - budget["max_spend_usd"]) > 1e-9
                or abs(float(approval.get("budget_units", -1)) - budget["max_units"]) > 1e-9):
            raise CaseApprovalRequired(f"the approved budget does not match the charter; approve again: {url}")
        if not approval.get("consumed_at") and self._expired(approval):
            raise CaseApprovalRequired(f"the approval expired; approve the charter again: {url}")
        return case, row, approval

    @staticmethod
    def _charter_inputs(charter: dict[str, Any]) -> dict[str, Any]:
        """Execution inputs come from the approved charter only, never from the client call."""
        budget = case_model.normalize_budget(charter.get("budget"))
        inputs: dict[str, Any] = dict(charter.get("sources") or {})
        inputs["objective"] = charter["objective"]
        inputs["max_spend_usd"] = budget["max_spend_usd"]
        return validate_remote_args(inputs)

    def _clarification_gate(self, a: dict[str, Any]) -> dict[str, Any] | None:
        objective = str(a.get("objective") or "").strip()
        if not objective or not requires_clarification(objective):
            return None
        # Nothing the client sends is authority. A `case_charter` argument (once honoured
        # when it said approved: true) is ignored; only a browser-recorded approval of the
        # latest charter version, used through case_id, lets serious research start.
        result = clarify_case_objective(
            objective,
            a.get("answers") if isinstance(a.get("answers"), dict) else None,
        )
        out = clarification_to_dict(result)
        out["approval_required"] = True
        out["message"] = (
            "This objective is consequential or underspecified. Open an Intelligence Case with "
            "clarify_objective, answer (or mark unknown) the highest-impact questions, have the account "
            "owner approve the Case Charter in the browser, then call again with the case_id."
        )
        return out

    def compile_objective(self, a: dict[str, Any]) -> dict[str, Any]:
        a = validate_remote_args(a)
        c = compile_intent(
            a["objective"],
            max_spend_usd=float(a.get("max_spend_usd", 5.0)),
            location=self._location(a),
        )
        return to_dict(c)

    def plan_research(
        self, a: dict[str, Any], user_id: str | None = None, base_url: str = "",
    ) -> dict[str, Any]:
        a = validate_remote_args(a)
        if a.get("case_id") not in (None, ""):
            if not user_id:
                raise CaseApprovalRequired("case research requires an authenticated account")
            case, row, approval = self._authorized_case(user_id, a, base_url)
            out = self._plan(self._charter_inputs(row["charter"]))
            out.update({
                "case_id": str(case["id"]),
                "charter_version": int(row["version"]),
                "execution_inputs": "approved_charter",
                "approved_budget": case_model.normalize_budget(row["charter"].get("budget")),
            })
            return out
        clarification = self._clarification_gate(a)
        if clarification is not None:
            return clarification
        return self._plan(a)

    def _plan(self, a: dict[str, Any]) -> dict[str, Any]:
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

    @staticmethod
    def _partial_usage(provider: Any, ledger: CostLedger) -> tuple[float, float, list[str]] | None:
        """(units, known cost, unpriced components) of the work done so far, or None when none was done."""
        calls = int(((getattr(provider, "usage", None) or {}).get("calls")) or 0)
        units = float(ledger.total_units)
        spent = any(float(e.usd) > 0 for e in ledger.entries)
        if units <= 0 and calls <= 0 and not spent:
            return None
        try:
            info = provider.describe()
        except Exception:
            info = {"name": "unknown", "usage": dict(getattr(provider, "usage", {}) or {})}
        cost = actual_run_cost(info, ledger)
        return units, cost.known_cost_usd, sorted(set(cost.unpriced_components) | {"partial_run"})

    def _charge_partial_run(self, reservation_id: str, provider: Any, ledger: CostLedger) -> None:
        """A run that raised: charge the work and cost incurred so far, or release if there was none."""
        try:
            partial = self._partial_usage(provider, ledger)
            if partial is None:
                self._release_quietly(reservation_id)
                return
            units, known, unpriced = partial
            self._settle_work(
                reservation_id,
                run_id=None,  # nothing was persisted, so the charge names no run
                actual_units=units,
                known_cost_usd=known,
                unpriced_components=unpriced,
            )
        except Exception:
            _LOG.error("could not account for the partial run on reservation %s", reservation_id)

    # ---- the research core shared by the synchronous path and the durable worker ----------

    def _prepare_research(
        self, objective: str, a: dict[str, Any], budget_units: float | None,
    ) -> tuple[dict[str, Any], Any, Any]:
        """Validate, compile and plan (no work, no reservation). Refuses a plan over the budget."""
        a = validate_remote_args(a)
        c = compile_intent(
            objective,
            max_spend_usd=float(a.get("max_spend_usd", 5.0)),
            location=self._location(a),
        )
        plan = plan_research(c, self._registry(a))
        if budget_units is not None and float(plan.estimated_work_units) > float(budget_units) + 1e-9:
            raise CaseBudgetExceeded(
                f"the research plan needs {plan.estimated_work_units:g} units but the approved charter allows "
                f"{float(budget_units):g}; revise the budget and approve the charter again"
            )
        return a, c, plan

    @staticmethod
    def _check_cost_ceiling(plan: Any, execution_plan: str) -> None:
        # Public safety ceiling independent of user-supplied max_spend.
        public_cap = float(os.environ.get("LI_PUBLIC_MAX_ESTIMATED_USD_PER_RUN", "5.0"))
        est = estimate_run(plan, execution_plan)
        if est.total_usd > public_cap:
            raise QuotaExceeded("run exceeds the public per-run cost ceiling")

    @staticmethod
    def _require_sync_bounded(plan: Any) -> None:
        """Refuse inline research that is not bounded well under the 60 s request cap."""
        try:
            max_units = float(os.environ.get("LI_SYNC_MAX_WORK_UNITS", "") or SYNC_MAX_WORK_UNITS)
        except ValueError:
            max_units = SYNC_MAX_WORK_UNITS
        try:
            max_network = int(os.environ.get("LI_SYNC_MAX_NETWORK_TASKS", "") or SYNC_MAX_NETWORK_TASKS)
        except ValueError:
            max_network = SYNC_MAX_NETWORK_TASKS
        if any(t.adapter_id in {"europepmc", "clinicaltrials", "operator_sources"} for t in plan.tasks):
            raise AsyncRequired("public-source retrieval requires start_research and the durable worker")
        network = sum(1 for t in plan.tasks if t.adapter_id not in LOCAL_ADAPTERS)
        if float(plan.estimated_work_units) > max_units + 1e-9 or network > max_network:
            raise AsyncRequired(
                "this research is too large to run inside one request; call start_research and poll "
                "get_job_status"
            )

    def _execute_research(
        self, attempt: _ResearchAttempt, contract: Any, a: dict[str, Any],
        stage_hook: Callable[[str], None] | None = None,
    ) -> None:
        """Run the investigation and snapshot it. On a raise, attempt.provider/ledger hold the work so far."""
        kwargs: dict[str, Any] = {}
        if stage_hook is not None:
            kwargs["stage_hook"] = stage_hook
        result = run_investigation(
            contract,
            self._registry(a),
            attempt.provider,
            attempt.execution_plan,
            approved=False,  # public V1 never executes external actions
            ledger=attempt.ledger,
            **kwargs,
        )
        attempt.result = result
        attempt.snap = durable_snapshot(result)
        attempt.cost = actual_run_cost(result.provider_info, result.ledger)

    def _run_record(self, user_id: str, objective: str, attempt: _ResearchAttempt) -> dict[str, Any]:
        snap, cost = attempt.snap, attempt.cost
        if snap is None or cost is None:
            raise PublicServiceError("the research produced no result to save")
        return {
            "run_id": snap["run_id"],
            "user_id": user_id,
            "objective": objective,
            "status": "complete" if attempt.result.completed else "stopped",
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

    def _persist_run(self, user_id: str, objective: str, attempt: _ResearchAttempt) -> None:
        self.store.save_run(self._run_record(user_id, objective, attempt))

    @staticmethod
    def _run_output(snap: dict[str, Any], cost: Any) -> dict[str, Any]:
        out = summary(snap)
        out["known_cost_usd"] = cost.known_cost_usd
        out["cost_fully_priced"] = cost.fully_priced
        out["unpriced_cost_components"] = list(cost.unpriced_components)
        return out

    def _run(
        self,
        user_id: str,
        objective: str,
        a: dict[str, Any],
        *,
        budget_units: float | None = None,
        before_work: Callable[[], None] | None = None,
    ) -> dict[str, Any]:
        """Synchronous research inside the request (bounded work only; see _require_sync_bounded)."""
        a, c, plan = self._prepare_research(objective, a, budget_units)
        self._require_sync_bounded(plan)
        reservation_id = self._reserve_usage(
            user_id, "investigate", float(plan.estimated_work_units)
        )
        execution_plan = os.environ.get("LI_RUNTIME_PLAN", "payg")
        try:
            self._check_cost_ceiling(plan, execution_plan)
            if before_work is not None:
                before_work()
        except BaseException:
            # Refused before any work: return the held allowance now.
            self._release_quietly(reservation_id)
            raise

        attempt = _ResearchAttempt(execution_plan)
        try:
            self._execute_research(attempt, c, a)
        except BaseException:
            self._charge_partial_run(reservation_id, attempt.provider, attempt.ledger)
            raise
        snap, cost = attempt.snap, attempt.cost
        assert snap is not None and cost is not None
        try:
            self._persist_run(user_id, objective, attempt)
        except BaseException:
            # The run did its work but its result was not persisted: charge the work
            # (with no run id, since no run row exists) and report the failure.
            self._settle_work(
                reservation_id,
                run_id=None,
                actual_units=snap["usage_units"],
                known_cost_usd=cost.known_cost_usd,
                unpriced_components=list(cost.unpriced_components),
            )
            raise
        self._settle_work(
            reservation_id,
            run_id=snap["run_id"],
            actual_units=snap["usage_units"],
            known_cost_usd=cost.known_cost_usd,
            unpriced_components=list(cost.unpriced_components),
        )
        return self._run_output(snap, cost)

    def investigate(self, user_id: str, a: dict[str, Any], base_url: str = "") -> dict[str, Any]:
        a = validate_remote_args(a)
        if a.get("case_id") not in (None, ""):
            return self._start_case_research(user_id, a, base_url)
        clarification = self._clarification_gate(a)
        if clarification is not None:
            return clarification
        return self._run(user_id, a["objective"], a)

    def _start_case_research(self, user_id: str, a: dict[str, Any], base_url: str) -> dict[str, Any]:
        case, row, approval = self._authorized_case(user_id, a, base_url)
        key = str(a.get("idempotency_key") or "").strip() or f"approval:{approval['id']}"
        if len(key) > 200:
            raise CaseInvalid("idempotency_key is too long")
        if approval.get("consumed_at"):
            return self._case_replay(user_id, case, row, approval, key)
        inputs = self._charter_inputs(row["charter"])
        approval_id = str(approval["id"])
        consumed: dict[str, bool] = {}

        def consume() -> None:
            got = self.store.consume_case_approval(approval_id, user_id, str(approval["token_hash"]), key)
            if got is None:
                current = self.store.latest_case_approval(user_id, case["id"]) or {}
                if str(current.get("id")) == approval_id and current.get("consumed_at"):
                    if current.get("idempotency_key") == key:
                        raise PublicService._ReplayStart(current)
                    raise CaseApprovalConsumed(
                        "this approval already started research; approve the charter again to run it again"
                    )
                raise CaseApprovalRequired(
                    "the approval is no longer valid; approve the latest charter in the browser: "
                    + self._approval_url(base_url, case["id"])
                )
            if not got.get("consumed_now"):
                raise PublicService._ReplayStart(got)
            consumed["yes"] = True

        try:
            out = self._run(
                user_id, inputs["objective"], inputs,
                budget_units=float(approval["budget_units"]),
                before_work=consume,
            )
        except PublicService._ReplayStart as replay:
            return self._case_replay(user_id, case, row, replay.approval, key)
        except BaseException:
            if consumed:
                try:
                    self.store.record_case_run(approval_id, user_id, None, "failed")
                except Exception:
                    _LOG.error("could not record the failed case run for approval %s", approval_id)
            raise
        try:
            self.store.record_case_run(approval_id, user_id, out["run_id"], "complete")
        except Exception:
            _LOG.error("could not bind run %s to case approval %s", out.get("run_id"), approval_id)
        out.update(self._case_run_fields(case, row, approval_id, key))
        return out

    @staticmethod
    def _case_run_fields(case: dict[str, Any], row: dict[str, Any], approval_id: str, key: str) -> dict[str, Any]:
        return {
            "case_id": str(case["id"]),
            "charter_version": int(row["version"]),
            "approval_id": approval_id,
            "idempotency_key": key,
            "execution_inputs": "approved_charter",
            "approved_budget": case_model.normalize_budget(row["charter"].get("budget")),
        }

    def _case_replay(
        self, user_id: str, case: dict[str, Any], row: dict[str, Any], approval: dict[str, Any], key: str,
    ) -> dict[str, Any]:
        """A start retried with the same idempotency key: the same run, never a second execution."""
        if approval.get("idempotency_key") != key:
            raise CaseApprovalConsumed(
                "this approval already started research; approve the charter again to run it again"
            )
        fields = self._case_run_fields(case, row, str(approval["id"]), key)
        status = approval.get("run_status")
        if status == "failed":
            raise CaseRunFailed("the research started from this approval failed; approve the charter again to retry")
        if status == "complete" and approval.get("run_id"):
            stored = self.store.get_run(user_id, str(approval["run_id"]))
            if stored and isinstance(stored.get("snapshot"), dict):
                out = summary(stored["snapshot"])
                unpriced = list(stored.get("unpriced_components") or [])
                out["known_cost_usd"] = float(stored.get("known_cost_usd") or 0.0)
                out["cost_fully_priced"] = not unpriced
                out["unpriced_cost_components"] = unpriced
                out.update(fields)
                out["replayed"] = True
                return out
        return {"kind": "intelligence_case_run", "status": "RUN_IN_PROGRESS", "replayed": True, **fields}

    # ---- durable research jobs: enqueue in the request, execute in the worker --------------
    #
    # start_research validates, plans and reserves usage in the request and enqueues a job
    # whose input is frozen (the approved charter's inputs for a case) and returns at once.
    # The worker (hosted/worker.py) claims the job under a lease and calls run_research_job,
    # which runs the same research core as the synchronous path. The reservation taken at
    # enqueue is held for the job's whole life and settled once by the M1 rules.

    def _job_settings(self) -> job_model.JobSettings:
        return getattr(self, "job_settings", None) or job_model.JobSettings.from_env()

    @staticmethod
    def _job_key(a: dict[str, Any]) -> str | None:
        raw = a.get("idempotency_key")
        if raw in (None, ""):
            return None
        key = str(raw).strip()
        if not job_model.IDEMPOTENCY_KEY_RE.match(key):
            raise JobInvalid("idempotency_key must be 1-190 characters of letters, digits, '.', '_', ':' or '-'")
        return key

    @staticmethod
    def _frozen_args(a: dict[str, Any]) -> dict[str, Any]:
        return {k: a[k] for k in job_model.SOURCE_ARG_KEYS if k in a and a[k] is not None}

    def start_research(self, user_id: str, a: dict[str, Any], base_url: str = "") -> dict[str, Any]:
        """Enqueue research as a durable job and return its job_id without running it."""
        a = validate_remote_args(a)
        key = self._job_key(a)
        if a.get("case_id") not in (None, ""):
            return self._start_case_job(user_id, a, base_url, key)
        clarification = self._clarification_gate(a)
        if clarification is not None:
            return clarification
        objective = str(a.get("objective") or "").strip()
        if not objective:
            raise JobInvalid("objective is required")
        key = key or f"job:{uuid.uuid4()}"
        existing = self.store.get_research_job_by_key(user_id, key)
        if existing:
            return self._job_started_view(existing, created=False)
        budget = a.get("max_units")
        budget_units = None
        if budget is not None:
            try:
                budget_units = float(budget)
            except (TypeError, ValueError):
                raise JobInvalid("max_units must be a number") from None
            if not math.isfinite(budget_units) or budget_units <= 0:
                raise JobInvalid("max_units must be a positive number")
        args, _c, plan = self._prepare_research(objective, a, budget_units)
        return self._enqueue_job(user_id, None, key, objective, args, plan, budget_units, None)

    def _start_case_job(
        self, user_id: str, a: dict[str, Any], base_url: str, key_in: str | None,
    ) -> dict[str, Any]:
        case, row, approval = self._authorized_case(user_id, a, base_url)
        approval_id = str(approval["id"])
        key = key_in or f"approval:{approval_id}"
        existing = self.store.get_research_job_by_key(user_id, key)
        if existing:
            return self._job_started_view(existing, created=False)
        # A job consumes the approval under "job:<key>", so the synchronous path and a job
        # can never both start research from one approval.
        consume_key = f"job:{key}"
        if approval.get("consumed_at") and approval.get("idempotency_key") != consume_key:
            raise CaseApprovalConsumed(
                "this approval already started research; approve the charter again to run it again"
            )
        inputs = self._charter_inputs(row["charter"])
        budget_units = float(approval["budget_units"])
        args, _c, plan = self._prepare_research(inputs["objective"], inputs, budget_units)
        case_ref = {
            "case_id": str(case["id"]),
            "approval_id": approval_id,
            "charter_version": int(row["version"]),
            "content_hash": row["content_hash"],
            "approved_budget": case_model.normalize_budget(row["charter"].get("budget")),
        }

        def consume() -> None:
            got = self.store.consume_case_approval(approval_id, user_id, str(approval["token_hash"]), consume_key)
            if got is None:
                current = self.store.latest_case_approval(user_id, case["id"]) or {}
                if str(current.get("id")) == approval_id and current.get("consumed_at"):
                    raise CaseApprovalConsumed(
                        "this approval already started research; approve the charter again to run it again"
                    )
                raise CaseApprovalRequired(
                    "the approval is no longer valid; approve the latest charter in the browser: "
                    + self._approval_url(base_url, case["id"])
                )

        out = self._enqueue_job(user_id, str(case["id"]), key, inputs["objective"], args, plan,
                                budget_units, case_ref, before_enqueue=consume)
        out.update(self._case_run_fields(case, row, approval_id, key))
        return out

    def _enqueue_job(
        self, user_id: str, case_id: str | None, key: str, objective: str, args: dict[str, Any], plan: Any,
        budget_units: float | None, case_ref: dict[str, Any] | None,
        *, before_enqueue: Callable[[], None] | None = None,
    ) -> dict[str, Any]:
        settings = self._job_settings()
        execution_plan = os.environ.get("LI_RUNTIME_PLAN", "payg")
        reservation_id = self._reserve_usage(user_id, "investigate", float(plan.estimated_work_units))
        job_input = {
            "schema": job_model.JOB_INPUT_SCHEMA,
            "objective": objective,
            "args": self._frozen_args(args),
            "budget_units": budget_units,
            "estimated_units": float(plan.estimated_work_units),
            "execution_plan": execution_plan,
            "execution_inputs": "approved_charter" if case_ref else "request",
            "case": case_ref,
        }
        try:
            self._check_cost_ceiling(plan, execution_plan)
            if before_enqueue is not None:
                before_enqueue()
            job = self.store.enqueue_research_job(
                str(uuid.uuid4()), user_id, case_id, "investigate", key, job_input, reservation_id,
                settings.max_attempts, settings.queue_ttl_seconds,
            )
        except BaseException:
            # Refused (or the store failed) before any work: return the held allowance now.
            self._release_quietly(reservation_id)
            raise
        if job is None:
            self._release_quietly(reservation_id)
            raise PublicServiceError("the research job could not be queued; try again")
        created = bool(job.get("created"))
        if not created:
            # Another request with the same key won the race: one job, one reservation.
            self._release_quietly(reservation_id)
        return self._job_started_view(job, created=created)

    @staticmethod
    def _job_started_view(job: dict[str, Any], *, created: bool) -> dict[str, Any]:
        view = job_model.public_job_view(job)
        view["created"] = created
        view["message"] = (
            "Research is queued as a durable job. Poll get_job_status with job_id; cancel_research stops it "
            "at the next stage. Retrying start_research with the same idempotency_key returns this job."
        )
        return view

    def _job(self, user_id: str, job_id: Any) -> dict[str, Any]:
        jid = str(job_id or "").strip()
        try:
            jid = str(uuid.UUID(jid))
        except ValueError:
            raise JobNotFound("unknown job_id") from None
        row = self.store.get_research_job(user_id, jid)
        if not row or str(row.get("user_id")) != str(user_id) or str(row.get("id")) != jid:
            raise JobNotFound("unknown job_id")
        return row

    def get_job_status(self, user_id: str, a: dict[str, Any]) -> dict[str, Any]:
        """The tenant's own job: status, attempts, units so far and, when its result was saved, the run
        summary (a job that succeeded, or one whose settlement was abandoned after the result was saved)."""
        job = self._job(user_id, a.get("job_id"))
        view = job_model.public_job_view(job)
        if view["run_id"]:
            stored = self.store.get_run(user_id, str(view["run_id"]))
            if stored and isinstance(stored.get("snapshot"), dict):
                self._validate_run_owner(stored, user_id, str(view["run_id"]))
                result = summary(stored["snapshot"])
                unpriced = list(stored.get("unpriced_components") or [])
                result["known_cost_usd"] = float(stored.get("known_cost_usd") or 0.0)
                result["cost_fully_priced"] = not unpriced
                result["unpriced_cost_components"] = unpriced
                view["result"] = result
        return view

    def get_job_result(self, user_id: str, a: dict[str, Any]) -> dict[str, Any]:
        """Inspect persisted research without asserting that job settlement succeeded."""
        job = self._job(user_id, a.get("job_id"))
        view = job_model.public_job_view(job)
        out = {"job_id": view["job_id"], "status": view["status"],
               "run_id": view["run_id"], "result_available": False}
        if not view["run_id"]:
            return out
        snap = self._snapshot(user_id, str(view["run_id"]))
        out.update({"result_available": True, "completed": snap["completed"],
                    "stopped_reason": snap["stopped_reason"], "report": snap["report"],
                    "findings": snap["findings"], "contradictions": snap["contradictions"],
                    "unknowns": snap["unknowns"], "traces": snap["traces"],
                    "receipt": self.get_receipt(user_id, {"run_id": view["run_id"]})})
        return out

    def list_owned_jobs(self, user_id: str) -> dict[str, Any]:
        """Bounded reconnect index; never expose frozen inputs or lease state."""
        rows = self.store.list_research_jobs(user_id, limit=101)
        owned = [row for row in rows if str(row.get("user_id")) == str(user_id)]
        return {"jobs": [job_model.public_job_view(row) for row in owned[:100]],
                "truncated": len(owned) > 100, "limit": 100}

    def cancel_research(self, user_id: str, a: dict[str, Any]) -> dict[str, Any]:
        """Cancel the tenant's own job: queued -> cancelled now; running -> stops at its next stage."""
        job = self._job(user_id, a.get("job_id"))
        before = str(job.get("status"))
        row = self.store.request_cancel_research_job(str(job["id"]), user_id)
        if row is None:
            raise JobNotFound("unknown job_id")
        if str(row.get("status")) == "cancelled" and before == "queued":
            # The database ended the job and accounted its reservation (released, or an
            # unsettled marker for work recorded by an earlier attempt): settle it now.
            try:
                self.reconcile_usage(str(row["reservation_id"]))
            except Exception:
                _LOG.warning("settlement of cancelled job %s is pending reconciliation", row.get("id"))
            self._record_case_outcome(row, None, "failed")
        view = job_model.public_job_view(row)
        status = view["status"]
        view["cancel_effect"] = {
            "cancelled": "cancelled" if before != "cancelled" else "already_cancelled",
            "cancel_requested": "will_stop_at_next_stage",
        }.get(status, "no_op_" + status)
        return view

    def _record_case_outcome(self, job: dict[str, Any], run_id: str | None, status: str) -> None:
        case_ref = (job.get("input") or {}).get("case") if isinstance(job.get("input"), dict) else None
        if not case_ref:
            return
        try:
            self.store.record_case_run(str(case_ref["approval_id"]), str(job["user_id"]), run_id, status)
        except Exception:
            _LOG.error("could not record the case outcome of research job %s", job.get("id"))

    # ---- worker side --------------------------------------------------------------------

    def run_research_job(self, job: dict[str, Any], lease: job_model.JobLease) -> dict[str, Any]:
        """Execute one claimed job with the shared research core (called by the worker).

        Uses only the frozen job input. Checkpoints and honours cancel/budget/shutdown at
        every stage boundary, then settles the job's reservation by the M1 rules: release
        when nothing ran, charge the partial cost after work began, and record an unsettled
        marker plus reconcile when settlement fails after the result is saved.
        Returns {"status": <final or transitional status>, ...}.
        """
        settings = self._job_settings()
        checkpoint = dict(job.get("checkpoint") or {})
        phase = checkpoint.get("phase")
        if phase == "result_saved":
            return self._complete_job(job, lease, checkpoint)
        if phase == "finalizing":
            return self._finalize_job(job, lease, checkpoint)

        base_units = float(job.get("cost_so_far") or 0.0)
        base_cost = float(checkpoint.get("known_cost_usd") or 0.0)
        attempts = int(job.get("attempts") or 1)

        def ending(code: str, units: float, known: float, unpriced: list[str]) -> dict[str, Any]:
            return self._finalize_job(job, lease, {
                "phase": "finalizing", "outcome": code, "units": units, "known_cost_usd": known,
                "unpriced": unpriced, "attempt": attempts,
            })

        inp = job.get("input")
        try:
            if (not isinstance(inp, dict) or inp.get("schema") != job_model.JOB_INPUT_SCHEMA
                    or not isinstance(inp.get("args"), dict) or not str(inp.get("objective") or "").strip()):
                raise JobInvalid("the stored job input is invalid")
            budget_units = None if inp.get("budget_units") is None else float(inp["budget_units"])
            execution_plan = str(inp.get("execution_plan") or "payg")
            if execution_plan not in PLANS:
                raise JobInvalid("the stored job input is invalid")
            objective = str(inp["objective"])
            args, contract, _plan = self._prepare_research(objective, dict(inp["args"]), None)
        except (PublicServiceError, PublicInputError, ValueError, TypeError, KeyError) as exc:
            code = getattr(exc, "code", "JOB_INVALID")
            return ending(code, base_units, base_cost, ["job_refused"])

        attempt = _ResearchAttempt(execution_plan)

        def progress() -> tuple[float, float]:
            partial = self._partial_usage(attempt.provider, attempt.ledger)
            if partial is None:
                return base_units, base_cost
            return base_units + partial[0], base_cost + partial[1]

        def stage_hook(stage: str) -> None:
            units, known = progress()
            lease.beat({"phase": "running", "stage": stage, "attempt": attempts,
                        "base_units": base_units, "known_cost_usd": known}, units)
            if lease.cancel_requested:
                raise job_model.JobCancelled(stage)
            if budget_units is not None and units > budget_units + 1e-9:
                raise job_model.JobBudgetExhausted(stage)
            if lease.shutdown.is_set():
                raise job_model.WorkerShutdown(stage)

        try:
            stage_hook("start")
            self._execute_research(attempt, contract, args, stage_hook=stage_hook)
        except job_model.LeaseLost:
            # The job belongs to whoever reclaimed it; this worker neither finishes nor charges.
            return {"status": "lease_lost"}
        except job_model.JobCancelled:
            units, known = progress()
            return ending("CANCELLED", units, known, self._partial_components(attempt))
        except job_model.JobBudgetExhausted:
            units, known = progress()
            return ending("BUDGET_EXHAUSTED", units, known, self._partial_components(attempt))
        except job_model.WorkerShutdown:
            units, known = progress()
            row = self.store.fail_research_job(
                str(job["id"]), lease.worker_id, "WORKER_SHUTDOWN", True, 0, units,
                {"phase": "requeued", "known_cost_usd": known, "attempt": attempts},
                settings.queue_ttl_seconds,
            )
            return {"status": str((row or {}).get("status") or "lease_lost")}
        except PublicServiceError as exc:
            units, known = progress()
            return ending(exc.code, units, known, self._partial_components(attempt))
        except Exception as exc:
            units, known = progress()
            code = ("PROVIDER_UNAVAILABLE"
                    if isinstance(exc, (OSError, TimeoutError, ConnectionError, urllib.error.URLError))
                    else "EXECUTION_FAILED")
            _LOG.warning("research job %s attempt %s failed (%s)", job.get("id"), attempts, code)
            if attempts < int(job.get("max_attempts") or 1):
                row = self.store.fail_research_job(
                    str(job["id"]), lease.worker_id, code, True, settings.backoff(attempts), units,
                    {"phase": "retry_wait", "known_cost_usd": known, "attempt": attempts, "last_error": code},
                    settings.queue_ttl_seconds,
                )
                if row is None:
                    return {"status": "lease_lost"}
                return {"status": str(row.get("status")), "error_code": code}
            return ending(code, units, known, self._partial_components(attempt))

        snap, cost = attempt.snap, attempt.cost
        assert snap is not None and cost is not None
        total_units = base_units + float(snap["usage_units"])
        total_known = base_cost + float(cost.known_cost_usd)
        saved = {
            "phase": "result_saved", "run_id": snap["run_id"], "units": total_units,
            "known_cost_usd": total_known, "unpriced": list(cost.unpriced_components), "attempt": attempts,
        }
        try:
            row = self.store.save_research_job_result(
                str(job["id"]), lease.worker_id,
                self._run_record(str(job["user_id"]), objective, attempt), saved,
                settings.lease_seconds, settings.hold_grace_seconds,
            )
        except Exception:
            # The transaction may have committed but its response was lost. Do not
            # finalize a failure: lease reclaim inspects the persisted checkpoint.
            _LOG.warning("research job %s result persistence outcome unknown; awaiting lease recovery", job["id"])
            return {"status": "persistence_unknown"}
        if row is None:
            return {"status": "lease_lost"}
        return self._complete_job(job, lease, saved)

    @staticmethod
    def _partial_components(attempt: _ResearchAttempt) -> list[str]:
        partial = PublicService._partial_usage(attempt.provider, attempt.ledger)
        return partial[2] if partial is not None else ["partial_run"]

    def _complete_job(self, job: dict[str, Any], lease: job_model.JobLease, saved: dict[str, Any]) -> dict[str, Any]:
        """Settle the saved result (idempotent) and mark the job succeeded."""
        settlement = self._settle_work(
            str(job["reservation_id"]),
            run_id=str(saved["run_id"]),
            actual_units=float(saved["units"]),
            known_cost_usd=float(saved.get("known_cost_usd") or 0.0),
            unpriced_components=list(saved.get("unpriced") or []),
        )
        row = self.store.complete_research_job(str(job["id"]), lease.worker_id, str(saved["run_id"]),
                                               float(saved["units"]))
        if row is None:
            return {"status": "lease_lost", "settlement": settlement}
        self._record_case_outcome(job, str(saved["run_id"]), "complete")
        return {"status": "succeeded", "run_id": str(saved["run_id"]), "settlement": settlement}

    def _finalize_job(self, job: dict[str, Any], lease: job_model.JobLease, final: dict[str, Any]) -> dict[str, Any]:
        """End a job that did not succeed: checkpoint the decision, account the reservation, finish the row."""
        settings = self._job_settings()
        units = float(final.get("units") or 0.0)
        known = float(final.get("known_cost_usd") or 0.0)
        code = str(final["outcome"])
        try:
            lease.beat(final, units)
        except job_model.LeaseLost:
            return {"status": "lease_lost"}
        reservation_id = str(job["reservation_id"])
        if units <= 0 and known <= 0:
            self._release_quietly(reservation_id)  # refused or stopped before any work
            settlement = "released"
        else:
            settlement = self._settle_work(
                reservation_id, run_id=None, actual_units=units, known_cost_usd=known,
                unpriced_components=sorted(set(final.get("unpriced") or []) | {"partial_run"}),
            )
        row = self.store.fail_research_job(
            str(job["id"]), lease.worker_id, code, False, 0, units, final, settings.queue_ttl_seconds,
        )
        if row is None:
            return {"status": "lease_lost", "settlement": settlement}
        self._record_case_outcome(job, None, "failed")
        return {"status": str(row.get("status")), "error_code": code, "settlement": settlement,
                "cost_units": units}

    def verify_claim(self, user_id: str, a: dict[str, Any]) -> dict[str, Any]:
        return self._run(user_id, f"Verify the claim: {a['claim']}", a)

    @staticmethod
    def _validate_run_owner(row: dict[str, Any], user_id: str, run_id: str) -> None:
        if str(row.get("user_id")) != str(user_id) or str(row.get("run_id")) != str(run_id):
            raise PublicServiceError("unknown run_id")
        snap = row.get("snapshot")
        if isinstance(snap, dict) and str(snap.get("run_id")) != str(run_id):
            raise PublicServiceError("stored run snapshot identity is invalid")
        if isinstance(snap, dict):
            receipt = snap.get("receipt")
            if not isinstance(receipt, dict) or str(receipt.get("research_id")) != str(run_id):
                raise PublicServiceError("stored receipt identity is invalid")

    def _snapshot(self, user_id: str, run_id: str) -> dict[str, Any]:
        row = self.store.get_run(user_id, run_id)
        if not row:
            raise PublicServiceError("unknown run_id")
        self._validate_run_owner(row, user_id, run_id)
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
        return {"intact": verify_receipt(snap["receipt"]), "receipt": snap["receipt"]}

    def export_state(self, user_id: str, a: dict[str, Any]) -> dict[str, Any]:
        return self._snapshot(user_id, a["run_id"])["state"]

    def export_knowledge_map2(self, user_id: str, a: dict[str, Any]) -> dict[str, Any]:
        return self._snapshot(user_id, a["run_id"])["knowledge_map2"]

    def render_report(self, user_id: str, a: dict[str, Any]) -> dict[str, Any]:
        snap = self._snapshot(user_id, a["run_id"])
        return {"run_id": a["run_id"], "format": "markdown", "report": snap["report"]}

    def _discovery_context(self, user_id: str, run_id: str) -> DiscoveryContext:
        snap = self._snapshot(user_id, run_id)
        try:
            return DiscoveryContext(snap["knowledge_map2"], snap["receipt"])
        except Exception:
            # Never echo the reason: it names stored objects and schema details.
            raise RunStateInvalid(
                "the stored research run cannot be reloaded; run investigate again"
            ) from None

    def _discovery_snapshot(self, user_id: str, discovery_id: str) -> dict[str, Any]:
        row = self.store.get_discovery(user_id, discovery_id)
        if not row:
            raise PublicServiceError("unknown discovery_id")
        snap = row.get("snapshot")
        if not isinstance(snap, dict) or any(k not in snap for k in REQUIRED_DISCOVERY_KEYS):
            raise DiscoveryStateInvalid(DISCOVERY_STATE_INVALID_MESSAGE)
        return snap

    def discover(self, user_id: str, a: dict[str, Any]) -> dict[str, Any]:
        run_id = str(a["run_id"])
        reserve = float(os.environ.get("LI_PUBLIC_MAX_DISCOVERY_UNITS", "100"))
        reservation_id = self._reserve_usage(user_id, "discover", reserve)
        result = None
        try:
            ctx = self._discovery_context(user_id, run_id)
            result = run_discovery(
                ctx,
                str(a["objective"]),
                design=a.get("design"),
                prior_art=a.get("prior_art"),
            )
            snap = durable_discovery_snapshot(result)
        except BaseException:
            ledger = getattr(result, "ledger", None)
            units = float(ledger.total_units) if ledger is not None else 0.0
            if units > 0:
                # Discovery ran but its snapshot failed: charge the measured work.
                self._settle_work(reservation_id, run_id=run_id, actual_units=units,
                                  unpriced_components=["discovery_compute"])
            else:
                # Refused or crashed before any measured work: nothing to charge.
                self._release_quietly(reservation_id)
            raise
        charge = {
            "run_id": run_id,
            "actual_units": snap["usage_units"],
            "known_cost_usd": 0.0,
            "unpriced_components": ["discovery_compute"],
        }
        if snap["usage_units"] > reserve:
            # The work already ran: charge what it used, then refuse to keep the result.
            self._settle_work(reservation_id, **charge)
            raise QuotaExceeded("discovery exceeded the public bounded-work ceiling")
        try:
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
        except BaseException:
            self._settle_work(reservation_id, **charge)
            raise
        self._settle_work(reservation_id, **charge)
        return {"kind": "discovery", "contract": "lofgren.mcp/2", **snap["summary"]}

    def find_prior_art(self, user_id: str, a: dict[str, Any]) -> dict[str, Any]:
        run_id = str(a["run_id"])
        return self._metered(user_id, "find_prior_art", run_id, lambda: self._find_prior_art(user_id, a))

    def _find_prior_art(self, user_id: str, a: dict[str, Any]) -> dict[str, Any]:
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
        return self._metered(user_id, "simulate_candidate", str(a["run_id"]),
                             lambda: self._simulate_candidate(user_id, a))

    def _simulate_candidate(self, user_id: str, a: dict[str, Any]) -> dict[str, Any]:
        result = self._adhoc_discovery(user_id, a)
        if not result.simulations:
            raise PublicServiceError("the parameters do not give every model input a value")
        return {
            "kind": "simulated",
            "confidence_kind": "simulation_uncertainty",
            "simulation": result.simulations[0].to_dict(),
        }

    def analyze_sensitivity(self, user_id: str, a: dict[str, Any]) -> dict[str, Any]:
        return self._metered(user_id, "analyze_sensitivity", str(a["run_id"]),
                             lambda: self._analyze_sensitivity(user_id, a))

    def _analyze_sensitivity(self, user_id: str, a: dict[str, Any]) -> dict[str, Any]:
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
        return self._metered(user_id, "optimize_solution", str(a["run_id"]),
                             lambda: self._optimize_solution(user_id, a))

    def _optimize_solution(self, user_id: str, a: dict[str, Any]) -> dict[str, Any]:
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
        try:
            row = self.store.get_discovery(user_id, discovery_id)
            if not row:
                raise PublicServiceError("unknown discovery_id")
            snap = self._discovery_snapshot(user_id, discovery_id)
            if not snap.get("handoff"):
                raise PublicServiceError("discovery has no validated V3 handoff")
            base = self._discovery_context(user_id, str(row["research_id"]))
            try:
                context = restore_discovery_context(base, snap)
            except Exception:
                # Restore fails closed on tampered, truncated or missing typed objects. The
                # internal reason (object ids, digests, receipt mismatches) stays on the server.
                raise DiscoveryStateInvalid(DISCOVERY_STATE_INVALID_MESSAGE) from None
            result = produce_artifact(
                snap["handoff"],
                discovery_receipt=snap["receipt"],
                context=context,
                kind=str(a.get("kind") or "structured_bundle"),
            )
        except BaseException:
            # Nothing was built: return the held allowance instead of letting it sit until expiry.
            self._release_quietly(reservation_id)
            raise
        charge = {"run_id": row["research_id"], "actual_units": 10.0, "unpriced_components": ["artifact_compute"]}
        try:
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
        except BaseException:
            # The artifact was built (the work ran) but not stored: charge it, then fail.
            self._settle_work(reservation_id, **charge)
            raise
        self._settle_work(reservation_id, **charge)
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
        # An arbitrary webhook may ignore Idempotency-Key. Until action
        # claiming and ambiguous-result recovery are durable, public hosted
        # execution must be disabled independently of user approval.
        if os.environ.get("LI_EXTERNAL_EXECUTION_ENABLED") != "true":
            raise PublicServiceError(
                "EXTERNAL_EXECUTION_DISABLED: hosted external execution is disabled; "
                "action proposals and receipt inspection remain available"
            )
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
        try:
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
        except BaseException:
            # Refused, or the evaluation failed with nothing to show: not charged.
            self._release_quietly(reservation_id)
            raise
        charge = {"run_id": None, "actual_units": 5.0, "unpriced_components": ["outcome_compute"]}
        try:
            self.store.save_outcome({
                "user_id": user_id,
                "outcome_id": outcome_id,
                "action_id": action["action_id"],
                "receipt": result.receipt,
                "v6_handoff": result.v6_handoff,
                "created_at": utcnow(),
            })
        except BaseException:
            self._settle_work(reservation_id, **charge)
            raise
        self._settle_work(reservation_id, **charge)
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
        try:
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
        except BaseException:
            # Refused, or the evaluation failed with nothing to show: not charged.
            self._release_quietly(reservation_id)
            raise
        charge = {"run_id": None, "actual_units": 5.0, "unpriced_components": ["improvement_compute"]}
        try:
            self.store.save_improvement({
                "user_id": user_id,
                "improvement_id": improvement_id,
                "outcome_id": outcome["outcome_id"],
                "receipt": result.receipt,
                "next_cycle": result.next_cycle,
                "created_at": utcnow(),
            })
        except BaseException:
            self._settle_work(reservation_id, **charge)
            raise
        self._settle_work(reservation_id, **charge)
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
        return self._metered(user_id, "satellite_passes", None, lambda: self._satellite_passes(a))

    def _satellite_passes(self, a: dict[str, Any]) -> dict[str, Any]:
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
        # The versioned plan catalog is what the hosted service sells and grants.
        d["catalog"] = CATALOG.as_dict()
        return d

    # The only checkout field a client may send. Price ids, allowances and
    # entitlement values are server-side catalog/configuration, never input.
    CHECKOUT_FIELDS = frozenset({"plan_id"})

    def checkout(self, user_id: str, base_url: str, request: dict[str, Any] | None = None) -> dict[str, Any]:
        request = dict(request or {})
        extra = sorted(str(k) for k in request if k not in self.CHECKOUT_FIELDS)
        if extra:
            raise PublicServiceError(
                "checkout accepts only plan_id; prices, allowances and entitlements are set by the server")
        requested = request.get("plan_id")
        if requested is not None and not isinstance(requested, str):
            raise PublicServiceError("plan_id must be a string")
        if os.environ.get("LI_BILLING_ENABLED", "").lower() not in {"1", "true", "yes"}:
            raise PublicServiceError("billing checkout is not enabled")
        sellable = checkout_plans()
        if requested is None:
            if len(sellable) != 1:
                raise PublicServiceError("no single paid plan is open for checkout")
            requested = next(iter(sellable))
        if requested not in sellable:
            raise PublicServiceError("plan is not available for checkout")
        plan = CATALOG.get(requested)
        gate = certify_paid_plan(self.store.cost_samples(), plan)
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
            plan_id=requested,
            success_url=base_url.rstrip("/") + "/billing/success?session_id={CHECKOUT_SESSION_ID}",
            cancel_url=base_url.rstrip("/") + "/billing/cancelled",
        )
        return {"checkout_url": session.get("url"), "session_id": session.get("id"), "plan_id": requested}


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
            "cases": self.store.list_cases(user_id),
            "research_jobs": [job_model.public_job_view(j) for j in self.store.list_research_jobs(user_id)],
        }

    def delete_account(self, user_id: str, confirmation: str) -> dict[str, Any]:
        """Permanently delete an account, completely and fail-closed.

        Order (docs/PUBLIC_PRIVACY.md, "User controls"):

        1. Stop new work. Every OAuth access token is revoked, every refresh
           token and unused authorization code is consumed, and the
           entitlement is closed (active = false), so li_reserve_usage and
           _reserve_usage refuse any new metered call from here on.
        2. Research jobs, per the job model (li_request_cancel_research_job):
           a queued job is cancelled at once and its reservation released (or
           its recorded work marked for settlement); a running job is asked to
           stop at its next stage.
        3. Usage accounting: every 'unsettled' marker is settled exactly once
           (li_settle_usage); an open reservation that can no longer belong to
           live work is released, exactly as li_reserve_usage would expire it.
        4. If any job has not settled yet, or any reservation may still belong
           to live work, deletion stops here (AccountDeletionPending) before
           anything is cancelled or deleted. A retry completes it; the account
           stays closed meanwhile.
        5. The Stripe subscription is cancelled. If that fails, deletion stops
           before identity/data removal (cancel_subscription treats an already
           ended or missing subscription as cancelled, so a retry is safe).
        6. The Supabase Auth user is deleted; the database cascade removes the
           account, entitlement, tokens, runs, discoveries, artifacts, actions,
           outcomes, improvements, cases, jobs, reservations and usage events.
           An already-deleted user counts as deleted (idempotent retry).
        7. The deletion is verified: no account, entitlement or job may remain.
        """
        if confirmation != ACCOUNT_DELETION_PHRASE:
            raise PublicServiceError(f"confirmation must exactly equal: {ACCOUNT_DELETION_PHRASE}")

        # 1. Stop new work before looking at what is still open.
        self.store.revoke_user_credentials(user_id)
        ent = self.store.get_entitlement(user_id)
        if ent and ent.get("active") is not False:
            self.store.close_entitlement(user_id)

        # 2. Research jobs.
        cancel_requested = 0
        for job in self.store.list_research_jobs(user_id):
            if job.get("status") in ("queued", "running"):
                if self.store.request_cancel_research_job(str(job["id"]), user_id) is not None:
                    cancel_requested += 1
        open_jobs = [job for job in self.store.list_research_jobs(user_id)
                     if job.get("status") not in job_model.TERMINAL_STATUSES]
        open_job_ids = {str(job.get("id")) for job in open_jobs}

        # 3. Usage accounting.
        settled = released = live = 0
        now = self._clock()
        for row in self.store.list_open_usage_reservations(user_id):
            rid = str(row["id"])
            status = row.get("status")
            if status == "unsettled":
                outcome = self.reconcile_usage(rid)
                if outcome == "settled":
                    settled += 1
                elif outcome != "already_settled":
                    live += 1
            elif status == "reserved":
                if _reservation_may_be_live(row, now, open_job_ids):
                    live += 1
                elif self.store.release_usage(rid):
                    released += 1
                else:
                    # It changed under us (settled or marked by its own work): look again on retry.
                    live += 1

        # 4. Refuse until everything has settled.
        if open_jobs or live:
            raise AccountDeletionPending(
                "account deletion is waiting for active research or usage to settle "
                f"({len(open_jobs)} research job(s) stopping, {live} usage reservation(s) open); "
                "nothing has been deleted and the account is closed to new work. Retry shortly."
            )

        # 5. Stripe first: no chargeable subscription may outlive the account.
        subscription_cancelled = False
        ent = self.store.get_entitlement(user_id)
        if ent and ent.get("stripe_subscription_id"):
            cancel_subscription(str(ent["stripe_subscription_id"]))
            subscription_cancelled = True

        # 6. Identity and the database cascade.
        self.store.delete_auth_user(user_id)

        # 7. Verify.
        if (self.store.get_account(user_id) is not None
                or self.store.get_entitlement(user_id) is not None
                or self.store.list_research_jobs(user_id)):
            raise PublicServiceError("account deletion did not complete; retry")
        return {
            "deleted": True,
            "subscription_cancelled": subscription_cancelled,
            "research_jobs_cancelled": cancel_requested,
            "usage_settled": settled,
            "usage_released": released,
        }


ACCOUNT_DELETION_PHRASE = "DELETE MY LOFGREN INTELLIGENCE ACCOUNT"

# li_reserve_usage expires an open reservation older than this unless a live job holds it.
RESERVATION_EXPIRY = timedelta(hours=1)


def _as_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, str) and value:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    return None


def _reservation_may_be_live(row: dict[str, Any], now: datetime, open_job_ids: set[str]) -> bool:
    """Could an open ('reserved') reservation still be settled by the work that took it?

    Yes while its job is open, while a job hold is live, or while it is younger than
    li_reserve_usage's expiry (an inline call may still finalize it). An unreadable
    timestamp is treated as live (unknown = false: never release what may be charged).
    """
    if row.get("job_id") is not None and str(row["job_id"]) in open_job_ids:
        return True
    held = row.get("held_until")
    if held is not None:
        held_dt = _as_datetime(held)
        if held_dt is None or held_dt > now:
            return True
    created = _as_datetime(row.get("created_at"))
    return created is None or created > now - RESERVATION_EXPIRY
