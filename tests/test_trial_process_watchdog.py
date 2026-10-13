"""Actual child termination proofs: no providers, databases or credentials."""
import asyncio
import json
import os
from pathlib import Path
import signal
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from lofgren_intelligence.hosted import process_watchdog as watchdog


@unittest.skipUnless(os.name == "posix", "isolated mode fails closed outside POSIX")
class WatchdogTests(unittest.IsolatedAsyncioTestCase):
    async def launch(self, script, *, deadline=0.25, disconnected=None):
        original = asyncio.create_subprocess_exec
        children = []
        async def create(*args, **kwargs):
            child = await original(sys.executable, "-c", script, **kwargs)
            children.append(child)
            return child
        with patch.object(asyncio, "create_subprocess_exec", create):
            try:
                return await watchdog.run_json_child(next(iter(watchdog.CHILD_MODULES)), {},
                    deadline=time.monotonic()+deadline, disconnected=disconnected)
            finally:
                self.assertEqual(len(children), 1)
                self.assertIsNotNone(children[0].returncode)
                with self.assertRaises(ProcessLookupError):
                    os.kill(children[0].pid, 0)

    async def test_blocking_child_is_killed_reaped_and_cannot_write_late_result(self):
        with tempfile.TemporaryDirectory() as directory:
            ready, late = Path(directory)/"ready", Path(directory)/"late"
            script = ("import pathlib,signal,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); "
                      f"pathlib.Path({str(ready)!r}).write_text('ready'); time.sleep(1); "
                      f"pathlib.Path({str(late)!r}).write_text('late success')")
            with self.assertRaisesRegex(watchdog.ChildRefused, "deadline"):
                await self.launch(script)
            self.assertTrue(ready.exists())
            await asyncio.sleep(1.05)
            self.assertFalse(late.exists())

    async def test_output_flood_refused_and_process_reaped(self):
        with self.assertRaisesRegex(watchdog.ChildRefused, "output bound"):
            await self.launch("import sys,time; sys.stdout.write('x'*2000000); sys.stdout.flush(); time.sleep(5)", deadline=2)

    async def test_disconnect_kills_actual_child_before_return(self):
        async def disconnect():
            await asyncio.sleep(0.1)
        with self.assertRaisesRegex(watchdog.ChildRefused, "disconnected"):
            await self.launch("import time; time.sleep(5)", deadline=2, disconnected=disconnect())

    async def test_cancelled_parent_reaps_actual_child(self):
        task = asyncio.create_task(self.launch("import time; time.sleep(5)", deadline=2))
        await asyncio.sleep(0.1)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task

    async def test_child_environment_excludes_unrelated_credentials(self):
        forbidden = {name: "must-not-reach-child" for name in (
            "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "BRAVE_API_KEY", "STRIPE_SECRET_KEY",
            "STRIPE_WEBHOOK_SECRET", "GOOGLE_API_KEY", "GITHUB_TOKEN", "VERCEL_TOKEN",
            "BROWSER_COOKIE", "AWS_SECRET_ACCESS_KEY", "HTTP_PROXY", "HTTPS_PROXY")}
        with patch.dict(os.environ, {**forbidden, "SUPABASE_SERVICE_ROLE_KEY": "internal-test-key"}):
            # Actual child inspects names only: no credential values are emitted.
            found = await self.launch("import json,os; print(json.dumps({'names':list(os.environ)}))", deadline=2)
        self.assertFalse(set(forbidden) & set(found["names"]))
        self.assertIn("SUPABASE_SERVICE_ROLE_KEY", found["names"])
        self.assertIn("LI_TRIAL_ISOLATION_CHILD", found["names"])

    async def test_normal_json_child_is_reaped(self):
        self.assertEqual(await self.launch("import json; print(json.dumps({'ok':True}))", deadline=2), {"ok": True})

    async def test_real_fixed_child_fails_closed_without_store_or_network(self):
        from tests.test_mcp_trial import TRIAL_ENV, INIT, HEADERS
        with patch.dict(os.environ, {**TRIAL_ENV, "SUPABASE_SERVICE_ROLE_KEY": "",
                                    "LI_TRIAL_ISOLATION_MODE": "required"}):
            result = await watchdog.run_json_child(next(iter(watchdog.CHILD_MODULES)), {
                "body": json.dumps(INIT), "headers": {k.lower():v for k,v in HEADERS.items()},
                "method": "POST", "ip": "isolated-test"}, deadline=time.monotonic()+5)
        self.assertEqual(result["status"], 503)
        self.assertIn("temporarily_unavailable", result["body"])

    async def test_client_module_is_not_executable(self):
        with patch.object(asyncio, "create_subprocess_exec") as create:
            with self.assertRaises(watchdog.ChildRefused):
                await watchdog.run_json_child("client_supplied_code", {}, deadline=time.monotonic()+1)
            create.assert_not_called()

    async def test_expired_request_refuses_before_process_or_admission(self):
        with patch.object(asyncio, "create_subprocess_exec") as create:
            with self.assertRaises(watchdog.ChildRefused):
                await watchdog.run_json_child(next(iter(watchdog.CHILD_MODULES)), {}, deadline=time.monotonic()-1)
            create.assert_not_called()


