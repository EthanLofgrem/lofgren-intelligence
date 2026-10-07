import hashlib
import hmac
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from lofgren_intelligence.hosted.auth import OAuthService, Principal, code_challenge_s256, token_hash
from lofgren_intelligence.hosted.costing import actual_run_cost
from lofgren_intelligence.hosted.economics import certify_paid_plan
from lofgren_intelligence.kernel.ledger import CostLedger
from lofgren_intelligence.hosted.security import PublicInputError, validate_remote_args
from lofgren_intelligence.hosted.service import DiscoveryStateInvalid, PaymentRequired, PublicService
from lofgren_intelligence.hosted.stripe import apply_webhook, verify_webhook
from lofgren_intelligence.discovery.fixtures import warehouse_design

from .helpers import STRIPE_TEST_ENV, TEXTS, active_researcher, open_researcher_catalog

OBJECTIVE = "Is industrial construction in the Phoenix metro increasing?"


class FakeStore:
    def __init__(self, activation_number=1, kind="founding_free", quota=500.0):
        self.activation_number = activation_number
        self.account = {"user_id": "u1", "email": "u@example.com", "activation_number": activation_number}
        self.entitlement = {
            "user_id": "u1", "kind": kind, "plan_id": kind, "active": kind != "paid_required",
            "quota_units_per_week": quota,
        }
        self.oauth_clients = {}
        self.oauth_codes = {}
        self.access_tokens = {}
        self.refresh_tokens = {}
        self.runs = {}
        self.discoveries = {}
        self.artifacts = {}
        self.actions = {}
        self.outcomes = {}
        self.improvements = {}
        self.usage = []
        self.reservations = {}
        self.billing_events = {}
        self.cases = {}
        self.case_charters = {}
        self.case_approvals = {}
        self.case_events = []
        self.verified_user = {"id": "u1", "email": "u@example.com"}

    def verify_supabase_user(self, token):
        if token != "supabase-session":
            raise RuntimeError("bad session")
        return self.verified_user

    def activate_account(self, user_id, email=None):
        return self.account

    def get_account(self, user_id):
        return self.account if user_id == "u1" else None

    def get_entitlement(self, user_id):
        return self.entitlement if user_id == "u1" else None

    def put_oauth_client(self, row):
        self.oauth_clients[row["client_id"]] = dict(row)

    def get_oauth_client(self, client_id):
        return self.oauth_clients.get(client_id)

    def put_oauth_code(self, row):
        self.oauth_codes[row["code_hash"]] = dict(row)

    def consume_oauth_code(self, digest):
        row = self.oauth_codes.pop(digest, None)
        # li_consume_oauth_code: a used code is refused.
        return row if row and not row.get("used_at") else None

    def put_access_token(self, row):
        self.access_tokens[row["token_hash"]] = dict(row)

    def get_access_token(self, digest):
        # SupabaseStore.get_access_token filters revoked_at is null.
        row = self.access_tokens.get(digest)
        return row if row and not row.get("revoked_at") else None

    def put_refresh_token(self, row):
        self.refresh_tokens[row["token_hash"]] = dict(row)

    def consume_refresh_token(self, digest):
        # li_consume_refresh_token: a used refresh token is refused.
        row = self.refresh_tokens.pop(digest, None)
        return row if row and not row.get("used_at") else None

    def save_run(self, row):
        self.runs[(row["user_id"], row["run_id"])] = dict(row)

    def get_run(self, user_id, run_id):
        return self.runs.get((user_id, run_id))

    def save_discovery(self, row):
        self.discoveries[(row["user_id"], row["discovery_id"])] = dict(row)

    def get_discovery(self, user_id, discovery_id):
        return self.discoveries.get((user_id, discovery_id))

    def list_discoveries(self, user_id, limit=1000):
        return [row for (uid, _), row in self.discoveries.items() if uid == user_id][:limit]

    def save_artifact(self, row):
        self.artifacts[(row["user_id"], row["artifact_id"])] = dict(row)

    def get_artifact(self, user_id, artifact_id):
        return self.artifacts.get((user_id, artifact_id))

    def list_artifacts(self, user_id, limit=1000):
        return [row for (uid, _), row in self.artifacts.items() if uid == user_id][:limit]

    def save_action(self, row):
        self.actions[(row["user_id"], row["action_id"])] = dict(row)

    def get_action(self, user_id, action_id):
        return self.actions.get((user_id, action_id))

    def list_actions(self, user_id, limit=1000):
        return [row for (uid, _), row in self.actions.items() if uid == user_id][:limit]

    def save_outcome(self, row):
        self.outcomes[(row["user_id"], row["outcome_id"])] = dict(row)

    def get_outcome(self, user_id, outcome_id):
        return self.outcomes.get((user_id, outcome_id))

    def list_outcomes(self, user_id, limit=1000):
        return [row for (uid, _), row in self.outcomes.items() if uid == user_id][:limit]

    def save_improvement(self, row):
        self.improvements[(row["user_id"], row["improvement_id"])] = dict(row)

    def get_improvement(self, user_id, improvement_id):
        return self.improvements.get((user_id, improvement_id))

    def list_improvements(self, user_id, limit=1000):
        return [row for (uid, _), row in self.improvements.items() if uid == user_id][:limit]

    def reserve_usage(self, reservation_id, user_id, operation, units):
        from datetime import timedelta
        now = self._job_now()
        # li_reserve_usage (li_research_jobs migration): a 'reserved' row older than one hour
        # expires unless a live job holds it (held_until in the future).
        for row in self.reservations.values():
            if (row["user_id"] == user_id and row["status"] == "reserved"
                    and row.get("created_at", now) < now - timedelta(hours=1)
                    and (row.get("held_until") is None or row["held_until"] < now)):
                row["status"] = "expired"
        quota = float(self.entitlement.get("quota_units_per_week") or 0)
        active = bool(self.entitlement.get("active"))
        used = sum(float(x.get("units") or 0) for x in self.usage if x["user_id"] == user_id)
        reserved = sum(self._held(x) for x in self.reservations.values() if x["user_id"] == user_id)
        if not active or used + reserved + float(units) > quota + 1e-9:
            return False
        self.reservations[reservation_id] = {
            "user_id": user_id, "operation": operation, "units": float(units), "status": "reserved",
            "created_at": now, "held_until": None, "job_id": None,
        }
        return True

    # ---- In-memory model of the li_research_jobs RPCs (li_research_jobs migration) ----------
    job_clock = None

    def _job_now(self):
        from datetime import datetime, timezone
        return (self.job_clock or (lambda: datetime.now(timezone.utc)))()

    @property
    def jobs(self):
        return self.__dict__.setdefault("_jobs", {})

    @property
    def _job_lock(self):
        import threading
        return self.__dict__.setdefault("_job_lock_obj", threading.RLock())

    @staticmethod
    def _job_out(row, **extra):
        from datetime import datetime
        out = {k: (v.isoformat() if isinstance(v, datetime) else v) for k, v in row.items()}
        out.update(extra)
        return json.loads(json.dumps(out))

    def _hold(self, job, until):
        res = self.reservations.get(job["reservation_id"])
        if res is not None:
            res["held_until"] = until

    def _job_account(self, job, reason):
        """li_research_job_account: release when no work was recorded, else an unsettled marker."""
        res = self.reservations.get(job["reservation_id"])
        if res is None:
            return
        cost = job["checkpoint"].get("known_cost_usd")
        cost = max(float(cost), 0.0) if isinstance(cost, (int, float)) and not isinstance(cost, bool) else 0.0
        if float(job["cost_so_far"]) > 0:
            if res["status"] in ("reserved", "expired", "released"):
                res.update({"status": "unsettled", "run_id": None, "pending_units": float(job["cost_so_far"]),
                            "pending_known_cost_usd": cost,
                            "pending_unpriced_components": ["partial_run", reason], "held_until": None})
        elif res["status"] == "reserved":
            res.update({"status": "released", "held_until": None})

    def _job_abandon_settlement(self, job):
        """li_research_job_abandon_settlement (finalize-cap migration): account a job whose settlement
        was abandoned. Open reservation: an unsettled marker with the checkpoint's units, cost and
        unpriced components (and the run for a saved result); settled or marked: unchanged."""
        res = self.reservations.get(job["reservation_id"])
        if res is None:
            return
        cp = job["checkpoint"]

        def number(key):
            v = cp.get(key)
            return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else 0.0

        saved = cp.get("phase") == "result_saved"
        units = max(float(job["cost_so_far"] or 0.0), number("units"), 0.0)
        cost = max(number("known_cost_usd"), 0.0)
        unpriced = list(cp["unpriced"]) if isinstance(cp.get("unpriced"), list) else []
        if not saved:
            unpriced.append("partial_run")
        unpriced.append("settlement_abandoned")
        if units > 0 or cost > 0:
            if res["status"] in ("reserved", "expired", "released"):
                res.update({"status": "unsettled", "run_id": job["result_run_id"] if saved else None,
                            "pending_units": units, "pending_known_cost_usd": cost,
                            "pending_unpriced_components": unpriced})
        elif res["status"] == "reserved":
            res["status"] = "released"
        res["held_until"] = None

    def enqueue_research_job(self, job_id, user_id, case_id, kind, idempotency_key, job_input,
                             reservation_id, max_attempts, queue_ttl_seconds):
        from datetime import timedelta
        with self._job_lock:
            if not idempotency_key or len(idempotency_key) > 200 or int(queue_ttl_seconds) <= 0:
                return None
            for row in self.jobs.values():
                if row["user_id"] == user_id and row["idempotency_key"] == idempotency_key:
                    return self._job_out(row, created=False)
            res = self.reservations.get(reservation_id)
            if (not res or res["user_id"] != user_id or res["status"] != "reserved"
                    or res.get("job_id") is not None):
                return None
            now = self._job_now()
            until = now + timedelta(seconds=int(queue_ttl_seconds))
            res.update({"held_until": until, "job_id": job_id})
            self.jobs[job_id] = {
                "id": job_id, "user_id": user_id, "case_id": case_id, "kind": kind,
                "idempotency_key": idempotency_key, "status": "queued", "input": json.loads(json.dumps(job_input)),
                "lease_owner": None, "lease_expires_at": None, "heartbeat_at": None, "attempts": 0,
                "max_attempts": max(1, min(int(max_attempts or 3), 10)), "checkpoint": {},
                "reservation_id": reservation_id, "result_run_id": None, "error_code": None, "cost_so_far": 0.0,
                "not_before": now, "queue_expires_at": until, "created_at": now, "updated_at": now,
                "started_at": None, "finished_at": None, "cancel_requested_at": None,
                "finalize_reclaims": 0,
            }
            return self._job_out(self.jobs[job_id], created=True)

    def claim_research_job(self, worker, lease_seconds, hold_grace_seconds):
        from datetime import timedelta
        with self._job_lock:
            now = self._job_now()
            ready = sorted((r for r in self.jobs.values() if r["status"] == "queued"
                            and r["not_before"] <= now and r["queue_expires_at"] > now),
                           key=lambda r: (r["not_before"], r["created_at"], r["id"]))
            if not ready or not worker or int(lease_seconds) <= 0:
                return None
            row = ready[0]
            row.update({"status": "running", "lease_owner": worker,
                        "lease_expires_at": now + timedelta(seconds=int(lease_seconds)), "heartbeat_at": now,
                        "attempts": row["attempts"] + 1, "started_at": row["started_at"] or now, "updated_at": now})
            self._hold(row, row["lease_expires_at"] + timedelta(seconds=max(int(hold_grace_seconds or 0), 0)))
            return self._job_out(row)

    def heartbeat_research_job(self, job_id, worker, lease_seconds, hold_grace_seconds,
                               checkpoint=None, cost_so_far=None):
        from datetime import timedelta
        with self._job_lock:
            now = self._job_now()
            row = self.jobs.get(job_id)
            if (not row or row["lease_owner"] != worker or row["status"] not in ("running", "cancel_requested")
                    or row["lease_expires_at"] is None or row["lease_expires_at"] <= now
                    or (cost_so_far is not None and float(cost_so_far) < 0)):
                return None
            row["lease_expires_at"] = now + timedelta(seconds=int(lease_seconds))
            row["heartbeat_at"] = now
            if checkpoint is not None:
                row["checkpoint"] = json.loads(json.dumps(checkpoint))
            if cost_so_far is not None:
                row["cost_so_far"] = max(float(row["cost_so_far"]), float(cost_so_far))
            row["updated_at"] = now
            self._hold(row, row["lease_expires_at"] + timedelta(seconds=max(int(hold_grace_seconds or 0), 0)))
            return self._job_out(row)

    def complete_research_job(self, job_id, worker, run_id, cost_so_far):
        with self._job_lock:
            row = self.jobs.get(job_id)
            if (not run_id or float(cost_so_far) < 0 or not row or row["lease_owner"] != worker
                    or row["status"] not in ("running", "cancel_requested")):
                return None
            now = self._job_now()
            row.update({"status": "succeeded", "result_run_id": run_id, "cost_so_far": float(cost_so_far),
                        "error_code": None, "lease_owner": None, "lease_expires_at": None,
                        "finished_at": now, "updated_at": now})
            self._hold(row, None)
            return self._job_out(row)

    def fail_research_job(self, job_id, worker, error_code, retryable, backoff_seconds, cost_so_far,
                          checkpoint, queue_ttl_seconds):
        from datetime import timedelta
        with self._job_lock:
            row = self.jobs.get(job_id)
            if (not error_code or len(error_code) > 64 or float(cost_so_far) < 0 or not row
                    or row["lease_owner"] != worker or row["status"] not in ("running", "cancel_requested")):
                return None
            now = self._job_now()
            cost = max(float(row["cost_so_far"]), float(cost_so_far))
            if (retryable and error_code != "CANCELLED" and row["status"] == "running"
                    and (row["attempts"] < row["max_attempts"] or error_code == "WORKER_SHUTDOWN")):
                backoff = timedelta(seconds=max(int(backoff_seconds or 0), 0))
                until = now + backoff + timedelta(seconds=max(int(queue_ttl_seconds or 86400), 1))
                row.update({"status": "queued", "lease_owner": None, "lease_expires_at": None,
                            "attempts": max(row["attempts"] - 1, 0) if error_code == "WORKER_SHUTDOWN"
                            else row["attempts"],
                            "error_code": error_code, "cost_so_far": cost,
                            "checkpoint": json.loads(json.dumps(checkpoint)) if checkpoint is not None
                            else row["checkpoint"],
                            "not_before": now + backoff, "queue_expires_at": until, "updated_at": now})
                self._hold(row, until)
                return self._job_out(row)
            row.update({"status": "cancelled" if error_code == "CANCELLED" else "failed",
                        "lease_owner": None, "lease_expires_at": None, "error_code": error_code,
                        "cost_so_far": cost,
                        "checkpoint": json.loads(json.dumps(checkpoint)) if checkpoint is not None
                        else row["checkpoint"],
                        "finished_at": now, "updated_at": now})
            self._job_account(row, error_code.lower())
            self._hold(row, None)
            return self._job_out(row)

    def request_cancel_research_job(self, job_id, user_id):
        with self._job_lock:
            row = self.jobs.get(job_id)
            if not row or row["user_id"] != user_id:
                return None
            now = self._job_now()
            if row["status"] == "queued":
                row.update({"status": "cancelled", "error_code": "CANCELLED", "cancel_requested_at": now,
                            "finished_at": now, "updated_at": now})
                self._job_account(row, "cancelled")
            elif row["status"] == "running":
                row.update({"status": "cancel_requested", "cancel_requested_at": now, "updated_at": now})
            return self._job_out(row)

    def reclaim_research_jobs(self, limit, queue_ttl_seconds, max_finalize_reclaims=3):
        from datetime import timedelta
        with self._job_lock:
            now = self._job_now()
            until = now + timedelta(seconds=max(int(queue_ttl_seconds or 86400), 1))
            cap = min(max(int(3 if max_finalize_reclaims is None else max_finalize_reclaims), 1), 10)
            ids = []
            expired = sorted((r for r in self.jobs.values() if r["status"] in ("running", "cancel_requested")
                              and r["lease_expires_at"] is not None and r["lease_expires_at"] <= now),
                             key=lambda r: (r["lease_expires_at"], r["id"]))[:max(int(limit or 100), 1)]
            for row in expired:
                finishing = row["checkpoint"].get("phase") in ("result_saved", "finalizing")
                if finishing and row["finalize_reclaims"] >= cap:
                    run_id = row["checkpoint"].get("run_id")
                    if row["checkpoint"].get("phase") == "result_saved" and run_id:
                        row["result_run_id"] = str(run_id)
                    row.update({"status": "failed", "error_code": "SETTLEMENT_ABANDONED",
                                "finalize_reclaims": row["finalize_reclaims"] + 1,
                                "lease_owner": None, "lease_expires_at": None, "finished_at": now, "updated_at": now})
                    self._job_abandon_settlement(row)
                elif not finishing and (row["status"] == "cancel_requested"
                                        or row["attempts"] >= row["max_attempts"]):
                    cancelled = row["status"] == "cancel_requested"
                    row.update({"status": "cancelled" if cancelled else "failed",
                                "error_code": "CANCELLED" if cancelled else "LEASE_EXPIRED",
                                "lease_owner": None, "lease_expires_at": None, "finished_at": now, "updated_at": now})
                    self._job_account(row, row["error_code"].lower())
                else:
                    row.update({"status": "queued", "lease_owner": None, "lease_expires_at": None,
                                "finalize_reclaims": row["finalize_reclaims"] + (1 if finishing else 0),
                                "not_before": now, "queue_expires_at": until, "updated_at": now})
                    self._hold(row, until)
                ids.append(row["id"])
            stale = sorted((r for r in self.jobs.values() if r["status"] == "queued" and r["queue_expires_at"] <= now),
                           key=lambda r: (r["queue_expires_at"], r["id"]))[:max(int(limit or 100), 1)]
            for row in stale:
                row.update({"status": "failed", "error_code": "QUEUE_EXPIRED", "finished_at": now, "updated_at": now})
                self._job_account(row, "queue_expired")
                ids.append(row["id"])
            return ids

    def get_research_job(self, user_id, job_id):
        row = self.jobs.get(job_id)
        return self._job_out(row) if row and row["user_id"] == user_id else None

    def get_research_job_by_key(self, user_id, idempotency_key):
        for row in self.jobs.values():
            if row["user_id"] == user_id and row["idempotency_key"] == idempotency_key:
                return self._job_out(row)
        return None

    def list_research_jobs(self, user_id, limit=1000):
        return [self._job_out(r) for r in self.jobs.values() if r["user_id"] == user_id][:limit]

    def finalize_usage(self, reservation_id, run_id, actual_units, known_cost_usd, unpriced_components):
        row = self.reservations.get(reservation_id)
        if not row or row["status"] != "reserved":
            return False
        quota = float(self.entitlement.get("quota_units_per_week") or 0)
        used = sum(float(x.get("units") or 0) for x in self.usage if x["user_id"] == row["user_id"])
        other = sum(self._held(x) for rid, x in self.reservations.items()
                    if rid != reservation_id and x["user_id"] == row["user_id"])
        if used + other + float(actual_units) > quota + 1e-9:
            return False
        self.usage.append({
            "id": reservation_id,
            "user_id": row["user_id"],
            "run_id": run_id,
            "operation": row["operation"],
            "units": float(actual_units),
            "known_cost_usd": float(known_cost_usd),
            "unpriced_components": list(unpriced_components),
        })
        row["status"] = "settled"
        return True

    def release_usage(self, reservation_id):
        row = self.reservations.get(reservation_id)
        if not row or row["status"] != "reserved":
            return False
        row["status"] = "released"
        return True

    @staticmethod
    def _held(row):
        if row["status"] == "reserved":
            return float(row["units"])
        if row["status"] == "unsettled":
            return float(row.get("pending_units", row["units"]))
        return 0.0

    # In-memory model of li_mark_usage_unsettled / li_settle_usage (usage_settlement migration).
    def mark_usage_unsettled(self, reservation_id, run_id, actual_units, known_cost_usd, unpriced_components):
        row = self.reservations.get(reservation_id)
        if not row or row["status"] not in ("reserved", "expired", "released", "unsettled"):
            return False
        if float(actual_units) < 0 or float(known_cost_usd) < 0:
            return False
        row.update({
            "status": "unsettled", "run_id": run_id, "pending_units": float(actual_units),
            "pending_known_cost_usd": float(known_cost_usd),
            "pending_unpriced_components": list(unpriced_components),
        })
        return True

    def settle_usage(self, reservation_id):
        row = self.reservations.get(reservation_id)
        if not row:
            return "missing"
        if row["status"] == "settled":
            return "already_settled"
        if row["status"] != "unsettled":
            return "not_unsettled"
        if not any(e.get("id") == reservation_id for e in self.usage):
            self.usage.append({
                "id": reservation_id,
                "user_id": row["user_id"],
                "run_id": row.get("run_id"),
                "operation": row["operation"],
                "units": float(row["pending_units"]),
                "known_cost_usd": float(row.get("pending_known_cost_usd") or 0.0),
                "unpriced_components": list(row.get("pending_unpriced_components") or []),
            })
        row["status"] = "settled"
        return "settled"

    def list_unsettled_usage(self, limit=100):
        return [{"id": rid, **row} for rid, row in self.reservations.items()
                if row["status"] == "unsettled"][:limit]

    # In-memory model of the li_cases / li_case_charters / li_case_approvals RPCs
    # (intelligence_cases migration). Rows go through JSON as a database would.
    case_clock = None

    def _case_now(self):
        from datetime import datetime, timezone
        return (self.case_clock or (lambda: datetime.now(timezone.utc)))()

    @staticmethod
    def _json(value):
        return json.loads(json.dumps(value))

    def _event(self, case_id, user_id, kind, version=None, **detail):
        self.case_events.append({"case_id": case_id, "user_id": user_id, "kind": kind,
                                 "charter_version": version, "detail": self._json(detail)})

    def _latest_version(self, case_id):
        versions = [v for (cid, v) in self.case_charters if cid == case_id]
        return max(versions) if versions else None

    def create_case(self, case_id, user_id, objective, status, content_hash, charter, answers):
        if case_id in self.cases:
            raise RuntimeError("duplicate case id")
        now = self._case_now().isoformat()
        self.cases[case_id] = {"id": case_id, "user_id": user_id, "objective": objective, "status": status,
                               "created_at": now, "updated_at": now}
        self.case_charters[(case_id, 1)] = {
            "case_id": case_id, "user_id": user_id, "version": 1, "content_hash": content_hash,
            "charter": self._json(charter), "answers": self._json(answers), "created_at": now,
        }
        self._event(case_id, user_id, "case_created", 1, content_hash=content_hash)
        return 1

    def revise_case(self, case_id, user_id, expected_version, status, content_hash, charter, answers):
        case = self.cases.get(case_id)
        if not case or case["user_id"] != user_id:
            return 0
        latest = self._latest_version(case_id)
        if latest is None or latest != int(expected_version):
            self._event(case_id, user_id, "revision_conflict", latest, expected_version=expected_version)
            return -1
        version = latest + 1
        if (case_id, version) in self.case_charters:  # unique (case_id, version)
            return -1
        self.case_charters[(case_id, version)] = {
            "case_id": case_id, "user_id": user_id, "version": version, "content_hash": content_hash,
            "charter": self._json(charter), "answers": self._json(answers),
            "created_at": self._case_now().isoformat(),
        }
        for row in self.case_approvals.values():
            if row["case_id"] == case_id and row["consumed_at"] is None and row["revoked_at"] is None:
                row["revoked_at"] = self._case_now().isoformat()
        case.update({"status": status, "updated_at": self._case_now().isoformat()})
        self._event(case_id, user_id, "charter_revised", version, content_hash=content_hash)
        return version

    def get_case(self, user_id, case_id):
        row = self.cases.get(case_id)
        return dict(row) if row and row["user_id"] == user_id else None

    def list_cases(self, user_id, limit=1000):
        return [dict(r) for r in self.cases.values() if r["user_id"] == user_id][:limit]

    def get_case_charter(self, user_id, case_id, version=None):
        if version is None:
            version = self._latest_version(case_id)
        row = self.case_charters.get((case_id, version))
        return self._json(row) if row and row["user_id"] == user_id else None

    def approve_case_charter(self, row):
        from datetime import datetime
        case = self.cases.get(row["case_id"])
        if not case or case["user_id"] != row["user_id"]:
            return False
        latest = self._latest_version(row["case_id"])
        current = self.case_charters.get((row["case_id"], latest)) if latest else None
        if (current is None or latest != int(row["charter_version"])
                or current["content_hash"] != row["content_hash"]
                or datetime.fromisoformat(row["expires_at"]) <= self._case_now()):
            return False
        for other in self.case_approvals.values():
            if other["case_id"] == row["case_id"] and other["consumed_at"] is None and other["revoked_at"] is None:
                other["revoked_at"] = self._case_now().isoformat()
        stored = self._json(row)
        stored.update({"approved_at": self._case_now().isoformat(), "consumed_at": None, "revoked_at": None,
                       "idempotency_key": None, "run_id": None, "run_status": None})
        self.case_approvals[row["id"]] = stored
        case["status"] = "approved"
        self._event(row["case_id"], row["user_id"], "charter_approved", row["charter_version"],
                    approval_id=row["id"])
        return True

    def latest_case_approval(self, user_id, case_id):
        rows = [r for r in self.case_approvals.values() if r["case_id"] == case_id and r["user_id"] == user_id]
        return dict(rows[-1]) if rows else None

    def consume_case_approval(self, approval_id, user_id, token_hash, idempotency_key):
        from datetime import datetime
        row = self.case_approvals.get(approval_id)
        if (not row or row["user_id"] != user_id or row["token_hash"] != token_hash
                or not idempotency_key or len(idempotency_key) > 200):
            return None
        if row["consumed_at"] is not None:
            if row["idempotency_key"] == idempotency_key:
                return {**row, "consumed_now": False}
            return None
        if (row["revoked_at"] is not None
                or datetime.fromisoformat(row["expires_at"]) <= self._case_now()
                or self._latest_version(row["case_id"]) != row["charter_version"]):
            return None
        row.update({"consumed_at": self._case_now().isoformat(), "idempotency_key": idempotency_key,
                    "run_status": "started"})
        self.cases[row["case_id"]]["status"] = "research_started"
        self._event(row["case_id"], user_id, "approval_consumed", row["charter_version"],
                    approval_id=approval_id, idempotency_key=idempotency_key)
        return {**row, "consumed_now": True}

    def record_case_run(self, approval_id, user_id, run_id, run_status):
        row = self.case_approvals.get(approval_id)
        if (run_status not in ("complete", "failed") or not row or row["user_id"] != user_id
                or row["consumed_at"] is None or row["run_status"] != "started"):
            return False
        row.update({"run_id": run_id, "run_status": run_status})
        self.cases[row["case_id"]]["status"] = (
            "research_complete" if run_status == "complete" else "research_failed")
        self._event(row["case_id"], user_id, "research_" + run_status, approval_id=approval_id, run_id=run_id)
        return True

    def add_case_event(self, row):
        self.case_events.append(dict(row))

    def list_case_events(self, user_id, case_id):
        return [e for e in self.case_events if e["case_id"] == case_id and e["user_id"] == user_id]

    def take_rate_limit(self, user_id, bucket="mcp", limit=60, window_seconds=60):
        return True

    # In-memory model of li_take_keyed_rate_limit (keyed sliding window, 0 = allowed).
    rate_limit_error = None
    rate_clock = None

    def take_keyed_rate_limit(self, bucket, key_hash, limit, window_seconds):
        import math
        import time
        if self.rate_limit_error is not None:
            raise self.rate_limit_error
        events = self.__dict__.setdefault("rate_events", {})
        now = (self.rate_clock or time.monotonic)()
        if limit <= 0 or window_seconds <= 0:
            return max(1, window_seconds)
        hits = [t for t in events.get((bucket, key_hash), []) if t > now - window_seconds]
        if len(hits) >= limit:
            events[(bucket, key_hash)] = hits
            return max(1, math.ceil(hits[0] + window_seconds - now))
        hits.append(now)
        events[(bucket, key_hash)] = hits
        return 0

    def list_runs(self, user_id, limit=1000):
        return [row for (uid, _), row in self.runs.items() if uid == user_id][:limit]

    def list_usage(self, user_id, limit=5000):
        return [row for row in self.usage if row["user_id"] == user_id][:limit]

    # In-memory model of the account-deletion store calls (SupabaseStore).
    def revoke_user_credentials(self, user_id):
        for table, column in ((self.access_tokens, "revoked_at"), (self.refresh_tokens, "used_at"),
                              (self.oauth_codes, "used_at")):
            for row in table.values():
                if row.get("user_id") == user_id and not row.get(column):
                    row[column] = "revoked"

    def close_entitlement(self, user_id):
        if user_id == "u1" and self.entitlement is not None:
            self.entitlement["active"] = False

    def list_open_usage_reservations(self, user_id):
        return [{"id": rid, **row} for rid, row in self.reservations.items()
                if row["user_id"] == user_id and row["status"] in ("reserved", "unsettled")]

    def delete_auth_user(self, user_id):
        """auth.users deletion and its ON DELETE CASCADE over every li_* table keyed by user_id."""
        if getattr(self, "deleted_user", None) == user_id:
            return False
        self.deleted_user = user_id
        if user_id == "u1":
            self.account = None
            self.entitlement = None
        for name in ("oauth_codes", "access_tokens", "refresh_tokens", "reservations", "cases",
                     "case_approvals"):
            table = getattr(self, name)
            setattr(self, name, {k: v for k, v in table.items() if v.get("user_id") != user_id})
        self.case_charters = {k: v for k, v in self.case_charters.items() if v.get("user_id") != user_id}
        self.case_events = [e for e in self.case_events if e.get("user_id") != user_id]
        jobs = self.jobs
        for job_id in [k for k, v in jobs.items() if v["user_id"] == user_id]:
            del jobs[job_id]
        self.runs = {k: v for k, v in self.runs.items() if k[0] != user_id}
        self.discoveries = {k: v for k, v in self.discoveries.items() if k[0] != user_id}
        self.artifacts = {k: v for k, v in self.artifacts.items() if k[0] != user_id}
        self.actions = {k: v for k, v in self.actions.items() if k[0] != user_id}
        self.outcomes = {k: v for k, v in self.outcomes.items() if k[0] != user_id}
        self.improvements = {k: v for k, v in self.improvements.items() if k[0] != user_id}
        self.usage = [row for row in self.usage if row["user_id"] != user_id]
        return True

    def record_usage(self, row):
        self.usage.append(dict(row))

    def usage_units_since(self, user_id, since_iso):
        return sum(float(x.get("units") or 0) for x in self.usage if x["user_id"] == user_id)

    def stripe_event_seen(self, event_id):
        return event_id in self.billing_events

    def record_stripe_event(self, row):
        self.billing_events[row["stripe_event_id"]] = dict(row)

    def get_entitlement_by_subscription(self, subscription_id):
        if self.entitlement and self.entitlement.get("stripe_subscription_id") == subscription_id:
            return self.entitlement
        return None

    def apply_stripe_entitlement_event(
        self, *, event_id, event_type, payload_hash, user_id, customer_id,
        subscription_id, active, plan_id, quota_units_per_week
    ):
        if event_id in self.billing_events:
            return False
        self.billing_events[event_id] = {
            "stripe_event_id": event_id,
            "event_type": event_type,
            "payload_hash": payload_hash,
        }
        if user_id is not None:
            self.entitlement = {
                **self.entitlement,
                "user_id": user_id,
                "kind": "paid" if active else "paid_required",
                "active": active,
                "plan_id": plan_id,
                "quota_units_per_week": quota_units_per_week if active else 0,
                "stripe_customer_id": customer_id or self.entitlement.get("stripe_customer_id"),
                "stripe_subscription_id": subscription_id or self.entitlement.get("stripe_subscription_id"),
            }
        return True

    def set_paid_entitlement(self, user_id, **kwargs):
        self.entitlement = {
            **self.entitlement,
            "user_id": user_id,
            "kind": "paid" if kwargs["active"] else "paid_required",
            "active": kwargs["active"],
            "plan_id": kwargs["plan_id"],
            "quota_units_per_week": kwargs["quota_units_per_week"],
            "stripe_customer_id": kwargs["customer_id"],
            "stripe_subscription_id": kwargs["subscription_id"],
        }

    def cost_samples(self, limit=5000):
        return []


