"""Regression tests for the public-readiness audit fixes on the release line.

Ported from build/public-readiness-audit (tests/test_public_readiness_audit.py)
and adapted to release/public-v1-v6: hosted V3-V6 tools, the atomic
li_reserve_usage reservation path and the user-journey consent page.
No test here makes a network, Stripe or Supabase call.
"""

import asyncio
import io
import json
import os
import unittest
import urllib.error
from unittest.mock import patch

from starlette.requests import Request

from lofgren_intelligence.hosted import stripe as li_stripe
from lofgren_intelligence.hosted import web_app
from lofgren_intelligence.hosted.journey import consent_intro_html
from lofgren_intelligence.hosted.service import PublicService
from lofgren_intelligence.hosted.stripe import StripeAPIError, StripeError, apply_webhook

from .test_public_hosted import FakeStore

ENV = {
    "LI_PUBLIC_BASE_URL": "https://li.example",
    "SUPABASE_URL": "https://project.supabase.example",
    "SUPABASE_PUBLISHABLE_KEY": "publishable-test-key",
}


def _request(path, query="", method="GET", client="203.0.113.9", body=b""):
    scope = {
        "type": "http", "method": method, "path": path, "raw_path": path.encode(),
        "query_string": query.encode(), "headers": [], "scheme": "https",
        "server": ("li.example", 443), "client": (client, 1234), "root_path": "",
    }
    sent = {"done": False}

    async def receive():
        if sent["done"]:
            return {"type": "http.disconnect"}
        sent["done"] = True
        return {"type": "http.request", "body": body, "more_body": False}

    return Request(scope, receive)


def _call(handler, path, query="", **kw):
    return asyncio.run(handler(_request(path, query, **kw)))


class _ClientStore:
    def __init__(self, *a, **k):
        pass

    def get_oauth_client(self, client_id):
        if client_id != "licl_attacker":
            return None
        return {
            "client_id": client_id,
            "client_name": "<b>Claude</b>",
            "redirect_uris": ["https://evil.example/cb"],
        }


class AuthorizeConsentTests(unittest.TestCase):
    """Fix 1: an existing browser session never authorizes without a click."""

    def _page(self):
        query = (
            "response_type=code&client_id=licl_attacker&redirect_uri=https%3A%2F%2Fevil.example%2Fcb"
            "&code_challenge=" + "A" * 43 + "&code_challenge_method=S256&state=s"
        )
        with patch.dict(os.environ, ENV), patch.object(web_app, "SupabaseStore", _ClientStore):
            resp = _call(web_app.oauth_authorize, "/oauth/authorize", query)
        self.assertEqual(resp.status_code, 200)
        return resp.body.decode("utf-8")

    def test_existing_session_never_completes_authorization_without_a_click(self):
        page = self._page()
        start = page.index("async function existing()")
        existing = page[start:page.index("\n", start)]
        self.assertNotIn("complete(", existing)
        self.assertIn('<button id="continue" hidden>', page)
        # complete() is defined once and called only from the approve button's
        # click handler; sign-in, sign-up and an existing session only arm it.
        callers = [line for line in page.splitlines()
                   if "complete(" in line and "async function complete(" not in line]
        self.assertTrue(callers)
        for line in callers:
            self.assertTrue(line.startswith("cont.onclick="), line)
        # The only automatic call on load is existing().
        self.assertEqual(page.count("existing()"), 2)

    def test_consent_names_client_and_redirect_host_escaped(self):
        page = self._page()
        self.assertIn("&lt;b&gt;Claude&lt;/b&gt;", page)
        self.assertNotIn("<b>Claude</b>", page)
        self.assertIn("<strong>evil.example</strong>", page)

    def test_consent_intro_escapes_redirect_host(self):
        page = consent_intro_html("Claude", "mcp", "https://a<b>.example/cb")
        self.assertNotIn("<b>", page)
        self.assertIn('id="consent"', page)
        # Without a redirect URI (journey preview) the notice is omitted.
        self.assertNotIn('id="consent"', consent_intro_html("Claude"))


