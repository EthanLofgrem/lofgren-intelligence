from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from lofgren_intelligence.hosted.certification import (
    MANIFEST_SCHEMA,
    TERMS,
    TRUST_SCHEMA,
    PublicMCPGate,
    evaluate_public_mcp_manifest,
)
from lofgren_intelligence.hosted.evidence_ledger import (
    LEDGER_SCHEMA,
    assemble_evidence_ledger,
    render_markdown,
)


class EvidenceLedgerTests(unittest.TestCase):
    SHA = "a" * 40
    NOW = datetime(2026, 10, 11, 23, 0, 0, tzinfo=timezone.utc)

    def _gate(self, passed: bool = True) -> PublicMCPGate:
        return PublicMCPGate(
            values={term: passed for term in TERMS},
            evidence={},
            reasons={term: "signature and trust policy verified" for term in TERMS},
        )

    def _manifest(self, root: Path, issued_at: str = "2026-10-11T22:00:00Z") -> dict:
        artifacts = []
        for index, term in enumerate(TERMS):
            name = f"{index:02d}-{term}.json"
            raw = json.dumps({"term": term, "issued_at": issued_at}).encode("utf-8")
            (root / name).write_bytes(raw)
            artifacts.append(
                {
                    "term": term,
                    "path": name,
                    "key_id": "operator",
                    "sha256": hashlib.sha256(raw).hexdigest(),
                }
            )
        return {"expected_sha": self.SHA, "artifacts": artifacts}

    def test_fresh_authenticated_unique_evidence_is_proven(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ledger = assemble_evidence_ledger(
                gate=self._gate(),
                manifest=self._manifest(root),
                manifest_dir=root,
                evaluated_at=self.NOW,
                max_age_seconds=7200,
            )
        self.assertEqual(ledger["schema"], LEDGER_SCHEMA)
        self.assertEqual(ledger["proven"], len(TERMS))
        self.assertTrue(ledger["PublicMCPReady"])
        self.assertIn("PublicMCPReady: TRUE", render_markdown(ledger))

    def test_missing_timestamp_is_blocked_even_when_signature_gate_passes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = self._manifest(root)
            first = manifest["artifacts"][0]
            raw = json.dumps({"term": first["term"]}).encode("utf-8")
            (root / first["path"]).write_bytes(raw)
            first["sha256"] = hashlib.sha256(raw).hexdigest()
            ledger = assemble_evidence_ledger(
                gate=self._gate(),
                manifest=manifest,
                manifest_dir=root,
                evaluated_at=self.NOW,
            )
        row = ledger["terms"][0]
        self.assertEqual(row["status"], "BLOCKED")
        self.assertEqual(row["blocker"], "signed artifact issued_at missing or malformed")
        self.assertFalse(ledger["PublicMCPReady"])

    def test_stale_and_future_evidence_are_blocked(self):
        for issued_at, expected in (
            ("2026-10-01T00:00:00Z", "stale"),
            ("2026-10-12T00:00:00Z", "future-dated"),
        ):
            with self.subTest(issued_at=issued_at), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                ledger = assemble_evidence_ledger(
                    gate=self._gate(),
                    manifest=self._manifest(root, issued_at),
                    manifest_dir=root,
                    evaluated_at=self.NOW,
                    max_age_seconds=7200,
                )
                self.assertEqual(ledger["terms"][0]["status"], "BLOCKED")
                self.assertIn(expected, ledger["terms"][0]["blocker"])

    def test_duplicates_are_blocked_and_never_hidden_by_a_valid_first_entry(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = self._manifest(root)
            manifest["artifacts"].append(dict(manifest["artifacts"][0]))
            ledger = assemble_evidence_ledger(
                gate=self._gate(),
                manifest=manifest,
                manifest_dir=root,
                evaluated_at=self.NOW,
            )
        self.assertEqual(ledger["terms"][0]["blocker"], "duplicate evidence")
        self.assertEqual(ledger["terms"][0]["status"], "BLOCKED")

    def test_gate_rejection_remains_blocking(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            gate = self._gate()
            term = TERMS[0]
            gate.values[term] = False
            gate.reasons[term] = "verifier key is revoked"
            ledger = assemble_evidence_ledger(
                gate=gate,
                manifest=self._manifest(root),
                manifest_dir=root,
                evaluated_at=self.NOW,
            )
        self.assertEqual(ledger["terms"][0]["blocker"], "verifier key is revoked")
        self.assertFalse(ledger["PublicMCPReady"])

    def test_nonpositive_freshness_window_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with self.assertRaisesRegex(ValueError, "must be positive"):
                assemble_evidence_ledger(
                    gate=self._gate(),
                    manifest=self._manifest(root),
                    manifest_dir=root,
                    evaluated_at=self.NOW,
                    max_age_seconds=0,
                )

    def test_bytes_changed_after_gate_cannot_supply_freshness(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = self._manifest(root, "2026-10-01T00:00:00Z")
            first = manifest["artifacts"][0]
            # Replace the authenticated (stale) bytes with unsigned fresh bytes.
            (root / first["path"]).write_text(
                json.dumps({"term": first["term"], "issued_at": "2026-10-11T22:00:00Z"}),
                encoding="utf-8",
            )
            ledger = assemble_evidence_ledger(
                gate=self._gate(),
                manifest=manifest,
                manifest_dir=root,
                evaluated_at=self.NOW,
            )
        row = ledger["terms"][0]
        self.assertEqual(row["status"], "BLOCKED")
        self.assertIn("digest", row["blocker"])
        self.assertFalse(ledger["PublicMCPReady"])

    def test_naive_evaluation_time_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with self.assertRaisesRegex(ValueError, "timezone-aware"):
                assemble_evidence_ledger(
                    gate=self._gate(),
                    manifest=self._manifest(root),
                    manifest_dir=root,
                    evaluated_at=datetime(2026, 10, 11, 23, 0, 0),
                )

    def test_unsigned_fresh_evidence_is_blocked_by_real_gate(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = self._manifest(root)
            manifest["schema"] = MANIFEST_SCHEMA
            gate = evaluate_public_mcp_manifest(
                manifest,
                manifest_dir=root,
                trust_policy={"schema": TRUST_SCHEMA, "keys": {}},
                local_head=self.SHA,
            )
            ledger = assemble_evidence_ledger(
                gate=gate,
                manifest=manifest,
                manifest_dir=root,
                evaluated_at=self.NOW,
            )
        self.assertEqual(ledger["proven"], 0)
        self.assertTrue(all(row["status"] == "BLOCKED" for row in ledger["terms"]))
        self.assertFalse(ledger["PublicMCPReady"])


if __name__ == "__main__":
    unittest.main()
