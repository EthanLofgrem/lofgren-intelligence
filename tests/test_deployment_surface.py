"""Dedicated MCP entry point preserves domain authorization and excludes workspace routes."""
import os
import unittest
from unittest.mock import patch
from starlette.testclient import TestClient
from lofgren_intelligence.hosted import web_app
from .test_public_hardening import ENV, _ClientStore

class DeploymentSurfaceTests(unittest.TestCase):
    def test_invalid_surface_fails_startup(self):
        with patch.dict(os.environ, {**ENV, "LI_DEPLOYMENT_SURFACE": "unknown"}):
            with self.assertRaisesRegex(RuntimeError, "LI_DEPLOYMENT_SURFACE"):
                web_app.build_app()

    def test_mcp_surface_excludes_customer_billing_and_demo_routes(self):
        with patch.dict(os.environ, {**ENV, "LI_DEPLOYMENT_SURFACE": "mcp"}):
            with TestClient(web_app.build_app(), base_url=ENV["LI_PUBLIC_BASE_URL"]) as client:
                self.assertEqual(client.get("/").json()["interface"], "MCP")
                for path, method in (("/workspace", "GET"), ("/workspace/cases", "POST"),
                                     ("/account", "GET"), ("/account/delete", "POST"),
                                     ("/stripe/webhook", "POST"), ("/billing/success", "GET"),
                                     ("/app", "GET"), ("/pricing", "GET"), ("/static/site.css", "GET")):
                    with self.subTest(path=path):
                        self.assertEqual(client.request(method, path).status_code, 404)
                self.assertEqual(client.get("/.well-known/oauth-authorization-server").status_code, 200)
                self.assertEqual(client.get("/.well-known/oauth-protected-resource").status_code, 200)
                denied = client.post("/mcp", json={"jsonrpc":"2.0","id":1,"method":"tools/list"},
                                     headers={"Accept":"application/json, text/event-stream"})
                self.assertEqual(denied.status_code, 401)
                self.assertEqual(denied.headers["cache-control"], "no-store")
                self.assertIn("resource_metadata=", denied.headers["www-authenticate"])
                self.assertEqual(client.get("/cases/case-test/charter").status_code, 401)
                self.assertEqual(client.post("/cases/case-test/approve").status_code, 401)
                self.assertEqual(client.get("/actions/action-test/details").status_code, 400)
                self.assertEqual(client.post("/actions/action-test/approve").status_code, 400)

    def test_default_surface_preserves_workspace(self):
        with patch.dict(os.environ, ENV):
            with patch.dict(os.environ):
                os.environ.pop("LI_DEPLOYMENT_SURFACE", None)
                with TestClient(web_app.build_app(), base_url=ENV["LI_PUBLIC_BASE_URL"]) as client:
                    self.assertEqual(client.get("/pricing").status_code, 200)
                    self.assertEqual(client.get("/workspace").status_code, 200)

    def test_approval_pages_and_pinned_login_dependency_remain_available(self):
        from lofgren_intelligence.hosted import site
        with patch.dict(os.environ, {**ENV, "LI_DEPLOYMENT_SURFACE": "mcp"}):
            with TestClient(web_app.build_app(), base_url=ENV["LI_PUBLIC_BASE_URL"]) as client:
                for path in ("/cases/case-test", "/actions/action-test"):
                    response = client.get(path)
                    self.assertEqual(response.status_code, 200)
                    self.assertEqual(response.headers["cache-control"], "no-store")
                    self.assertIn("/static/vendor/", response.text)
                for name in site.VENDOR_TYPES:
                    self.assertEqual(client.get("/static/vendor/" + name).status_code, 200)
                with patch.object(web_app, "SupabaseStore", _ClientStore):
                    response = client.get("/oauth/authorize", params={"response_type":"code", "client_id":"licl_attacker",
                        "redirect_uri":"https://evil.example/cb", "code_challenge":"A"*43,
                        "code_challenge_method":"S256", "scope":"mcp"})
                    self.assertEqual(response.status_code, 200)
                    self.assertEqual(response.headers["cache-control"], "no-store")
                    self.assertIn("/static/vendor/", response.text)
