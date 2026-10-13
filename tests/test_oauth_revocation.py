"""OAuth token revocation (RFC 7009), refresh rotation and replay, keyed limiter digests, consent CSP.

Ported onto the OAuth schema already applied on staging (20261012060000_oauth_disconnect,
20261012202113_oauth_revocation_replay, 20261013050420_oauth_issuance_expiry) plus the one
forward migration 20261013190000_li_oauth_refresh_replay. The unit of revocation is a user's
connection of one client (li_disconnect_oauth_client), not a separate token family.

1. POST /oauth/revoke: public client, client_id required (401 invalid_client when missing or
   unknown), refresh or access token, optional token_type_hint; always 200 for unknown,
   expired, already-revoked and other clients' tokens; a valid token revokes the user's
   connection of that client; advertised in the metadata; rate-limited (oauth_revoke);
   on the mcp surface allowlist; no-store; a revoked access token gets 401 on /mcp.
2. Refresh rotation: a successful refresh revokes the access token issued with the consumed
   refresh token; presenting a consumed refresh token again (replay) or another client's
   refresh token revokes the client connection.
3. Limiter keys are HMAC-SHA256 under LI_RATE_LIMIT_HMAC_KEY; a missing key fails
   closed for sign-up/activation buckets, degrades ordinary buckets to the per-instance
   limiter, and /readyz reports it without naming any value.
4. The consent page carries no inline style attributes (its CSP allows only the nonce'd
   <style> block).
5. An unknown or missing client_id at the token endpoint is 401 invalid_client (RFC 6749 5.2).

The SQL side (li_issue_oauth_tokens, li_oauth_refresh_replay, li_disconnect_oauth_client,
li_revoke_oauth_token) is proven on real PostgreSQL in tests/pg/test_migrations_pg.py
(OAuthAndBillingTest, OAuthRefreshReplayTest). No test here makes a network call.
"""

from __future__ import annotations

import os
import re
import unittest
from pathlib import Path
from unittest.mock import patch

from starlette.testclient import TestClient

from lofgren_intelligence.hosted import ratelimit
from lofgren_intelligence.hosted import web_app
from lofgren_intelligence.hosted.auth import AuthError, OAuthService, code_challenge_s256, token_hash
from lofgren_intelligence.hosted.journey import CONSENT_CSS, consent_intro_html

from .test_public_hosted import FakeStore

ROOT = Path(__file__).resolve().parents[1]
BASE = "https://li.example"
REDIRECT = "https://client.example/cb"
VERIFIER = "v" * 64
# Dummy test values, not secrets.
TEST_HMAC_KEY = "test-only-rate-limit-hmac-key-not-a-secret"
ENV = {
    "LI_PUBLIC_BASE_URL": BASE,
    "SUPABASE_URL": "https://project.supabase.example",
    "SUPABASE_SERVICE_ROLE_KEY": "service-role-test",
    "SUPABASE_PUBLISHABLE_KEY": "publishable-test-key",
    "LI_RELEASE_SHA": "0" * 40,
    "LI_RATE_LIMIT_BACKEND": "memory",
    "LI_RATE_LIMIT_HMAC_KEY": TEST_HMAC_KEY,
}
MCP_HEADERS = {"accept": "application/json, text/event-stream"}
TOOLS_LIST = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}


def _env(values, drop=()):
    env = {k: v for k, v in os.environ.items() if k not in drop and not k.startswith("LI_IP_RATE_LIMIT_")}
    env.update(values)
    return patch.dict(os.environ, env, clear=True)