class RedirectURIValidationTests(unittest.TestCase):
    """Fix 2: loopback redirect hosts are compared exactly, not by prefix."""

    def _register(self, uri):
        from lofgren_intelligence.hosted.auth import OAuthService
        return OAuthService(FakeStore()).register_client({"redirect_uris": [uri]})

    def test_lookalike_loopback_hosts_are_refused(self):
        from lofgren_intelligence.hosted.auth import AuthError
        for uri in ("http://localhost.attacker.example/cb", "http://127.0.0.1.attacker.example/cb",
                    "http://localhostattacker.example/cb", "http://attacker.example/cb",
                    "http://localhost@attacker.example/cb", "https://user:pw@client.example/cb",
                    "https://client.example/cb#frag", "javascript:alert(1)", "http://localhost:99999/cb"):
            with self.subTest(uri=uri), self.assertRaises(AuthError):
                self._register(uri)

    def test_https_and_exact_loopback_are_accepted(self):
        for uri in ("https://client.example/cb", "http://localhost/cb", "http://localhost:33418/callback",
                    "http://127.0.0.1:8080/cb", "http://[::1]:8080/cb", "http://LOCALHOST/cb"):
            with self.subTest(uri=uri):
                self.assertEqual(self._register(uri)["redirect_uris"], [uri])


class _Resp:
    def __init__(self, body):
        self._b = json.dumps(body).encode()

    def read(self):
        return self._b

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _http_error(status, code):
    body = json.dumps({"error": {"code": code, "type": "invalid_request_error"}}).encode()
    return urllib.error.HTTPError("https://api.stripe.com/x", status, "err", {}, io.BytesIO(body))


class _FakeStripe:
    """Stands in for urllib.request.urlopen; no network and no real Stripe call."""

    def __init__(self, delete, get=None):
        self.delete, self.get, self.calls = delete, get, []

    def __call__(self, req, timeout=None):
        self.calls.append(req.get_method())
        outcome = self.delete if req.get_method() == "DELETE" else self.get
        if isinstance(outcome, Exception):
            raise outcome
        return _Resp(outcome)


class StripeCancellationTests(unittest.TestCase):
    """Fix 3: deletion proceeds when the subscription has already ended."""

    PHRASE = "DELETE MY LOFGREN INTELLIGENCE ACCOUNT"

    def _paid_store(self):
        store = FakeStore(activation_number=1001, kind="paid", quota=2000)
        store.entitlement.update({"stripe_subscription_id": "sub_test", "stripe_customer_id": "cus_test"})
        return store

    def _delete(self, store, fake):
        with patch.dict(os.environ, {"STRIPE_SECRET_KEY": "sk_test_dummy"}), \
                patch.object(li_stripe.urllib.request, "urlopen", fake):
            return PublicService(store).delete_account("u1", self.PHRASE)

    def test_already_cancelled_subscription_does_not_block_account_deletion(self):
        store = self._paid_store()
        fake = _FakeStripe(_http_error(400, "resource_invalid_state"), {"id": "sub_test", "status": "canceled"})
        self.assertTrue(self._delete(store, fake)["deleted"])
        self.assertEqual(store.deleted_user, "u1")
        self.assertEqual(fake.calls, ["DELETE", "GET"])

    def test_missing_subscription_does_not_block_account_deletion(self):
        store = self._paid_store()
        self.assertTrue(self._delete(store, _FakeStripe(_http_error(404, "resource_missing")))["deleted"])

    def test_live_subscription_that_fails_to_cancel_stops_deletion(self):
        store = self._paid_store()
        fake = _FakeStripe(_http_error(500, "api_error"), {"id": "sub_test", "status": "active"})
        with self.assertRaises(StripeAPIError):
            self._delete(store, fake)
        self.assertFalse(hasattr(store, "deleted_user"))
        self.assertIsNotNone(store.account)

    def test_network_failure_is_a_stripe_error_not_a_raw_urllib_error(self):
        store = self._paid_store()
        with self.assertRaises(StripeError):
            self._delete(store, _FakeStripe(urllib.error.URLError("down")))
        self.assertFalse(hasattr(store, "deleted_user"))