class OAuthTests(unittest.TestCase):
    def test_dynamic_registration_pkce_exchange_and_refresh_rotation(self):
        store = FakeStore()
        oauth = OAuthService(store)
        client = oauth.register_client({
            "client_name": "Test MCP",
            "redirect_uris": ["http://127.0.0.1/callback"],
            "token_endpoint_auth_method": "none",
        })
        verifier = "a" * 64
        with patch.dict(os.environ, {"LI_PUBLIC_BASE_URL": "https://li.example"}, clear=False):
            code = oauth.authorize_from_supabase_session(
                supabase_access_token="supabase-session",
                client_id=client["client_id"],
                redirect_uri="http://127.0.0.1/callback",
                code_challenge=code_challenge_s256(verifier),
                scope="mcp",
                resource="https://li.example/mcp",
            )
        tokens = oauth.exchange_code(
            code=code,
            code_verifier=verifier,
            client_id=client["client_id"],
            redirect_uri="http://127.0.0.1/callback",
        )
        self.assertTrue(tokens["access_token"].startswith("lit_"))
        self.assertTrue(tokens["refresh_token"].startswith("lir_"))
        principal = oauth.authenticate(tokens["access_token"])
        self.assertEqual(principal.user_id, "u1")
        refreshed = oauth.refresh(refresh_token=tokens["refresh_token"], client_id=client["client_id"])
        self.assertNotEqual(refreshed["access_token"], tokens["access_token"])
        with self.assertRaises(Exception):
            oauth.refresh(refresh_token=tokens["refresh_token"], client_id=client["client_id"])

    def test_redirect_must_be_registered(self):
        store = FakeStore()
        oauth = OAuthService(store)
        client = oauth.register_client({"redirect_uris": ["https://client.example/callback"]})
        with self.assertRaises(Exception):
            oauth.authorize_from_supabase_session(
                supabase_access_token="supabase-session",
                client_id=client["client_id"],
                redirect_uri="https://evil.example/callback",
                code_challenge=code_challenge_s256("b" * 64),
                scope="mcp",
                resource="https://li.example/mcp",
            )


