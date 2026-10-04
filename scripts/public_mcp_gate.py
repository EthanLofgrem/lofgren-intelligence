#!/usr/bin/env python3
"""Evaluate public MCP release evidence. Unknown is false."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from lofgren_intelligence.hosted.certification import TERMS, evaluate_public_mcp


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--evidence", required=True)
    args = parser.parse_args()
    evidence = json.loads(Path(args.evidence).read_text(encoding="utf-8"))
    gate = evaluate_public_mcp(evidence)
    print("Lofgren Intelligence Public MCP Gate")
    print("=" * 42)
    for term in TERMS:
        print(f"{term:38} {'TRUE' if gate.values[term] else 'FALSE'}")
    print("-" * 42)
    print(f"TRUE: {gate.passed_count}/{gate.total}")
    print(f"PublicMCPReady = {'TRUE' if gate.ready else 'FALSE'}")
    return 0 if gate.ready else 1


if __name__ == "__main__":
    raise SystemExit(main())
