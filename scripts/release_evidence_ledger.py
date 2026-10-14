#!/usr/bin/env python3
"""Assemble JSON and Markdown ledgers from operator-supplied release evidence."""

from __future__ import annotations

import argparse
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from lofgren_intelligence.hosted.certification import evaluate_public_mcp_manifest
from lofgren_intelligence.hosted.evidence_ledger import (
    DEFAULT_MAX_AGE_SECONDS,
    assemble_evidence_ledger,
    render_markdown,
)


def _head() -> str | None:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    return result.stdout.strip() if result.returncode == 0 else None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--evidence", required=True)
    parser.add_argument("--trust-policy", required=True)
    parser.add_argument("--output-json", required=True)
    parser.add_argument("--output-markdown", required=True)
    parser.add_argument("--max-age-seconds", type=int, default=DEFAULT_MAX_AGE_SECONDS)
    parser.add_argument(
        "--evaluated-at",
        help="optional deterministic UTC time such as 2026-10-11T23:00:00Z",
    )
    args = parser.parse_args()

    evidence_path = Path(args.evidence).resolve()
    policy_path = Path(args.trust_policy).resolve()
    manifest = json.loads(evidence_path.read_text(encoding="utf-8"))
    policy = json.loads(policy_path.read_text(encoding="utf-8"))
    head = _head()
    gate = evaluate_public_mcp_manifest(
        manifest,
        manifest_dir=evidence_path.parent,
        trust_policy=policy,
        local_head=head,
    )
    if args.evaluated_at:
        if not args.evaluated_at.endswith("Z"):
            parser.error("--evaluated-at must be UTC and end in Z")
        evaluated_at = datetime.fromisoformat(args.evaluated_at[:-1] + "+00:00")
    else:
        evaluated_at = datetime.now(timezone.utc)

    ledger = assemble_evidence_ledger(
        gate=gate,
        manifest=manifest,
        manifest_dir=evidence_path.parent,
        evaluated_at=evaluated_at,
        max_age_seconds=args.max_age_seconds,
    )
    Path(args.output_json).write_text(
        json.dumps(ledger, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    Path(args.output_markdown).write_text(render_markdown(ledger), encoding="utf-8")
    print(
        f"EvidenceLedger = {ledger['proven']}/{ledger['total']} "
        f"PublicMCPReady = {'TRUE' if ledger['PublicMCPReady'] else 'FALSE'}"
    )
    return 0 if ledger["PublicMCPReady"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