class HostedRunTests(unittest.TestCase):
    def test_durable_run_survives_service_instance(self):
        store = FakeStore()
        with patch.dict(os.environ, {
            "LOFGREN_PROVIDER": "heuristic",
            "LI_INFRA_USD_PER_RUN": "0",
            "LI_RETRIEVAL_USD_PER_CALL": "0",
        }, clear=False):
            first = PublicService(store)
            out = first.investigate("u1", {"objective": OBJECTIVE, "texts": TEXTS})
            self.assertTrue(out["run_id"].startswith("RR-"))
            self.assertIn(("u1", out["run_id"]), store.runs)
            second = PublicService(store)
            receipt = second.get_receipt("u1", {"run_id": out["run_id"]})
            self.assertTrue(receipt["intact"])
            report = second.render_report("u1", {"run_id": out["run_id"]})
            self.assertIn("Lofgren Intelligence report", report["report"])
            self.assertGreater(store.usage[0]["units"], 0)

    def test_paid_required_account_cannot_run(self):
        store = FakeStore(activation_number=1001, kind="paid_required", quota=0)
        service = PublicService(store)
        with self.assertRaises(PaymentRequired):
            service.investigate("u1", {"objective": OBJECTIVE, "texts": TEXTS})

    def test_same_research_id_is_tenant_scoped(self):
        store = FakeStore()
        row = {"run_id": "RR-SAME", "user_id": "u1", "snapshot": {"owner": "u1"}}
        store.save_run(row)
        store.save_run({"run_id": "RR-SAME", "user_id": "u2", "snapshot": {"owner": "u2"}})
        self.assertEqual(store.get_run("u1", "RR-SAME")["snapshot"]["owner"], "u1")
        self.assertEqual(store.get_run("u2", "RR-SAME")["snapshot"]["owner"], "u2")


    def test_account_export_is_tenant_scoped(self):
        store = FakeStore()
        store.save_run({"user_id": "u1", "run_id": "RR-1", "snapshot": {"owner": "u1"}})
        store.save_run({"user_id": "u2", "run_id": "RR-2", "snapshot": {"owner": "u2"}})
        service = PublicService(store)
        exported = service.export_account_data("u1")
        self.assertEqual([x["run_id"] for x in exported["runs"]], ["RR-1"])

    def test_account_delete_requires_exact_phrase_and_cancels_paid_subscription_first(self):
        store = FakeStore(activation_number=1001, kind="paid", quota=2000)
        store.entitlement.update({"stripe_subscription_id": "sub_test", "stripe_customer_id": "cus_test"})
        service = PublicService(store)
        with self.assertRaises(Exception):
            service.delete_account("u1", "DELETE")
        with patch("lofgren_intelligence.hosted.service.cancel_subscription") as cancel:
            out = service.delete_account("u1", "DELETE MY LOFGREN INTELLIGENCE ACCOUNT")
        cancel.assert_called_once_with("sub_test")
        self.assertTrue(out["deleted"])
        self.assertEqual(store.deleted_user, "u1")


