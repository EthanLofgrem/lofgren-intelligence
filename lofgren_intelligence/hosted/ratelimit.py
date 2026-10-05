"""In-process, per-client-IP rate limiting for unauthenticated HTTP endpoints.

The MCP endpoint is limited per user in the database (`li_take_rate_limit`).
The OAuth, account and action-approval endpoints are reachable before a user
is known (`/oauth/register` writes a row per call), so they are limited here
per client IP.

Scope, stated plainly: the limiter lives in process memory, so it is
**per instance**. On a serverless host with many concurrent instances the
effective limit is (limit x instances), and a cold start resets it. It bounds
abuse from one client against one instance; a global limit belongs at the
hosting edge (provider firewall/WAF) or in a database-backed bucket.
"""

from __future__ import annotations

import math
import os
import threading
import time
from collections import deque
from typing import Any, Awaitable, Callable

from starlette.requests import Request
from starlette.responses import JSONResponse, Response

# bucket -> (default requests per window, window seconds)
DEFAULT_LIMITS: dict[str, tuple[int, int]] = {
    "oauth_register": (10, 60),
    "oauth_authorize": (60, 60),
    "oauth_complete": (20, 60),
    "oauth_token": (60, 60),
    "account": (20, 60),
    "actions": (60, 60),
}

MAX_TRACKED_KEYS = 10_000


class SlidingWindowLimiter:
    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._hits: dict[tuple[str, str], deque[float]] = {}
        self._lock = threading.Lock()

    def reset(self) -> None:
        with self._lock:
            self._hits.clear()

    def take(self, bucket: str, key: str, limit: int, window_s: int) -> float:
        """Record one request; return 0 if allowed, else seconds until a slot frees."""
        now = self._clock()
        with self._lock:
            if len(self._hits) >= MAX_TRACKED_KEYS:
                self._prune(now)
            hits = self._hits.setdefault((bucket, key), deque())
            while hits and hits[0] <= now - window_s:
                hits.popleft()
            if len(hits) >= limit:
                return max(0.001, hits[0] + window_s - now)
            hits.append(now)
            return 0.0

    def _prune(self, now: float) -> None:
        longest = max(w for _, w in DEFAULT_LIMITS.values())
        for k in [k for k, v in self._hits.items() if not v or v[-1] <= now - longest]:
            del self._hits[k]
        if len(self._hits) >= MAX_TRACKED_KEYS:
            # Still full of active keys: drop the oldest half rather than grow unbounded.
            for k in sorted(self._hits, key=lambda k: self._hits[k][-1])[: MAX_TRACKED_KEYS // 2]:
                del self._hits[k]


LIMITER = SlidingWindowLimiter()


def _limit_for(bucket: str) -> tuple[int, int]:
    limit, window = DEFAULT_LIMITS[bucket]
    raw = os.environ.get("LI_IP_RATE_LIMIT_" + bucket.upper(), "")
    if raw:
        try:
            limit = max(1, int(raw))
        except ValueError:
            pass
    return limit, window


def client_ip(request: Request) -> str:
    """The caller's IP.

    Forwarded headers are client-controlled unless the host overwrites them, so
    one is used only when the operator names it in LI_CLIENT_IP_HEADER (for
    example `x-real-ip` or `x-forwarded-for` on a platform that sets it).
    """
    header = os.environ.get("LI_CLIENT_IP_HEADER", "").strip().lower()
    if header:
        value = request.headers.get(header, "")
        first = value.split(",", 1)[0].strip()
        if first:
            return first
    return request.client.host if request.client else "unknown"


def rate_limited(bucket: str, handler: Callable[[Request], Awaitable[Response]]):
    """Wrap a Starlette endpoint with a per-IP limit for `bucket`."""
    if bucket not in DEFAULT_LIMITS:
        raise ValueError(f"unknown rate-limit bucket {bucket!r}")

    async def limited(request: Request) -> Any:
        limit, window = _limit_for(bucket)
        wait = LIMITER.take(bucket, client_ip(request), limit, window)
        if wait:
            return JSONResponse(
                {"error": "rate_limited", "error_description": "too many requests; retry later"},
                status_code=429,
                headers={"Retry-After": str(max(1, math.ceil(wait))), "Cache-Control": "no-store"},
            )
        return await handler(request)

    limited.__name__ = getattr(handler, "__name__", "limited")
    limited.__doc__ = handler.__doc__
    return limited
