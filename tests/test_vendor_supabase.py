"""The supabase-js browser client is vendored, pinned and integrity-checked (queue item Q6).

The account, consent, action-approval and case-approval pages used to load a floating
``@supabase/supabase-js@2`` from cdn.jsdelivr.net with no Subresource Integrity, under a CSP
that allowed the whole CDN. They now load one exact, vendored UMD build from
``/static/vendor/`` with a nonce and an ``integrity`` attribute, and no CSP names an external
script host. No test here makes a network, Stripe or Supabase call.
"""

from __future__ import annotations

import base64
import hashlib
import os
import re
import unittest
from importlib import resources
from pathlib import Path
from unittest.mock import patch

from lofgren_intelligence.hosted import site, web_app

from .test_public_hardening import ENV, _ClientStore, _call

ROOT = Path(__file__).resolve().parent.parent
VENDOR = ROOT / "lofgren_intelligence" / "hosted" / "static" / "vendor"
VENDOR_FILE = VENDOR / site.SUPABASE_JS_FILE
README = VENDOR / "README.md"

AUTHORIZE_QUERY = (
    "response_type=code&client_id=licl_attacker&redirect_uri=https%3A%2F%2Fevil.example%2Fcb"
    "&code_challenge=" + "A" * 43 + "&code_challenge_method=S256&state=s"
)


def _sri(data: bytes) -> str:
    return "sha384-" + base64.b64encode(hashlib.sha384(data).digest()).decode("ascii")


def _supabase_pages():
    """Every page that loads supabase-js, rendered with a configured (fake) Supabase project."""
    with patch.dict(os.environ, ENV), patch.object(web_app, "SupabaseStore", _ClientStore):
        return {
            "consent": _call(web_app.oauth_authorize, "/oauth/authorize", AUTHORIZE_QUERY),
            "account": _call(web_app.account_page, "/account"),
            "action": asyncio_call(web_app.action_page, "/actions/ACT-1", {"action_id": "ACT-1"}),
            "case": asyncio_call(web_app.case_page, "/cases/CASE-1", {"case_id": "CASE-1"}),
        }


def asyncio_call(handler, path, path_params):
    import asyncio

    from .test_public_hardening import _request

    request = _request(path)
    request.scope["path_params"] = path_params
    return asyncio.run(handler(request))


class PinnedVersionTests(unittest.TestCase):
    def test_version_is_one_exact_stable_2x_release(self):
        self.assertRegex(site.SUPABASE_JS_VERSION, r"^2\.\d+\.\d+$")
        self.assertEqual(site.SUPABASE_JS_FILE, f"supabase-js-{site.SUPABASE_JS_VERSION}.umd.js")
        self.assertEqual(site.SUPABASE_JS_PATH, f"/static/vendor/{site.SUPABASE_JS_FILE}")

    def test_exactly_one_vendored_file_and_it_is_the_pinned_one(self):
        files = sorted(p.name for p in VENDOR.iterdir() if p.suffix == ".js")
        self.assertEqual(files, [site.SUPABASE_JS_FILE])
        self.assertEqual(set(site.VENDOR_TYPES), {site.SUPABASE_JS_FILE})

    def test_vendored_file_is_the_umd_build_that_defines_window_supabase(self):
        text = VENDOR_FILE.read_bytes().decode("utf-8")
        self.assertTrue(text.startswith("var supabase=(function("), text[:60])
        self.assertIn("createClient", text)
        self.assertNotIn("sourceMappingURL", text)


