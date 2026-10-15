"""Opt-in, Preview-only bounded subprocess acceptance; no research activation."""
import asyncio
import json
import os
import secrets
import time
from starlette.responses import JSONResponse
from .process_watchdog import ChildRefused, run_json_child

MODULE = "lofgren_intelligence.hosted.runtime_probe_child"
_LOCK = asyncio.Lock()
_USED = False


async def runtime_probe(request):
    global _USED
    headers = {"Cache-Control": "no-store", "Pragma": "no-cache"}
    def reply(body, status=200):
        return JSONResponse(body, status_code=status, headers=headers)
    secret = os.environ.get("LI_RUNTIME_PROBE_TOKEN", "")
    if os.environ.get("VERCEL_ENV") != "preview" or len(secret) < 32:
        return reply({"error": "not_found"}, 404)
    # Compare bytes: a str compare_digest raises TypeError on non-ASCII header text,
    # which would turn an unauthenticated request into a server fault instead of 401.
    presented = request.headers.get("authorization", "").encode("latin-1", "replace")
    if not secrets.compare_digest(presented, ("Bearer " + secret).encode("utf-8")):
        return reply({"error": "unauthorized"}, 401)
    # No client input, commands, URLs or network requests. Ignore no data silently.
    if request.headers.get("content-length", "0") != "0" or request.headers.get("transfer-encoding"):
        return reply({"error": "body_not_allowed"}, 400)
    if _LOCK.locked() or _USED:
        return reply({"error": "probe_already_used"}, 429)
    async with _LOCK:
        _USED = True  # at most one attempt per warm instance
        start = time.monotonic()
        normal, blocked = {}, {}
        try:
            result = await run_json_child(MODULE, {"mode": "launch"}, deadline=start + 2, lifecycle=normal)
            try:
                await run_json_child(MODULE, {"mode": "timeout"}, deadline=time.monotonic() + 1, lifecycle=blocked)
            except ChildRefused as exc:
                timed_out = str(exc) == "request deadline exceeded"
            else:
                timed_out = False
            try:
                blocked_startup = json.loads(blocked.get("startup_output", b""))
            except (ValueError, UnicodeError):
                blocked_startup = None
            passed = (blocked_startup == result and result == {"started": True, "network_denied": True, "clean_environment": True}
                      and normal.get("reaped") is True and normal.get("returncode") == 0
                      and timed_out and blocked.get("output_seen") is True
                      and blocked.get("reaped") is True and blocked.get("returncode") == -9
                      and blocked.get("pid_absent") is True and normal.get("pid_absent") is True)
            return reply({"probe": "runtime-process/1", "passed": passed,
                          "launch": normal.get("launched", False), "network_denied": result.get("network_denied", False),
                          "clean_environment": result.get("clean_environment", False), "timeout": timed_out,
                          "kill": blocked.get("returncode") == -9, "reap": blocked.get("reaped", False), "pid_absent": blocked.get("pid_absent", False),
                          "elapsed_ms": round((time.monotonic()-start)*1000),
                          "release_sha": os.environ.get("VERCEL_GIT_COMMIT_SHA", "unknown")}, 200 if passed else 503)
        except ChildRefused:
            return reply({"probe": "runtime-process/1", "passed": False, "error": "runtime_unavailable"}, 503)
