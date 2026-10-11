"""Server misconfiguration and store outages answer 503, never a client 4xx, and never leak internals.

Live finding (2026-10-11): with SUPABASE_SERVICE_ROLE_KEY missing, /oauth/register and
/oauth/token answered 400 invalid_client_metadata / invalid_grant and echoed the secret's
name. RFC 6749 4.1.2.1 / RFC 7591 / MCP authorization: a server fault is 503 with
server_error (misconfiguration) or temporarily_unavailable (outage) and Retry-After; the
cause is logged server-side only. No test here makes a network call.
"""

import os
import unittest
import urllib.error
from unittest.mock import patch

from starlette.testclient import TestClient

from lofgren_intelligence.hosted import ratelimit
from lofgren_intelligence.hosted import store as li_store
from lofgren_intelligence.hosted import web_app
from lofgren_intelligence.hosted.http import HTTPError

from .test_public_hosted import FakeStore

BASE = "https://li.example"
# Deployed without the service-role key (the live finding); no Stripe webhook secret either.
MISCONFIGURED = {
    "LI_PUBLIC_BASE_URL": BASE,
    "SUPABASE_URL": "https://project.supabase.example",
    "SUPABASE_PUBLISHABLE_KEY": "publishable-test-key",
    "LI_RELEASE_SHA": "0" * 40,
    "LI_RATE_LIMIT_BACKEND": "memory",
}
CONFIGURED = {**MISCONFIGURED, "SUPABASE_SERVICE_ROLE_KEY": "service-test-key"}
UNSET = ("SUPABASE_SERVICE_ROLE_KEY", "STRIPE_WEBHOOK_SECRET", "STRIPE_SECRET_KEY")
# Nothing that names configuration or internals may reach a public body.
FORBIDDEN = ("SUPABASE", "SERVICE_ROLE", "STRIPE_", "LI_", "_KEY", "is required", "not configured",
             "HTTP 5", "unreachable", "Traceback")
CHALLENGE = "A" * 43
VERIFIER = "v" * 43
BEARER = {"authorization": "Bearer sb-session-token"}


def _env(values):
    env = {k: v for k, v in os.environ.items() if k not in UNSET}
    env.update(values)
    return patch.dict(os.environ, env, clear=True)


class _Base(unittest.TestCase):
    env = MISCONFIGURED

    def setUp(self):
        ratelimit.LIMITER.reset()
        self.addCleanup(ratelimit.LIMITER.reset)
        ctx = _env(self.env)
        ctx.start()
        self.addCleanup(ctx.stop)
        self.client = TestClient(web_app.build_app(), base_url=BASE, raise_server_exceptions=False)

    def assertServerFault(self, resp, code="server_error"):
        self.assertEqual(resp.status_code, 503, resp.text)
        body = resp.json()
        self.assertEqual(body["error"], code)
        self.assertEqual(body["error_description"], web_app.UNAVAILABLE_DESCRIPTION)
        self.assertTrue(int(resp.headers["retry-after"]) > 0)
        self.assertEqual(resp.headers["cache-control"], "no-store")
        self.assertNoInternals(resp)

    def assertNoInternals(self, resp):
        for needle in FORBIDDEN:
            self.assertNotIn(needle, resp.text)


