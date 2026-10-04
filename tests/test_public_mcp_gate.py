import unittest

from lofgren_intelligence.hosted.certification import TERMS, evaluate_public_mcp


class PublicMCPGateTests(unittest.TestCase):
    def test_unknown_is_false(self):
        gate = evaluate_public_mcp({})
        self.assertFalse(gate.ready)
        self.assertEqual(gate.passed_count, 0)

    def test_all_terms_require_exact_sha_binding(self):
        evidence = {term: True for term in TERMS}
        evidence.update({"expected_sha": "abc", "tested_sha": "abc", "deployed_sha": "abc"})
        gate = evaluate_public_mcp(evidence)
        self.assertTrue(gate.ready)
        self.assertEqual(gate.passed_count, len(TERMS))

    def test_wrong_deployed_sha_fails(self):
        evidence = {term: True for term in TERMS}
        evidence.update({"expected_sha": "abc", "tested_sha": "abc", "deployed_sha": "def"})
        gate = evaluate_public_mcp(evidence)
        self.assertFalse(gate.ready)
        self.assertFalse(gate.values["DeployedSHAMatches"])


if __name__ == "__main__":
    unittest.main()
