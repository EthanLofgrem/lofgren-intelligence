"""Regression tests for defects found by the public-readiness audit."""

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
from lofgren_intelligence.hosted.service import PublicService
from lofgren_intelligence.hosted.stripe import StripeAPIError, StripeError

from .test_public_hosted import FakeStore

ENV = {
    "LI_PUBLIC_BASE_URL": "https://li.example",
    "SUPABASE_URL": "https://project.supabase.example",
    "SUPABASE_PUBLISHABLE_KEY": "publishable-test-key",
}


def _get(handler, path, query=""):
    scope = {
        "type": "http", "method": "GET", "path": path, "raw_path": path.encode(),
        "query_string": query.encode(), "headers": [], "scheme": "https",
        "server": ("li.example", 443), "client": ("203.0.113.9", 1234), "root_path": "",
    }
    return asyncio.run(handler(Request(scope)))


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
    def _page(self):
        query = (
            "response_type=code&client_id=licl_attacker&redirect_uri=https%3A%2F%2Fevil.example%2Fcb"
            "&code_challenge=" + "A" * 43 + "&code_challenge_method=S256&state=s"
        )
        with patch.dict(os.environ, ENV), patch.object(web_app, "SupabaseStore", _ClientStore):
            resp = _get(web_app.oauth_authorize, "/oauth/authorize", query)
        self.assertEqual(resp.status_code, 200)
        return resp.body.decode("utf-8")

    def test_existing_session_never_completes_authorization_without_a_click(self):
        page = self._page()
        # The only call sites of complete() must sit inside click handlers.
        start = page.index("async function existing()")
        existing = page[start:page.index("\n", start)]
        self.assertNotIn("await complete(x.data.session)", existing)
        self.assertIn("cont.onclick=", existing)
        self.assertIn('<button id="continue" hidden>', page)

    def test_consent_names_client_and_redirect_host_escaped(self):
        page = self._page()
        self.assertIn("&lt;b&gt;Claude&lt;/b&gt;", page)
        self.assertNotIn("<b>Claude</b>", page)
        self.assertIn("<strong>evil.example</strong>", page)


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
    PHRASE = "DELETE MY LOFGREN INTELLIGENCE ACCOUNT"

    def _paid_store(self):
        store = FakeStore(activation_number=1001, kind="paid", quota=2000)
        store.entitlement.update({"stripe_subscription_id": "sub_test", "stripe_customer_id": "cus_test"})
        return store

    def _delete(self, store, fake):
        with patch.dict(os.environ, {"STRIPE_SECRET_KEY": "sk_test_dummy"}), patch.object(li_stripe.urllib.request, "urlopen", fake):
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


if __name__ == "__main__":
    unittest.main()