class MissingServiceRoleKeyTests(_Base):
    """Each hosted route that touches the store answers 503 server_error, generically."""

    def test_register(self):
        with self.assertLogs("lofgren_intelligence.public", "ERROR") as logs:
            resp = self.client.post("/oauth/register", json={"redirect_uris": ["https://client.example/cb"]})
        self.assertServerFault(resp)
        # The specific cause is kept for operators, server-side.
        self.assertIn("SUPABASE_SERVICE_ROLE_KEY is required", "\n".join(logs.output))

    def test_token_authorization_code_and_refresh(self):
        for form in ({"grant_type": "authorization_code", "code": "c", "code_verifier": VERIFIER,
                      "client_id": "licl_x", "redirect_uri": "https://client.example/cb"},
                     {"grant_type": "refresh_token", "refresh_token": "r", "client_id": "licl_x"}):
            self.assertServerFault(self.client.post("/oauth/token", data=form))

    def test_authorize(self):
        resp = self.client.get("/oauth/authorize", params={
            "response_type": "code", "client_id": "licl_x", "redirect_uri": "https://client.example/cb",
            "code_challenge": CHALLENGE, "code_challenge_method": "S256"})
        self.assertServerFault(resp)

    def test_authorize_complete(self):
        self.assertServerFault(self.client.post("/oauth/authorize/complete", json={}, headers=BEARER))

    def test_mcp_with_a_bearer_token(self):
        resp = self.client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                                headers={**BEARER, "accept": "application/json, text/event-stream"})
        self.assertServerFault(resp)

    def test_account_cases_actions_and_workspace(self):
        for method, path in (("GET", "/account/export"), ("POST", "/account/delete"),
                             ("GET", "/workspace/cases"), ("GET", "/workspace/jobs"),
                             ("GET", "/cases/c1/charter"), ("POST", "/cases/c1/approve"),
                             ("GET", "/actions/a1/details"), ("POST", "/actions/a1/approve")):
            with self.subTest(path=path):
                self.assertServerFault(self.client.request(method, path, json={}, headers=BEARER))

    def test_billing_webhook_without_webhook_secret(self):
        self.assertServerFault(self.client.post("/stripe/webhook", content=b"{}",
                                                headers={"stripe-signature": "t=1,v1=x"}))

    def test_readyz_names_generic_components_only(self):
        with self.assertLogs("lofgren_intelligence.public", "ERROR") as logs:
            resp = self.client.get("/readyz")
        self.assertEqual(resp.status_code, 503)
        self.assertEqual(resp.json(), {"ready": False, "missing": ["database"]})
        self.assertNoInternals(resp)
        self.assertIn("SUPABASE_SERVICE_ROLE_KEY", "\n".join(logs.output))


class ClientErrorsUnderMisconfigurationTests(_Base):
    """A request that is wrong on its face stays the caller's fault whatever the deployment state."""

    def test_unsupported_grant_and_malformed_requests(self):
        resp = self.client.post("/oauth/token", data={"grant_type": "password"})
        self.assertEqual((resp.status_code, resp.json()["error"]), (400, "unsupported_grant_type"))
        resp = self.client.get("/oauth/authorize", params={"response_type": "token", "client_id": "x"})
        self.assertEqual((resp.status_code, resp.json()["error"]), (400, "invalid_request"))

    def test_missing_credentials_are_401(self):
        self.assertEqual(self.client.get("/account/export").status_code, 401)
        self.assertEqual(self.client.post("/cases/c1/approve", json={}).status_code, 401)
        self.assertEqual(self.client.post("/mcp", json={}).status_code, 401)
        resp = self.client.post("/oauth/authorize/complete", json={})
        self.assertEqual((resp.status_code, resp.json()["error"]), (400, "invalid_request"))


class StoreOutageTests(_Base):
    """A configured but unreachable or failing store is temporarily_unavailable."""

    env = CONFIGURED

    def test_unreachable_database(self):
        with patch.object(li_store, "json_request", side_effect=urllib.error.URLError("down")):
            self.assertServerFault(self.client.post(
                "/oauth/register", json={"redirect_uris": ["https://client.example/cb"]}),
                "temporarily_unavailable")
            self.assertServerFault(self.client.post("/oauth/token", data={
                "grant_type": "refresh_token", "refresh_token": "r", "client_id": "licl_x"}),
                "temporarily_unavailable")
            resp = self.client.get("/readyz")
        self.assertEqual(resp.status_code, 503)
        self.assertEqual(resp.json()["database"], "unavailable")
        self.assertNoInternals(resp)

    def test_identity_provider_outage_is_not_an_invalid_session(self):
        with patch.object(li_store, "json_request", side_effect=HTTPError(502, "bad gateway")):
            self.assertServerFault(self.client.get("/account/export", headers=BEARER),
                                   "temporarily_unavailable")
        # A rejected session stays a client error.
        with patch.object(li_store, "json_request", side_effect=HTTPError(401, "bad jwt")):
            resp = self.client.get("/account/export", headers=BEARER)
        self.assertEqual(resp.status_code, 401)


