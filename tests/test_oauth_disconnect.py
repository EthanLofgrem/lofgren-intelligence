"""Offline revocation boundaries; real SQL atomicity is covered in tests/pg."""
import json
import os
import unittest
from unittest.mock import Mock, patch
from starlette.requests import Request
from lofgren_intelligence.hosted import web_app
from lofgren_intelligence.hosted.auth import AuthError, OAuthService, token_hash, code_challenge_s256
from tests.test_public_hosted import FakeStore
from lofgren_intelligence.hosted.store import StoreError

class OAuthDisconnectTests(unittest.IsolatedAsyncioTestCase):
    async def request(self, handler, body, authorization=None):
        headers = [] if authorization is None else [(b"authorization", authorization.encode())]
        scope = {"type": "http", "method": "POST", "path": "/oauth/revoke", "headers": headers}
        async def receive():
            return {"type": "http.request", "body": body, "more_body": False}
        return await handler(Request(scope, receive))

    async def test_revocation_never_passes_raw_token_to_store_and_returns_empty_success(self):
        store = Mock()
        store.get_oauth_client.return_value = {"client_id": "c"}
        with patch.object(web_app, "SupabaseStore", return_value=store):
            response = await self.request(web_app.oauth_revoke, b"token=private-token&client_id=c")
        store.revoke_oauth_token.assert_called_once_with(token_hash("private-token"), "c")
        self.assertEqual((response.status_code, response.body), (200, b""))
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertEqual(response.headers["pragma"], "no-cache")

    async def test_duplicate_malformed_missing_revocation_inputs_do_not_touch_storage(self):
        # A missing client_id is a client identification failure (RFC 6749 5.2: 401 invalid_client);
        # the other malformed requests are 400 invalid_request. None reaches the store.
        for body, status, error in ((b"token=a&token=b&client_id=c", 400, "invalid_request"),
                                    (b"\xff", 400, "invalid_request"), (b"client_id=c", 400, "invalid_request"),
                                    (b"token=a", 401, "invalid_client")):
            with self.subTest(body=body), patch.object(web_app, "SupabaseStore") as store:
                response = await self.request(web_app.oauth_revoke, body)
                self.assertEqual((response.status_code, json.loads(response.body)["error"]), (status, error))
                store.assert_not_called()

    async def test_unknown_client_is_401_invalid_client(self):
        store = Mock()
        store.get_oauth_client.return_value = None
        with patch.object(web_app, "SupabaseStore", return_value=store):
            response = await self.request(web_app.oauth_revoke, b"token=private-token&client_id=unknown")
        self.assertEqual((response.status_code, json.loads(response.body)["error"]), (401, "invalid_client"))
        store.revoke_oauth_token.assert_not_called()

    async def test_disconnect_requires_session_and_ignores_forged_user_id(self):
        store = Mock()
        store.verify_supabase_user.return_value = {"id": "actual-user"}
        body = json.dumps({"client_id": "c", "user_id": "victim"}).encode()
        with patch.object(web_app, "SupabaseStore", return_value=store):
            response = await self.request(web_app.oauth_disconnect, body)
            self.assertEqual(response.status_code, 401)
            store.verify_supabase_user.assert_not_called()
            response = await self.request(web_app.oauth_disconnect, body, "Bearer session")
        self.assertEqual(response.status_code, 200)
        store.disconnect_oauth_client.assert_called_once_with("actual-user", "c")

    async def test_invalid_supabase_session_cannot_disconnect(self):
        store = Mock()
        store.verify_supabase_user.side_effect = StoreError("invalid Supabase user session")
        with patch.object(web_app, "SupabaseStore", return_value=store):
            response = await self.request(web_app.oauth_disconnect, b'{"client_id":"c"}', "Bearer invalid")
        self.assertEqual(response.status_code, 401)
        store.disconnect_oauth_client.assert_not_called()

    def test_revoked_grant_cannot_issue_tokens_after_consume(self):
        store = Mock()
        store.issue_oauth_tokens.return_value = False
        oauth = OAuthService(store)
        with self.assertRaises(AuthError):
            oauth._issue_tokens("u", "c", "mcp", "https://li.example/mcp",
                                grant_kind="refresh_token", grant_hash="h")
        store.put_access_token.assert_not_called()
        store.put_refresh_token.assert_not_called()

    def test_revoke_invalidates_access_refresh_and_pending_codes_but_allows_new_consent(self):
        store = FakeStore()
        oauth = OAuthService(store)
        client = oauth.register_client({"redirect_uris": ["https://client.example/callback"]})["client_id"]
        verifier = "v" * 64
        def consent():
            with patch.dict(os.environ, {"LI_PUBLIC_BASE_URL": "https://li.example"}):
                return oauth.authorize_from_supabase_session(supabase_access_token="supabase-session", client_id=client,
                    redirect_uri="https://client.example/callback", code_challenge=code_challenge_s256(verifier),
                    scope="mcp", resource="https://li.example/mcp")
        def exchange(code):
            return oauth.exchange_code(code=code, code_verifier=verifier, client_id=client,
                                       redirect_uri="https://client.example/callback")
        tokens = exchange(consent())
        pending = consent()
        oauth.revoke(token=tokens["access_token"], client_id=client)
        with self.assertRaises(AuthError):
            oauth.authenticate(tokens["access_token"])
        with self.assertRaises(AuthError):
            oauth.refresh(refresh_token=tokens["refresh_token"], client_id=client)
        with self.assertRaises(AuthError):
            exchange(pending)
        new_tokens = exchange(consent())
        oauth.revoke(token=tokens["access_token"], client_id=client)
        self.assertEqual(oauth.authenticate(new_tokens["access_token"]).user_id, "u1")
        refreshed = oauth.refresh(refresh_token=new_tokens["refresh_token"], client_id=client)
        self.assertEqual(oauth.authenticate(refreshed["access_token"]).user_id, "u1")
