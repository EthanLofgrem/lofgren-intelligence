"""Rate limiting for endpoints reachable before a user is known.

The MCP endpoint is limited per user in the database (`li_take_rate_limit`).
The OAuth, account and action-approval endpoints, and Founding Free
activation, are reachable before (or while) a user is established, so they are
limited per client IP here.

Two backends, one rule (sliding window of `limit` requests per `window` s):

* **Store-backed (global).** When the Supabase store is configured
  (`SUPABASE_URL` and `SUPABASE_SERVICE_ROLE_KEY`), every take goes through
  the atomic SQL function `li_take_keyed_rate_limit`, so the limit holds
  across all serverless instances and survives cold starts. Keys are sent as
  SHA-256 digests; raw IPs and email domains never reach the database.
* **In-memory (per instance).** Used when the store is not configured (local
  development, tests) or `LI_RATE_LIMIT_BACKEND=memory`, and as the fallback
  when the store errors for an ordinary bucket. In memory the limit is per
  instance: with N instances the effective limit is up to N times higher.

Fail closed: for the sign-up/activation buckets (`FAIL_CLOSED_BUCKETS`) a store
error refuses the request (HTTP 503 with Retry-After) instead of falling back,
because an attacker who can make the store error must not gain unlimited
registrations or Founding Free activations.
"""

from __future__ import annotations

import hashlib
import math
import os
import threading
import time
from collections import deque
from typing import Any, Awaitable, Callable

from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from .store import SupabaseStore

# bucket -> (default requests per window, window seconds)
DEFAULT_LIMITS: dict[str, tuple[int, int]] = {
    "oauth_register": (10, 60),
    "oauth_authorize": (60, 60),
    "oauth_complete": (20, 60),
    "oauth_token": (60, 60),
    "account": (20, 60),
    "actions": (60, 60),
}

# Sign-up/activation buckets: a store error refuses instead of falling back.
FAIL_CLOSED_BUCKETS = frozenset({"oauth_register", "oauth_complete"})

MAX_TRACKED_KEYS = 10_000
UNAVAILABLE_RETRY_AFTER_S = 30


class RateLimitUnavailable(RuntimeError):
    """The global limiter could not answer for a fail-closed bucket; refuse the request."""


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


def limit_for(bucket: str) -> tuple[int, int]:
    limit, window = DEFAULT_LIMITS[bucket]
    raw = os.environ.get("LI_IP_RATE_LIMIT_" + bucket.upper(), "")
    if raw:
        try:
            limit = max(1, int(raw))
        except ValueError:
            pass
    return limit, window


_limit_for = limit_for  # backwards-compatible name


def rate_key(bucket: str, key: str) -> str:
    """The digest stored for (bucket, key); the raw key never leaves the process."""
    return hashlib.sha256(f"{bucket}:{key}".encode("utf-8")).hexdigest()


def store_backend_configured() -> bool:
    if os.environ.get("LI_RATE_LIMIT_BACKEND", "auto").strip().lower() == "memory":
        return False
    return bool(os.environ.get("SUPABASE_URL") and os.environ.get("SUPABASE_SERVICE_ROLE_KEY"))


def take(bucket: str, key: str, *, store: Any = None, limit: int | None = None,
         window: int | None = None) -> float:
    """Take one request for (bucket, key). Returns 0 when allowed, else seconds to wait.

    `store` forces the store-backed path (the service passes the store it already
    holds); otherwise the store is used when configured. Raises
    RateLimitUnavailable when the store fails for a fail-closed bucket.
    """
    if bucket not in DEFAULT_LIMITS:
        raise ValueError(f"unknown rate-limit bucket {bucket!r}")
    default_limit, default_window = limit_for(bucket)
    limit = default_limit if limit is None else limit
    window = default_window if window is None else window
    if store is not None or store_backend_configured():
        try:
            backend = store if store is not None else SupabaseStore()
            return float(backend.take_keyed_rate_limit(bucket, rate_key(bucket, key), limit, window))
        except Exception:
            if bucket in FAIL_CLOSED_BUCKETS:
                raise RateLimitUnavailable("rate limiter is unavailable") from None
            # Ordinary buckets degrade to the per-instance limiter rather than to no limit.
    return LIMITER.take(bucket, key, limit, window)


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
        ip = client_ip(request)
        try:
            # The store-backed take is a blocking HTTP call; keep it off the event loop.
            wait = await run_in_threadpool(take, bucket, ip)
        except RateLimitUnavailable:
            return JSONResponse(
                {"error": "temporarily_unavailable", "error_description": "please retry shortly"},
                status_code=503,
                headers={"Retry-After": str(UNAVAILABLE_RETRY_AFTER_S), "Cache-Control": "no-store"},
            )
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
