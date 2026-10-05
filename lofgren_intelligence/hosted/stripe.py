"""Stripe Billing integration for the hosted LI service.

Checkout is Stripe-hosted. Webhooks are signature-checked and entitlement
mutation is applied transactionally with the event receipt in Supabase.
Live charging is still separately gated by LI_BILLING_ENABLED, measured P95
economics, deployment configuration and owner release authorization.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from .store import SupabaseStore


class StripeError(RuntimeError):
    pass


def _secret() -> str:
    value = os.environ.get("STRIPE_SECRET_KEY", "")
    if not value:
        raise StripeError("STRIPE_SECRET_KEY is not configured")
    return value


def _price_id() -> str:
    value = os.environ.get("LI_STRIPE_PRICE_ID", "")
    if not value:
        raise StripeError("LI_STRIPE_PRICE_ID is not configured")
    return value


class StripeAPIError(StripeError):
    """A Stripe API call failed; carries the HTTP status and Stripe error code."""

    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(f"Stripe API error {status}: {code or message or 'request failed'}")
        self.status = status
        self.code = code


def _stripe_call(req: urllib.request.Request) -> dict[str, Any]:
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        code, message = "", ""
        try:
            err = (json.loads(exc.read().decode("utf-8")) or {}).get("error") or {}
            code, message = str(err.get("code") or ""), str(err.get("type") or "")
        except Exception:
            pass
        raise StripeAPIError(int(exc.code), code, message) from None
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise StripeError("Stripe API is unreachable") from exc


def stripe_post(path: str, params: dict[str, Any]) -> dict[str, Any]:
    body = urllib.parse.urlencode(params, doseq=True).encode("utf-8")
    req = urllib.request.Request(
        "https://api.stripe.com" + path,
        data=body,
        method="POST",
        headers={
            "authorization": f"Bearer {_secret()}",
            "content-type": "application/x-www-form-urlencoded",
        },
    )
    return _stripe_call(req)


def stripe_get(path: str) -> dict[str, Any]:
    req = urllib.request.Request(
        "https://api.stripe.com" + path,
        method="GET",
        headers={"authorization": f"Bearer {_secret()}"},
    )
    return _stripe_call(req)


def stripe_delete(path: str) -> dict[str, Any]:
    req = urllib.request.Request(
        "https://api.stripe.com" + path,
        method="DELETE",
        headers={"authorization": f"Bearer {_secret()}"},
    )
    return _stripe_call(req)


_ENDED = {"canceled", "incomplete_expired"}


def cancel_subscription(subscription_id: str) -> dict[str, Any]:
    """Cancel a subscription; an already-ended or missing one counts as cancelled.

    A user whose subscription already ended (for example through the billing
    portal) keeps its id on the entitlement. Without this, account deletion
    would fail forever on Stripe's error for the second cancellation.
    Any other failure still raises, so deletion stops before data removal.
    """
    if not subscription_id:
        raise StripeError("Stripe subscription id is required")
    path = "/v1/subscriptions/" + urllib.parse.quote(subscription_id, safe="")
    try:
        return stripe_delete(path)
    except StripeAPIError as exc:
        if exc.status == 404:
            return {"id": subscription_id, "status": "canceled", "already_ended": True}
        try:
            current = stripe_get(path)
        except StripeAPIError as again:
            if again.status == 404:
                return {"id": subscription_id, "status": "canceled", "already_ended": True}
            raise
        if str(current.get("status") or "") in _ENDED:
            return {**current, "already_ended": True}
        raise


def create_checkout(user_id: str, *, success_url: str, cancel_url: str) -> dict[str, Any]:
    params: dict[str, Any] = {
        "mode": "subscription",
        "line_items[0][price]": _price_id(),
        "line_items[0][quantity]": "1",
        "success_url": success_url,
        "cancel_url": cancel_url,
        "client_reference_id": user_id,
        "metadata[li_user_id]": user_id,
        "subscription_data[metadata][li_user_id]": user_id,
        "allow_promotion_codes": "true",
    }
    return stripe_post("/v1/checkout/sessions", params)


def create_billing_portal(customer_id: str, *, return_url: str) -> dict[str, Any]:
    if not customer_id:
        raise StripeError("Stripe customer id is required")
    return stripe_post("/v1/billing_portal/sessions", {
        "customer": customer_id,
        "return_url": return_url,
    })


def verify_webhook(
    payload: bytes,
    signature: str,
    *,
    tolerance_s: int = 300,
    now_s: int | None = None,
) -> dict[str, Any]:
    secret = os.environ.get("STRIPE_WEBHOOK_SECRET", "")
    if not secret:
        raise StripeError("STRIPE_WEBHOOK_SECRET is not configured")
    parts: dict[str, list[str]] = {}
    for chunk in signature.split(","):
        if "=" not in chunk:
            continue
        k, v = chunk.split("=", 1)
        parts.setdefault(k, []).append(v)
    try:
        timestamp = int(parts["t"][0])
    except Exception as exc:
        raise StripeError("invalid Stripe-Signature timestamp") from exc
    current = int(time.time() if now_s is None else now_s)
    if abs(current - timestamp) > tolerance_s:
        raise StripeError("Stripe webhook timestamp outside tolerance")
    signed = str(timestamp).encode("ascii") + b"." + payload
    expected = hmac.new(secret.encode("utf-8"), signed, hashlib.sha256).hexdigest()
    if not any(hmac.compare_digest(expected, candidate) for candidate in parts.get("v1", [])):
        raise StripeError("invalid Stripe webhook signature")
    try:
        event = json.loads(payload.decode("utf-8"))
    except Exception as exc:
        raise StripeError("invalid Stripe webhook JSON") from exc
    if not isinstance(event, dict) or not event.get("id") or not event.get("type"):
        raise StripeError("invalid Stripe event")
    return event


def _user_id(obj: dict[str, Any]) -> str | None:
    metadata = obj.get("metadata") or {}
    return metadata.get("li_user_id") or obj.get("client_reference_id")


def _subscription_id(obj: dict[str, Any]) -> str | None:
    value = obj.get("subscription")
    if isinstance(value, dict):
        return value.get("id")
    return value


def _lookup_user_for_subscription(store: SupabaseStore, subscription_id: str | None) -> str | None:
    if not subscription_id:
        return None
    row = store.get_entitlement_by_subscription(subscription_id)
    return str(row["user_id"]) if row and row.get("user_id") else None


def _payload_hash(event: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(event, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def apply_webhook(store: SupabaseStore, event: dict[str, Any]) -> str:
    """Apply one Stripe lifecycle event exactly once.

    Subscription state is authoritative for continuing access. A failed invoice
    disables access immediately; a later customer.subscription.updated with
    active/trialing status can restore it. Unrelated Stripe events are still
    receipted but do not mutate an entitlement.
    """
    event_id = str(event["id"])
    kind = str(event["type"])
    obj = ((event.get("data") or {}).get("object") or {})
    if not isinstance(obj, dict):
        raise StripeError("Stripe event object must be a JSON object")

    user_id: str | None = _user_id(obj)
    customer_id: str | None = obj.get("customer")
    subscription_id: str | None = None
    mutate = False
    active = False

    if kind in {"checkout.session.completed", "checkout.session.async_payment_succeeded"}:
        if not user_id:
            raise StripeError("checkout session missing li_user_id")
        subscription_id = _subscription_id(obj)
        if not subscription_id:
            raise StripeError("checkout session missing subscription")
        active = str(obj.get("payment_status") or "paid") in {"paid", "no_payment_required"}
        mutate = True

    elif kind == "checkout.session.async_payment_failed":
        if not user_id:
            raise StripeError("checkout session missing li_user_id")
        subscription_id = _subscription_id(obj)
        active = False
        mutate = True

    elif kind in {"customer.subscription.created", "customer.subscription.updated", "customer.subscription.deleted"}:
        subscription_id = str(obj.get("id") or "") or None
        user_id = user_id or _lookup_user_for_subscription(store, subscription_id)
        if not user_id:
            raise StripeError("subscription event cannot be mapped to an LI user")
        status = str(obj.get("status") or "")
        active = kind != "customer.subscription.deleted" and status in {"active", "trialing"}
        mutate = True

    elif kind == "invoice.payment_failed":
        subscription_id = _subscription_id(obj)
        user_id = _lookup_user_for_subscription(store, subscription_id)
        if not user_id:
            raise StripeError("failed invoice cannot be mapped to an LI user")
        active = False
        mutate = True

    plan_id = os.environ.get("LI_PAID_PLAN_ID", "researcher")
    quota = float(os.environ.get("LI_PAID_WEEKLY_UNITS", "2000"))
    applied = store.apply_stripe_entitlement_event(
        event_id=event_id,
        event_type=kind,
        payload_hash=_payload_hash(event),
        user_id=user_id if mutate else None,
        customer_id=str(customer_id) if customer_id else None,
        subscription_id=subscription_id,
        active=active,
        plan_id=plan_id,
        quota_units_per_week=quota,
    )
    return "processed" if applied else "duplicate"
