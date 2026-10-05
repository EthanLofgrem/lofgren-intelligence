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
    """Call Supabase and translate failures into StoreError.

    Raw HTTPError/URLError escaped every web handler (they catch StoreError)
    as a 500, and PostgREST messages (schema, constraint and column names)
    reached MCP clients verbatim. Only the HTTP status is kept.
    """
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

    def _select_all(self, table: str, query: dict[str, Any], *, page: int = 1000, cap: int | None = None) -> list[dict[str, Any]]:
        """Read every matching row, page by page.

        PostgREST silently truncates a response at the project's max-rows
        setting (1000 by default on Supabase), so a single GET cannot be
        trusted for sums or exports. Paging stops only on an empty page, which
        stays correct whatever max-rows is configured to.
        """
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
            if not rows:
                break
            out.extend(rows)
            offset += len(rows)
        return out

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

    def record_usage(self, row: dict[str, Any]) -> None:
        self._table("li_usage_events", "POST", body=row, prefer="return=minimal")

    def usage_units_since(self, user_id: str, since_iso: str) -> float:
        rows = self._select_all(
            "li_usage_events",
            {
                "select": "units",
                "user_id": f"eq.{user_id}",
                "created_at": f"gte.{since_iso}",
                "order": "created_at.asc,id.asc",
            },
        )
        return float(sum(float(r.get("units") or 0.0) for r in (rows or [])))

    def cost_samples(self, limit: int = 5000) -> list[dict[str, Any]]:
        rows = self._table(
            "li_usage_events",
            query={
                "select": "units,known_cost_usd,unpriced_components,created_at",
                "units": "gt.0",
                "order": "created_at.desc",
                "limit": str(int(limit)),
            },
        )
        return list(rows or [])

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