class WebhookPaymentStatusTests(unittest.TestCase):
    """Fix 9: a checkout without payment_status grants nothing."""

    def _apply(self, obj):
        store = FakeStore(activation_number=1001, kind="paid_required", quota=0)
        event = {"id": "evt_" + str(len(obj)), "type": "checkout.session.completed", "data": {"object": obj}}
        apply_webhook(store, event, subscription_status=lambda s: "active")
        return store.entitlement

    def test_missing_payment_status_does_not_grant_paid_access(self):
        ent = self._apply({"customer": "cus_t", "subscription": "sub_t", "metadata": {"li_user_id": "u1"}})
        self.assertFalse(ent["active"])
        self.assertEqual(ent["kind"], "paid_required")

    def test_unpaid_checkout_does_not_grant_paid_access(self):
        ent = self._apply({"customer": "cus_t", "subscription": "sub_t", "metadata": {"li_user_id": "u1"},
                           "payment_status": "unpaid"})
        self.assertFalse(ent["active"])

    def test_paid_checkout_still_grants_access(self):
        ent = self._apply({"customer": "cus_t", "subscription": "sub_t", "metadata": {"li_user_id": "u1"},
                           "payment_status": "paid"})
        self.assertTrue(ent["active"])
        self.assertEqual(ent["kind"], "paid")


class WebhookOrderingTests(unittest.TestCase):
    """Fix 10: Stripe does not order deliveries; the subscription's current status decides."""

    ENV = {"LI_PAID_PLAN_ID": "researcher", "LI_PAID_WEEKLY_UNITS": "2000"}

    def _event(self, event_id, kind, obj):
        return {"id": event_id, "type": kind, "data": {"object": obj}}

    def _checkout(self):
        return self._event("evt_checkout", "checkout.session.completed", {
            "customer": "cus_t", "subscription": "sub_new", "metadata": {"li_user_id": "u1"},
            "payment_status": "paid"})

    def test_stale_incomplete_created_event_does_not_revoke_paid_access(self):
        store = FakeStore(activation_number=1001, kind="paid_required", quota=0)
        stripe_now = lambda sub: "active"
        with patch.dict(os.environ, self.ENV, clear=False):
            apply_webhook(store, self._checkout(), subscription_status=stripe_now)
            stale = self._event("evt_created", "customer.subscription.created", {
                "id": "sub_new", "customer": "cus_t", "status": "incomplete", "metadata": {"li_user_id": "u1"}})
            self.assertEqual(apply_webhook(store, stale, subscription_status=stripe_now), "processed")
        self.assertTrue(store.entitlement["active"])
        self.assertEqual(store.entitlement["kind"], "paid")

    def test_stale_active_event_does_not_restore_a_cancelled_subscription(self):
        store = FakeStore(activation_number=1001, kind="paid", quota=2000)
        store.entitlement.update({"stripe_subscription_id": "sub_new", "active": True})
        stale = self._event("evt_old_update", "customer.subscription.updated", {
            "id": "sub_new", "customer": "cus_t", "status": "active", "metadata": {"li_user_id": "u1"}})
        with patch.dict(os.environ, self.ENV, clear=False):
            apply_webhook(store, stale, subscription_status=lambda sub: "canceled")
        self.assertFalse(store.entitlement["active"])
        self.assertEqual(store.entitlement["kind"], "paid_required")

    def test_old_subscription_deletion_does_not_revoke_a_newer_subscription(self):
        store = FakeStore(activation_number=1001, kind="paid", quota=2000)
        store.entitlement.update({"stripe_subscription_id": "sub_new", "active": True})
        old = self._event("evt_old_deleted", "customer.subscription.deleted", {
            "id": "sub_old", "customer": "cus_t", "status": "canceled", "metadata": {"li_user_id": "u1"}})
        with patch.dict(os.environ, self.ENV, clear=False):
            self.assertEqual(apply_webhook(store, old, subscription_status=lambda sub: "canceled"), "processed")
        self.assertTrue(store.entitlement["active"])
        self.assertEqual(store.entitlement["stripe_subscription_id"], "sub_new")

    def test_unreadable_subscription_status_applies_nothing(self):
        store = FakeStore(activation_number=1001, kind="paid_required", quota=0)

        def down(sub):
            raise StripeError("Stripe API is unreachable")

        with patch.dict(os.environ, self.ENV, clear=False), self.assertRaises(StripeError):
            apply_webhook(store, self._checkout(), subscription_status=down)
        self.assertEqual(store.billing_events, {})
        self.assertFalse(store.entitlement["active"])

    def test_default_status_lookup_reads_stripe_and_treats_missing_as_cancelled(self):
        with patch.object(li_stripe, "stripe_get", return_value={"id": "sub_x", "status": "past_due"}) as get:
            self.assertEqual(li_stripe.current_subscription_status("sub_x"), "past_due")
        self.assertEqual(get.call_args[0][0], "/v1/subscriptions/sub_x")
        with patch.object(li_stripe, "stripe_get", side_effect=StripeAPIError(404, "resource_missing", "")):
            self.assertEqual(li_stripe.current_subscription_status("sub_x"), "canceled")
        with patch.object(li_stripe, "stripe_get", side_effect=StripeAPIError(500, "", "api_error")):
            with self.assertRaises(StripeAPIError):
                li_stripe.current_subscription_status("sub_x")


