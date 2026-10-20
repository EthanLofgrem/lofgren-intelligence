#!/usr/bin/env python3
"""Validate a candidate-bound LI activation/rollback handoff."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from lofgren_intelligence.hosted.activation_package import validate_activation_package


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--package", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    path = Path(args.package).resolve()
    package = json.loads(path.read_text(encoding="utf-8"))
    result = validate_activation_package(package, package_dir=path.parent).as_dict()
    Path(args.output).write_text(
        json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(
        "ActivationPackageReady = "
        + ("TRUE" if result["ready_to_request_owner_activation"] else "FALSE")
    )
    return 0 if result["ready_to_request_owner_activation"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