class HostedDiscoveryTests(unittest.TestCase):
    def _research(self, store):
        with patch.dict(os.environ, {
            "LOFGREN_PROVIDER": "heuristic",
            "LI_INFRA_USD_PER_RUN": "0",
            "LI_RETRIEVAL_USD_PER_CALL": "0",
            "LI_PUBLIC_MAX_DISCOVERY_UNITS": "100",
        }, clear=False):
            return PublicService(store).investigate("u1", {"objective": OBJECTIVE, "texts": TEXTS})

    def test_v2_discovery_is_durable_across_service_instances(self):
        store = FakeStore(quota=1000)
        run = self._research(store)
        first = PublicService(store)
        out = first.discover("u1", {
            "run_id": run["run_id"],
            "objective": "Choose a fictional warehouse size",
            "design": warehouse_design(),
        })
        self.assertEqual(out["kind"], "discovery")
        self.assertTrue(out["discovery_id"].startswith("DR-"))
        second = PublicService(store)
        receipt = second.get_discovery_receipt("u1", {"discovery_id": out["discovery_id"]})
        self.assertTrue(receipt["intact"])
        report = second.render_discovery_report("u1", {"discovery_id": out["discovery_id"]})
        self.assertIn("Discovery", report["report"])
        verification = second.verify_discovery("u1", {"discovery_id": out["discovery_id"]})
        self.assertTrue(verification["receipt_intact"])

    def test_same_discovery_id_is_tenant_scoped(self):
        store = FakeStore()
        row = {"user_id": "u1", "discovery_id": "DR-SAME", "snapshot": {"owner": "u1"}}
        store.save_discovery(row)
        store.save_discovery({"user_id": "u2", "discovery_id": "DR-SAME", "snapshot": {"owner": "u2"}})
        self.assertEqual(store.get_discovery("u1", "DR-SAME")["snapshot"]["owner"], "u1")
        self.assertEqual(store.get_discovery("u2", "DR-SAME")["snapshot"]["owner"], "u2")

    def test_account_export_contains_only_callers_discoveries(self):
        store = FakeStore()
        store.save_discovery({"user_id": "u1", "discovery_id": "DR-1", "snapshot": {}})
        store.save_discovery({"user_id": "u2", "discovery_id": "DR-2", "snapshot": {}})
        exported = PublicService(store).export_account_data("u1")
        self.assertEqual([x["discovery_id"] for x in exported["discoveries"]], ["DR-1"])


