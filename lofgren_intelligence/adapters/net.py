"""Network safety for public-source retrieval.

Public HTTP(S) reads are pinned to an IP address that was resolved and checked
before the connection is opened. Redirects are resolved and checked again.
This closes the DNS-validation/request race that a simple preflight lookup
would leave open.
"""

from __future__ import annotations

import http.client
import ipaddress
import socket
import ssl
import urllib.parse
import urllib.request
from email.message import Message
from typing import Any


class UnsafeURL(ValueError):
    pass


MAX_RESPONSE_BYTES = 2_000_000
MAX_REDIRECTS = 5
_REDIRECTS = {301, 302, 303, 307, 308}


_NAT64 = ipaddress.ip_network("64:ff9b::/96")


def _embedded_ipv4(ip: ipaddress.IPv6Address) -> ipaddress.IPv4Address | None:
    """IPv4 address carried inside an IPv6 one (mapped, 6to4, NAT64, compat)."""
    if ip.ipv4_mapped is not None:
        return ip.ipv4_mapped
    if ip.sixtofour is not None:
        return ip.sixtofour
    if ip in _NAT64:
        return ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)
    if int(ip) >> 32 == 0 and int(ip) > 1:  # deprecated IPv4-compatible ::a.b.c.d
        return ipaddress.IPv4Address(int(ip))
    return None


def _public_ip(value: str) -> bool:
    ip = ipaddress.ip_address(value.split("%", 1)[0])
    if isinstance(ip, ipaddress.IPv6Address):
        inner = _embedded_ipv4(ip)
        if inner is not None and not _public_ip(str(inner)):
            return False
    # is_global excludes shared/CGNAT space (100.64.0.0/10, used for cloud
    # metadata on some providers), benchmarking, documentation and other
    # special-purpose ranges that the private/loopback checks miss.
    return ip.is_global and not (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    )


def _resolve_public(host: str, port: int) -> list[str]:
    try:
        literal = ipaddress.ip_address(host.strip("[]"))
        if not _public_ip(str(literal)):
            raise UnsafeURL("non-public IP addresses are not allowed")
        return [str(literal)]
    except ValueError:
        pass

    try:
        rows = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise UnsafeURL("hostname could not be resolved") from exc
    ips: list[str] = []
    for row in rows:
        addr = str(row[4][0])
        if not _public_ip(addr):
            raise UnsafeURL("hostname resolves to a non-public address")
        if addr not in ips:
            ips.append(addr)
    if not ips:
        raise UnsafeURL("hostname could not be resolved")
    return ips


def validate_public_url(url: str) -> str:
    p = urllib.parse.urlsplit(url)
    if p.scheme.lower() not in {"http", "https"}:
        raise UnsafeURL("only http and https URLs are allowed")
    if not p.hostname:
        raise UnsafeURL("URL hostname is required")
    if p.username or p.password:
        raise UnsafeURL("credentials in URLs are not allowed")
    host = p.hostname.rstrip(".").lower()
    if host in {"localhost", "localhost.localdomain"} or host.endswith(".local"):
        raise UnsafeURL("local hostnames are not allowed")
    port = p.port or (443 if p.scheme.lower() == "https" else 80)
    _resolve_public(host, port)
    return url


class _PinnedHTTPConnection(http.client.HTTPConnection):
    def __init__(self, host: str, port: int, ip: str, timeout: float) -> None:
        super().__init__(host, port, timeout=timeout)
        self._pinned_ip = ip

    def connect(self) -> None:
        self.sock = socket.create_connection(
            (self._pinned_ip, self.port),
            self.timeout,
            self.source_address,
        )
        if self._tunnel_host:
            self._tunnel()


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, host: str, port: int, ip: str, timeout: float) -> None:
        super().__init__(host, port, timeout=timeout, context=ssl.create_default_context())
        self._pinned_ip = ip

    def connect(self) -> None:
        sock = socket.create_connection(
            (self._pinned_ip, self.port),
            self.timeout,
            self.source_address,
        )
        self.sock = self._context.wrap_socket(sock, server_hostname=self.host)


class _SafeResponse:
    def __init__(self, response: http.client.HTTPResponse, connection: http.client.HTTPConnection, url: str) -> None:
        self._response = response
        self._connection = connection
        self.url = url
        self.status = response.status
        self.headers: Message = response.headers

    def read(self, amt: int | None = None) -> bytes:
        limit = MAX_RESPONSE_BYTES if amt is None else min(max(0, int(amt)), MAX_RESPONSE_BYTES)
        data = self._response.read(limit + (1 if amt is None else 0))
        if amt is None and len(data) > MAX_RESPONSE_BYTES:
            raise ValueError("HTTP response exceeds public fetch limit")
        return data[:limit]

    def close(self) -> None:
        try:
            self._response.close()
        finally:
            self._connection.close()

    def __enter__(self) -> "_SafeResponse":
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.close()


def _request_parts(req_or_url: Any) -> tuple[str, str, dict[str, str]]:
    if isinstance(req_or_url, urllib.request.Request):
        url = req_or_url.full_url
        method = req_or_url.get_method().upper()
        headers = {str(k): str(v) for k, v in req_or_url.header_items()}
        if req_or_url.data is not None:
            raise UnsafeURL("public evidence fetches cannot send request bodies")
    else:
        url = str(req_or_url)
        method = "GET"
        headers = {}
    if method not in {"GET", "HEAD"}:
        raise UnsafeURL("public evidence fetches are read-only")
    return url, method, headers


def safe_urlopen(req_or_url: Any, timeout: float = 15.0) -> _SafeResponse:
    url, method, headers = _request_parts(req_or_url)

    for redirect_count in range(MAX_REDIRECTS + 1):
        validate_public_url(url)
        p = urllib.parse.urlsplit(url)
        assert p.hostname is not None
        scheme = p.scheme.lower()
        host = p.hostname.rstrip(".")
        port = p.port or (443 if scheme == "https" else 80)
        ips = _resolve_public(host, port)
        ip = ips[0]

        conn: http.client.HTTPConnection
        if scheme == "https":
            conn = _PinnedHTTPSConnection(host, port, ip, timeout)
        else:
            conn = _PinnedHTTPConnection(host, port, ip, timeout)

        path = urllib.parse.urlunsplit(("", "", p.path or "/", p.query, ""))
        request_headers = dict(headers)
        default_port = 443 if scheme == "https" else 80
        request_headers["Host"] = host if port == default_port else f"{host}:{port}"
        request_headers.setdefault("Connection", "close")
        try:
            conn.request(method, path, headers=request_headers)
            response = conn.getresponse()
        except Exception:
            conn.close()
            raise

        if response.status not in _REDIRECTS:
            return _SafeResponse(response, conn, url)

        location = response.headers.get("Location")
        response.close()
        conn.close()
        if not location:
            raise UnsafeURL("redirect response did not provide a Location")
        if redirect_count >= MAX_REDIRECTS:
            raise UnsafeURL("too many redirects")
        url = urllib.parse.urljoin(url, location)

    raise UnsafeURL("too many redirects")
