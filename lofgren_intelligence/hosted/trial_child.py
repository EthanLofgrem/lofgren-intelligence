"""Private subprocess entrypoint for one bounded anonymous HTTP request.

Only TrialEndpoint can launch it. There is no module/code/command field in the
input and it never dispatches to an authenticated route.
"""
from __future__ import annotations
import json
import os
import sys


def main() -> int:
    from .process_watchdog import MAX_INPUT_BYTES
    raw = sys.stdin.buffer.read(MAX_INPUT_BYTES + 1)
    if len(raw) > MAX_INPUT_BYTES or os.environ.get("LI_TRIAL_ISOLATION_CHILD") != "1":
        return 2
    try:
        payload = json.loads(raw)
        if not isinstance(payload, dict) or set(payload) != {"body", "headers", "method", "ip"}:
            return 2
        body = payload["body"]
        method = payload["method"]
        ip = payload["ip"]
        allowed_headers = {"accept", "content-type", "mcp-protocol-version", "mcp-method", "mcp-name", "host", "origin"}
        if (not isinstance(body, str) or len(body.encode()) > 16_384 or
            method not in {"POST", "GET", "DELETE"} or not isinstance(ip, str) or len(ip) > 128 or
            not isinstance(payload["headers"], dict) or
            any(k not in allowed_headers or not isinstance(v, str) or len(v) > 1024
                for k, v in payload["headers"].items())):
            return 2
        # Logs go only to discarded stderr; stdout is the bounded result channel.
        from starlette.testclient import TestClient
        from . import trial
        trial.ISOLATION_CHILD_PROCESS = True
        from .web_app import build_app, public_base
        with TestClient(build_app(), base_url=public_base(), client=(ip, 0)) as client:
            response = client.request(method, "/mcp-trial", content=body.encode(), headers=payload["headers"])
        if len(response.content) > 550_000:
            return 2
        headers = {k: v for k, v in response.headers.items()
                   if k.lower() in {"content-type", "cache-control", "pragma", "retry-after"}}
        sys.stdout.write(json.dumps({"status": response.status_code, "headers": headers,
                                     "body": response.text}, separators=(",", ":")))
        return 0
    except Exception:
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