class HostedLifecycleTests(HostedDiscoveryTests):
    def test_v3_artifact_is_durable_and_tenant_scoped(self):
        store = FakeStore(quota=5000)
        run = self._research(store)
        discovery = PublicService(store).discover("u1", {
            "run_id": run["run_id"],
            "objective": "Choose a fictional warehouse size",
            "design": warehouse_design(),
        })
        built = PublicService(store).build_artifact("u1", {
            "discovery_id": discovery["discovery_id"],
            "kind": "structured_bundle",
        })
        self.assertTrue(built["verified"])
        second = PublicService(store).get_artifact("u1", {"artifact_id": built["artifact_id"]})
        self.assertTrue(second["verified"])
        self.assertTrue(second["receipt_intact"])
        row = store.get_artifact("u1", built["artifact_id"])
        store.save_artifact({**row, "user_id": "u2"})
        self.assertIsNotNone(store.get_artifact("u1", built["artifact_id"]))
        self.assertIsNotNone(store.get_artifact("u2", built["artifact_id"]))

    def test_stored_then_reloaded_discovery_still_builds_an_artifact(self):
        # A durable store hands back JSON, never the in-memory discovery: round-trip every row through JSON
        # and build from a fresh service instance. The V3 upstream check must still pass untouched.
        store = FakeStore(quota=5000)
        run = self._research(store)
        discovery = PublicService(store).discover("u1", {
            "run_id": run["run_id"],
            "objective": "Choose a fictional warehouse size",
            "design": warehouse_design(),
        })
        for key, row in list(store.discoveries.items()):
            store.discoveries[key] = json.loads(json.dumps(row))
        for key, row in list(store.runs.items()):
            store.runs[key] = json.loads(json.dumps(row))
        snap = store.get_discovery("u1", discovery["discovery_id"])["snapshot"]
        self.assertEqual({x["data"]["id"] for x in snap["context_objects"]}, set(snap["receipt"]["objects"]))
        built = PublicService(store).build_artifact("u1", {
            "discovery_id": discovery["discovery_id"],
            "kind": "structured_bundle",
        })
        self.assertTrue(built["verified"])

    def test_tampered_or_incomplete_stored_discovery_is_refused(self):
        store = FakeStore(quota=5000)
        run = self._research(store)
        discovery = PublicService(store).discover("u1", {
            "run_id": run["run_id"],
            "objective": "Choose a fictional warehouse size",
            "design": warehouse_design(),
        })
        original = json.loads(json.dumps(store.get_discovery("u1", discovery["discovery_id"])))
        args = {"discovery_id": discovery["discovery_id"]}

        tampered = json.loads(json.dumps(original))
        assumption = next(x for x in tampered["snapshot"]["context_objects"] if x["type"] == "Assumption")
        assumption["data"]["value"] = float(assumption["data"]["value"]) * 2 + 1
        store.save_discovery(tampered)
        with self.assertRaises(DiscoveryStateInvalid):
            PublicService(store).build_artifact("u1", args)

        dropped = json.loads(json.dumps(original))
        dropped["snapshot"]["context_objects"] = [
            x for x in dropped["snapshot"]["context_objects"] if x["type"] != "Simulation"
        ]
        store.save_discovery(dropped)
        with self.assertRaises(DiscoveryStateInvalid):
            PublicService(store).build_artifact("u1", args)

        missing = json.loads(json.dumps(original))
        del missing["snapshot"]["context_objects"]
        store.save_discovery(missing)
        with self.assertRaises(DiscoveryStateInvalid):
            PublicService(store).build_artifact("u1", args)
        self.assertEqual(store.artifacts, {})

    def test_v4_action_requires_browser_approval_before_execution(self):
        store = FakeStore(quota=5000)
        run = self._research(store)
        discovery = PublicService(store).discover("u1", {
            "run_id": run["run_id"],
            "objective": "Choose a fictional warehouse size",
            "design": warehouse_design(),
        })
        built = PublicService(store).build_artifact("u1", {
            "discovery_id": discovery["discovery_id"],
            "kind": "structured_bundle",
        })
        service = PublicService(store)
        with patch("lofgren_intelligence.hosted.service.HTTPSWebhookAdapter.preflight", return_value={"ready": True}):
            proposed = service.propose_action("u1", {
                "artifact_id": built["artifact_id"],
                "target": "https://example.com/hook",
                "payload": {"hello": "world"},
                "cost_usd": 0,
            }, "https://li.example")
        self.assertEqual(proposed["status"], "awaiting_human_approval")
        with self.assertRaises(Exception):
            service.execute_action("u1", {"action_id": proposed["action_id"]})
        approved = service.approve_action("u1", proposed["action_id"])
        self.assertEqual(approved["status"], "approved")
        status = service.action_status("u1", {"action_id": proposed["action_id"]})
        self.assertEqual(status["status"], "approved")

    def test_v5_and_v6_are_durable_after_verified_action(self):
        from lofgren_intelligence.execution.certification import NOW, _base as v4_base
        from lofgren_intelligence.execution.core import InMemoryAdapter, execute_authorized

        store = FakeStore(quota=5000)
        product, request, grant, approval = v4_base()
        executed = execute_authorized(
            product.v4_handoff, request, grant, approval, InMemoryAdapter(),
            subject="user-cert", now=NOW,
        )
        self.assertIsNotNone(executed.v5_handoff)
        store.save_action({
            "user_id": "u1",
            "action_id": request.action_id,
            "artifact_id": request.artifact_id,
            "request": {},
            "status": "executed",
            "grant_record": None,
            "approval_record": None,
            "receipt": executed.receipt,
            "v5_handoff": executed.v5_handoff,
            "created_at": NOW.isoformat(),
            "approved_at": NOW.isoformat(),
            "executed_at": NOW.isoformat(),
        })
        measurements = [
            {
                "metric": item["metric"],
                "value": item["mean"],
                "unit": item.get("unit", ""),
                "observed_at": "2026-11-04T12:00:00+00:00",
                "source": "hosted-test",
                "observation_id": f"OBS-{i}",
            }
            for i, item in enumerate(executed.v5_handoff["expected_outcomes"], 1)
        ]
        service = PublicService(store)
        measured = service.measure_outcome("u1", {
            "action_id": request.action_id,
            "measurements": measurements,
        })
        self.assertTrue(measured["receipt_intact"])
        self.assertTrue(measured["v6_improvement_allowed"])
        outcome = service.get_outcome("u1", {"outcome_id": measured["outcome_id"]})
        self.assertTrue(outcome["receipt_intact"])

        improved = service.evaluate_improvement("u1", {
            "outcome_id": measured["outcome_id"],
            "proposal": {
                "proposal_id": "IMP-hosted",
                "baseline_id": "BASE-1",
                "candidate_id": "CAND-2",
                "change_summary": "Improve routing threshold.",
                "evaluation_dataset": {
                    "dataset_id": "DS-heldout",
                    "content_hash": "sha256:" + "a" * 64,
                    "sample_count": 100,
                    "held_out": True,
                },
                "primary_metric": {
                    "metric": "task_success",
                    "baseline": 0.75,
                    "candidate": 0.82,
                    "direction": "higher_is_better",
                },
                "min_gain": 0.03,
                "safety_constraints": [
                    {
                        "metric": "evidence_integrity",
                        "baseline": 0.99,
                        "candidate": 0.99,
                        "max_regression": 0.01,
                    }
                ],
            },
        })
        self.assertTrue(improved["receipt_intact"])
        self.assertTrue(improved["human_review_required"])
        self.assertFalse(improved["mutation_performed"])
        recovered = PublicService(store).get_improvement("u1", {"improvement_id": improved["improvement_id"]})
        self.assertTrue(recovered["receipt_intact"])


