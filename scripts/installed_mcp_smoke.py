"""Smoke-test an installed lofgren executable as an external MCP client."""

from __future__ import annotations

import json
import subprocess
import sys


def main() -> int:
    exe = sys.argv[1] if len(sys.argv) > 1 else "lofgren"
    messages = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize",
         "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "ci", "version": "1"}}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
    ]
    payload = "".join(json.dumps(x) + "\n" for x in messages)
    proc = subprocess.run([exe, "mcp"], input=payload, text=True, capture_output=True, timeout=30)
    if proc.returncode != 0:
        print(proc.stderr, file=sys.stderr)
        return 1
    replies = [json.loads(line) for line in proc.stdout.splitlines() if line.strip()]
    by_id = {row.get("id"): row for row in replies if row.get("id") is not None}
    if by_id.get(1, {}).get("result", {}).get("protocolVersion") != "2025-06-18":
        raise SystemExit("initialize did not negotiate expected protocol")
    tools = by_id.get(2, {}).get("result", {}).get("tools", [])
    names = {x.get("name") for x in tools}
    required = {"compile_objective", "investigate", "verify_claim", "get_receipt", "export_state"}
    missing = required - names
    if missing:
        raise SystemExit(f"missing MCP tools: {sorted(missing)}")
    print(f"installed MCP smoke PASS ({len(names)} tools)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
