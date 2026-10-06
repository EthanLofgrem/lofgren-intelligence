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
        if url.endswith('/oauth-protected-resource'):
            return 200, {}, {'resource': BASE + '/mcp', 'authorization_servers': [BASE],
                             'scopes_supported': ['mcp'], 'bearer_methods_supported': ['header']}
        if url.endswith('/oauth-authorization-server'):
            return 200, {}, {'issuer': BASE, 'authorization_endpoint': BASE + '/oauth/authorize',
                             'token_endpoint': BASE + '/oauth/token', 'registration_endpoint': BASE + '/oauth/register',
                             'code_challenge_methods_supported': ['S256'],
                             'grant_types_supported': ['authorization_code', 'refresh_token']}
        return 401, {'WWW-Authenticate': 'Bearer resource_metadata="' + BASE + '/.well-known/oauth-protected-resource"'}, {}

    def test_expected_contract(self):
        self.assertTrue(m.check(BASE, SHA, self.request)['passed'])

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