class QuotaReservationTests(unittest.TestCase):
    def test_inflight_reservation_prevents_concurrent_oversubscription(self):
        store = FakeStore(quota=500)
        self.assertTrue(store.reserve_usage("r1", "u1", "research", 300))
        self.assertFalse(store.reserve_usage("r2", "u1", "research", 300))
        self.assertTrue(store.finalize_usage("r1", None, 250, 0, []))
        self.assertTrue(store.reserve_usage("r3", "u1", "research", 250))
        self.assertFalse(store.reserve_usage("r4", "u1", "research", 1))

    def test_release_returns_reserved_capacity(self):
        store = FakeStore(quota=100)
        self.assertTrue(store.reserve_usage("r1", "u1", "research", 100))
        self.assertFalse(store.reserve_usage("r2", "u1", "research", 1))
        self.assertTrue(store.release_usage("r1"))
        self.assertTrue(store.reserve_usage("r3", "u1", "research", 100))


class PublicSecurityTests(unittest.TestCase):
    def test_remote_rejects_server_file_paths(self):
        with self.assertRaises(PublicInputError):
            validate_remote_args({"objective": "x", "files": ["/etc/passwd"]})

    def test_ssrf_local_targets_are_refused(self):
        for url in ("http://127.0.0.1/x", "http://169.254.169.254/latest/meta-data", "http://[::1]/"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                validate_remote_args({"objective": "x", "urls": [url]})


class StripeWebhookTests(unittest.TestCase):
    def test_signature_and_idempotent_entitlement(self):
        store = FakeStore(activation_number=1001, kind="paid_required", quota=0)
        event = {
            "id": "evt_test",
            "type": "checkout.session.completed",
            "data": {"object": {
                "id": "cs_test", "customer": "cus_test", "subscription": "sub_test",
                "client_reference_id": "u1", "metadata": {"li_user_id": "u1"}, "payment_status": "paid",
            }},
        }
        raw = json.dumps(event, separators=(",", ":")).encode()
        with patch.dict(os.environ, {"STRIPE_WEBHOOK_SECRET": "whsec_test", **STRIPE_TEST_ENV}, clear=False):
            ts = 1_800_000_000
            sig = hmac.new(b"whsec_test", str(ts).encode() + b"." + raw, hashlib.sha256).hexdigest()
            parsed = verify_webhook(raw, f"t={ts},v1={sig}", now_s=ts)
            catalog = open_researcher_catalog()
            self.assertEqual(
                apply_webhook(store, parsed, subscription_status=active_researcher, catalog=catalog),
                "processed",
            )
            self.assertEqual(store.entitlement["kind"], "paid")
            self.assertEqual(store.entitlement["plan_id"], "researcher")
            self.assertEqual(store.entitlement["quota_units_per_week"], 2000.0)
            self.assertEqual(
                apply_webhook(store, parsed, subscription_status=active_researcher, catalog=catalog),
                "duplicate",
            )



    def test_failed_invoice_disables_paid_access_and_subscription_update_recovers(self):
        store = FakeStore(activation_number=1001, kind="paid", quota=2000)
        store.entitlement.update({
            "stripe_subscription_id": "sub_test",
            "stripe_customer_id": "cus_test",
            "active": True,
        })
        with patch.dict(os.environ, STRIPE_TEST_ENV, clear=False):
            failed = {
                "id": "evt_failed",
                "type": "invoice.payment_failed",
                "data": {"object": {"customer": "cus_test", "subscription": "sub_test"}},
            }
            self.assertEqual(
                apply_webhook(store, failed, subscription_status=lambda _: "past_due"),
                "processed",
            )
            self.assertEqual(store.entitlement["kind"], "paid_required")
            self.assertFalse(store.entitlement["active"])

            recovered = {
                "id": "evt_recovered",
                "type": "customer.subscription.updated",
                "data": {"object": {
                    "id": "sub_test",
                    "customer": "cus_test",
                    "status": "active",
                    "metadata": {"li_user_id": "u1"},
                }},
            }
            self.assertEqual(
                apply_webhook(store, recovered, subscription_status=active_researcher,
                              catalog=open_researcher_catalog()),
                "processed",
            )
            self.assertEqual(store.entitlement["kind"], "paid")
            self.assertTrue(store.entitlement["active"])


class CostingTests(unittest.TestCase):
    def test_external_data_cost_is_reconstructed_without_counting_customer_rate_as_cogs(self):
        ledger = CostLedger(0.0312)
        ledger.record("sense", "external_data", "licensed-provider", work_units=2, external_usd=0.75)
        with patch.dict(os.environ, {"LI_INFRA_USD_PER_RUN": "0"}, clear=False):
            result = actual_run_cost({"name": "heuristic", "usage": {"calls": 0}}, ledger)
        self.assertAlmostEqual(result.known_cost_usd, 0.75, places=6)
        self.assertTrue(result.fully_priced)


class EconomicGateTests(unittest.TestCase):
    def test_paid_plan_gate_fails_closed_without_samples(self):
        with patch.dict(os.environ, {
            "LI_PAID_MONTHLY_USD": "49.99",
            "LI_PAID_WEEKLY_UNITS": "2000",
            "LI_PAYMENT_FEE_PERCENT": "0.029",
            "LI_PAYMENT_FEE_FIXED_USD": "0.30",
            "LI_ECON_MIN_SAMPLES": "100",
            "LI_TARGET_GROSS_MARGIN": "0.65",
        }, clear=False):
            gate = certify_paid_plan([])
        self.assertFalse(gate.passed)
        self.assertIn("insufficient_samples:0/100", gate.reasons)

    def test_paid_plan_gate_uses_p95_cost_not_average(self):
        samples = [
            {"units": 1, "known_cost_usd": 0.001, "unpriced_components": []}
            for _ in range(94)
        ] + [
            {"units": 1, "known_cost_usd": 0.02, "unpriced_components": []}
            for _ in range(6)
        ]
        with patch.dict(os.environ, {
            "LI_PAID_MONTHLY_USD": "49.99",
            "LI_PAID_WEEKLY_UNITS": "2000",
            "LI_PAYMENT_FEE_PERCENT": "0.029",
            "LI_PAYMENT_FEE_FIXED_USD": "0.30",
            "LI_ECON_MIN_SAMPLES": "100",
            "LI_TARGET_GROSS_MARGIN": "0.65",
        }, clear=False):
            gate = certify_paid_plan(samples)
        self.assertFalse(gate.passed)
        self.assertIn("p95_cost_exceeds_margin_ceiling", gate.reasons)

    _CHEAP_ENV = {
        "LI_PAID_MONTHLY_USD": "49.99",
        "LI_PAID_WEEKLY_UNITS": "2000",
        "LI_PAYMENT_FEE_PERCENT": "0.029",
        "LI_PAYMENT_FEE_FIXED_USD": "0.30",
        "LI_TARGET_GROSS_MARGIN": "0.65",
    }

    @staticmethod
    def _cheap_samples(n):
        return [{"units": 1, "known_cost_usd": 0.001, "unpriced_components": []} for _ in range(n)]

    def test_lower_min_samples_env_cannot_lower_the_100_sample_floor(self):
        # Q20: the env may only raise the floor. Ten cheap samples would pass a floor of 5.
        for low in ("0", "1", "5", "99", "-50"):
            with self.subTest(env=low), patch.dict(os.environ, {**self._CHEAP_ENV, "LI_ECON_MIN_SAMPLES": low},
                                                   clear=False):
                gate = certify_paid_plan(self._cheap_samples(10))
                self.assertFalse(gate.passed)
                self.assertIn("insufficient_samples:10/100", gate.reasons)
                gate99 = certify_paid_plan(self._cheap_samples(99))
                self.assertFalse(gate99.passed)
                self.assertIn("insufficient_samples:99/100", gate99.reasons)

    def test_floor_of_100_still_passes_with_enough_samples_and_env_can_raise_it(self):
        with patch.dict(os.environ, {**self._CHEAP_ENV, "LI_ECON_MIN_SAMPLES": "5"}, clear=False):
            self.assertTrue(certify_paid_plan(self._cheap_samples(100)).passed)
        with patch.dict(os.environ, {**self._CHEAP_ENV, "LI_ECON_MIN_SAMPLES": "250"}, clear=False):
            gate = certify_paid_plan(self._cheap_samples(100))
            self.assertFalse(gate.passed)
            self.assertIn("insufficient_samples:100/250", gate.reasons)
            self.assertTrue(certify_paid_plan(self._cheap_samples(250)).passed)

    def test_unset_or_invalid_min_samples_uses_the_floor_and_invalid_fails_closed(self):
        from lofgren_intelligence.hosted.economics import MIN_SAMPLES_FLOOR, min_samples_required
        self.assertEqual(MIN_SAMPLES_FLOOR, 100)
        env = {k: v for k, v in os.environ.items() if k != "LI_ECON_MIN_SAMPLES"}
        with patch.dict(os.environ, env, clear=True):
            self.assertEqual(min_samples_required(), 100)
        with patch.dict(os.environ, {**self._CHEAP_ENV, "LI_ECON_MIN_SAMPLES": "ten"}, clear=False):
            reasons: list[str] = []
            self.assertEqual(min_samples_required(reasons), 100)
            self.assertEqual(reasons, ["invalid:LI_ECON_MIN_SAMPLES"])
            gate = certify_paid_plan(self._cheap_samples(150))
            self.assertFalse(gate.passed)
            self.assertIn("invalid:LI_ECON_MIN_SAMPLES", gate.reasons)


class MigrationContractTests(unittest.TestCase):
    def test_first_1000_rule_and_rls_are_present(self):
        sql = Path("supabase/migrations/20261004201650_public_mcp.sql").read_text(encoding="utf-8")
        self.assertIn("v_num <= 1000", sql)
        self.assertIn("'founding_free'", sql)
        self.assertIn("'paid_required'", sql)
        self.assertGreaterEqual(sql.count("enable row level security"), 10)
        self.assertIn("for update", sql.lower())
        self.assertNotIn("nextval('public.li_activation_seq')", sql)
        self.assertIn("primary key (user_id, run_id)", sql)
        self.assertIn("li_take_rate_limit", sql)
        self.assertIn("create table if not exists public.li_discoveries", sql.lower())
        self.assertIn("primary key (user_id, discovery_id)", sql)


if __name__ == "__main__":
    unittest.main()