class RealClientErrorsUnchangedTests(_Base):
    """With a working store, OAuth client errors keep their RFC 6749/7591 codes."""

    env = CONFIGURED

    def setUp(self):
        super().setUp()
        self.store = FakeStore()
        p = patch.object(web_app, "SupabaseStore", lambda *a, **k: self.store)
        p.start()
        self.addCleanup(p.stop)
        resp = self.client.post("/oauth/register", json={"redirect_uris": ["https://client.example/cb"]})
        self.assertEqual(resp.status_code, 201, resp.text)
        self.client_id = resp.json()["client_id"]

    def _expect(self, resp, status, code):
        self.assertEqual((resp.status_code, resp.json()["error"]), (status, code), resp.text)

    def test_bad_redirect_registration(self):
        self._expect(self.client.post("/oauth/register", json={"redirect_uris": ["http://evil.example/cb"]}),
                     400, "invalid_client_metadata")

    def test_unknown_client_and_unregistered_redirect_at_authorize(self):
        params = {"response_type": "code", "redirect_uri": "https://client.example/cb",
                  "code_challenge": CHALLENGE, "code_challenge_method": "S256"}
        self._expect(self.client.get("/oauth/authorize", params={**params, "client_id": "licl_unknown"}),
                     400, "invalid_client")
        self._expect(self.client.get("/oauth/authorize", params={
            **params, "client_id": self.client_id, "redirect_uri": "https://other.example/cb"}),
            400, "invalid_client")

    def test_bad_pkce_and_unknown_code_at_token(self):
        form = {"grant_type": "authorization_code", "code": "nope", "client_id": self.client_id,
                "redirect_uri": "https://client.example/cb"}
        self._expect(self.client.post("/oauth/token", data={**form, "code_verifier": "short"}),
                     400, "invalid_grant")
        self._expect(self.client.post("/oauth/token", data={**form, "code_verifier": VERIFIER}),
                     400, "invalid_grant")
        self._expect(self.client.post("/oauth/token", data={
            "grant_type": "refresh_token", "refresh_token": "nope", "client_id": "licl_unknown"}),
            400, "invalid_grant")


class _Rows:
    def __init__(self, body):
        self.body = body


GARBAGE = {"authorization": "Bearer garbage"}
MCP_ACCEPT = {"accept": "application/json, text/event-stream"}
PUBLIC_GETS = ("/healthz", "/.well-known/oauth-protected-resource",
               "/.well-known/oauth-protected-resource/mcp", "/.well-known/oauth-authorization-server",
               "/no-such-route")


class ArbitraryBearerTests(_Base):
    """Live finding: any `Authorization: Bearer <x>` turned every route into a bare 500."""

    def test_misconfigured_store_public_routes_ignore_the_bearer(self):
        for path in PUBLIC_GETS:
            with self.subTest(path=path):
                plain, bearer = self.client.get(path), self.client.get(path, headers=GARBAGE)
                self.assertEqual(bearer.status_code, plain.status_code)
                self.assertNotEqual(bearer.status_code, 500)
        self.assertEqual(self.client.get("/readyz", headers=GARBAGE).status_code, 503)
        # Registration is not bearer-authenticated; its 503 is the store fault itself.
        self.assertServerFault(self.client.post("/oauth/register", headers=GARBAGE,
                                                json={"redirect_uris": ["https://client.example/cb"]}))

    def test_misconfigured_store_mcp_with_bearer_is_503_not_500(self):
        self.assertServerFault(self.client.post("/mcp", json={}, headers={**GARBAGE, **MCP_ACCEPT}))

    def test_configured_store_unknown_token(self):
        ctx = _env(CONFIGURED)
        ctx.start()
        self.addCleanup(ctx.stop)
        with patch.object(li_store, "json_request", return_value=_Rows([])):
            for path in PUBLIC_GETS:
                with self.subTest(path=path):
                    self.assertNotEqual(self.client.get(path, headers=GARBAGE).status_code, 500)
            resp = self.client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                                    headers={**GARBAGE, **MCP_ACCEPT})
        self.assertEqual(resp.status_code, 401)
        self.assertEqual(resp.json()["error"], "invalid_token")
        challenge = resp.headers["www-authenticate"]
        self.assertIn('error="invalid_token"', challenge)
        self.assertIn("resource_metadata=", challenge)
        self.assertIn("/.well-known/oauth-protected-resource", challenge)

    def test_configured_store_outage_during_verification_is_503(self):
        ctx = _env(CONFIGURED)
        ctx.start()
        self.addCleanup(ctx.stop)
        with patch.object(li_store, "json_request", side_effect=urllib.error.URLError("down")):
            self.assertEqual(self.client.get("/healthz", headers=GARBAGE).status_code, 200)
            self.assertServerFault(self.client.post("/mcp", json={}, headers={**GARBAGE, **MCP_ACCEPT}),
                                   "temporarily_unavailable")


