"""Regression tests for the public-readiness audit fixes on the release line.

Ported from build/public-readiness-audit (tests/test_public_readiness_audit.py)
and adapted to release/public-v1-v6: hosted V3-V6 tools, the atomic
li_reserve_usage reservation path and the user-journey consent page.
No test here makes a network, Stripe or Supabase call.
"""

import asyncio
import os
import unittest
from unittest.mock import patch

from starlette.requests import Request

from lofgren_intelligence.hosted import web_app
from lofgren_intelligence.hosted.journey import consent_intro_html

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


if __name__ == "__main__":
    unittest.main()