class IntegrityTests(unittest.TestCase):
    def test_file_sha384_equals_the_pinned_sri(self):
        self.assertEqual(_sri(VENDOR_FILE.read_bytes()), site.SUPABASE_JS_SRI)

    def test_packaged_resource_bytes_match_the_pinned_sri(self):
        data = resources.files("lofgren_intelligence.hosted").joinpath("static", "vendor", site.SUPABASE_JS_FILE)
        self.assertEqual(_sri(data.read_bytes()), site.SUPABASE_JS_SRI)
        self.assertEqual(site.vendor_bytes(site.SUPABASE_JS_FILE), VENDOR_FILE.read_bytes())

    def test_readme_records_version_source_hashes_and_license(self):
        readme = README.read_text(encoding="utf-8")
        version = site.SUPABASE_JS_VERSION
        self.assertIn(f"## @supabase/supabase-js {version}", readme)
        self.assertIn(f"`{site.SUPABASE_JS_FILE}`", readme)
        self.assertIn(f"https://registry.npmjs.org/@supabase/supabase-js/-/supabase-js-{version}.tgz", readme)
        self.assertRegex(readme, r"`sha512-[A-Za-z0-9+/]{86}==`")
        self.assertIn(f"`{site.SUPABASE_JS_SRI}`", readme)
        self.assertIn(f"`{hashlib.sha256(VENDOR_FILE.read_bytes()).hexdigest()}`", readme)
        self.assertIn(f"| Size | {VENDOR_FILE.stat().st_size} bytes |", readme)
        self.assertIn("MIT License", readme)
        self.assertIn("Copyright (c) 2020 Supabase", readme)
        self.assertIn('THE SOFTWARE IS PROVIDED "AS IS"', readme)
        notice = (ROOT / "NOTICE").read_text(encoding="utf-8")
        self.assertIn(f"@supabase/supabase-js {version}", notice)

    def test_gitattributes_keeps_vendored_bytes_exact(self):
        attrs = (ROOT / ".gitattributes").read_text(encoding="utf-8")
        self.assertIn("lofgren_intelligence/hosted/static/vendor/*.js -text", attrs)
        self.assertNotIn(b"\r", VENDOR_FILE.read_bytes())


class PageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.pages = _supabase_pages()

    def test_every_supabase_page_renders(self):
        self.assertEqual(set(self.pages), {"consent", "account", "action", "case"})
        for name, resp in self.pages.items():
            with self.subTest(page=name):
                self.assertEqual(resp.status_code, 200)

    def test_pages_load_the_vendored_file_with_nonce_and_integrity(self):
        for name, resp in self.pages.items():
            with self.subTest(page=name):
                page = resp.body.decode("utf-8")
                csp = resp.headers["content-security-policy"]
                nonce = re.search(r"'nonce-([^']+)'", csp).group(1)
                tags = re.findall(r"<script\b[^>]*\bsrc=[^>]*>", page)
                self.assertEqual(tags, [
                    f'<script nonce="{nonce}" src="{site.SUPABASE_JS_PATH}" '
                    f'integrity="{site.SUPABASE_JS_SRI}" crossorigin="anonymous">'
                ])
                integrity = re.search(r'integrity="([^"]+)"', tags[0]).group(1)
                self.assertEqual(integrity, _sri(VENDOR_FILE.read_bytes()))
                # The client is still created from the UMD global, after the library tag.
                self.assertIn("const sb=supabase.createClient(", page)
                self.assertLess(page.index(site.SUPABASE_JS_PATH), page.index("supabase.createClient("))
                # Every <script> carries this response's nonce.
                for tag in re.findall(r"<script\b[^>]*>", page):
                    self.assertIn(f'nonce="{nonce}"', tag)

    def test_csp_names_no_external_script_host(self):
        for name, resp in self.pages.items():
            with self.subTest(page=name):
                csp = resp.headers["content-security-policy"]
                directives = dict(d.strip().split(" ", 1) for d in csp.split(";") if " " in d.strip())
                nonce = re.search(r"'nonce-([^']+)'", csp).group(1)
                self.assertEqual(directives["script-src"], f"'self' 'nonce-{nonce}'")
                self.assertEqual(directives["default-src"], "'none'")
                self.assertNotIn("jsdelivr", csp)
                self.assertNotIn("unsafe-inline", csp)
                self.assertNotIn("unsafe-eval", csp)
                # connect-src still reaches the configured Supabase project for auth calls.
                self.assertIn("https://project.supabase.example", directives["connect-src"])

    def test_no_page_references_an_external_script_host(self):
        for name, resp in self.pages.items():
            with self.subTest(page=name):
                page = resp.body.decode("utf-8")
                self.assertNotIn("jsdelivr", page)
                for src in re.findall(r"<script\b[^>]*\bsrc=\"([^\"]+)\"", page):
                    self.assertTrue(src.startswith("/static/"), src)


