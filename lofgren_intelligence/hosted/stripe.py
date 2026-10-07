"""Stripe Billing integration for the hosted LI service.

Checkout is Stripe-hosted. Webhooks are signature-checked and entitlement
mutation is applied transactionally with the event receipt in Supabase.
Live charging is still separately gated by LI_BILLING_ENABLED, measured P95
economics, deployment configuration and owner release authorization.

Plans and prices come from the versioned plan catalog (billing/catalog.py):
checkout sells only a catalog plan that is available, has a decided allowance
and a price id configured for the current LI_STRIPE_MODE; a webhook maps the
subscription's price id back to that plan and grants the catalog allowance.
A price id the catalog does not map grants nothing. Clients never supply a
price id, an allowance or an entitlement value.
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
from typing import Any, Callable, Mapping

from ..billing.catalog import (
    CATALOG_VERSION,
    Catalog,
    CatalogPlan,
    checkout_plans,
    effective_allowance_units,
    plan_for_price,
    stripe_mode,
)
from .store import SupabaseStore


class StripeError(RuntimeError):
    pass


def _secret() -> str:
    value = os.environ.get("STRIPE_SECRET_KEY", "")
    if not value:
        raise StripeError("STRIPE_SECRET_KEY is not configured")
    return value


def _require_mode_matches_key() -> str:
    """The configured LI_STRIPE_MODE; refuses when it is unset or contradicts the secret key's mode."""
    mode = stripe_mode()
    if mode is None:
        raise StripeError("LI_STRIPE_MODE must be 'test' or 'live'")
    key = os.environ.get("STRIPE_SECRET_KEY", "")
    other = "live" if mode == "test" else "test"
    if key.startswith((f"sk_{other}_", f"rk_{other}_")):
        raise StripeError("LI_STRIPE_MODE does not match the configured Stripe key")
    return mode


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
    Any other failure still raises, so deletion stops before data removal. So
    does an answer that does not confirm the subscription ended (unknown =
    not cancelled): account deletion never proceeds on an unconfirmed cancel.
    """
    if not subscription_id:
        raise StripeError("Stripe subscription id is required")
    path = "/v1/subscriptions/" + urllib.parse.quote(subscription_id, safe="")
    try:
        result = stripe_delete(path)
        if not isinstance(result, dict) or str(result.get("status") or "") not in _ENDED:
            raise StripeError("Stripe did not confirm that the subscription was cancelled")
        return result
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


def checkout_price_for(plan_id: str, catalog: Catalog | None = None) -> str:
    """The server-configured price id for a checkout-able catalog plan, or StripeError."""
    _require_mode_matches_key()
    price = checkout_plans(catalog).get(plan_id)
    if not price:
        raise StripeError("plan is not available for checkout")
    return price


def create_checkout(user_id: str, *, plan_id: str, success_url: str, cancel_url: str,
                    catalog: Catalog | None = None) -> dict[str, Any]:
    price = checkout_price_for(plan_id, catalog)
    params: dict[str, Any] = {
        "mode": "subscription",
        "line_items[0][price]": price,
        "line_items[0][quantity]": "1",
        "success_url": success_url,
        "cancel_url": cancel_url,
        "client_reference_id": user_id,
        "metadata[li_user_id]": user_id,
        "subscription_data[metadata][li_user_id]": user_id,
        "metadata[li_plan_id]": plan_id,
        "metadata[li_catalog_version]": CATALOG_VERSION,
        "subscription_data[metadata][li_plan_id]": plan_id,
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


_ACTIVE = {"active", "trialing"}


def _price_ids(subscription: Mapping[str, Any]) -> list[str]:
    items = subscription.get("items") or {}
    rows = items.get("data") if isinstance(items, Mapping) else None
    out: list[str] = []
    for row in rows or []:
        price = row.get("price") if isinstance(row, Mapping) else None
        pid = price.get("id") if isinstance(price, Mapping) else price
        if isinstance(pid, str) and pid:
            out.append(pid)
    return out


def current_subscription(subscription_id: str) -> dict[str, Any]:
    """The subscription's status and price ids as Stripe reports them now."""
    path = "/v1/subscriptions/" + urllib.parse.quote(subscription_id, safe="")
    try:
        sub = stripe_get(path)
    except StripeAPIError as exc:
        if exc.status == 404:
            return {"status": "canceled", "price_ids": []}
        raise
    return {"status": str(sub.get("status") or ""), "price_ids": _price_ids(sub)}


def _grantable_plan(price_ids: list[str], catalog: Catalog | None, livemode: Any) -> CatalogPlan | None:
    """Exactly one catalog plan for the subscription's prices, in the configured mode; else None."""
    mode = stripe_mode()
    if isinstance(livemode, bool) and mode != ("live" if livemode else "test"):
        return None
    if len(set(price_ids)) != 1:
        return None
    return plan_for_price(price_ids[0], catalog)


def current_subscription_status(subscription_id: str) -> str:
    """The subscription's status as Stripe reports it now ("canceled" if it no longer exists)."""
    return str(current_subscription(subscription_id)["status"])


