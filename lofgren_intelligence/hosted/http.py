"""Small JSON HTTP client used by the hosted layer (stdlib only)."""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable


class HTTPError(RuntimeError):
    def __init__(self, status: int, message: str, body: Any = None) -> None:
        super().__init__(f"{status}: {message}")
        self.status = status
        self.body = body


@dataclass
class JSONResponse:
    status: int
    headers: dict[str, str]
    body: Any


def json_request(
    url: str,
    method: str = "GET",
    *,
    headers: dict[str, str] | None = None,
    body: Any = None,
    timeout: float = 30.0,
    opener: Callable[..., Any] = urllib.request.urlopen,
) -> JSONResponse:
    data = None if body is None else json.dumps(body, separators=(",", ":")).encode("utf-8")
    h = {"accept": "application/json", **(headers or {})}
    if body is not None:
        h.setdefault("content-type", "application/json")
    req = urllib.request.Request(url, data=data, method=method, headers=h)
    try:
        with opener(req, timeout=timeout) as resp:
            raw = resp.read()
            payload = None if not raw else json.loads(raw.decode("utf-8"))
            return JSONResponse(
                int(getattr(resp, "status", 200)),
                {str(k).lower(): str(v) for k, v in resp.headers.items()},
                payload,
            )
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            payload = json.loads(raw.decode("utf-8")) if raw else None
        except Exception:
            payload = raw.decode("utf-8", "replace") if raw else None
        message = ""
        if isinstance(payload, dict):
            message = str(payload.get("message") or payload.get("error_description") or payload.get("error") or "")
        raise HTTPError(exc.code, message or exc.reason, payload) from exc


def with_query(url: str, params: dict[str, Any]) -> str:
    clean = {k: v for k, v in params.items() if v is not None}
    return url + ("&" if "?" in url else "?") + urllib.parse.urlencode(clean, doseq=True)