class SourceTests(unittest.TestCase):
    """No source file, CSP or config in the shipped package or deployment refers to a script CDN."""

    CDN_HOSTS = ("jsdelivr", "unpkg.com", "cdnjs", "esm.sh", "skypack", "googleapis.com/ajax")

    def test_no_cdn_reference_in_package_api_or_deploy_config(self):
        paths = [*(ROOT / "lofgren_intelligence").rglob("*.py"), *(ROOT / "api").rglob("*.py"), ROOT / "vercel.json"]
        paths += [p for p in (ROOT / "lofgren_intelligence" / "hosted" / "static").glob("*") if p.is_file()]
        for path in paths:
            text = path.read_text(encoding="utf-8").lower()
            for host in self.CDN_HOSTS:
                with self.subTest(path=str(path.relative_to(ROOT)), host=host):
                    self.assertNotIn(host, text)

    def test_no_supabase_page_loads_a_floating_version(self):
        source = (ROOT / "lofgren_intelligence" / "hosted" / "web_app.py").read_text(encoding="utf-8")
        self.assertNotIn("supabase-js@", source)
        self.assertEqual(source.count("{site.supabase_script_tag(nonce)}"), 4)


class StaticRouteTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from starlette.testclient import TestClient

        cls._env = patch.dict(os.environ, ENV)
        cls._env.start()
        cls.client = TestClient(web_app.build_app(), base_url="https://li.example")

    @classmethod
    def tearDownClass(cls):
        cls._env.stop()

    def test_vendored_file_is_served_with_type_and_long_lived_cache(self):
        response = self.client.get(site.SUPABASE_JS_PATH)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["content-type"], "text/javascript; charset=utf-8")
        self.assertEqual(response.headers["cache-control"], "public, max-age=31536000, immutable")
        self.assertIn("nosniff", response.headers.get("x-content-type-options", ""))
        self.assertEqual(_sri(response.content), site.SUPABASE_JS_SRI)

    def test_everything_else_under_vendor_is_404(self):
        for path in (
            "/static/vendor/README.md",
            "/static/vendor/supabase-js-2.0.0.umd.js",
            "/static/vendor/supabase.js",
            "/static/vendor/",
            "/static/vendor",
            "/static/vendor/..%2F..%2Fsite.py",
            "/static/vendor/%2E%2E%2Fsite.css",
            f"/static/{site.SUPABASE_JS_FILE}",
            f"/static/vendor/{site.SUPABASE_JS_FILE}/x",
            f"/static/vendor/{site.SUPABASE_JS_FILE.upper()}",
        ):
            with self.subTest(path=path):
                self.assertEqual(self.client.get(path).status_code, 404)

    def test_site_assets_keep_their_existing_allowlist(self):
        self.assertEqual(set(site.STATIC_TYPES), {"site.css", "site.js", "mark.svg"})
        for name in ("site.css", "site.js", "mark.svg"):
            self.assertEqual(self.client.get(f"/static/{name}").status_code, 200)


class PackageDataTests(unittest.TestCase):
    def test_pyproject_ships_vendor_js_and_its_license_notice(self):
        pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
        self.assertIn('"static/vendor/*.js"', pyproject)
        self.assertIn('"static/vendor/README.md"', pyproject)

    def test_ci_wheel_check_requires_the_vendored_file(self):
        workflow = (ROOT / ".github" / "workflows" / "tests.yml").read_text(encoding="utf-8")
        line = next(l for l in workflow.splitlines() if "static-assets-ok" in l)
        self.assertIn(f"vendor/{site.SUPABASE_JS_FILE}", line)
        self.assertIn(site.SUPABASE_JS_SRI.removeprefix("sha384-"), line)


if __name__ == "__main__":
    unittest.main()
