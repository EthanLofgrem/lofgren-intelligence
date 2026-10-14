"""Real subprocess proof and failclosed Preview entry point."""
import os
import unittest
from unittest.mock import patch
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient
from lofgren_intelligence.hosted import runtime_probe as probe


class RuntimeProbeTests(unittest.TestCase):
    def request(self, env, headers=None):
        with patch.dict(os.environ, env), TestClient(Starlette(routes=[Route('/probe', probe.runtime_probe, methods=['POST'])])) as client:
            return client.post('/probe', headers=headers or {})

    def setUp(self):
        probe._USED = False

    def test_production_and_missing_configuration_fail_closed(self):
        for env in ({'VERCEL_ENV':'production','LI_RUNTIME_PROBE_TOKEN':'x'*32}, {'VERCEL_ENV':'preview','LI_RUNTIME_PROBE_TOKEN':''}):
            self.assertEqual(self.request(env).status_code, 404)

    def test_auth_and_body_rejected_before_spawn(self):
        env={'VERCEL_ENV':'preview','LI_RUNTIME_PROBE_TOKEN':'x'*32}
        with patch.object(probe, 'run_json_child') as spawn:
            self.assertEqual(self.request(env).status_code, 401)
            self.assertEqual(self.request(env, {'Authorization':'Bearer '+'x'*32,'Content-Length':'1'}).status_code, 400)
            spawn.assert_not_called()

    def test_non_ascii_authorization_is_refused_not_a_server_fault(self):
        import asyncio
        app = Starlette(routes=[Route('/probe', probe.runtime_probe, methods=['POST'])])
        async def call(value):
            sent = []
            scope = {'type': 'http', 'method': 'POST', 'path': '/probe', 'raw_path': b'/probe',
                     'query_string': b'', 'headers': [(b'authorization', value)], 'http_version': '1.1',
                     'scheme': 'https', 'server': ('t', 443), 'client': ('1.2.3.4', 1), 'root_path': ''}
            async def receive():
                return {'type': 'http.request', 'body': b'', 'more_body': False}
            async def send(message):
                sent.append(message)
            await app(scope, receive, send)
            return sent[0]['status']
        env = {'VERCEL_ENV': 'preview', 'LI_RUNTIME_PROBE_TOKEN': 'x'*32}
        with patch.dict(os.environ, env), patch.object(probe, 'run_json_child') as spawn:
            for value in (b'Bearer \xe9' + b'x'*31, b'\xff\xfe', b'Bearer ' + b'x'*31 + b'\x80'):
                self.assertEqual(asyncio.run(call(value)), 401, value)
            spawn.assert_not_called()

    @unittest.skipUnless(os.name=='posix', 'POSIX process isolation')
    def test_real_launch_network_block_secret_stripping_timeout_kill_and_reap(self):
        env={'VERCEL_ENV':'preview','LI_RUNTIME_PROBE_TOKEN':'x'*32,
             'OPENAI_API_KEY':'should-never-reach-child','SUPABASE_SERVICE_ROLE_KEY':'should-never-reach-child',
             'VERCEL_GIT_COMMIT_SHA':'a'*40}
        result=self.request(env, {'Authorization':'Bearer '+'x'*32})
        self.assertEqual(result.status_code,200,result.text)
        for key in ('passed','launch','network_denied','clean_environment','timeout','kill','reap','pid_absent'):
            self.assertIs(result.json()[key],True,key)
        self.assertLess(result.json()['elapsed_ms'],5000)
        self.assertEqual(result.headers['cache-control'],'no-store')
        self.assertNotIn('should-never',result.text)
        self.assertEqual(self.request(env, {'Authorization':'Bearer '+'x'*32}).status_code,429)

    def test_failed_runtime_is_sanitized(self):
        from lofgren_intelligence.hosted.process_watchdog import ChildRefused
        env={'VERCEL_ENV':'preview','LI_RUNTIME_PROBE_TOKEN':'x'*32}
        with patch.object(probe,'run_json_child',side_effect=ChildRefused('secret detail')):
            response=self.request(env,{'Authorization':'Bearer '+'x'*32})
        self.assertEqual(response.status_code,503)
        self.assertNotIn('secret detail',response.text)
