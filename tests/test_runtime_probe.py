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

    def test_real_launch_network_block_secret_stripping_timeout_kill_and_reap(self):
        env={'VERCEL_ENV':'preview','LI_RUNTIME_PROBE_TOKEN':'x'*32,
             'OPENAI_API_KEY':'should-never-reach-child','SUPABASE_SERVICE_ROLE_KEY':'should-never-reach-child',
             'VERCEL_GIT_COMMIT_SHA':'a'*40}
        result=self.request(env, {'Authorization':'Bearer '+'x'*32})
        if os.name!='posix':
            # Process isolation is POSIX-only: elsewhere the probe fails closed, never passes.
            self.assertEqual(result.status_code,503,result.text)
            self.assertEqual(result.json(),{'probe':'runtime-process/1','passed':False,'error':'runtime_unavailable'})
            self.assertNotIn('should-never',result.text)
            return
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
