"""Hard-bounded upstream fetch for the anonymous trial (`/mcp-trial`).

One `TrialFetcher` serves exactly one trial call. It is the `opener` of that
call's `PublicHTTPClient`, so every upstream byte of the call passes through it.
It enforces, at runtime:

* a request budget: at most `max_requests` HTTP requests per call (the trial
  uses 1). Retries and redirect hops would be further requests, so the trial
  runs with no retries and refuses redirects (`max_redirects=0`);
* a hard wall-clock deadline shared with the whole call. The connect and every
  socket operation use `min(op_timeout_s, time left)`, the body is read in
  small pieces with the deadline checked between them, and a watchdog timer
  shuts the socket down at the deadline, so a slow or drip-feeding server cannot
  hold the call past it;
* a body-size limit, checked against Content-Length before reading and against
  the bytes actually read (chunked or unsized bodies included);
* the same SSRF rules as `adapters.net.safe_urlopen`: http(s) only, no
  credentials, no local names, every address resolved and checked public and
  the connection pinned to the checked address.

Refusals raise `TrialFetchRefused` (an `UnsafeURL`, which `PublicHTTPClient`
never retries); deadline hits raise `TimeoutError`; an oversized body raises
`ValueError`. `PublicHTTPClient` turns each into `ProviderUnavailable`, so a
failed fetch is always reported as a failed retrieval, never as an empty one.
"""

from __future__ import annotations

import http.client
import socket
import threading
import time
import urllib.parse
from email.message import Message
from typing import Any, Callable

from ..adapters import net

REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
READ_CHUNK = 16_384


class TrialFetchRefused(net.UnsafeURL):
    """The trial's request budget, redirect bound or URL rules refused a request. Never retried."""


def resolve_public(host: str, port: int) -> list[str]:
    """Resolve and check that every address is public (module attribute so tests can allow loopback)."""
    return net._resolve_public(host, port)


def _target(url: str) -> tuple[str, str, int, str, str]:
    p = urllib.parse.urlsplit(url)
    scheme = p.scheme.lower()
    if scheme not in {"http", "https"}:
        raise TrialFetchRefused("only http and https URLs are allowed")
    if not p.hostname:
        raise TrialFetchRefused("URL hostname is required")
    if p.username or p.password:
        raise TrialFetchRefused("credentials in URLs are not allowed")
    host = p.hostname.rstrip(".")
    lowered = host.lower()
    if lowered in {"localhost", "localhost.localdomain"} or lowered.endswith((".local", ".localhost", ".internal")):
        raise TrialFetchRefused("local hostnames are not allowed")
    try:
        port = p.port or (443 if scheme == "https" else 80)
    except ValueError:
        raise TrialFetchRefused("invalid port") from None
    ip = resolve_public(host, port)[0]
    path = urllib.parse.urlunsplit(("", "", p.path or "/", p.query, ""))
    return scheme, host, port, ip, path


class _Response:
    """The fully read, bounded response handed back to PublicHTTPClient."""

    def __init__(self, status: int, headers: Message, body: bytes, url: str) -> None:
        self.status = status
        self.headers = headers
        self.url = url
        self._body = body

    def read(self, amt: int | None = None) -> bytes:
        return self._body if amt is None else self._body[: max(0, int(amt))]

    def close(self) -> None:
        pass

    def __enter__(self) -> "_Response":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()


