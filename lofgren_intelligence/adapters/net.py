"""Network safety for public-source retrieval.

Rejects local/private/link-local/multicast/reserved targets and re-validates
redirect destinations to prevent obvious SSRF paths.  DNS is resolved before
the request; clients should still run inside a network boundary that blocks
metadata/private address ranges as a second line of defense.
"""

from __future__ import annotations

import ipaddress
import socket
import urllib.parse
import urllib.request
from typing import Any


class UnsafeURL(ValueError):
    pass


def _public_ip(value: str) -> bool:
    ip = ipaddress.ip_address(value)
    return not (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    )


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
    try:
        literal = ipaddress.ip_address(host.strip("[]"))
        if not _public_ip(str(literal)):
            raise UnsafeURL("non-public IP addresses are not allowed")
    except ValueError:
        try:
            rows = socket.getaddrinfo(host, p.port or (443 if p.scheme == "https" else 80), type=socket.SOCK_STREAM)
        except socket.gaierror as exc:
            raise UnsafeURL("hostname could not be resolved") from exc
        if not rows:
            raise UnsafeURL("hostname could not be resolved")
        for row in rows:
            addr = row[4][0]
            if not _public_ip(addr):
                raise UnsafeURL("hostname resolves to a non-public address")
    return url


class _SafeRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str):
        validate_public_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_SAFE_OPENER = urllib.request.build_opener(_SafeRedirect())


def safe_urlopen(req_or_url: Any, timeout: float = 15.0):
    url = req_or_url.full_url if isinstance(req_or_url, urllib.request.Request) else str(req_or_url)
    validate_public_url(url)
    return _SAFE_OPENER.open(req_or_url, timeout=timeout)
