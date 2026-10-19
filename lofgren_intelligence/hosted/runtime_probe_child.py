"""Fixed local-only probe. Never invokes research handlers, DB clients or providers."""
import json
import os
import signal
import socket
import sys
import time


def main():
    def deny_network(event, args):
        if event.startswith("socket."):
            raise PermissionError("network disabled")
    sys.addaudithook(deny_network)
    payload = json.loads(sys.stdin.buffer.read(128))
    if payload not in ({"mode": "launch"}, {"mode": "timeout"}):
        raise SystemExit(2)
    try:
        socket.socket()
    except PermissionError:
        denied = True
    else:
        raise SystemExit(3)
    # Only trusted import-path plumbing can reach this process.
    safe_env = set(os.environ) <= {"PYTHONPATH", "LC_CTYPE"}
    if not safe_env:
        raise SystemExit(4)
    print(json.dumps({"started": True, "network_denied": denied, "clean_environment": safe_env}), flush=True)
    if payload["mode"] == "timeout":
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        while True:
            time.sleep(60)


if __name__ == "__main__":
    main()