class TrialFetcher:
    """Opener for one trial call: request budget, redirect bound, body limit and a hard deadline."""

    def __init__(self, *, deadline_at: float, max_requests: int = 1, max_redirects: int = 0,
                 max_body_bytes: int = 1_000_000, op_timeout_s: float = 10.0,
                 clock: Callable[[], float] = time.monotonic,
                 cancelled: Callable[[], bool] = lambda: False) -> None:
        if max_requests < 1 or max_redirects < 0 or max_body_bytes < 1 or op_timeout_s <= 0:
            raise ValueError("invalid trial fetch bounds")
        self.deadline_at = deadline_at
        self.max_requests = max_requests
        self.max_redirects = max_redirects
        self.max_body_bytes = max_body_bytes
        self.op_timeout_s = op_timeout_s
        self.clock = clock
        self.cancelled = cancelled
        self.attempts = 0         # HTTP requests actually sent (connections opened)
        self.refused = 0          # requests refused by the budget/redirect bound (never sent)
        self.bytes_read = 0
        self.aborted = False      # the watchdog closed a socket at the deadline

    def remaining(self) -> float:
        return self.deadline_at - self.clock()

    def __call__(self, req: Any, timeout: float | None = None) -> _Response:
        url, method, headers = net._request_parts(req)
        for hop in range(self.max_redirects + 1):
            if self.attempts >= self.max_requests:
                self.refused += 1
                raise TrialFetchRefused(f"the trial upstream request budget ({self.max_requests} per call) is used")
            if self.cancelled():
                raise TimeoutError("the trial call was stopped at its time bound")
            scheme, host, port, ip, path = _target(url)
            if self.remaining() <= 0:
                raise TimeoutError("the trial time bound was reached before the upstream request")
            self.attempts += 1
            status, response_headers, body = self._one(scheme, host, port, ip, path, method, headers)
            if status not in REDIRECT_STATUSES:
                return _Response(status, response_headers, body, url)
            if hop >= self.max_redirects:
                raise TrialFetchRefused(f"upstream redirect (HTTP {status}) not followed: the trial redirect "
                                        f"bound is {self.max_redirects}")
            location = response_headers.get("Location")
            if not location:
                raise TrialFetchRefused("redirect response did not provide a Location")
            url = urllib.parse.urljoin(url, location)
        raise TrialFetchRefused("too many redirects")  # pragma: no cover - loop always returns or raises

    def _abort(self, sock_box: dict[str, Any]) -> None:
        self.aborted = True
        sock = sock_box.get("sock")
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                sock.close()
            except OSError:
                pass

    def _one(self, scheme: str, host: str, port: int, ip: str, path: str, method: str,
             headers: dict[str, str]) -> tuple[int, Message, bytes]:
        remaining = self.remaining()
        op = max(0.001, min(self.op_timeout_s, remaining))
        conn: http.client.HTTPConnection
        if scheme == "https":
            conn = net._PinnedHTTPSConnection(host, port, ip, op)
        else:
            conn = net._PinnedHTTPConnection(host, port, ip, op)
        default_port = 443 if scheme == "https" else 80
        request_headers = dict(headers)
        request_headers["Host"] = host if port == default_port else f"{host}:{port}"
        request_headers["Connection"] = "close"
        sock_box: dict[str, Any] = {}
        watchdog = threading.Timer(max(0.0, remaining), self._abort, args=(sock_box,))
        watchdog.daemon = True
        watchdog.start()
        try:
            conn.connect()
            sock_box["sock"] = conn.sock
            if self.aborted:
                raise TimeoutError("the trial time bound was reached while connecting upstream")
            conn.request(method, path, headers=request_headers)
            response = conn.getresponse()
            status = int(response.status)
            response_headers = response.headers
            if status != 200:
                return status, response_headers, b""
            declared = response_headers.get("Content-Length")
            try:
                declared_bytes = int(declared) if declared is not None else None
            except ValueError:
                declared_bytes = None  # unreadable: the bytes actually read are still counted below
            if declared_bytes is not None and declared_bytes > self.max_body_bytes:
                raise ValueError(f"upstream body ({declared_bytes} bytes declared) exceeds the trial limit of "
                                 f"{self.max_body_bytes} bytes")
            chunks: list[bytes] = []
            total = 0
            while True:
                # Each read is bounded by the socket timeout set at connect (<= op_timeout_s and <= the time left
                # then); the watchdog shuts the socket at the deadline; the deadline is re-checked between reads.
                if self.remaining() <= 0 or self.aborted:
                    raise TimeoutError("the trial time bound was reached while reading the upstream body")
                chunk = response.read1(READ_CHUNK)
                if not chunk:
                    break
                total += len(chunk)
                self.bytes_read = total
                if total > self.max_body_bytes:
                    raise ValueError(f"upstream body exceeds the trial limit of {self.max_body_bytes} bytes")
                chunks.append(chunk)
            return status, response_headers, b"".join(chunks)
        except ValueError:
            if self.aborted:
                raise TimeoutError("the trial time bound was reached; the upstream connection was closed") from None
            raise
        except (OSError, http.client.HTTPException):
            if self.aborted:
                raise TimeoutError("the trial time bound was reached; the upstream connection was closed") from None
            raise
        finally:
            watchdog.cancel()
            try:
                conn.close()
            except OSError:
                pass
            sock = sock_box.get("sock")
            if sock is not None:
                try:
                    sock.close()
                except OSError:
                    pass