def apply_webhook(
    store: SupabaseStore,
    event: dict[str, Any],
    *,
    subscription_status: Callable[[str], str | Mapping[str, Any]] | None = None,
    catalog: Catalog | None = None,
) -> str:
    """Apply one Stripe lifecycle event exactly once.

    Stripe does not deliver events in order, so the status carried by an event
    can be stale: a `customer.subscription.created` (status `incomplete`)
    processed after the paid checkout used to switch a paying account back to
    `paid_required`. Every entitlement-mutating event therefore re-reads the
    subscription from Stripe and applies its current status; whichever event
    is processed last leaves the entitlement matching Stripe. A checkout event
    additionally needs a paid (or no-payment-required) session. If the status
    cannot be read, nothing is applied and Stripe retries the delivery.
    Unrelated Stripe events are still receipted but do not mutate an entitlement.

    The plan and allowance come from the catalog through the subscription's
    price id (read from Stripe together with the status; an injected lookup
    that returns a bare status falls back to the event object's own items).
    An unknown price, a plan that is not available or an undecided allowance
    grants nothing: the entitlement is left untouched, except that an earlier
    grant on the same subscription is revoked, so a plan change can never
    keep a stale grant.
    """
    lookup = subscription_status or current_subscription
    seen_prices: list[str] = []
    event_id = str(event["id"])
    kind = str(event["type"])
    obj = ((event.get("data") or {}).get("object") or {})
    if not isinstance(obj, dict):
        raise StripeError("Stripe event object must be a JSON object")

    def status_of(sub_id: str) -> str:
        result = lookup(sub_id)
        if isinstance(result, Mapping):
            seen_prices[:] = [str(p) for p in (result.get("price_ids") or []) if p]
            return str(result.get("status") or "")
        seen_prices[:] = _price_ids(obj) if str(obj.get("id") or "") == sub_id else []
        return str(result)

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
        # Unknown payment state is not payment: a missing payment_status
        # must not grant paid access (it used to default to "paid").
        paid = str(obj.get("payment_status") or "") in {"paid", "no_payment_required"}
        active = paid and status_of(subscription_id) in _ACTIVE
        mutate = True

    elif kind == "checkout.session.async_payment_failed":
        if not user_id:
            raise StripeError("checkout session missing li_user_id")
        subscription_id = _subscription_id(obj)
        active = bool(subscription_id) and status_of(str(subscription_id)) in _ACTIVE
        mutate = True

    elif kind in {"customer.subscription.created", "customer.subscription.updated", "customer.subscription.deleted"}:
        subscription_id = str(obj.get("id") or "") or None
        user_id = user_id or _lookup_user_for_subscription(store, subscription_id)
        if not user_id:
            raise StripeError("subscription event cannot be mapped to an LI user")
        active = bool(subscription_id) and status_of(str(subscription_id)) in _ACTIVE
        mutate = True

    elif kind == "invoice.payment_failed":
        subscription_id = _subscription_id(obj)
        user_id = _lookup_user_for_subscription(store, subscription_id)
        if not user_id:
            raise StripeError("failed invoice cannot be mapped to an LI user")
        active = status_of(str(subscription_id)) in _ACTIVE
        mutate = True

    plan = _grantable_plan(seen_prices, catalog, event.get("livemode")) if mutate else None
    quota = effective_allowance_units(plan) if plan is not None else None
    if mutate and active and quota is None:
        # Unknown price, unavailable plan or undecided allowance: grant nothing.
        active = False
        current = (store.get_entitlement(str(user_id)) or {}) if user_id else {}
        if not subscription_id or str(current.get("stripe_subscription_id") or "") != str(subscription_id):
            mutate = False

    if mutate and not active and user_id and subscription_id:
        # A lapsed subscription must not revoke access granted by a newer one
        # (an old subscription's deletion delivered after the user re-subscribed).
        current = store.get_entitlement(str(user_id)) or {}
        newer = str(current.get("stripe_subscription_id") or "")
        if current.get("active") and newer and newer != str(subscription_id):
            mutate = False

    if mutate and user_id and store.get_entitlement(str(user_id)) is None:
        # The LI account no longer exists (deleted) or never existed. An event that
        # ends or lapses a subscription is receipted without touching anything: there
        # is no entitlement left to revoke (writing one would also violate the
        # auth.users foreign key and make Stripe retry forever). An event that would
        # grant paid access to a missing account is refused and not receipted, so it
        # stays visible and Stripe keeps retrying: whether such a subscription is
        # cancelled or refunded is an operator/product decision LI does not make.
        if active:
            raise StripeError("active subscription event for an LI account that does not exist; not applied")
        mutate = False

    plan_id = plan.id if plan is not None else "paid_required"
    applied = store.apply_stripe_entitlement_event(
        event_id=event_id,
        event_type=kind,
        payload_hash=_payload_hash(event),
        user_id=user_id if mutate else None,
        customer_id=str(customer_id) if customer_id else None,
        subscription_id=subscription_id,
        active=active,
        plan_id=plan_id,
        quota_units_per_week=float(quota) if active and quota is not None else 0.0,
    )
    return "processed" if applied else "duplicate"