class SSRFAddressTests(unittest.TestCase):
    """Fix 5: shared/CGNAT and IPv4-in-IPv6 special addresses are not public."""

    BLOCKED = [
        "100.100.100.200",         # CGNAT / shared space; cloud metadata on some providers
        "100.64.0.1",
        "198.18.0.1",              # benchmarking
        "::ffff:169.254.169.254",  # IPv4-mapped metadata
        "::ffff:127.0.0.1",        # IPv4-mapped loopback
        "64:ff9b::a9fe:a9fe",      # NAT64 of 169.254.169.254
        "2002:7f00:1::1",          # 6to4 of 127.0.0.1
        "::127.0.0.1",             # IPv4-compatible loopback
    ]

    def test_special_purpose_literals_are_refused(self):
        from lofgren_intelligence.adapters.net import UnsafeURL, validate_public_url
        for addr in self.BLOCKED:
            host = f"[{addr}]" if ":" in addr else addr
            with self.subTest(addr=addr), self.assertRaises(UnsafeURL):
                validate_public_url(f"http://{host}/latest/meta-data/")

    def test_hostname_resolving_to_cgnat_is_refused(self):
        from lofgren_intelligence.adapters.net import UnsafeURL, validate_public_url
        fake = lambda *a, **k: [(2, 1, 6, "", ("100.100.100.200", 80))]
        with patch("lofgren_intelligence.adapters.net.socket.getaddrinfo", fake), self.assertRaises(UnsafeURL):
            validate_public_url("http://metadata.attacker.example/")

    def test_global_address_is_still_allowed(self):
        from lofgren_intelligence.adapters.net import validate_public_url
        fake = lambda *a, **k: [(2, 1, 6, "", ("93.184.215.14", 443))]
        with patch("lofgren_intelligence.adapters.net.socket.getaddrinfo", fake):
            self.assertEqual(validate_public_url("https://example.com/x"), "https://example.com/x")


class _PostgREST:
    """Fake PostgREST that truncates every response at max_rows, like Supabase."""

    def __init__(self, rows, max_rows=1000):
        self.rows, self.max_rows, self.requests = rows, max_rows, 0

    def __call__(self, url, method="GET", headers=None, body=None, **kw):
        import urllib.parse as up
        from lofgren_intelligence.hosted.http import JSONResponse
        self.requests += 1
        q = dict(up.parse_qsl(up.urlsplit(url).query))
        offset = int(q.get("offset", 0))
        limit = min(int(q.get("limit", 10**9)), self.max_rows)
        mine = [r for r in self.rows if q.get("user_id") == "eq." + r["user_id"]]
        return JSONResponse(200, {}, mine[offset:offset + limit])