class _AppCase(unittest.TestCase):
    env = ENV
    surface = "full"

    def setUp(self):
        ratelimit.LIMITER.reset()
        self.addCleanup(ratelimit.LIMITER.reset)
        ctx = _env({**self.env, "LI_DEPLOYMENT_SURFACE": self.surface})
        ctx.start()
        self.addCleanup(ctx.stop)
        self.store = FakeStore()
        for target in ("lofgren_intelligence.hosted.web_app.SupabaseStore",
                       "lofgren_intelligence.hosted.mcp_sdk.SupabaseStore",
                       "lofgren_intelligence.hosted.service.SupabaseStore"):
            try:
                p = patch(target, lambda *a, **k: self.store)
                p.start()
            except AttributeError:
                continue
            self.addCleanup(p.stop)
        # Entered so the app lifespan runs (the MCP session manager needs it).
        self.client = TestClient(web_app.build_app(), base_url=BASE, raise_server_exceptions=False)
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)
        resp = self.client.post("/oauth/register", json={"redirect_uris": [REDIRECT]})
        self.assertEqual(resp.status_code, 201, resp.text)
        self.client_id = resp.json()["client_id"]

    def authorize(self, client_id=None):
        client_id = client_id or self.client_id
        resp = self.client.post("/oauth/authorize/complete", headers={"authorization": "Bearer supabase-session"},
                                json={"client_id": client_id, "redirect_uri": REDIRECT,
                                      "code_challenge": code_challenge_s256(VERIFIER), "scope": "mcp",
                                      "resource": BASE + "/mcp", "state": "s"})
        self.assertEqual(resp.status_code, 200, resp.text)
        code = re.search(r"[?&]code=([^&]+)", resp.json()["redirect_url"]).group(1)
        resp = self.client.post("/oauth/token", data={
            "grant_type": "authorization_code", "code": code, "code_verifier": VERIFIER,
            "client_id": client_id, "redirect_uri": REDIRECT})
        self.assertEqual(resp.status_code, 200, resp.text)
        return resp.json()

    def refresh(self, refresh_token, client_id=None):
        return self.client.post("/oauth/token", data={
            "grant_type": "refresh_token", "refresh_token": refresh_token,
            "client_id": client_id or self.client_id})

    def revoke(self, data):
        return self.client.post("/oauth/revoke", data=data)

    def mcp_status(self, access_token):
        return self.client.post("/mcp", json=TOOLS_LIST,
                                headers={**MCP_HEADERS, "authorization": "Bearer " + access_token}).status_code

    def access_row(self, token):
        return self.store.access_tokens[token_hash(token)]

    def refresh_row(self, token):
        return self.store.refresh_tokens[token_hash(token)]


