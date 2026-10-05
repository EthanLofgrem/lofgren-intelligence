"""Regression tests for defects found by the public-readiness audit."""

import asyncio
import os
import unittest
from unittest.mock import patch

from starlette.requests import Request

from lofgren_intelligence.hosted import web_app

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


if __name__ == "__main__":
    unittest.main()
