"""Minimal Stripe Billing integration for the hosted service.

Stripe-hosted Checkout is used. The runtime secret and webhook signing secret
come only from environment variables. This module never enables live mode by
itself; deployment configuration controls which Stripe keys are present.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import time
import urllib.parse
import urllib.request
from typing import Any

from .store import SupabaseStore, utcnow


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
    with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310 - fixed Stripe host
        return json.loads(resp.read().decode("utf-8"))


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


def verify_webhook(payload: bytes, signature: str, *, tolerance_s: int = 300, now_s: int | None = None) -> dict[str, Any]:
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


def apply_webhook(store: SupabaseStore, event: dict[str, Any]) -> str:
    event_id = str(event["id"])
    if store.stripe_event_seen(event_id):
        return "duplicate"
    kind = str(event["type"])
    obj = ((event.get("data") or {}).get("object") or {})
    user_id = _user_id(obj)
    if kind == "checkout.session.completed":
        if not user_id:
            raise StripeError("checkout session missing li_user_id")
        store.set_paid_entitlement(
            user_id,
            customer_id=obj.get("customer"),
            subscription_id=obj.get("subscription"),
            active=True,
            plan_id=os.environ.get("LI_PAID_PLAN_ID", "researcher"),
            quota_units_per_week=float(os.environ.get("LI_PAID_WEEKLY_UNITS", "2000")),
        )
    elif kind in {"customer.subscription.updated", "customer.subscription.deleted"}:
        if not user_id:
            # Subscription webhooks should have metadata set at Checkout creation.
            raise StripeError("subscription missing li_user_id metadata")
        status = str(obj.get("status") or "")
        active = kind != "customer.subscription.deleted" and status in {"active", "trialing"}
        store.set_paid_entitlement(
            user_id,
            customer_id=obj.get("customer"),
            subscription_id=obj.get("id"),
            active=active,
            plan_id=os.environ.get("LI_PAID_PLAN_ID", "researcher"),
            quota_units_per_week=float(os.environ.get("LI_PAID_WEEKLY_UNITS", "2000")),
        )
    store.record_stripe_event(
        {
            "stripe_event_id": event_id,
            "event_type": kind,
            "payload_hash": hashlib.sha256(json.dumps(event, sort_keys=True, separators=(",", ":")).encode()).hexdigest(),
            "processed_at": utcnow(),
        }
    )
    return "processed"