class RevocationEndpointTests(_AppCase):
    def test_metadata_advertises_the_revocation_endpoint(self):
        meta = self.client.get("/.well-known/oauth-authorization-server").json()
        self.assertEqual(meta["revocation_endpoint"], BASE + "/oauth/revoke")
        self.assertEqual(meta["revocation_endpoint_auth_methods_supported"], ["none"])

    def test_a_working_access_token_reaches_mcp(self):
        tokens = self.authorize()
        self.assertEqual(self.mcp_status(tokens["access_token"]), 200)

    def test_revoking_a_refresh_token_revokes_the_client_connection_and_mcp_answers_401(self):
        tokens = self.authorize()
        resp = self.revoke({"token": tokens["refresh_token"], "client_id": self.client_id,
                            "token_type_hint": "refresh_token"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.content, b"")
        self.assertTrue(self.access_row(tokens["access_token"])["revoked_at"])
        self.assertNotIn(token_hash(tokens["refresh_token"]), self.store.refresh_tokens)
        self.assertEqual(self.mcp_status(tokens["access_token"]), 401)
        resp = self.refresh(tokens["refresh_token"])
        self.assertEqual((resp.status_code, resp.json()["error"]), (400, "invalid_grant"))

    def test_revoking_an_access_token_gives_401_on_mcp_and_revokes_the_refresh_token_too(self):
        tokens = self.authorize()
        self.assertEqual(self.mcp_status(tokens["access_token"]), 200)
        resp = self.revoke({"token": tokens["access_token"], "client_id": self.client_id,
                            "token_type_hint": "access_token"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self.mcp_status(tokens["access_token"]), 401)
        # RFC 7009 2.1 allows (and the applied schema does) revoking the whole connection of
        # the client: the refresh token stops working too.
        resp = self.refresh(tokens["refresh_token"])
        self.assertEqual((resp.status_code, resp.json()["error"]), (400, "invalid_grant"))

    def test_a_wrong_or_missing_hint_still_finds_the_token(self):
        for hint in ("access_token", "unknown_kind", None):
            with self.subTest(hint=hint):
                tokens = self.authorize()
                data = {"token": tokens["refresh_token"], "client_id": self.client_id}
                if hint:
                    data["token_type_hint"] = hint
                self.assertEqual(self.revoke(data).status_code, 200)
                self.assertEqual(self.mcp_status(tokens["access_token"]), 401)

    def test_unknown_and_already_revoked_tokens_answer_200(self):
        tokens = self.authorize()
        self.assertEqual(self.revoke({"token": "lir_unknown", "client_id": self.client_id}).status_code, 200)
        for _ in range(2):
            self.assertEqual(self.revoke({"token": tokens["access_token"], "client_id": self.client_id})
                             .status_code, 200)
        for _ in range(2):
            self.assertEqual(self.revoke({"token": tokens["refresh_token"], "client_id": self.client_id})
                             .status_code, 200)

    def test_another_clients_token_is_not_revoked(self):
        tokens = self.authorize()
        other = self.client.post("/oauth/register", json={"redirect_uris": [REDIRECT]}).json()["client_id"]
        for token in (tokens["access_token"], tokens["refresh_token"]):
            self.assertEqual(self.revoke({"token": token, "client_id": other}).status_code, 200)
        self.assertFalse(self.access_row(tokens["access_token"]).get("revoked_at"))
        self.assertFalse(self.refresh_row(tokens["refresh_token"]).get("revoked_at"))
        self.assertEqual(self.mcp_status(tokens["access_token"]), 200)

    def test_client_identification_and_malformed_requests(self):
        tokens = self.authorize()
        cases = (
            ({"token": tokens["access_token"]}, 401, "invalid_client"),
            ({"token": tokens["access_token"], "client_id": "licl_unknown"}, 401, "invalid_client"),
            ({"client_id": self.client_id}, 400, "invalid_request"),
            ({"token": "", "client_id": self.client_id}, 400, "invalid_request"),
        )
        for data, status, code in cases:
            with self.subTest(data=sorted(data)):
                resp = self.revoke(data)
                self.assertEqual((resp.status_code, resp.json()["error"]), (status, code))
        resp = self.client.post("/oauth/revoke", content=b"token=a&token=b&client_id=" + self.client_id.encode(),
                                headers={"content-type": "application/x-www-form-urlencoded"})
        self.assertEqual((resp.status_code, resp.json()["error"]), (400, "invalid_request"))
        resp = self.client.post("/oauth/revoke", content=b"x" * 100_001,
                                headers={"content-type": "application/x-www-form-urlencoded"})
        self.assertEqual(resp.status_code, 413)
        # Nothing was revoked by a refused request.
        self.assertEqual(self.mcp_status(tokens["access_token"]), 200)

    def test_responses_are_no_store(self):
        tokens = self.authorize()
        for data in ({"token": tokens["access_token"], "client_id": self.client_id},
                     {"token": "x", "client_id": "licl_unknown"}, {"client_id": self.client_id}):
            resp = self.revoke(data)
            self.assertEqual(resp.headers["cache-control"], "no-store")
            self.assertEqual(resp.headers["pragma"], "no-cache")
        self.assertEqual(web_app._public_headers("/oauth/revoke", [(b"cache-control", b"public")]),
                         [(b"cache-control", b"no-store"), (b"pragma", b"no-cache")])

    def test_revocation_is_rate_limited_like_the_token_endpoint(self):
        self.assertEqual(ratelimit.DEFAULT_LIMITS["oauth_revoke"], ratelimit.DEFAULT_LIMITS["oauth_token"])
        self.assertNotIn("oauth_revoke", ratelimit.FAIL_CLOSED_BUCKETS)
        with patch.dict(os.environ, {"LI_IP_RATE_LIMIT_OAUTH_REVOKE": "2"}):
            codes = [self.revoke({"token": "x", "client_id": self.client_id}).status_code for _ in range(3)]
        self.assertEqual(codes, [200, 200, 429])

    def test_store_outage_is_503_not_a_success(self):
        from lofgren_intelligence.hosted.store import StoreUnavailable

        def down(*a, **k):
            raise StoreUnavailable("database is unreachable")
        self.store.revoke_oauth_token = down
        resp = self.revoke({"token": "x", "client_id": self.client_id})
        self.assertEqual((resp.status_code, resp.json()["error"]), (503, "temporarily_unavailable"))
        self.assertNotIn("database", resp.text)


class McpSurfaceRevocationTests(_AppCase):
    surface = "mcp"

    def test_the_mcp_surface_serves_revocation(self):
        tokens = self.authorize()
        self.assertEqual(self.revoke({"token": tokens["access_token"], "client_id": self.client_id}).status_code,
                         200)
        self.assertEqual(self.mcp_status(tokens["access_token"]), 401)
        self.assertEqual(self.client.get("/.well-known/oauth-authorization-server").json()["revocation_endpoint"],
                         BASE + "/oauth/revoke")


class RefreshRotationTests(_AppCase):
    def test_each_access_token_records_the_refresh_token_issued_with_it(self):
        tokens = self.authorize()
        self.assertEqual(self.access_row(tokens["access_token"])["refresh_token_hash"],
                         token_hash(tokens["refresh_token"]))
        rotated = self.refresh(tokens["refresh_token"]).json()
        self.assertEqual(self.access_row(rotated["access_token"])["refresh_token_hash"],
                         token_hash(rotated["refresh_token"]))

    def test_a_successful_refresh_revokes_the_previous_access_token(self):
        tokens = self.authorize()
        resp = self.refresh(tokens["refresh_token"])
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(resp.headers["cache-control"], "no-store")
        new = resp.json()
        self.assertEqual(self.mcp_status(tokens["access_token"]), 401)
        self.assertEqual(self.mcp_status(new["access_token"]), 200)
        # Another authorization of the same user keeps its own access token.
        other = self.authorize()
        self.refresh(new["refresh_token"])
        self.assertEqual(self.mcp_status(other["access_token"]), 200)

    def test_replaying_a_used_refresh_token_revokes_the_client_connection(self):
        tokens = self.authorize()
        rotated = self.refresh(tokens["refresh_token"]).json()
        self.assertEqual(self.mcp_status(rotated["access_token"]), 200)
        replay = self.refresh(tokens["refresh_token"])
        self.assertEqual((replay.status_code, replay.json()["error"]), (400, "invalid_grant"))
        # The current refresh token and the current access token are revoked.
        self.assertEqual(self.mcp_status(rotated["access_token"]), 401)
        resp = self.refresh(rotated["refresh_token"])
        self.assertEqual((resp.status_code, resp.json()["error"]), (400, "invalid_grant"))
        self.assertFalse([r for r in self.store.refresh_tokens.values() if r["client_id"] == self.client_id])
        self.assertFalse(self.store.consumed_refresh, "disconnect clears the replay tombstones")
        # A fresh authorization still works, and the old token cannot touch it.
        again = self.authorize()
        self.assertEqual(self.refresh(tokens["refresh_token"]).status_code, 400)
        self.assertEqual(self.mcp_status(again["access_token"]), 200)

    def test_another_client_presenting_the_refresh_token_revokes_the_owner_connection(self):
        tokens = self.authorize()
        other = self.client.post("/oauth/register", json={"redirect_uris": [REDIRECT]}).json()["client_id"]
        resp = self.refresh(tokens["refresh_token"], client_id=other)
        self.assertEqual((resp.status_code, resp.json()["error"]), (400, "invalid_grant"))
        self.assertEqual(self.mcp_status(tokens["access_token"]), 401)
        self.assertEqual(self.refresh(tokens["refresh_token"]).status_code, 400)

    def test_a_legacy_access_token_without_a_sibling_hash_expires_on_its_own(self):
        tokens = self.authorize()
        self.access_row(tokens["access_token"]).pop("refresh_token_hash")
        rotated = self.refresh(tokens["refresh_token"]).json()
        self.assertEqual(self.mcp_status(rotated["access_token"]), 200)
        # Written before the forward migration, it cannot be tied to its refresh token.
        self.assertFalse(self.access_row(tokens["access_token"]).get("revoked_at"))


class TokenEndpointClientAuthTests(_AppCase):
    """Item 5: an unknown client at the token endpoint is invalid_client / 401 (RFC 6749 5.2)."""

    def _expect(self, resp, status, code):
        self.assertEqual((resp.status_code, resp.json()["error"]), (status, code), resp.text)
        self.assertEqual(resp.headers["cache-control"], "no-store")

    def test_unknown_or_missing_client_is_invalid_client_401(self):
        tokens = self.authorize()
        for client in ("licl_unknown", ""):
            with self.subTest(client=client):
                self._expect(self.client.post("/oauth/token", data={
                    "grant_type": "refresh_token", "refresh_token": tokens["refresh_token"],
                    "client_id": client}), 401, "invalid_client")
                self._expect(self.client.post("/oauth/token", data={
                    "grant_type": "authorization_code", "code": "lic_x", "code_verifier": VERIFIER,
                    "client_id": client, "redirect_uri": REDIRECT}), 401, "invalid_client")
        # The refused requests consumed nothing: the refresh token still rotates.
        self.assertEqual(self.refresh(tokens["refresh_token"]).status_code, 200)

    def test_grant_problems_of_a_known_client_stay_invalid_grant(self):
        self._expect(self.refresh("lir_unknown"), 400, "invalid_grant")
        self._expect(self.client.post("/oauth/token", data={
            "grant_type": "authorization_code", "code": "lic_x", "code_verifier": VERIFIER,
            "client_id": self.client_id, "redirect_uri": REDIRECT}), 400, "invalid_grant")
        # An unregistered redirect for a known client is a grant problem, not client auth.
        self._expect(self.client.post("/oauth/token", data={
            "grant_type": "authorization_code", "code": "lic_x", "code_verifier": VERIFIER,
            "client_id": self.client_id, "redirect_uri": "https://other.example/cb"}), 400, "invalid_grant")


class OAuthServiceRevokeTests(unittest.TestCase):
    def test_unknown_client_is_refused_before_any_store_write(self):
        store = FakeStore()
        with self.assertRaises(AuthError):
            OAuthService(store).revoke(token="lit_x", client_id="licl_unknown")

    def test_replay_answer_is_true_only_for_a_boolean_true(self):
        from lofgren_intelligence.hosted.store import SupabaseStore
        store = SupabaseStore("https://db.example", "service-role-test")
        for answer, expected in ((True, True), (False, False), (None, False), ("true", False), (1, False),
                                 ([True], False), ({"x": 1}, False)):
            with self.subTest(answer=answer), patch.object(store, "rpc", return_value=answer):
                self.assertIs(store.oauth_refresh_replay("a" * 64, "licl_x"), expected)
        with patch.object(store, "rpc", return_value=True) as rpc:
            store.oauth_refresh_replay("a" * 64, "licl_x")
        name, body = rpc.call_args.args
        self.assertEqual((name, sorted(body)), ("li_oauth_refresh_replay", ["p_client_id", "p_token_hash"]))

    def test_refresh_sends_only_hashes_to_the_store(self):
        store = FakeStore()
        oauth = OAuthService(store)
        client = oauth.register_client({"redirect_uris": [REDIRECT]})["client_id"]
        raw = "lir_" + "r" * 43
        store.put_refresh_token({"token_hash": token_hash(raw), "user_id": "u1", "client_id": client,
                                 "scope": "mcp", "resource": BASE + "/mcp",
                                 "expires_at": "2999-01-01T00:00:00+00:00"})
        seen = []
        for name in ("consume_refresh_token", "issue_oauth_tokens", "oauth_refresh_replay"):
            real = getattr(store, name)

            def spy(*args, _real=real, _name=name):
                seen.append((_name, args))
                return _real(*args)
            setattr(store, name, spy)
        new = oauth.refresh(refresh_token=raw, client_id=client)
        with self.assertRaises(AuthError):
            oauth.refresh(refresh_token=raw, client_id=client)  # replay
        raws = {raw, new["refresh_token"], new["access_token"]}
        flat = repr(seen)
        for value in raws:
            self.assertNotIn(value, flat)
        issued = next(args for name, args in seen if name == "issue_oauth_tokens")
        self.assertEqual(issued[1], token_hash(raw))
        self.assertEqual((issued[2]["token_hash"], issued[3]["token_hash"]),
                         (token_hash(new["access_token"]), token_hash(new["refresh_token"])))
        self.assertIn(("oauth_refresh_replay", (token_hash(raw), client)), seen)


class KeyedLimiterDigestTests(unittest.TestCase):
    def setUp(self):
        ratelimit.LIMITER.reset()
        self.addCleanup(ratelimit.LIMITER.reset)

    def test_digest_is_hmac_under_the_env_secret(self):
        import hashlib
        import hmac
        with _env({"LI_RATE_LIMIT_HMAC_KEY": TEST_HMAC_KEY}):
            digest = ratelimit.keyed_rate_key("activation_ip", "198.51.100.1")
        expected = hmac.new(TEST_HMAC_KEY.encode(), b"activation_ip:198.51.100.1", hashlib.sha256).hexdigest()
        self.assertEqual(digest, expected)
        self.assertNotEqual(digest, hashlib.sha256(b"activation_ip:198.51.100.1").hexdigest())
        with _env({"LI_RATE_LIMIT_HMAC_KEY": TEST_HMAC_KEY + "-other"}):
            self.assertNotEqual(ratelimit.keyed_rate_key("activation_ip", "198.51.100.1"), digest)

    def test_missing_or_short_key_raises(self):
        for value in (None, "", "short", " " * 40, "x" * 31):
            with self.subTest(value=value):
                values = {} if value is None else {"LI_RATE_LIMIT_HMAC_KEY": value}
                with _env(values, drop=("LI_RATE_LIMIT_HMAC_KEY",)):
                    self.assertFalse(ratelimit.rate_limit_key_configured())
                    with self.assertRaises(ratelimit.RateLimitKeyMissing):
                        ratelimit.keyed_rate_key("oauth_token", "198.51.100.1")

    def test_missing_key_fails_closed_for_sign_up_buckets_and_sends_nothing(self):
        store = FakeStore()
        with _env({}, drop=("LI_RATE_LIMIT_HMAC_KEY",)), self.assertLogs("lofgren_intelligence.ratelimit", "ERROR"):
            for bucket in sorted(ratelimit.FAIL_CLOSED_BUCKETS - {"trial_ip", "trial_global", "trial_call_ip",
                                                                    "trial_spend"}):
                with self.subTest(bucket=bucket), self.assertRaises(ratelimit.RateLimitUnavailable):
                    ratelimit.take(bucket, "198.51.100.1", store=store)
        self.assertFalse(getattr(store, "rate_events", None))

    def test_missing_key_degrades_ordinary_buckets_to_the_per_instance_limiter(self):
        store = FakeStore()
        with _env({"LI_IP_RATE_LIMIT_OAUTH_TOKEN": "2"}, drop=("LI_RATE_LIMIT_HMAC_KEY",)), \
                self.assertLogs("lofgren_intelligence.ratelimit", "ERROR"):
            waits = [ratelimit.take("oauth_token", "198.51.100.1", store=store) for _ in range(3)]
        self.assertEqual(waits[:2], [0.0, 0.0])
        self.assertGreater(waits[2], 0)
        self.assertFalse(getattr(store, "rate_events", None))

    def test_activation_without_the_key_is_refused_503(self):
        from lofgren_intelligence.hosted.auth import ActivationRefused
        store = FakeStore()
        store.account = None  # a first authorization takes an activation
        store.verified_user = {"id": "u1", "email": "u@example.com", "email_confirmed_at": "2026-10-01T00:00:00Z"}
        oauth = OAuthService(store)
        client = oauth.register_client({"redirect_uris": [REDIRECT]})
        with _env({"LI_PUBLIC_BASE_URL": BASE}, drop=("LI_RATE_LIMIT_HMAC_KEY",)), \
                self.assertLogs("lofgren_intelligence.ratelimit", "ERROR"), \
                self.assertRaises(ActivationRefused) as refused:
            oauth.authorize_from_supabase_session(
                supabase_access_token="supabase-session", client_id=client["client_id"], redirect_uri=REDIRECT,
                code_challenge=code_challenge_s256(VERIFIER), scope="mcp", resource=BASE + "/mcp",
                client_ip="198.51.100.1")
        self.assertEqual((refused.exception.status, refused.exception.error), (503, "temporarily_unavailable"))
        self.assertFalse(store.oauth_codes)

    def test_readyz_reports_the_missing_key_generically(self):
        with _env({**ENV, "LI_RATE_LIMIT_HMAC_KEY": "too-short"}):
            client = TestClient(web_app.build_app(), base_url=BASE, raise_server_exceptions=False)
            with self.assertLogs("lofgren_intelligence.public", "ERROR") as logs:
                resp = client.get("/readyz")
        self.assertEqual(resp.status_code, 503)
        self.assertEqual(resp.json(), {"ready": False, "missing": ["rate_limit_key"]})
        self.assertNotIn("too-short", resp.text)
        self.assertNotIn("LI_", resp.text)
        self.assertNotIn("too-short", "\n".join(logs.output))
        self.assertIn("LI_RATE_LIMIT_HMAC_KEY", "\n".join(logs.output))

    def test_readyz_does_not_list_the_key_when_it_is_configured(self):
        with _env({k: v for k, v in ENV.items() if k != "SUPABASE_SERVICE_ROLE_KEY"},
                  drop=("SUPABASE_SERVICE_ROLE_KEY",)):
            client = TestClient(web_app.build_app(), base_url=BASE, raise_server_exceptions=False)
            with self.assertLogs("lofgren_intelligence.public", "ERROR"):
                resp = client.get("/readyz")
        self.assertEqual(resp.json(), {"ready": False, "missing": ["database"]})


class ConsentPageCspTests(unittest.TestCase):
    def test_consent_intro_has_no_inline_style_attributes(self):
        for args in (("Claude",), ("Claude", "mcp", REDIRECT)):
            page = consent_intro_html(*args)
            self.assertNotRegex(page, r"(?i)\sstyle\s*=")
            self.assertIn('class="consent-eyebrow"', page)
            self.assertIn('class="consent-fine"', page)

    def test_rendered_consent_page_has_no_style_attributes_and_nonced_rules(self):
        store = FakeStore()
        store.put_oauth_client({"client_id": "licl_c", "client_name": "Claude", "redirect_uris": [REDIRECT]})
        with _env(ENV):
            with patch.object(web_app, "SupabaseStore", lambda *a, **k: store):
                client = TestClient(web_app.build_app(), base_url=BASE)
                resp = client.get("/oauth/authorize", params={
                    "response_type": "code", "client_id": "licl_c", "redirect_uri": REDIRECT,
                    "code_challenge": "A" * 43, "code_challenge_method": "S256", "state": "s"})
        self.assertEqual(resp.status_code, 200, resp.text)
        page = resp.text
        # No inline style attribute anywhere (CSP style-src has no 'unsafe-inline').
        self.assertNotRegex(page, r"(?i)<[^>]*\sstyle\s*=")
        csp = resp.headers["content-security-policy"]
        nonce = re.search(r"style-src 'nonce-([^']+)'", csp).group(1)
        self.assertNotIn("unsafe-inline", csp)
        blocks = re.findall(r"<style([^>]*)>(.*?)</style>", page, re.S)
        self.assertEqual(len(blocks), 1)
        attrs, css = blocks[0]
        self.assertEqual(attrs.strip(), f'nonce="{nonce}"')
        self.assertIn(CONSENT_CSS, css)
        for cls in ("consent-eyebrow", "consent-fine"):
            self.assertIn("." + cls + "{", css)
            self.assertIn(f'class="{cls}"', page)


class MigrationContractTests(unittest.TestCase):
    NAME = "20261013190000_li_oauth_refresh_replay.sql"
    APPLIED = ("20261012060000_oauth_disconnect.sql", "20261012202113_oauth_revocation_replay.sql",
               "20261013050420_oauth_issuance_expiry.sql")

    def test_one_forward_migration_after_the_applied_oauth_schema(self):
        migrations = ROOT / "supabase" / "migrations"
        names = sorted(p.name for p in migrations.glob("*.sql"))
        self.assertEqual(names[-1], self.NAME)
        for applied in self.APPLIED:
            self.assertIn(applied, names)
            self.assertLess(applied, self.NAME)
        # No second, competing token-family schema.
        self.assertNotIn("20261013120000_li_oauth_token_families.sql", names)
        sql = (migrations / self.NAME).read_text(encoding="utf-8").lower()
        self.assertNotIn("grant_id", sql)
        self.assertNotIn("li_rotate_refresh_token", sql)
        for fn in ("li_issue_oauth_tokens(text,text,jsonb,jsonb)", "li_disconnect_oauth_client(uuid,text)",
                   "li_oauth_refresh_replay(text,text)"):
            self.assertIn(f"revoke all on function public.{fn} from public, anon, authenticated", sql)
            self.assertIn(f"grant execute on function public.{fn} to service_role", sql)
        self.assertEqual(sql.count("security definer"), 3)
        self.assertEqual(sql.count("set search_path = public"), 3)
        self.assertIn("pg_advisory_xact_lock", sql)
        self.assertIn("enable row level security", sql)


if __name__ == "__main__":
    unittest.main()