class StorePaginationTests(unittest.TestCase):
    """Fix 4: weekly usage sum and export are not truncated at max-rows."""

    def _store(self):
        from lofgren_intelligence.hosted import store as li_store
        return li_store, li_store.SupabaseStore("https://db.example", "service-role-test", "publishable-test")

    def test_weekly_usage_sum_is_not_truncated_by_max_rows(self):
        li_store, store = self._store()
        rows = [{"user_id": "u1", "id": str(i), "units": 1} for i in range(2500)]
        with patch.object(li_store, "json_request", _PostgREST(rows)):
            self.assertEqual(store.usage_units_since("u1", "2026-01-01T00:00:00+00:00"), 2500.0)

    def test_paging_survives_a_smaller_server_max_rows(self):
        li_store, store = self._store()
        rows = [{"user_id": "u1", "id": str(i), "units": 1} for i in range(1234)]
        with patch.object(li_store, "json_request", _PostgREST(rows, max_rows=100)):
            self.assertEqual(len(store.list_usage("u1")), 1234)
            self.assertEqual(store.usage_units_since("u1", "2026-01-01T00:00:00+00:00"), 1234.0)

    def test_account_export_lists_are_complete_and_tenant_scoped(self):
        li_store, store = self._store()
        rows = [{"user_id": "u1", "run_id": f"R{i}"} for i in range(1500)]
        rows += [{"user_id": "u2", "run_id": "OTHER"}]
        with patch.object(li_store, "json_request", _PostgREST(rows)):
            got = store.list_runs("u1")
        self.assertEqual(len(got), 1500)
        self.assertNotIn("OTHER", {r["run_id"] for r in got})


class QuotaPolicyTests(unittest.TestCase):
    """Fix 4: an explicit stored quota of 0 blocks instead of granting the plan default."""

    def test_explicit_zero_quota_blocks_instead_of_granting_plan_default(self):
        from lofgren_intelligence.hosted.entitlements import access_for_run
        decision = access_for_run(FakeStore(kind="founding_free", quota=0.0), "u1", 1.0)
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.quota_units, 0.0)


class StoreErrorTranslationTests(unittest.TestCase):
    """Fix 8: database failures become StoreError without server detail."""

    def _raising(self, exc):
        def fake(*a, **k):
            raise exc
        return fake

    def test_postgrest_failure_becomes_store_error_without_server_detail(self):
        from lofgren_intelligence.hosted import store as li_store
        from lofgren_intelligence.hosted.http import HTTPError
        detail = 'relation "public.li_runs" violates constraint li_runs_pkey'
        store = li_store.SupabaseStore("https://db.example", "service-role-test", "publishable-test")
        with patch.object(li_store, "json_request", self._raising(HTTPError(409, detail, {"message": detail}))):
            with self.assertRaises(li_store.StoreError) as ctx:
                store.get_run("u1", "RR-1")
        self.assertIn("409", str(ctx.exception))
        self.assertNotIn("li_runs", str(ctx.exception))

    def test_unreachable_database_and_rpc_failures_are_store_errors(self):
        from lofgren_intelligence.hosted import store as li_store
        store = li_store.SupabaseStore("https://db.example", "service-role-test", "publishable-test")
        with patch.object(li_store, "json_request", self._raising(urllib.error.URLError("refused"))):
            for call in (lambda: store.take_rate_limit("u1"), lambda: store.delete_auth_user("u1"),
                         lambda: store.reserve_usage("r1", "u1", "investigate", 1.0)):
                with self.assertRaises(li_store.StoreError):
                    call()


def _tle():
    from pathlib import Path
    return (Path(__file__).resolve().parent.parent / "examples" / "sample.tle").read_text(encoding="utf-8")


class SatellitePassBoundsTests(unittest.TestCase):
    """Fix 6: satellite_passes work per call is bounded."""

    def test_unbounded_window_and_bad_coordinates_are_refused(self):
        from lofgren_intelligence.hosted.service import PublicServiceError
        service = PublicService(FakeStore())
        for bad in ({"hours": 1e7}, {"hours": 0}, {"hours": float("nan")}, {"lat": 91},
                    {"lon": -181}, {"min_elevation_deg": 120}):
            args = {"lat": 33.4, "lon": -112.0, "tle_text": _tle(), **bad}
            with self.subTest(bad=bad), self.assertRaises(PublicServiceError):
                service.satellite_passes("u1", args)

    def test_too_many_element_sets_are_refused(self):
        from lofgren_intelligence.hosted.service import PublicServiceError
        with self.assertRaises(PublicServiceError):
            PublicService(FakeStore()).satellite_passes("u1", {"lat": 0, "lon": 0, "tle_text": _tle() * 11})

    def test_bounded_request_still_predicts(self):
        out = PublicService(FakeStore()).satellite_passes(
            "u1", {"lat": 33.4, "lon": -112.0, "hours": 24, "min_elevation_deg": 10, "tle_text": _tle()})
        self.assertIn("passes", out)


