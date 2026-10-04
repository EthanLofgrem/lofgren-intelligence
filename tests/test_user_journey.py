from __future__ import annotations

import unittest

from lofgren_intelligence.hosted.journey import (
    checkout_return_html,
    consent_intro_html,
    landing_html,
)


class UserJourneyTests(unittest.TestCase):
    def test_landing_has_connection_endpoint_and_v1_v2_truth(self):
        page = landing_html("https://li.example")
        self.assertIn("https://li.example/mcp", page)
        self.assertIn("V1 certified", page)
        self.assertIn("V2 certified", page)
        self.assertIn("Build, execution, measurement and improvement", page)
        self.assertNotIn("V3 certified", page)

    def test_landing_has_access_and_usage_first(self):
        page = landing_html("https://li.example")
        self.assertIn("account_status", page)
        self.assertIn("usage_status", page)
        self.assertIn("Founding Free", page)

    def test_landing_has_research_discovery_receipt_journey(self):
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
