import importlib.util
from pathlib import Path
import unittest

spec = importlib.util.spec_from_file_location('preflight', Path(__file__).resolve().parents[1] / 'scripts/li_mcp_preflight.py')
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
BASE = 'https://li.example'
SHA = 'a' * 40


class PreflightTests(unittest.TestCase):
    def request(self, url, method='GET', body=None):
        if url.endswith('/healthz'):
            return 200, {}, {'service': 'lofgren-intelligence', 'status': 'ok'}
        if url.endswith('/readyz'):
            return 200, {}, {'ready': True, 'release_sha': SHA}
        if url.endswith('/oauth-protected-resource') or url.endswith('/oauth-protected-resource/mcp'):
            return 200, {}, {'resource': BASE + '/mcp', 'authorization_servers': [BASE + '/'],
                             'scopes_supported': ['mcp'], 'bearer_methods_supported': ['header']}
        if url.endswith('/oauth-authorization-server'):
            return 200, {}, {'issuer': BASE + '/', 'authorization_endpoint': BASE + '/oauth/authorize',
                             'token_endpoint': BASE + '/oauth/token', 'registration_endpoint': BASE + '/oauth/register',
                             'code_challenge_methods_supported': ['S256'],
                             'grant_types_supported': ['authorization_code', 'refresh_token']}
        return 401, {'Cache-Control': 'no-store', 'WWW-Authenticate': 'Bearer resource_metadata="' + BASE + '/.well-known/oauth-protected-resource"'}, {}

    def test_expected_contract(self):
        self.assertTrue(m.check(BASE, SHA, self.request)['passed'])

    def test_sdk_advertised_path_is_fetched(self):
        calls = []
        target = BASE + '/.well-known/oauth-protected-resource/mcp'
        def request(url, method='GET', body=None):
            calls.append((url, method))
            if method == 'POST':
                return 401, {'Cache-Control': 'no-store', 'WWW-Authenticate':
                             'Bearer resource_metadata="' + target + '"'}, {}
            return self.request(url, method, body)
        self.assertTrue(m.check(BASE, SHA, request)['passed'])
        self.assertIn((target, 'GET'), calls)

    def test_misleading_challenges_do_not_trigger_external_fetch(self):
        root = BASE + '/.well-known/oauth-protected-resource'
        for challenge in ('Bearer resource_metadata="' + root + '.attacker.test"',
                          'Bearer error_description="' + root + '"',
                          'Bearer resource_metadata="' + root + '", resource_metadata="' + root + '"',
                          'Basic resource_metadata="' + root + '"'):
            calls = []
            def request(url, method='GET', body=None):
                calls.append(url)
                if method == 'POST':
                    return 401, {'Cache-Control': 'no-store', 'WWW-Authenticate': challenge}, {}
                return self.request(url, method, body)
            with self.subTest(challenge=challenge):
                self.assertFalse(m.check(BASE, SHA, request)['passed'])
                self.assertEqual(len(calls), 5)

    def test_advertised_failure_and_cacheable_denial_fail(self):
        target = BASE + '/.well-known/oauth-protected-resource/mcp'
        def request(url, method='GET', body=None):
            if method == 'POST':
                return 401, {'Cache-Control': 'public', 'WWW-Authenticate':
                             'Bearer resource_metadata="' + target + '"'}, {}
            if url == target:
                return 404, {}, {}
            return self.request(url, method, body)
        checks = {x['check']: x['passed'] for x in m.check(BASE, SHA, request)['checks']}
        self.assertFalse(checks['advertised_resource_metadata'])
        self.assertFalse(checks['mcp_response_not_cached'])

    def test_actual_asgi_metadata_and_challenge_pass_preflight(self):
        import os
        from unittest.mock import patch
        from starlette.testclient import TestClient
        from lofgren_intelligence.hosted import web_app
        with patch.dict(os.environ, {'LI_PUBLIC_BASE_URL': BASE, 'LI_DEPLOYMENT_SURFACE': 'mcp'}):
            with TestClient(web_app.build_app(), base_url=BASE) as client:
                def request(url, method='GET', body=None):
                    if url.endswith('/readyz'):
                        return 200, {}, {'ready': True, 'release_sha': SHA}
                    response = client.request(method, url, content=body,
                        headers={'Accept': 'application/json, text/event-stream', 'Content-Type': 'application/json'})
                    return response.status_code, dict(response.headers), response.json()
                self.assertTrue(m.check(BASE, SHA, request)['passed'])

    def test_inconsistent_issuer_identity_fails(self):
        def request(url, method='GET', body=None):
            status, headers, data = self.request(url, method, body)
            if url.endswith('/oauth-authorization-server'):
                data = {**data, 'issuer': BASE}
            return status, headers, data
        self.assertFalse(m.check(BASE, SHA, request)['passed'])

    def test_wrong_sha_fails(self):
        self.assertFalse(m.check(BASE, 'b' * 40, self.request)['passed'])

    def test_public_mcp_access_fails(self):
        def request(url, method='GET', body=None):
            return (200, {}, {}) if url.endswith('/mcp') else self.request(url, method, body)
        self.assertFalse(m.check(BASE, SHA, request)['passed'])

    def test_bad_origin_and_sha_rejected(self):
        for base in ('http://li.example', 'https://user:secret@li.example', BASE + '/path', BASE + '?secret=x'):
            with self.subTest(base=base), self.assertRaises(ValueError):
                m.check(base, SHA, self.request)
        with self.assertRaises(ValueError):
            m.check(BASE, 'main', self.request)


if __name__ == '__main__':
    unittest.main()