class SatellitePassEntitlementTests(unittest.TestCase):
    """Fix 6: satellite_passes needs an active entitlement with quota left."""

    def _args(self):
        return {"lat": 33.4, "lon": -112.0, "hours": 6, "tle_text": _tle()}

    def test_unpaid_account_cannot_run_pass_prediction(self):
        from lofgren_intelligence.hosted.service import PaymentRequired
        store = FakeStore(activation_number=1001, kind="paid_required", quota=0.0)
        with self.assertRaises(PaymentRequired):
            PublicService(store).satellite_passes("u1", self._args())

    def test_exhausted_quota_refuses_pass_prediction(self):
        from lofgren_intelligence.hosted.service import QuotaExceeded
        store = FakeStore(quota=10.0)
        store.record_usage({"id": "e1", "user_id": "u1", "run_id": None, "units": 10.0})
        with self.assertRaises(QuotaExceeded):
            PublicService(store).satellite_passes("u1", self._args())


class AdHocComputeQuotaTests(unittest.TestCase):
    """Fix 7: ad hoc discovery compute is refused once quota or entitlement lapsed."""

    CALLS = {
        "simulate_candidate": {"run_id": "RR-1", "model": {}, "parameters": {}},
        "analyze_sensitivity": {"run_id": "RR-1", "model": {}, "parameters": {}},
        "optimize_solution": {"run_id": "RR-1", "problem": {}},
        "find_prior_art": {"run_id": "RR-1", "subject": "x", "queries": ["x"], "records": [], "coverage": {}},
    }

    def _assert_refused(self, store, exc):
        service = PublicService(store)
        for name, args in self.CALLS.items():
            with self.subTest(tool=name), self.assertRaises(exc):
                getattr(service, name)("u1", dict(args))

    def test_exhausted_weekly_quota_refuses_unmetered_compute(self):
        from lofgren_intelligence.hosted.service import QuotaExceeded
        store = FakeStore(quota=10.0)
        store.record_usage({"id": "e1", "user_id": "u1", "run_id": None, "units": 10.0})
        self._assert_refused(store, QuotaExceeded)

    def test_unpaid_account_is_refused_before_any_compute(self):
        from lofgren_intelligence.hosted.service import PaymentRequired
        self._assert_refused(FakeStore(activation_number=1001, kind="paid_required", quota=0.0), PaymentRequired)


class PublicDocsTruthfulnessTests(unittest.TestCase):
    """Fix 11: public docs and pages describe what this line actually hosts and enforces."""

    def _doc(self):
        from pathlib import Path
        return (Path(__file__).resolve().parent.parent / "docs" / "PUBLIC_MCP.md").read_text(encoding="utf-8")

    def test_public_mcp_doc_matches_the_hosted_v1_v6_surface(self):
        doc = self._doc()
        self.assertNotIn("V2 is partial", doc)
        self.assertNotIn("V3–V6 are not public capability", doc)
        for tool in ("build_artifact", "propose_action", "execute_action", "measure_outcome",
                     "evaluate_improvement"):
            self.assertIn(tool, doc)

    def test_public_mcp_doc_states_the_hardening_rules(self):
        doc = self._doc()
        for rule in ("Approve and", "redirect host", "exactly\n`localhost`", "payment_status",
                     "current status", "100.64.0.0/10", "168 hours", "li_reserve_usage",
                     "explicit stored weekly quota of 0", "max-rows"):
            self.assertIn(rule, doc)

    def test_landing_and_checkout_return_stay_truthful(self):
        from lofgren_intelligence.hosted.journey import checkout_return_html, landing_html
        page = landing_html("https://li.example")
        self.assertIn("Public readiness is a separate gate", page)
        self.assertIn("does not prove payment", checkout_return_html())


