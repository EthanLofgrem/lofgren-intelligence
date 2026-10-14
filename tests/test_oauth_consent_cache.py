"""Authorization codes and activation refusals must never be cacheable."""
import json
import unittest
from unittest.mock import Mock, patch
from starlette.requests import Request
from lofgren_intelligence.hosted import web_app
from lofgren_intelligence.hosted.auth import ActivationRefused


class OAuthConsentCacheTests(unittest.IsolatedAsyncioTestCase):
    async def complete(self, oauth):
        body = json.dumps({"client_id": "client", "redirect_uri": "https://client.example/callback?existing=1",
                           "code_challenge": "a" * 43, "resource": "https://li.example/mcp",
                           "scope": "mcp", "state": "client-state"}).encode()
        scope = {"type": "http", "method": "POST", "path": "/oauth/authorize/complete",
                 "headers": [(b"authorization", b"Bearer supabase-session")], "client": ("192.0.2.1", 1)}
        async def receive():
            return {"type": "http.request", "body": body, "more_body": False}
        with patch.object(web_app, "SupabaseStore"), patch.object(web_app, "OAuthService", return_value=oauth):
            return await web_app.oauth_complete(Request(scope, receive))

    async def test_authorization_code_response_is_not_cacheable(self):
        oauth = Mock()
        oauth.authorize_from_supabase_session.return_value = "single-use-code"
        response = await self.complete(oauth)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(json.loads(response.body)["redirect_url"],
                         "https://client.example/callback?existing=1&code=single-use-code&state=client-state")
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertEqual(response.headers["pragma"], "no-cache")

    async def test_activation_refusal_is_not_cacheable_and_keeps_retry(self):
        oauth = Mock()
        oauth.authorize_from_supabase_session.side_effect = ActivationRefused("rate_limited", "retry later", 429, 30)
        response = await self.complete(oauth)
        self.assertEqual(response.status_code, 429)
        self.assertEqual(response.headers["retry-after"], "30")
        self.assertEqual(json.loads(response.body)["error"], "rate_limited")
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertEqual(response.headers["pragma"], "no-cache")
