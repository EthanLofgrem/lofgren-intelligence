"""Bounded process isolation for fixed internal operations, never client code."""
from __future__ import annotations

import asyncio
import json
import inspect
import os
import signal
import sys
import time
from typing import Any, Awaitable

CHILD_MODULES = frozenset({"lofgren_intelligence.hosted.trial_child"})
PROBE_MODULE = "lofgren_intelligence.hosted.runtime_probe_child"
# Exact allowlist: no paid-provider, search, billing, browser or proxy secrets.
CHILD_ENV_KEYS = frozenset({
    "PATH", "LANG", "LC_ALL", "TZ", "SSL_CERT_FILE", "SSL_CERT_DIR",
    "VERCEL_ENV", "VERCEL_URL", "LI_PUBLIC_BASE_URL", "LI_DEPLOYMENT_SURFACE",
    "LI_ALLOWED_ORIGINS", "LI_TRIAL_ENABLED", "LI_TRIAL_ISOLATION_MODE",
    "LI_TRIAL_IP_REQUESTS_PER_MINUTE", "LI_TRIAL_GLOBAL_REQUESTS_PER_MINUTE",
    "LI_TRIAL_IP_CALLS_PER_HOUR", "LI_TRIAL_SPEND_CEILING_USD",
    "LI_TRIAL_SPEND_WINDOW_SECONDS", "LI_TRIAL_SOURCES_MANIFEST",
    "SUPABASE_URL", "SUPABASE_SERVICE_ROLE_KEY", "SUPABASE_PUBLISHABLE_KEY",
})


def child_environment() -> dict[str, str]:
    env = {key: value for key, value in os.environ.items() if key in CHILD_ENV_KEYS}
    # Use trusted runtime import paths rather than copying arbitrary environment
    # settings. This supports serverless dependency bundles and isolated venvs.
    env["PYTHONPATH"] = os.pathsep.join(str(path) for path in sys.path if path)
    env["LI_TRIAL_ISOLATION_CHILD"] = "1"
    return env


MAX_INPUT_BYTES = 32_768
MAX_OUTPUT_BYTES = 1_100_000  # bounded encoded HTTP/MCP envelope, including duplicated text


class ChildRefused(RuntimeError):
    """Sanitized child failure; stderr and private exception text are discarded."""


async def run_json_child(module: str, payload: dict[str, Any], *, deadline: float,
                         disconnected: Awaitable[Any] | None = None,
                         lifecycle: dict[str, Any] | None = None) -> dict[str, Any]:
    def refuse(message: str) -> None:
        if inspect.iscoroutine(disconnected):
            disconnected.close()
        raise ChildRefused(message)
    if (module not in CHILD_MODULES and module != PROBE_MODULE) or os.name != "posix":
        refuse("isolation is unavailable")
    try:
        data = json.dumps(payload, allow_nan=False, separators=(",", ":")).encode()
    except (ValueError, TypeError):
        refuse("invalid child input")
    if len(data) > MAX_INPUT_BYTES or time.monotonic() >= deadline:
        refuse("request bound exceeded")
    child = None
    tasks: list[asyncio.Task[Any]] = []
    disconnect_task = None
    try:
        if disconnected is not None:
            disconnect_task = asyncio.ensure_future(disconnected)
        else:
            disconnect_task = None
        child = await asyncio.create_subprocess_exec(
            sys.executable, "-m", module, stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
            start_new_session=True,
            env=({"PYTHONPATH": os.pathsep.join(str(p) for p in sys.path if p)}
                 if module == PROBE_MODULE else child_environment()),
        )
        if lifecycle is not None:
            lifecycle["launched"] = True
        async def exchange() -> dict[str, Any]:
            assert child is not None and child.stdin is not None and child.stdout is not None
            child.stdin.write(data)
            await child.stdin.drain()
            child.stdin.close()
            chunks: list[bytes] = []
            count = 0
            while True:
                chunk = await child.stdout.read(16_384)
                if not chunk:
                    break
                if lifecycle is not None:
                    lifecycle["output_seen"] = True
                    if module == PROBE_MODULE:
                        lifecycle["startup_output"] = (lifecycle.get("startup_output", b"") + chunk)[:256]
                count += len(chunk)
                if count > MAX_OUTPUT_BYTES:
                    raise ChildRefused("child output bound exceeded")
                chunks.append(chunk)
            code = await child.wait()
            if code != 0:
                raise ChildRefused("child failed")
            try:
                result = json.loads(b"".join(chunks))
            except (ValueError, UnicodeError):
                raise ChildRefused("child returned invalid output") from None
            if not isinstance(result, dict):
                raise ChildRefused("child returned invalid output")
            return result
        work = asyncio.create_task(exchange())
        tasks.append(work)
        if disconnect_task is not None:
            tasks.append(disconnect_task)
        done, _ = await asyncio.wait(tasks, timeout=max(0, deadline-time.monotonic()),
                                     return_when=asyncio.FIRST_COMPLETED)
        if len(tasks) > 1 and tasks[1] in done:
            raise ChildRefused("client disconnected")
        if work not in done:
            raise ChildRefused("request deadline exceeded")
        return work.result()
    except OSError:
        raise ChildRefused("isolation is unavailable") from None
    finally:
        async def cleanup() -> None:
            # Kill before releasing capacity. Cancel the reader, then discard
            # residual pipe bytes: asyncio subprocess wait can otherwise wait
            # forever on a full, paused stdout transport after the process exits.
            if child is not None:
                try:
                    os.killpg(child.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            if disconnect_task is not None and disconnect_task not in tasks:
                tasks.append(disconnect_task)
            for task in tasks:
                if not task.done():
                    task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            if child is not None:
                if child.stdout is not None:
                    while await child.stdout.read(16_384):
                        pass  # discard without allocating an accumulated buffer
                await child.wait()
                if lifecycle is not None:
                    lifecycle.update(reaped=True, returncode=child.returncode)
                    try:
                        os.kill(child.pid, 0)
                    except ProcessLookupError:
                        lifecycle["pid_absent"] = True
                    else:
                        lifecycle["pid_absent"] = False
        cleanup_task = asyncio.create_task(cleanup())
        cancelled = False
        while not cleanup_task.done():
            try:
                await asyncio.shield(cleanup_task)
            except asyncio.CancelledError:
                cancelled = True  # defer even repeated cancellation until reaped
        cleanup_task.result()
        if cancelled:
            raise asyncio.CancelledError