class _RegisterStore:
    def __init__(self, *a, **k):
        pass

    def put_oauth_client(self, row):
        pass


class UnauthenticatedRateLimitTests(unittest.TestCase):
    """Open item O4: per-IP (per-instance) limits on unauthenticated endpoints."""

    def setUp(self):
        from lofgren_intelligence.hosted import ratelimit
        ratelimit.LIMITER.reset()
        self.addCleanup(ratelimit.LIMITER.reset)

    def _register(self, handler, ip="203.0.113.9", headers=None):
        body = json.dumps({"redirect_uris": ["https://client.example/cb"]}).encode()
        req = _request("/oauth/register", method="POST", client=ip, body=body)
        if headers:
            req.scope["headers"] = [(k.encode(), v.encode()) for k, v in headers.items()]
        with patch.dict(os.environ, ENV), patch.object(web_app, "SupabaseStore", _RegisterStore):
            return asyncio.run(handler(req))

    def test_client_registration_is_limited_per_ip(self):
        from lofgren_intelligence.hosted.ratelimit import rate_limited
        handler = rate_limited("oauth_register", web_app.oauth_register)
        codes = [self._register(handler).status_code for _ in range(11)]
        self.assertEqual(codes[:10], [201] * 10)
        self.assertEqual(codes[10], 429)
        limited = self._register(handler)
        self.assertGreaterEqual(int(limited.headers["retry-after"]), 1)
        # Another client is unaffected.
        self.assertEqual(self._register(handler, ip="198.51.100.7").status_code, 201)

    def test_forwarded_header_is_ignored_unless_the_operator_trusts_it(self):
        from lofgren_intelligence.hosted.ratelimit import rate_limited
        handler = rate_limited("oauth_register", web_app.oauth_register)
        for i in range(10):
            # A client rotating X-Forwarded-For does not escape the limit by default.
            self.assertEqual(self._register(handler, headers={"x-forwarded-for": f"10.0.0.{i}"}).status_code, 201)
        self.assertEqual(self._register(handler, headers={"x-forwarded-for": "10.0.1.1"}).status_code, 429)
        with patch.dict(os.environ, {"LI_CLIENT_IP_HEADER": "x-real-ip"}):
            self.assertEqual(self._register(handler, headers={"x-real-ip": "192.0.2.44"}).status_code, 201)

    def test_window_slides(self):
        from lofgren_intelligence.hosted.ratelimit import SlidingWindowLimiter
        now = [1000.0]
        limiter = SlidingWindowLimiter(clock=lambda: now[0])
        self.assertEqual([limiter.take("b", "ip", 2, 60) for _ in range(2)], [0.0, 0.0])
        self.assertAlmostEqual(limiter.take("b", "ip", 2, 60), 60.0)
        now[0] += 60.0
        self.assertEqual(limiter.take("b", "ip", 2, 60), 0.0)

    def test_limit_is_configurable_per_bucket(self):
        from lofgren_intelligence.hosted.ratelimit import rate_limited
        handler = rate_limited("oauth_register", web_app.oauth_register)
        with patch.dict(os.environ, {"LI_IP_RATE_LIMIT_OAUTH_REGISTER": "2"}):
            codes = [self._register(handler).status_code for _ in range(3)]
        self.assertEqual(codes, [201, 201, 429])

    def test_app_routes_wrap_every_unauthenticated_write_and_lookup(self):
        import inspect
        source = inspect.getsource(web_app.build_app)
        for bucket, handler in (("oauth_register", "oauth_register"), ("oauth_authorize", "oauth_authorize"),
                                ("oauth_complete", "oauth_complete"), ("oauth_token", "oauth_token"),
                                ("actions", "action_details"), ("actions", "action_approve"),
                                ("account", "account_export"), ("account", "account_delete")):
            self.assertIn(f'rate_limited("{bucket}", {handler})', source)


if __name__ == "__main__":
    unittest.main()
