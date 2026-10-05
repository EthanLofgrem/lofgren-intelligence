from __future__ import annotations

import unittest

from lofgren_intelligence.hosted.journey import (
    checkout_return_html,
    consent_intro_html,
    landing_html,
)


class UserJourneyTests(unittest.TestCase):
    def test_landing_has_connection_endpoint_and_v1_v6_truth(self):
        page = landing_html("https://li.example")
        self.assertIn("https://li.example/mcp", page)
        for version in ("V1 certified", "V2 certified", "V3 certified", "V4 certified", "V5 certified", "V6 certified"):
            self.assertIn(version, page)
        self.assertIn("Public readiness is a separate gate", page)

    def test_landing_has_access_and_usage_first(self):
        page = landing_html("https://li.example")
        self.assertIn("account_status", page)
        self.assertIn("usage_status", page)
        self.assertIn("Founding Free", page)

    def test_landing_has_full_governed_journey(self):
        page = landing_html("https://li.example")
        for expected in (
            "Objective",
            "Research",
            "Verify",
            "Discover",
            "Receipt",
            "compile_objective",
            "plan_research",
            "investigate",
            "get_receipt",
            "export_knowledge_map2",
            "discover",
            "build_artifact",
            "propose_action",
            "execute_action",
            "measure_outcome",
            "evaluate_improvement",
        ):
            self.assertIn(expected, page)

    def test_insufficient_evidence_is_a_recovery_state(self):
        page = landing_html("https://li.example")
        self.assertIn("INSUFFICIENT_EVIDENCE", page)
        self.assertIn("missing-evidence", page)

    def test_checkout_return_does_not_claim_payment(self):
        page = checkout_return_html()
        self.assertIn("does not prove payment succeeded", page)
        self.assertIn("signed webhook", page)
        self.assertIn("account_status", page)
        self.assertNotIn("Payment received", page)

    def test_consent_identifies_and_escapes_client(self):
        page = consent_intro_html('<Bad Client & Co>', "mcp")
        self.assertIn("&lt;Bad Client &amp; Co&gt;", page)
        self.assertIn("<code>mcp</code>", page)
        self.assertIn("does not reveal your password", page)
        self.assertNotIn("<Bad Client & Co>", page)

    def test_consent_does_not_claim_broad_account_access(self):
        page = consent_intro_html("Claude")
        self.assertIn("authorized LI MCP tools", page)
        self.assertNotIn("full account access", page)

    def test_mobile_layout_exists(self):
        page = landing_html("https://li.example")
        self.assertIn("@media(max-width:820px)", page)
        self.assertIn('name="viewport"', page)


if __name__ == "__main__":
    unittest.main()
