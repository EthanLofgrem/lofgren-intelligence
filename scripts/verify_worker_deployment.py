#!/usr/bin/env python3
"""Offline worker deployment contract check; does not contact or deploy a host.

Requires PyYAML in the verification environment, not in the worker runtime.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import sys


def inspect(path: Path, expected_sha: str) -> dict:
    try:
        import yaml
    except ImportError:
        return {"status": "BLOCKED", "errors": ["Install PyYAML in an isolated verification environment."]}
    try:
        document = yaml.safe_load(path.read_text())
        services = document["services"]
        if not isinstance(services, list) or len(services) != 1:
            raise ValueError("exactly one worker service is required")
        service = services[0]
        entries = service["envVars"]
        keys = [item["key"] for item in entries]
        if len(keys) != len(set(keys)):
            raise ValueError("duplicate environment keys")
        env = {item["key"]: item for item in entries}
    except (OSError, KeyError, TypeError, ValueError, yaml.YAMLError):
        return {"status": "FAIL", "errors": ["Malformed or unsupported worker configuration."]}
    errors: list[str] = []
    def require(condition: bool, message: str) -> None:
        if not condition:
            errors.append(message)
    require(bool(re.fullmatch(r"[0-9a-f]{40}", expected_sha)), "Expected application SHA must be a complete lowercase commit hash.")
    require(service.get("type") == "worker", "Service must be a background worker.")
    require(service.get("runtime") == "python", "Worker runtime must be Python.")
    require(service.get("numInstances") == 1, "Initial staging deployment must have one instance.")
    require(service.get("autoDeployTrigger") == "off", "Automatic deployment must be disabled.")
    require(service.get("plan") != "free", "Render background workers do not have a free hosting plan.")
    require(not document.get("databases"), "This deployment must not provision an additional database.")
    require(not service.get("disk"), "This deployment must not provision a persistent disk.")
    require(service.get("startCommand") == ".venv/bin/python -m lofgren_intelligence.hosted.worker", "Start command must invoke the durable worker directly.")
    build = service.get("buildCommand", "")
    require(f"git checkout --detach {expected_sha}" in build and f'"$(git rev-parse HEAD)" = "{expected_sha}"' in build, "Build must check out and assert the exact application SHA.")
    require("uv sync --frozen --extra hosted --no-dev" in build, "Install must use the frozen hosted dependency lock.")
    require(bool(re.search(r"uv==[0-9]+\.[0-9]+\.[0-9]+", build)), "uv must be pinned to an exact version.")
    def value(key: str):
        return env.get(key, {}).get("value")
    require(value("LI_RELEASE_SHA") == expected_sha, "Release identity must match the application pin.")
    require(value("LI_EXTERNAL_EXECUTION_ENABLED") == "false", "External effects must remain disabled.")
    require(env.get("SUPABASE_SERVICE_ROLE_KEY") == {"key": "SUPABASE_SERVICE_ROLE_KEY", "sync": False}, "Service-role key must be supplied privately at creation, with no inline value.")
    require(bool(re.fullmatch(r"3\.[0-9]+\.[0-9]+", str(value("PYTHON_VERSION")))), "Python must be pinned to an exact version.")
    try:
        heartbeat = float(value("LI_WORKER_HEARTBEAT_SECONDS"))
        lease = float(value("LI_WORKER_LEASE_SECONDS"))
        grace = float(value("LI_WORKER_SHUTDOWN_GRACE_SECONDS"))
        host_shutdown = float(service["maxShutdownDelaySeconds"])
        require(0 < heartbeat <= lease / 3, "Heartbeat must fit comfortably within the lease.")
        require(0 <= grace < host_shutdown <= 300, "Host shutdown allowance must exceed application grace and remain bounded.")
        require(int(value("LI_WORKER_CONCURRENCY")) == 1, "Initial staging concurrency must be one.")
    except (TypeError, ValueError, KeyError):
        errors.append("Invalid timing or concurrency configuration.")
    return {"status": "FAIL" if errors else "PASS", "application_sha": expected_sha,
            "errors": errors, "proof_scope": "Offline configuration contract only; hosted execution, cost approval and secret installation remain unverified."}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("config", type=Path)
    parser.add_argument("--application-sha", required=True)
    args = parser.parse_args()
    result = inspect(args.config, args.application_sha)
    print(json.dumps(result, indent=2))
    return 0 if result["status"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
