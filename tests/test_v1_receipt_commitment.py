"""Receipt semantic commitment (boundary certification, phase A).

A research receipt (lofgren.research-receipt/2) commits to the run's complete knowledge state through
knowledge_state_hash. A knowledge-map/2 edited anywhere, even with every self-computed value (KM2, KM2C and its own
copy of the commitment) recomputed, no longer matches the intact receipt it claims. All data is fictional.
"""

from __future__ import annotations

import copy
import unittest

from lofgren_intelligence.discovery.context import DiscoveryContext, KnowledgeMapRefused
from lofgren_intelligence.kernel import export_knowledge_map, verify_receipt
from lofgren_intelligence.kernel.knowledge_map import (
    compute_fingerprints,
    knowledge_map_problems,
    knowledge_state,
    knowledge_state_hash,
)
from lofgren_intelligence.kernel.receipt import (
    LEGACY_RECEIPT_SCHEMAS,
    RECEIPT_SCHEMA,
    canonical_hash,
)

from .test_v1_knowledge_map2 import phoenix_run, refingerprint

COMMITTED = "its knowledge state is not the state the receipt committed to"


def reissue(receipt: dict) -> dict:
    """What a forger who rewrites a receipt would do: recompute its research id."""
    body = {k: v for k, v in receipt.items() if k != "research_id"}
    return {**body, "research_id": "RR-" + canonical_hash(body)[:20]}


class Commitment(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.result = phoenix_run()
        cls.receipt = cls.result.receipt
        cls.map = export_knowledge_map(cls.result)

    def m(self) -> dict:
        return copy.deepcopy(self.map)

    def test_receipt_commits_to_the_exported_state(self):
        self.assertEqual(self.receipt["schema"], RECEIPT_SCHEMA)
        self.assertEqual(self.receipt["knowledge_state_hash"], knowledge_state_hash(knowledge_state(self.result)))
        self.assertEqual(self.receipt["knowledge_state_hash"], knowledge_state_hash(self.map))
        self.assertEqual(self.map["receipt"]["knowledge_state_hash"], self.receipt["knowledge_state_hash"])
        self.assertTrue(verify_receipt(self.receipt))

    # Each field the receipt did not record before phase A: edit, recompute everything the map computes about
    # itself, keep the original receipt. The map alone is well formed; against its receipt it is refused.
    def test_each_newly_bound_field(self):
        edits = {
            "claim question_ids": lambda m: next(c for c in m["claims"] if len(c["question_ids"]) > 1)[
                "question_ids"].pop(),
            "assessment factor": lambda m: m["claims"][0]["assessment"]["factors"].update(quality=0.01),
            "evidence location": lambda m: m["evidence"][0].update(location={"lat": 33.45, "lon": -112.07,
                                                                             "name": "elsewhere"}),
            "evidence transformation": lambda m: m["evidence"][0]["transformations"].append("silently rescaled"),
            "source quality": lambda m: m["sources"][0].update(quality=0.99 if m["sources"][0]["quality"] != 0.99
                                                               else 0.5),
        }
        for name, edit in edits.items():
            with self.subTest(field=name):
                m = self.m()
                before = copy.deepcopy(m)
                edit(m)
                self.assertNotEqual(m, before, "the edit must change the map")
                refingerprint(m)
                problems = knowledge_map_problems(m)
                self.assertFalse([p for p in problems if "knowledge_state" in p or "fingerprint" in p], problems)
                self.assertIn(COMMITTED, " ".join(knowledge_map_problems(m, self.receipt)))
                with self.assertRaises(KnowledgeMapRefused):
                    DiscoveryContext(m, self.receipt)

    def test_question_ids_edit_is_meaningful(self):
        # Removing an association a finding relies on is caught by derivation rules too; removing one nothing
        # relies on is caught only by the commitment.
        m = self.m()
        claim = next(c for c in m["claims"] if len(c["question_ids"]) > 1)
        used = {q for f in m["findings"] for via in f["claim_scopes"].values() for q in via}
        spare = next((q for q in claim["question_ids"] if q not in used and q not in
                      {f["question_id"] for f in m["findings"] if claim["id"] in f["claim_ids"]}), None)
        if spare is None:
            self.skipTest("no association without a dependent finding in this fixture")
        claim["question_ids"].remove(spare)
        refingerprint(m)
        self.assertIn(COMMITTED, " ".join(knowledge_map_problems(m, self.receipt)))

    def test_map_copy_of_the_commitment_must_match_its_state(self):
        m = self.m()
        m["sources"][0]["quality"] = 0.01
        m["fingerprint"] = compute_fingerprints(m)  # fingerprints recomputed, the map's commitment left stale
        self.assertTrue(any("does not match the map's knowledge state" in p for p in knowledge_map_problems(m)))

    # Changing the receipt's commitment without updating its integrity hash is detected.
    def test_receipt_commitment_tampering(self):
        forged = copy.deepcopy(self.receipt)
        forged["knowledge_state_hash"] = "0" * 64
        self.assertFalse(verify_receipt(forged))
        self.assertIn("not intact", " ".join(knowledge_map_problems(self.map, forged)))
        with self.assertRaises(KnowledgeMapRefused):
            DiscoveryContext(self.m(), forged)

    def test_reissued_receipt_is_another_run(self):
        # A forger can recompute the unkeyed research id, but then it is no longer the receipt the map names or
        # the consumer holds: the forgery has to change the research id and so cannot pass as the original.
        m = self.m()
        m["sources"][0]["quality"] = 0.01
        refingerprint(m)
        forged = reissue({**copy.deepcopy(self.receipt), "knowledge_state_hash": knowledge_state_hash(m)})
        self.assertTrue(verify_receipt(forged))
        self.assertNotEqual(forged["research_id"], self.receipt["research_id"])
        self.assertIn("another run's map", " ".join(knowledge_map_problems(m, forged)))


class LegacyReceipts(unittest.TestCase):
    """Receipt version 1 keeps its meaning and still verifies, but never vouches for a knowledge-map/2."""

    @classmethod
    def setUpClass(cls):
        cls.result = phoenix_run()
        cls.map = export_knowledge_map(cls.result)
        legacy = {k: v for k, v in cls.result.receipt.items() if k != "knowledge_state_hash"}
        legacy["schema"] = "lofgren.research-receipt/1"
        cls.legacy = reissue(legacy)

    def test_legacy_receipt_still_verifies(self):
        self.assertIn(self.legacy["schema"], LEGACY_RECEIPT_SCHEMAS)
        self.assertTrue(verify_receipt(self.legacy))
        for k in ("inputs_hash", "state_hash", "contract_hash"):
            self.assertEqual(self.legacy[k], self.result.receipt[k])  # hash meanings unchanged

    def test_legacy_receipt_cannot_vouch_for_map2(self):
        problems = " ".join(knowledge_map_problems(self.map, self.legacy))
        self.assertIn("carries no knowledge_state_hash", problems)
        with self.assertRaises(KnowledgeMapRefused):
            DiscoveryContext(copy.deepcopy(self.map), self.legacy)

    def test_map_claiming_a_legacy_receipt_is_refused(self):
        m = copy.deepcopy(self.map)
        m["receipt"]["schema"] = "lofgren.research-receipt/1"
        refingerprint(m)
        self.assertIn("predates the knowledge-state commitment", " ".join(knowledge_map_problems(m)))


if __name__ == "__main__":
    unittest.main()