class CorsTests(_Base):
    def _preflight(self, path, origin, headers="authorization, content-type, mcp-protocol-version"):
        return self.client.options(path, headers={
            "origin": origin, "access-control-request-method": "POST",
            "access-control-request-headers": headers})

    def test_documented_mcp_client_origins_are_allowed(self):
        for origin in ("https://chatgpt.com", "https://chat.openai.com", "https://claude.ai"):
            for path in ("/mcp", "/oauth/token", "/oauth/register"):
                with self.subTest(origin=origin, path=path):
                    resp = self._preflight(path, origin)
                    self.assertEqual(resp.status_code, 200, resp.text)
                    self.assertEqual(resp.headers["access-control-allow-origin"], origin)
                    self.assertNotIn("access-control-allow-credentials", resp.headers)
                    allowed = resp.headers["access-control-allow-headers"].lower()
                    for header in ("authorization", "content-type", "mcp-session-id", "mcp-protocol-version"):
                        self.assertIn(header, allowed)
                    self.assertNotIn("PUT", resp.headers["access-control-allow-methods"])
        resp = self.client.get("/healthz", headers={"origin": "https://claude.ai"})
        exposed = resp.headers["access-control-expose-headers"].lower()
        self.assertIn("www-authenticate", exposed)
        self.assertIn("mcp-session-id", exposed)

    def test_unknown_origins_and_headers_are_still_refused(self):
        self.assertEqual(self._preflight("/mcp", "https://evil.example").status_code, 400)
        self.assertEqual(self._preflight("/mcp", "https://claude.ai", "x-evil").status_code, 400)

    def test_env_override_replaces_the_default_and_never_wildcards(self):
        with patch.dict(os.environ, {"LI_MCP_CLIENT_ORIGINS": "https://client.example, *",
                                     "LI_ALLOWED_ORIGINS": "*"}):
            origins = web_app.allowed_origins(BASE)
        self.assertEqual(origins, [BASE, "https://client.example"])


class CacheHeaderTests(_Base):
    def test_oauth_responses_are_no_store_and_token_is_also_no_cache(self):
        for resp in (self.client.post("/oauth/register", json={"redirect_uris": ["https://c.example/cb"]}),
                     self.client.post("/oauth/register", content=b"{not json"),
                     self.client.get("/oauth/authorize", params={"response_type": "token"}),
                     self.client.post("/oauth/token", data={"grant_type": "client_credentials"})):
            self.assertEqual(resp.headers["cache-control"], "no-store")
        token = self.client.post("/oauth/token", data={"grant_type": "client_credentials"})
        self.assertEqual(token.headers["pragma"], "no-cache")

    def test_metadata_documents_share_one_short_policy_and_one_vary(self):
        for path in ("/.well-known/oauth-protected-resource", "/.well-known/oauth-protected-resource/mcp",
                     "/.well-known/oauth-authorization-server"):
            with self.subTest(path=path):
                resp = self.client.get(path, headers={"origin": "https://claude.ai"})
                self.assertEqual(resp.status_code, 200)
                self.assertEqual(resp.headers.get_list("cache-control"), ["public, max-age=300"])
                vary = resp.headers.get_list("vary")
                self.assertLessEqual(len(vary), 1)
                if vary:
                    tokens = [t.strip().lower() for t in vary[0].split(",")]
                    self.assertEqual(len(tokens), len(set(tokens)), vary)


class OAuthRequestValidationTests(_Base):
    def test_grant_type_is_validated_before_the_store(self):
        resp = self.client.post("/oauth/token", data={"grant_type": "client_credentials"})
        self.assertEqual((resp.status_code, resp.json()["error"]), (400, "unsupported_grant_type"))

    def test_bad_json_registration_does_not_echo_parser_messages(self):
        for raw in (b"{not json", b"[1, 2]", b"\xff\xfe"):
            resp = self.client.post("/oauth/register", content=raw,
                                    headers={"content-type": "application/json"})
            self.assertEqual((resp.status_code, resp.json()["error"]), (400, "invalid_client_metadata"))
            self.assertEqual(resp.json()["error_description"], "client metadata must be a bounded JSON object")
            for needle in ("Expecting", "char", "codec", "line 1"):
                self.assertNotIn(needle, resp.text)


if __name__ == "__main__":
    unittest.main()
