"""Offline OAuth grant integrity; no hosted-client proof is implied."""
import os
import unittest
from unittest.mock import Mock, patch
from lofgren_intelligence.hosted.auth import AuthError, OAuthService, code_challenge_s256


class OAuthGrantBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.store = Mock()
        self.store.get_oauth_client.return_value = {"redirect_uris": ["https://client.example/callback"]}
        self.oauth = OAuthService(self.store)
        self.verifier = "a" * 64
        self.grant = {"user_id": "user-1", "client_id": "client-1", "scope": "mcp",
                      "resource": "https://li.example/mcp", "redirect_uri": "https://client.example/callback",
                      "code_challenge": code_challenge_s256(self.verifier)}

    def test_unsupported_scope_rejected_before_session_or_account_access(self):
        for scope in ("admin", "mcp admin", "mcp mcp", 123):
            with self.subTest(scope=scope), patch.dict(os.environ, {"LI_PUBLIC_BASE_URL": "https://li.example"}):
                with self.assertRaises(AuthError):
                    self.oauth.authorize_from_supabase_session(supabase_access_token="session", client_id="client-1",
                        redirect_uri=self.grant["redirect_uri"], code_challenge=self.grant["code_challenge"],
                        scope=scope, resource=self.grant["resource"])
        self.store.verify_supabase_user.assert_not_called()
        self.store.activate_account.assert_not_called()

    def test_malformed_code_and_refresh_grants_issue_no_tokens(self):
        for field in ("user_id", "scope", "resource"):
            for invalid in (None, "", 123):
                row = {**self.grant, field: invalid}
                self.store.consume_oauth_code.return_value = row
                self.store.consume_refresh_token.return_value = row
                with self.subTest(field=field, invalid=invalid):
                    with self.assertRaises(AuthError):
                        self.oauth.exchange_code(code="code", code_verifier=self.verifier, client_id="client-1",
                                                 redirect_uri=self.grant["redirect_uri"])
                    with self.assertRaises(AuthError):
                        self.oauth.refresh(refresh_token="refresh", client_id="client-1")
        self.store.put_access_token.assert_not_called()
        self.store.put_refresh_token.assert_not_called()
        self.store.issue_oauth_tokens.assert_not_called()

    def test_valid_grant_preserves_permissions(self):
        self.store.consume_refresh_token.return_value = self.grant
        result = self.oauth.refresh(refresh_token="refresh", client_id="client-1")
        self.assertEqual(result["scope"], "mcp")
        self.assertEqual(self.store.issue_oauth_tokens.call_args.args[2]["resource"], self.grant["resource"])

    def test_redirect_control_characters_and_non_strings_rejected(self):
        for uri in ("https://client.example/\ncallback", " https://client.example/callback", 123, None):
            with self.subTest(uri=uri), self.assertRaises(AuthError):
                self.oauth.register_client({"redirect_uris": [uri]})
        self.store.put_oauth_client.assert_not_called()
