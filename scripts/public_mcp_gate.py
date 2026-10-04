#!/usr/bin/env python3
"""Evaluate evidence-backed public MCP release evidence. Unknown is false."""

from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path

from lofgren_intelligence.hosted.certification import TERMS, evaluate_public_mcp_manifest


def _head() -> str | None:
    out = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    return out.stdout.strip() if out.returncode == 0 else None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--evidence", required=True, help="release evidence manifest JSON")
    args = parser.parse_args()
    path = Path(args.evidence).resolve()
    manifest = json.loads(path.read_text(encoding="utf-8"))
    gate = evaluate_public_mcp_manifest(
        manifest,
        manifest_dir=path.parent,
        local_head=_head(),
    )
    print("Lofgren Intelligence Public MCP Gate")
    print("=" * 66)
    for term in TERMS:
        value = "TRUE" if gate.values[term] else "FALSE"
        print(f"{term:38} {value:5}  {gate.reasons[term]}")
    print("-" * 66)
    print(f"TRUE: {gate.passed_count}/{gate.total}")
    print(f"PublicMCPReady = {'TRUE' if gate.ready else 'FALSE'}")
    return 0 if gate.ready else 1


if __name__ == "__main__":
    raise SystemExit(main())
