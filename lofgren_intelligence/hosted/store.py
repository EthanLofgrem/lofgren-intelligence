"""Durable store backed by Supabase PostgREST.

Only the hosted service uses the service-role key. Browser clients never
receive it. All public tables are RLS-protected; mutations happen through this
server or narrowly-scoped SECURITY DEFINER RPCs.
"""

from __future__ import annotations

import os
import urllib.error
import urllib.parse
from datetime import datetime, timezone
from typing import Any

from .http import HTTPError, json_request, with_query


class StoreError(RuntimeError):
    pass


def _db(url: str, method: str, **kw: Any) -> Any:
    """Call Supabase without leaking PostgREST/schema details to public callers."""
    try:
        return json_request(url, method, **kw).body
    except HTTPError as exc:
        raise StoreError(f"database request failed (HTTP {exc.status})") from None
    except (urllib.error.URLError, OSError, ValueError):
        raise StoreError("database is unreachable") from None


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


class SupabaseStore:
    def __init__(
        self,
        url: str | None = None,
        service_key: str | None = None,
        publishable_key: str | None = None,
    ) -> None:
        self.url = (url or os.environ.get("SUPABASE_URL", "")).rstrip("/")
        self.service_key = service_key or os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")
        self.publishable_key = publishable_key or os.environ.get("SUPABASE_PUBLISHABLE_KEY", "")
        if not self.url:
            raise StoreError("SUPABASE_URL is required")
        if not self.service_key:
            raise StoreError("SUPABASE_SERVICE_ROLE_KEY is required")

    @property
    def _headers(self) -> dict[str, str]:
        return {
            "apikey": self.service_key,
            "authorization": f"Bearer {self.service_key}",
        }

    def _table(
        self,
        table: str,
        method: str = "GET",
        *,
        query: dict[str, Any] | None = None,
        body: Any = None,
        prefer: str | None = None,
    ) -> Any:
        url = f"{self.url}/rest/v1/{table}"
        if query:
            url = with_query(url, query)
        headers = dict(self._headers)
        if prefer:
            headers["prefer"] = prefer
        return _db(url, method, headers=headers, body=body)

    def rpc(self, name: str, body: dict[str, Any]) -> Any:
        return _db(
            f"{self.url}/rest/v1/rpc/{name}",
            "POST",
            headers=self._headers,
            body=body,
        )

    def verify_supabase_user(self, access_token: str) -> dict[str, Any]:
        key = self.publishable_key or self.service_key
        try:
            response = json_request(
                f"{self.url}/auth/v1/user",
                "GET",
                headers={"apikey": key, "authorization": f"Bearer {access_token}"},
            )
        except HTTPError as exc:
            raise StoreError("invalid Supabase user session") from exc
        if not isinstance(response.body, dict) or not response.body.get("id"):
            raise StoreError("invalid Supabase user session")
        return response.body

    def activate_account(self, user_id: str, email: str | None = None) -> dict[str, Any]:
        rows = self.rpc("li_activate_account", {"p_user_id": user_id, "p_email": email})
        if isinstance(rows, list):
            if not rows:
                raise StoreError("activation returned no account")
            return rows[0]
        if not isinstance(rows, dict):
            raise StoreError("activation returned invalid account")
        return rows

    def get_account(self, user_id: str) -> dict[str, Any] | None:
        rows = self._table(
            "li_accounts",
            query={"select": "*", "user_id": f"eq.{user_id}", "limit": "1"},
        )
        return rows[0] if rows else None

    def get_entitlement(self, user_id: str) -> dict[str, Any] | None:
        rows = self._table(
            "li_entitlements",
            query={"select": "*", "user_id": f"eq.{user_id}", "limit": "1"},
        )
        return rows[0] if rows else None

    def put_oauth_client(self, row: dict[str, Any]) -> None:
        self._table("li_oauth_clients", "POST", body=row, prefer="return=minimal")

    def get_oauth_client(self, client_id: str) -> dict[str, Any] | None:
        rows = self._table(
            "li_oauth_clients",
            query={"select": "*", "client_id": f"eq.{client_id}", "active": "eq.true", "limit": "1"},
        )
        return rows[0] if rows else None

    def put_oauth_code(self, row: dict[str, Any]) -> None:
        self._table("li_oauth_codes", "POST", body=row, prefer="return=minimal")

    def consume_oauth_code(self, code_hash: str) -> dict[str, Any] | None:
        rows = self.rpc("li_consume_oauth_code", {"p_code_hash": code_hash})
        if isinstance(rows, list):
            return rows[0] if rows else None
        return rows if isinstance(rows, dict) else None

    def put_access_token(self, row: dict[str, Any]) -> None:
        self._table("li_access_tokens", "POST", body=row, prefer="return=minimal")

    def get_access_token(self, token_hash: str) -> dict[str, Any] | None:
        rows = self._table(
            "li_access_tokens",
            query={
                "select": "*",
                "token_hash": f"eq.{token_hash}",
                "revoked_at": "is.null",
                "limit": "1",
            },
        )
        return rows[0] if rows else None

    def put_refresh_token(self, row: dict[str, Any]) -> None:
        self._table("li_refresh_tokens", "POST", body=row, prefer="return=minimal")

    def consume_refresh_token(self, refresh_hash: str) -> dict[str, Any] | None:
        rows = self.rpc("li_consume_refresh_token", {"p_token_hash": refresh_hash})
        if isinstance(rows, list):
            return rows[0] if rows else None
        return rows if isinstance(rows, dict) else None

    def revoke_access_token(self, token_hash: str) -> None:
        self._table(
            "li_access_tokens",
            "PATCH",
            query={"token_hash": f"eq.{token_hash}"},
            body={"revoked_at": utcnow()},
            prefer="return=minimal",
        )

    def save_run(self, row: dict[str, Any]) -> None:
        self._table(
            "li_runs",
            "POST",
            body=row,
            prefer="resolution=merge-duplicates,return=minimal",
        )

    def get_run(self, user_id: str, run_id: str) -> dict[str, Any] | None:
        rows = self._table(
            "li_runs",
            query={
                "select": "*",
                "user_id": f"eq.{user_id}",
                "run_id": f"eq.{run_id}",
                "limit": "1",
            },
        )
        return rows[0] if rows else None

    @staticmethod
    def _rpc_bool(result: Any) -> bool:
        if isinstance(result, bool):
            return result
        if isinstance(result, list) and result:
            value = result[0]
            if isinstance(value, bool):
                return value
            if isinstance(value, dict):
                return bool(next(iter(value.values()), False))
        if isinstance(result, dict):
            return bool(next(iter(result.values()), False))
        return bool(result)

    def _select_all(
        self,
        table: str,
        query: dict[str, Any],
        *,
        page: int = 1000,
        cap: int | None = None,
    ) -> list[dict[str, Any]]:
        """Read every matching row in deterministic pages, avoiding PostgREST max-row truncation."""
        out: list[dict[str, Any]] = []
        offset = 0
        while True:
            want = page if cap is None else min(page, cap - len(out))
            if want <= 0:
                break
            q = dict(query)
            q["limit"] = str(want)
            q["offset"] = str(offset)
            rows = list(self._table(table, query=q) or [])
            # Stop only on an empty page. A short page does not mean the end:
            # the server's max-rows may be smaller than the requested page
            # (Supabase lets operators lower it), and stopping on a short page
            # would silently truncate weekly usage sums and exports again.
            if not rows:
                break
            out.extend(rows)
            offset += len(rows)
        return out

    def reserve_usage(self, reservation_id: str, user_id: str, operation: str, units: float) -> bool:
        return self._rpc_bool(self.rpc("li_reserve_usage", {
            "p_id": reservation_id,
            "p_user_id": user_id,
            "p_operation": operation,
            "p_units": float(units),
            "p_window_seconds": 604800,
        }))

    def finalize_usage(
        self,
        reservation_id: str,
        run_id: str | None,
        actual_units: float,
        known_cost_usd: float,
        unpriced_components: list[str],
    ) -> bool:
        return self._rpc_bool(self.rpc("li_finalize_usage", {
            "p_id": reservation_id,
            "p_run_id": run_id,
            "p_actual_units": float(actual_units),
            "p_known_cost_usd": float(known_cost_usd),
            "p_unpriced_components": unpriced_components,
        }))

    def release_usage(self, reservation_id: str) -> bool:
        return self._rpc_bool(self.rpc("li_release_usage", {"p_id": reservation_id}))

    def mark_usage_unsettled(
        self,
        reservation_id: str,
        run_id: str | None,
        actual_units: float,
        known_cost_usd: float,
        unpriced_components: list[str],
    ) -> bool:
        """Durable reconciliation marker (li_mark_usage_unsettled) for work that ran but did not settle."""
        return self._rpc_bool(self.rpc("li_mark_usage_unsettled", {
            "p_id": reservation_id,
            "p_run_id": run_id,
            "p_actual_units": float(actual_units),
            "p_known_cost_usd": float(known_cost_usd),
            "p_unpriced_components": unpriced_components,
        }))

    def settle_usage(self, reservation_id: str) -> str:
        """Idempotent reconcile (li_settle_usage): 'settled', 'already_settled', 'not_unsettled' or 'missing'."""
        result = self.rpc("li_settle_usage", {"p_id": reservation_id})
        if isinstance(result, list) and len(result) == 1:
            result = result[0]
        if isinstance(result, dict) and len(result) == 1:
            result = next(iter(result.values()))
        if result not in ("settled", "already_settled", "not_unsettled", "missing"):
            raise StoreError("usage settlement returned an invalid answer")
        return str(result)

    def list_unsettled_usage(self, limit: int = 100) -> list[dict[str, Any]]:
        return self._select_all(
            "li_usage_reservations",
            {"select": "*", "status": "eq.unsettled", "order": "marked_at.asc,id.asc"},
            cap=max(0, int(limit)),
        )

    # ---- Intelligence Cases (li_cases, li_case_charters, li_case_approvals, li_case_events) ----

    @staticmethod
    def _rpc_int(result: Any) -> int:
        if isinstance(result, list) and len(result) == 1:
            result = result[0]
        if isinstance(result, dict) and len(result) == 1:
            result = next(iter(result.values()))
        if isinstance(result, bool) or not isinstance(result, (int, float)):
            raise StoreError("case store returned an invalid answer")
        return int(result)

    def create_case(
        self, case_id: str, user_id: str, objective: str, status: str,
        content_hash: str, charter: dict[str, Any], answers: dict[str, Any],
    ) -> int:
        return self._rpc_int(self.rpc("li_create_case", {
            "p_case_id": case_id, "p_user_id": user_id, "p_objective": objective,
            "p_status": status, "p_content_hash": content_hash, "p_charter": charter,
            "p_answers": answers,
        }))

    def revise_case(
        self, case_id: str, user_id: str, expected_version: int, status: str,
        content_hash: str, charter: dict[str, Any], answers: dict[str, Any],
    ) -> int:
        """New charter version, or -1 on a version conflict, or 0 when the case is not the caller's."""
        return self._rpc_int(self.rpc("li_revise_case", {
            "p_case_id": case_id, "p_user_id": user_id, "p_expected_version": int(expected_version),
            "p_status": status, "p_content_hash": content_hash, "p_charter": charter,
            "p_answers": answers,
        }))

    def get_case(self, user_id: str, case_id: str) -> dict[str, Any] | None:
        rows = self._table(
            "li_cases",
            query={"select": "*", "id": f"eq.{case_id}", "user_id": f"eq.{user_id}", "limit": "1"},
        )
        return rows[0] if rows else None

    def list_cases(self, user_id: str, limit: int | None = None) -> list[dict[str, Any]]:
        return self._select_all(
            "li_cases",
            {"select": "*", "user_id": f"eq.{user_id}", "order": "created_at.asc,id.asc"},
            cap=limit,
        )

    def get_case_charter(self, user_id: str, case_id: str, version: int | None = None) -> dict[str, Any] | None:
        """The given charter version, or the latest when version is None."""
        query: dict[str, Any] = {
            "select": "*", "case_id": f"eq.{case_id}", "user_id": f"eq.{user_id}",
            "order": "version.desc", "limit": "1",
        }
        if version is not None:
            query["version"] = f"eq.{int(version)}"
        rows = self._table("li_case_charters", query=query)
        return rows[0] if rows else None

    def approve_case_charter(self, row: dict[str, Any]) -> bool:
        return self._rpc_bool(self.rpc("li_approve_case_charter", {
            "p_id": row["id"], "p_case_id": row["case_id"], "p_user_id": row["user_id"],
            "p_charter_version": int(row["charter_version"]), "p_content_hash": row["content_hash"],
            "p_scope": row["scope"], "p_budget_usd": float(row["budget_usd"]),
            "p_budget_units": float(row["budget_units"]), "p_expires_at": row["expires_at"],
            "p_token_hash": row["token_hash"],
        }))

    def latest_case_approval(self, user_id: str, case_id: str) -> dict[str, Any] | None:
        rows = self._table(
            "li_case_approvals",
            query={"select": "*", "case_id": f"eq.{case_id}", "user_id": f"eq.{user_id}",
                   "order": "approved_at.desc,id.desc", "limit": "1"},
        )
        return rows[0] if rows else None

    def consume_case_approval(
        self, approval_id: str, user_id: str, token_hash: str, idempotency_key: str,
    ) -> dict[str, Any] | None:
        result = self.rpc("li_consume_case_approval", {
            "p_id": approval_id, "p_user_id": user_id, "p_token_hash": token_hash,
            "p_idempotency_key": idempotency_key,
        })
        if isinstance(result, list):
            result = result[0] if result else None
        if isinstance(result, dict) and len(result) == 1 and "li_consume_case_approval" in result:
            result = result["li_consume_case_approval"]
        return result if isinstance(result, dict) else None

    def record_case_run(self, approval_id: str, user_id: str, run_id: str | None, run_status: str) -> bool:
        return self._rpc_bool(self.rpc("li_record_case_run", {
            "p_id": approval_id, "p_user_id": user_id, "p_run_id": run_id, "p_run_status": run_status,
        }))

    def add_case_event(self, row: dict[str, Any]) -> None:
        self._table("li_case_events", "POST", body=row, prefer="return=minimal")

    def list_case_events(self, user_id: str, case_id: str) -> list[dict[str, Any]]:
        return self._select_all(
            "li_case_events",
            {"select": "*", "case_id": f"eq.{case_id}", "user_id": f"eq.{user_id}", "order": "created_at.asc,id.asc"},
        )

    # ---- durable research jobs (li_research_jobs; li_research_jobs migration) ----------------

    @staticmethod
    def _rpc_row(result: Any, name: str) -> dict[str, Any] | None:
        """A jsonb RPC answer: the row as a dict, or None (refused / not found)."""
        if isinstance(result, list):
            result = result[0] if result else None
        if isinstance(result, dict) and len(result) == 1 and name in result:
            result = result[name]
        if result is None:
            return None
        if not isinstance(result, dict) or not result.get("id"):
            raise StoreError("job store returned an invalid answer")
        return result

    def enqueue_research_job(
        self, job_id: str, user_id: str, case_id: str | None, kind: str, idempotency_key: str,
        job_input: dict[str, Any], reservation_id: str, max_attempts: int, queue_ttl_seconds: int,
    ) -> dict[str, Any] | None:
        """The new job (created=True), the existing job for the key (created=False), or None."""
        return self._rpc_row(self.rpc("li_enqueue_research_job", {
            "p_id": job_id, "p_user_id": user_id, "p_case_id": case_id, "p_kind": kind,
            "p_idempotency_key": idempotency_key, "p_input": job_input,
            "p_reservation_id": reservation_id, "p_max_attempts": int(max_attempts),
            "p_queue_ttl_seconds": int(queue_ttl_seconds),
        }), "li_enqueue_research_job")

    def claim_research_job(self, worker: str, lease_seconds: int, hold_grace_seconds: int) -> dict[str, Any] | None:
        return self._rpc_row(self.rpc("li_claim_research_job", {
            "p_worker": worker, "p_lease_seconds": int(lease_seconds),
            "p_hold_grace_seconds": int(hold_grace_seconds),
        }), "li_claim_research_job")

    def heartbeat_research_job(
        self, job_id: str, worker: str, lease_seconds: int, hold_grace_seconds: int,
        checkpoint: dict[str, Any] | None = None, cost_so_far: float | None = None,
    ) -> dict[str, Any] | None:
        """The job row while this worker holds the lease; None once the lease is lost."""
        return self._rpc_row(self.rpc("li_heartbeat_research_job", {
            "p_id": job_id, "p_worker": worker, "p_lease_seconds": int(lease_seconds),
            "p_hold_grace_seconds": int(hold_grace_seconds), "p_checkpoint": checkpoint,
            "p_cost_so_far": None if cost_so_far is None else float(cost_so_far),
        }), "li_heartbeat_research_job")

    def complete_research_job(self, job_id: str, worker: str, run_id: str, cost_so_far: float) -> dict[str, Any] | None:
        return self._rpc_row(self.rpc("li_complete_research_job", {
            "p_id": job_id, "p_worker": worker, "p_run_id": run_id, "p_cost_so_far": float(cost_so_far),
        }), "li_complete_research_job")

    def fail_research_job(
        self, job_id: str, worker: str, error_code: str, retryable: bool, backoff_seconds: int,
        cost_so_far: float, checkpoint: dict[str, Any] | None, queue_ttl_seconds: int,
    ) -> dict[str, Any] | None:
        return self._rpc_row(self.rpc("li_fail_research_job", {
            "p_id": job_id, "p_worker": worker, "p_error_code": error_code, "p_retryable": bool(retryable),
            "p_backoff_seconds": int(backoff_seconds), "p_cost_so_far": float(cost_so_far),
            "p_checkpoint": checkpoint, "p_queue_ttl_seconds": int(queue_ttl_seconds),
        }), "li_fail_research_job")

    def request_cancel_research_job(self, job_id: str, user_id: str) -> dict[str, Any] | None:
        return self._rpc_row(self.rpc("li_request_cancel_research_job", {
            "p_id": job_id, "p_user_id": user_id,
        }), "li_request_cancel_research_job")

    def reclaim_research_jobs(self, limit: int, queue_ttl_seconds: int) -> list[str]:
        result = self.rpc("li_reclaim_research_jobs", {
            "p_limit": int(limit), "p_queue_ttl_seconds": int(queue_ttl_seconds),
        })
        if isinstance(result, dict) and len(result) == 1 and "li_reclaim_research_jobs" in result:
            result = result["li_reclaim_research_jobs"]
        if result is None:
            return []
        if not isinstance(result, list) or not all(isinstance(x, str) for x in result):
            raise StoreError("job store returned an invalid answer")
        return list(result)

    def get_research_job(self, user_id: str, job_id: str) -> dict[str, Any] | None:
        rows = self._table(
            "li_research_jobs",
            query={"select": "*", "id": f"eq.{job_id}", "user_id": f"eq.{user_id}", "limit": "1"},
        )
        return rows[0] if rows else None

    def get_research_job_by_key(self, user_id: str, idempotency_key: str) -> dict[str, Any] | None:
        rows = self._table(
            "li_research_jobs",
            query={"select": "*", "user_id": f"eq.{user_id}", "idempotency_key": f"eq.{idempotency_key}",
                   "limit": "1"},
        )
        return rows[0] if rows else None

    def list_research_jobs(self, user_id: str, limit: int | None = None) -> list[dict[str, Any]]:
        return self._select_all(
            "li_research_jobs",
            {"select": "*", "user_id": f"eq.{user_id}", "order": "created_at.asc,id.asc"},
            cap=limit,
        )

    def take_rate_limit(self, user_id: str, bucket: str = "mcp", limit: int = 60, window_seconds: int = 60) -> bool:
        result = self.rpc("li_take_rate_limit", {
            "p_user_id": user_id,
            "p_bucket": bucket,
            "p_limit": int(limit),
            "p_window_seconds": int(window_seconds),
        })
        if isinstance(result, bool):
            return result
        if isinstance(result, list) and result:
            value = result[0]
            if isinstance(value, bool):
                return value
            if isinstance(value, dict):
                return bool(next(iter(value.values()), False))
        return bool(result)

    def take_keyed_rate_limit(self, bucket: str, key_hash: str, limit: int, window_seconds: int) -> int:
        """Global sliding-window take (li_take_keyed_rate_limit): 0 = allowed, else seconds to wait.

        `key_hash` is already a SHA-256 hex digest; raw IPs and email domains
        never reach the database. An unreadable answer is a StoreError so that
        the caller can fail closed.
        """
        result = self.rpc("li_take_keyed_rate_limit", {
            "p_bucket": bucket,
            "p_key_hash": key_hash,
            "p_limit": int(limit),
            "p_window_seconds": int(window_seconds),
        })
        if isinstance(result, list) and len(result) == 1:
            result = result[0]
        if isinstance(result, dict) and len(result) == 1:
            result = next(iter(result.values()))
        if isinstance(result, bool) or not isinstance(result, (int, float)) or result < 0:
            raise StoreError("rate limiter returned an invalid answer")
        return int(result)

    def list_runs(self, user_id: str, limit: int | None = None) -> list[dict[str, Any]]:
        return self._select_all(
            "li_runs",
            {"select": "*", "user_id": f"eq.{user_id}", "order": "created_at.asc,run_id.asc"},
            cap=limit,
        )

    def list_usage(self, user_id: str, limit: int | None = None) -> list[dict[str, Any]]:
        return self._select_all(
            "li_usage_events",
            {"select": "*", "user_id": f"eq.{user_id}", "order": "created_at.asc,id.asc"},
            cap=limit,
        )

    def delete_auth_user(self, user_id: str) -> None:
        quoted = urllib.parse.quote(user_id, safe="")
        _db(
            f"{self.url}/auth/v1/admin/users/{quoted}",
            "DELETE",
            headers=self._headers,
        )

    def save_discovery(self, row: dict[str, Any]) -> None:
        self._table("li_discoveries", "POST", body=row, prefer="return=minimal,resolution=merge-duplicates")

    def get_discovery(self, user_id: str, discovery_id: str) -> dict[str, Any] | None:
        rows = self._table(
            "li_discoveries",
            query={
                "select": "*",
                "user_id": f"eq.{user_id}",
                "discovery_id": f"eq.{discovery_id}",
                "limit": "1",
            },
        )
        return rows[0] if rows else None

    def list_discoveries(self, user_id: str, limit: int | None = None) -> list[dict[str, Any]]:
        return self._select_all(
            "li_discoveries",
            {"select": "*", "user_id": f"eq.{user_id}", "order": "created_at.asc,discovery_id.asc"},
            cap=limit,
        )

    def save_artifact(self, row: dict[str, Any]) -> None:
        self._table("li_artifacts", "POST", body=row, prefer="return=minimal,resolution=merge-duplicates")

    def get_artifact(self, user_id: str, artifact_id: str) -> dict[str, Any] | None:
        rows = self._table(
            "li_artifacts",
            query={"select": "*", "user_id": f"eq.{user_id}", "artifact_id": f"eq.{artifact_id}", "limit": "1"},
        )
        return rows[0] if rows else None

    def list_artifacts(self, user_id: str, limit: int | None = None) -> list[dict[str, Any]]:
        return self._select_all(
            "li_artifacts",
            {"select": "*", "user_id": f"eq.{user_id}", "order": "created_at.asc,artifact_id.asc"},
            cap=limit,
        )

    def save_action(self, row: dict[str, Any]) -> None:
        self._table("li_action_proposals", "POST", body=row, prefer="return=minimal,resolution=merge-duplicates")

    def get_action(self, user_id: str, action_id: str) -> dict[str, Any] | None:
        rows = self._table(
            "li_action_proposals",
            query={"select": "*", "user_id": f"eq.{user_id}", "action_id": f"eq.{action_id}", "limit": "1"},
        )
        return rows[0] if rows else None

    def list_actions(self, user_id: str, limit: int | None = None) -> list[dict[str, Any]]:
        return self._select_all(
            "li_action_proposals",
            {"select": "*", "user_id": f"eq.{user_id}", "order": "created_at.asc,action_id.asc"},
            cap=limit,
        )

    def save_outcome(self, row: dict[str, Any]) -> None:
        self._table("li_outcomes", "POST", body=row, prefer="return=minimal,resolution=merge-duplicates")

    def get_outcome(self, user_id: str, outcome_id: str) -> dict[str, Any] | None:
        rows = self._table(
            "li_outcomes",
            query={"select": "*", "user_id": f"eq.{user_id}", "outcome_id": f"eq.{outcome_id}", "limit": "1"},
        )
        return rows[0] if rows else None

    def list_outcomes(self, user_id: str, limit: int | None = None) -> list[dict[str, Any]]:
        return self._select_all(
            "li_outcomes",
            {"select": "*", "user_id": f"eq.{user_id}", "order": "created_at.asc,outcome_id.asc"},
            cap=limit,
        )

    def save_improvement(self, row: dict[str, Any]) -> None:
        self._table("li_improvements", "POST", body=row, prefer="return=minimal,resolution=merge-duplicates")

    def get_improvement(self, user_id: str, improvement_id: str) -> dict[str, Any] | None:
        rows = self._table(
            "li_improvements",
            query={"select": "*", "user_id": f"eq.{user_id}", "improvement_id": f"eq.{improvement_id}", "limit": "1"},
        )
        return rows[0] if rows else None

    def list_improvements(self, user_id: str, limit: int | None = None) -> list[dict[str, Any]]:
        return self._select_all(
            "li_improvements",
            {"select": "*", "user_id": f"eq.{user_id}", "order": "created_at.asc,improvement_id.asc"},
            cap=limit,
        )

    def record_usage(self, row: dict[str, Any]) -> None:
        self._table("li_usage_events", "POST", body=row, prefer="return=minimal")

    def usage_units_since(self, user_id: str, since_iso: str) -> float:
        rows = self._select_all(
            "li_usage_events",
            {"select": "units", "user_id": f"eq.{user_id}", "created_at": f"gte.{since_iso}", "order": "created_at.asc,id.asc"},
        )
        return float(sum(float(r.get("units") or 0.0) for r in rows))

    def cost_samples(self, limit: int = 5000) -> list[dict[str, Any]]:
        return self._select_all(
            "li_usage_events",
            {"select": "units,known_cost_usd,unpriced_components,created_at", "units": "gt.0", "order": "created_at.desc,id.desc"},
            cap=max(0, int(limit)),
        )

    def get_entitlement_by_subscription(self, subscription_id: str) -> dict[str, Any] | None:
        rows = self._table(
            "li_entitlements",
            query={
                "select": "*",
                "stripe_subscription_id": f"eq.{subscription_id}",
                "limit": "1",
            },
        )
        return rows[0] if rows else None

    def apply_stripe_entitlement_event(
        self,
        *,
        event_id: str,
        event_type: str,
        payload_hash: str,
        user_id: str | None,
        customer_id: str | None,
        subscription_id: str | None,
        active: bool,
        plan_id: str,
        quota_units_per_week: float,
    ) -> bool:
        result = self.rpc("li_apply_stripe_entitlement_event", {
            "p_event_id": event_id,
            "p_event_type": event_type,
            "p_payload_hash": payload_hash,
            "p_user_id": user_id,
            "p_customer_id": customer_id,
            "p_subscription_id": subscription_id,
            "p_active": active,
            "p_plan_id": plan_id,
            "p_quota_units_per_week": quota_units_per_week,
        })
        if isinstance(result, bool):
            return result
        if isinstance(result, list) and result:
            value = result[0]
            if isinstance(value, bool):
                return value
            if isinstance(value, dict):
                return bool(next(iter(value.values()), False))
        return bool(result)

    def stripe_event_seen(self, event_id: str) -> bool:
        rows = self._table(
            "li_billing_events",
            query={"select": "stripe_event_id", "stripe_event_id": f"eq.{event_id}", "limit": "1"},
        )
        return bool(rows)

    def record_stripe_event(self, row: dict[str, Any]) -> None:
        self._table("li_billing_events", "POST", body=row, prefer="return=minimal")

    def set_paid_entitlement(
        self,
        user_id: str,
        *,
        customer_id: str | None,
        subscription_id: str | None,
        active: bool,
        plan_id: str,
        quota_units_per_week: float,
    ) -> None:
        self._table(
            "li_entitlements",
            "POST",
            body={
                "user_id": user_id,
                "kind": "paid" if active else "paid_required",
                "plan_id": plan_id,
                "active": active,
                "quota_units_per_week": quota_units_per_week,
                "stripe_customer_id": customer_id,
                "stripe_subscription_id": subscription_id,
                "updated_at": utcnow(),
            },
            prefer="resolution=merge-duplicates,return=minimal",
        )