class TrialIsolationEndpointTests(unittest.TestCase):
    def test_required_unverified_isolation_refuses_before_admission(self):
        from starlette.testclient import TestClient
        from lofgren_intelligence.hosted import trial, web_app
        from tests.test_mcp_trial import TRIAL_ENV, HEADERS, INIT
        with patch.dict(os.environ, {**TRIAL_ENV, "LI_TRIAL_ISOLATION_MODE": "required",
                                    "LI_TRIAL_ISOLATION_VERIFIED": "false", "LI_TRIAL_ISOLATION_CHILD": "1"}), \
             patch.object(trial, "take_request_allowance") as admission, \
             TestClient(web_app.build_app(), base_url=TRIAL_ENV["LI_PUBLIC_BASE_URL"]) as client:
            result = client.post("/mcp-trial", json=INIT, headers=HEADERS)
        self.assertEqual(result.status_code, 503)
        self.assertEqual(result.headers["cache-control"], "no-store")
        admission.assert_not_called()

    def test_isolated_origin_and_host_rejected_before_child_or_admission(self):
        from unittest.mock import AsyncMock
        from starlette.testclient import TestClient
        from lofgren_intelligence.hosted import trial, web_app
        from tests.test_mcp_trial import TRIAL_ENV, HEADERS, INIT
        with patch.dict(os.environ, {**TRIAL_ENV, "LI_TRIAL_ISOLATION_MODE": "required",
                                    "LI_TRIAL_ISOLATION_VERIFIED": "true"}), \
             patch.object(trial, "take_request_allowance") as admission, \
             patch.object(watchdog, "run_json_child", AsyncMock()) as spawn, \
             TestClient(web_app.build_app(), base_url=TRIAL_ENV["LI_PUBLIC_BASE_URL"]) as client:
            bad_origin = client.post("/mcp-trial", json=INIT, headers={**HEADERS, "Origin": "https://evil.invalid"})
            bad_host = client.post("/mcp-trial", json=INIT, headers={**HEADERS, "Host": "evil.invalid"})
        self.assertEqual(bad_origin.status_code, 403)
        self.assertEqual(bad_host.status_code, 421)
        spawn.assert_not_called()
        admission.assert_not_called()

    def test_supervisor_covers_admission_and_strips_credentials(self):
        from unittest.mock import AsyncMock
        from starlette.testclient import TestClient
        from lofgren_intelligence.hosted import trial, web_app
        from tests.test_mcp_trial import TRIAL_ENV, HEADERS, INIT
        supervised = AsyncMock(return_value={"status": 200, "headers": {"content-type": "application/json"},
                                            "body": '{"ok":true}'})
        with patch.dict(os.environ, {**TRIAL_ENV, "LI_TRIAL_ISOLATION_MODE": "required",
                                    "LI_TRIAL_ISOLATION_VERIFIED": "true", "LI_TRIAL_ISOLATION_CHILD": ""}), \
             patch.object(trial, "take_request_allowance") as admission, \
             patch.object(watchdog, "run_json_child", supervised), \
             TestClient(web_app.build_app(), base_url=TRIAL_ENV["LI_PUBLIC_BASE_URL"]) as client:
            result = client.post("/mcp-trial", json=INIT, headers={**HEADERS, "Authorization": "Bearer private",
                                                                 "Cookie": "private=1",
                                                                 "Origin": TRIAL_ENV["LI_PUBLIC_BASE_URL"],
                                                                 "X-Forwarded-For": "8.8.8.8",
                                                                 "X-Real-IP": "8.8.8.8"})
        self.assertEqual(result.status_code, 200)
        admission.assert_not_called()
        args, kwargs = supervised.call_args
        self.assertEqual(args[0], "lofgren_intelligence.hosted.trial_child")
        self.assertNotIn("authorization", args[1]["headers"])
        self.assertNotIn("cookie", args[1]["headers"])
        self.assertEqual(args[1]["headers"]["origin"], TRIAL_ENV["LI_PUBLIC_BASE_URL"])
        self.assertNotIn("x-forwarded-for", args[1]["headers"])
        self.assertNotIn("x-real-ip", args[1]["headers"])
        self.assertEqual(args[1]["ip"], "testclient")
        self.assertLessEqual(kwargs["deadline"]-time.monotonic(), 50)
        # This mock never started a child and does not consume the disconnect coroutine.
        kwargs["disconnected"].close()
