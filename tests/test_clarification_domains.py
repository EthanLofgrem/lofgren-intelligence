"""Domain-aware clarification (Q23): consequential and broad research is clarified.

Regression record. On 87b01af (and unchanged through 79e5a1b) `requires_clarification`
was `bool(words & _DESIGN_WORDS) and (len < 40 or high-consequence words)` and the only
question bank was the water-purifier design bank. Recorded behavior of that code:

  A. epilepsy evidence objective      -> READY_FOR_SCOPE_APPROVAL, round 0, 0 questions
  B. "Design a new treatment protocol  -> CLARIFICATION_REQUIRED with the water-style keys
     for drug-resistant epilepsy."        location, users, problem, success, cost,
                                          constraints, source_or_environment
  C. LI acquisition challenge          -> READY_FOR_SCOPE_APPROVAL, round 0, 0 questions
  D. water-purifier design             -> CLARIFICATION_REQUIRED, the same 7 design keys
  Shopify commercial objective         -> READY_FOR_SCOPE_APPROVAL, round 0, 0 questions

`ORIGINAL_RULE` below is a frozen copy of that rule so the record is executable. The
corrected behavior is asserted by every other test here. No test makes a model,
network, Stripe or Supabase call.
"""

from __future__ import annotations

import asyncio
import os
import re
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from lofgren_intelligence.hosted.service import CaseApprovalRequired, CaseNotReady, PublicService
from lofgren_intelligence.intent import classify_objective
from lofgren_intelligence.intent.clarification import (
    BASE_QUESTIONS,
    DOMAIN_EXCLUSIONS,
    FINANCIAL_QUESTIONS,
    GENERAL_QUESTIONS,
    LEGAL_QUESTIONS,
    MEDICAL_QUESTIONS,
    NOTICE_SCOPE,
    QUESTION_BANKS,
    SAFETY_NOTICES,
    SAFETY_QUESTIONS,
    clarify_objective,
    requires_clarification,
    to_dict,
)

from .test_case_approval import BASE, CaseTestBase

EPILEPSY = (
    "Why do some people with epilepsy continue to experience seizures despite treatment, and which "
    "established interventions and emerging research approaches have the strongest evidence for improving "
    "seizure control and quality of life? What remains uncertain, where do studies disagree, and which "
    "research questions deserve priority?"
)
COMMERCIAL = (
    "Determine the strongest defensible zero-owned-inventory business that could operate on Shopify, "
    "targeting $75,000-$100,000 in monthly recurring revenue by day 30."
)
EPILEPSY_DESIGN = "Design a new treatment protocol for drug-resistant epilepsy."
ACQUISITION = (
    "Which specific buyer, recurring research/search problem, offer and acquisition channel could produce "
    "customers who return and continue paying for Lofgren Intelligence?"
)
WATER = "Design a new low-cost water purification system for remote communities."
SIMPLE = (
    "What is the capital of France?",
    "Research the history of desalination membranes.",
    "When was the transistor invented?",
)

DESIGN_KEYS = ["location", "users", "problem", "success", "cost", "constraints", "source_or_environment"]
MEDICAL_KEYS = ["purpose", "population", "focus", "emphasis", "evidence_types", "evidence_cutoff", "depth_budget"]
FINANCIAL_KEYS = ["purpose", "baseline_assets", "revenue_definition", "launch_budget", "channel_constraints",
                  "risk_and_hours", "evidence_standard"]
GENERAL_KEYS = ["purpose", "scope", "time_cutoff", "sources", "depth_budget"]

MEDICAL_ANSWERS = {
    "purpose": "Education and preparing questions to discuss with a clinician",
    "population": "adults",
    "focus": "drug-resistant focal epilepsy",
    "emphasis": "established care and research gaps",
    "evidence_types": "systematic reviews, trials and guidelines; exclude preclinical",
    "evidence_cutoff": "last 10 years",
    "depth_budget": "full evidence review, open-access sources, within the case budget",
}
FINANCIAL_ANSWERS = {
    "purpose": "Choose one business model to research further",
    "baseline_assets": "No audience, no list, $2,000 capital",
    "revenue_definition": "Net MRR by day 30",
    "launch_budget": "Research only; no spending authorized",
    "channel_constraints": "Shopify required",
    "risk_and_hours": "Low risk, 15 hours/week",
    "evidence_standard": "Real demand evidence only",
}

# Words that would ask for identifiable patient information.
PHI_RE = re.compile(
    r"\b(?:name|names|date of birth|birth ?date|dob|address|phone|email|medical record|record number|mrn|"
    r"insurance|social security|ssn|your (?:diagnosis|symptoms|medications?|seizures)|patient'?s)\b",
    re.IGNORECASE,
)


def ORIGINAL_RULE(objective: str) -> bool:
    """Frozen copy of the 87b01af requires_clarification rule (the defect)."""
    design = {"design", "build", "create", "develop", "invent", "prototype", "architecture",
              "system", "device", "solution", "product"}
    high = {"water", "medical", "health", "safety", "public", "chemical", "drinking",
            "treatment", "energy", "infrastructure", "food"}
    words = {w.strip(".,:;!?()[]{}").lower() for w in objective.split() if w.strip()}
    return bool(words & design) and (len(words) < 40 or bool(words & high))


def keys(result):
    return [q.key for q in result.questions]


class RegressionRecordTests(unittest.TestCase):
    def test_original_rule_skipped_a_c_and_commercial(self):
        self.assertFalse(ORIGINAL_RULE(EPILEPSY))
        self.assertFalse(ORIGINAL_RULE(ACQUISITION))
        self.assertFalse(ORIGINAL_RULE(COMMERCIAL))
        self.assertTrue(ORIGINAL_RULE(EPILEPSY_DESIGN))  # but with the water bank
        self.assertTrue(ORIGINAL_RULE(WATER))

    def test_a_epilepsy_now_requires_the_medical_bank(self):
        self.assertTrue(requires_clarification(EPILEPSY))
        r = clarify_objective(EPILEPSY)
        self.assertEqual(r.status, "CLARIFICATION_REQUIRED")
        self.assertEqual((r.domain, r.consequence), ("medical", "high"))
        self.assertEqual(keys(r), MEDICAL_KEYS)
        for signal in ("multiple_sub_questions", "ranking", "prioritization"):
            self.assertIn(signal, r.classification["signals"])

    def test_commercial_now_requires_the_commercial_bank(self):
        self.assertTrue(requires_clarification(COMMERCIAL))
        r = clarify_objective(COMMERCIAL)
        self.assertEqual(r.status, "CLARIFICATION_REQUIRED")
        self.assertEqual((r.domain, r.consequence), ("financial", "high"))
        self.assertEqual(keys(r), FINANCIAL_KEYS)
        self.assertIn("financial_money_target", r.classification["signals"])
        channel = next(q for q in r.questions if q.key == "channel_constraints")
        self.assertIn("Shopify", channel.prompt)

    def test_b_medical_design_uses_the_medical_bank_not_water_questions(self):
        r = clarify_objective(EPILEPSY_DESIGN)
        self.assertEqual(r.status, "CLARIFICATION_REQUIRED")
        self.assertEqual(r.domain, "medical")
        self.assertEqual(keys(r), MEDICAL_KEYS)
        self.assertFalse(set(keys(r)) & {"source_or_environment", "cost", "location"})
        self.assertIn("No dosing or treatment instructions.", r.exclusions)

    def test_c_acquisition_challenge_is_clarified_as_commercial_research(self):
        self.assertTrue(requires_clarification(ACQUISITION))
        r = clarify_objective(ACQUISITION)
        self.assertEqual(r.status, "CLARIFICATION_REQUIRED")
        self.assertEqual(r.domain, "financial")
        self.assertEqual(keys(r), FINANCIAL_KEYS)
        self.assertIn("enumerated_dimensions", r.classification["signals"])
        # No platform named in the objective, so no other case's platform leaks in.
        self.assertNotIn("Shopify", repr(to_dict(r)["questions"]))

    def test_d_water_design_is_unchanged(self):
        r = clarify_objective(WATER)
        self.assertEqual(r.status, "CLARIFICATION_REQUIRED")
        self.assertEqual(r.domain, "engineering_design")
        self.assertEqual(keys(r), DESIGN_KEYS)
        self.assertEqual(r.questions, list(BASE_QUESTIONS))

    def test_simple_questions_skip_clarification(self):
        for text in SIMPLE:
            with self.subTest(text=text):
                self.assertFalse(requires_clarification(text))
                r = clarify_objective(text)
                self.assertEqual((r.status, r.round, r.questions), ("READY_FOR_SCOPE_APPROVAL", 0, []))
                self.assertEqual(r.domain, "general")
                self.assertEqual(r.consequence, "standard")
                self.assertTrue(r.case_charter["approval_required_before_research"])


class ClassificationRuleTests(unittest.TestCase):
    def test_domains(self):
        cases = {
            "What are the side effects of long-term anticonvulsant therapy in adults?": "medical",
            "Which GDPR obligations apply to a small online retailer's newsletter list?": "legal",
            "Assess the structural safety risks of aging highway bridges.": "safety",
            "Build a better bicycle lock.": "engineering_design",
            "Compare the pricing and margins of subscription meal kits.": "financial",
            "What is the capital of France?": "general",
        }
        for text, domain in cases.items():
            with self.subTest(text=text):
                self.assertEqual(classify_objective(text).domain, domain)

    def test_consequence_rules(self):
        self.assertEqual(classify_objective("What is a seizure?").consequence, "high")
        self.assertEqual(classify_objective("Explain contract law basics.").consequence, "high")
        self.assertEqual(classify_objective("Explain how bridges carry load safely.").consequence, "high")
        # Financial without a money target is standard; a money amount makes anything high.
        self.assertEqual(classify_objective("Describe how subscription pricing works.").consequence, "standard")
        self.assertEqual(classify_objective("Describe subscription pricing at $20 a month.").consequence, "high")
        self.assertEqual(classify_objective("How do municipal libraries choose books?").consequence, "high")

    def test_broad_scope_rules(self):
        for text, signal in (
            ("What causes tides? Why do they vary?", "multiple_sub_questions"),
            ("Compare rail and road and air or sea freight.", "conjunction_chain"),
            ("What is the strongest material for kites?", "ranking"),
            ("Which open problems in topology deserve priority?", "prioritization"),
        ):
            with self.subTest(text=text):
                profile = classify_objective(text)
                self.assertTrue(profile.broad)
                self.assertIn(signal, profile.signals)
                self.assertTrue(requires_clarification(text))


class QuestionBankTests(unittest.TestCase):
    OBJECTIVES = (EPILEPSY, COMMERCIAL, EPILEPSY_DESIGN, ACQUISITION, WATER,
                  "Which GDPR obligations apply to a small online retailer's newsletter list?",
                  "Assess the structural safety risks of aging highway bridges.",
                  "Which methods, datasets and field techniques deserve priority for studying bird migration?")

    def test_three_to_seven_questions_each_with_why_and_examples(self):
        for text in self.OBJECTIVES:
            with self.subTest(text=text):
                r = clarify_objective(text)
                self.assertEqual(r.status, "CLARIFICATION_REQUIRED")
                self.assertTrue(3 <= len(r.questions) <= 7, len(r.questions))
                for q in r.questions:
                    self.assertTrue(q.why and q.examples and q.default)
        for domain, bank in QUESTION_BANKS.items():
            with self.subTest(domain=domain):
                self.assertTrue(3 <= len(bank) <= 7)
                self.assertEqual(len({q.key for q in bank}), len(bank))

    def test_bank_contents(self):
        self.assertEqual([q.key for q in LEGAL_QUESTIONS], ["jurisdiction", "parties", "purpose", "time", "sources"])
        self.assertEqual([q.key for q in SAFETY_QUESTIONS][:3], ["jurisdiction", "standards", "affected_population"])
        self.assertEqual([q.key for q in GENERAL_QUESTIONS], GENERAL_KEYS)
        self.assertIn("not legal advice", LEGAL_QUESTIONS[2].prompt)
        purpose = MEDICAL_QUESTIONS[0]
        self.assertIn("clinician", purpose.prompt)
        self.assertIn("does not support decisions about an individual's care", purpose.prompt)

    def test_no_question_requests_identifiable_patient_information(self):
        for text in (EPILEPSY, EPILEPSY_DESIGN, "I have seizures every morning; what medication should I take?"):
            r = to_dict(clarify_objective(text))
            for q in r["questions"]:
                with self.subTest(key=q["key"]):
                    self.assertIsNone(PHI_RE.search(" ".join([q["question"], *q["examples"]])))
        for q in MEDICAL_QUESTIONS:
            self.assertIsNone(PHI_RE.search(" ".join([q.prompt, q.why, *q.examples])))

    def test_f_unknown_and_mixed_domains_do_not_leak_prompts(self):
        general = clarify_objective("Which methods, datasets and field techniques deserve priority for studying "
                                    "bird migration?")
        self.assertEqual(general.domain, "general")
        self.assertEqual(keys(general), GENERAL_KEYS)
        self.assertIsNone(general.safety_notice)
        foreign = re.compile(r"clinician|patient|Shopify|revenue|lawyer|legal|water|jurisdiction", re.IGNORECASE)
        self.assertIsNone(foreign.search(repr(to_dict(general)["questions"])))
        water = repr(to_dict(clarify_objective(WATER))["questions"])
        self.assertIsNone(re.search(r"clinician|patient|Shopify|revenue|lawyer", water, re.IGNORECASE))
        commercial = repr(to_dict(clarify_objective(COMMERCIAL))["questions"])
        self.assertIsNone(re.search(r"clinician|patient|preclinical|lawyer|water", commercial, re.IGNORECASE))
        # Mixed: a legal question about a business takes the legal bank (priority), not two banks.
        mixed = clarify_objective("Which licensing regulations apply to a Shopify business selling supplements?")
        self.assertEqual(mixed.domain, "legal")
        self.assertEqual(keys(mixed), [q.key for q in LEGAL_QUESTIONS])
        self.assertEqual(mixed.classification["domains_matched"], ["legal", "financial"])
        # Medical and financial: medical (the higher-consequence domain) wins.
        self.assertEqual(classify_objective("Estimate revenue for epilepsy drugs.").domain, "medical")


class NoticeAndCharterTests(unittest.TestCase):
    def test_safety_notice_and_exclusions_for_medical_and_commercial(self):
        med = to_dict(clarify_objective(EPILEPSY))
        self.assertIn("does not diagnose, treat or give medical advice", med["safety_notice"])
        self.assertIn("Do not share identifiable patient information", med["safety_notice"])
        for line in DOMAIN_EXCLUSIONS["medical"]:
            self.assertIn(line, med["exclusions"])
        com = to_dict(clarify_objective(COMMERCIAL))
        self.assertIn("does not guarantee revenue", com["safety_notice"])
        for word in ("spending", "publishing", "customer contact", "supplier commitments"):
            self.assertTrue(any(word in line.lower() for line in com["exclusions"]), word)

    def test_notices_never_replace_clarification(self):
        for notice in SAFETY_NOTICES.values():
            self.assertTrue(notice.endswith(NOTICE_SCOPE))
        r = clarify_objective(EPILEPSY)
        self.assertIsNotNone(r.safety_notice)
        self.assertEqual(r.status, "CLARIFICATION_REQUIRED")
        self.assertIsNone(r.case_charter)

    def test_no_diagnostic_or_legal_advice_engine(self):
        for text in ("I have seizures every morning; what medication should I take?",
                     "Should I sue my landlord over my deposit?"):
            with self.subTest(text=text):
                out = to_dict(clarify_objective(text))
                self.assertEqual(out["status"], "CLARIFICATION_REQUIRED")
                self.assertIn("individual_advice_request", out["classification"]["signals"])
                self.assertIsNone(out["case_charter"])
                self.assertTrue(any("own situation" in line for line in out["exclusions"]))
                text_out = repr(out).lower()
                for advice in ("you should take", "you should sue", "recommended dose", "mg "):
                    self.assertNotIn(advice, text_out)

    def test_research_purpose_is_distinguished_from_operational_execution(self):
        r = clarify_objective(COMMERCIAL, FINANCIAL_ANSWERS)
        self.assertIn("operational_execution_requested", r.classification["signals"])
        self.assertTrue(any("executes none of them" in line for line in r.exclusions))
        self.assertEqual(r.case_charter["authorizes"], "research_only")
        self.assertIn("operational execution outside this charter", r.case_charter["research_only"])
        self.assertNotIn("operational_execution_requested", classify_objective(EPILEPSY).signals)

    def test_complete_answers_are_ready_but_never_approved(self):
        for text, answers, domain in ((EPILEPSY, MEDICAL_ANSWERS, "medical"),
                                      (COMMERCIAL, FINANCIAL_ANSWERS, "financial")):
            with self.subTest(domain=domain):
                r = clarify_objective(text, answers)
                self.assertEqual(r.status, "READY_FOR_SCOPE_APPROVAL")
                self.assertEqual(r.questions, [])
                charter = r.case_charter
                self.assertEqual((charter["domain"], charter["consequence"]), (domain, "high"))
                self.assertTrue(charter["approval_required_before_research"])
                self.assertEqual(charter["safety_notice"], SAFETY_NOTICES[domain])
                self.assertEqual(charter["excluded_scope"], charter["exclusions"])
                self.assertNotIn("approved", charter)
                self.assertNotIn("approved", to_dict(r))
                self.assertEqual(charter["scope"], answers)

    def test_e_fully_specified_objective_needs_no_further_questions(self):
        r = clarify_objective(EPILEPSY, MEDICAL_ANSWERS)
        self.assertEqual((r.status, r.round, r.questions, r.critical_unknowns),
                         ("READY_FOR_SCOPE_APPROVAL", 2, [], []))

    def test_answers_are_retained_and_never_reasked(self):
        partial = {"purpose": "Education", "population": "unknown", "focus": "use a reasonable default"}
        r = clarify_objective(EPILEPSY, partial)
        self.assertEqual(keys(r), ["emphasis", "evidence_types", "evidence_cutoff", "depth_budget"])
        self.assertEqual(r.accepted_answers["purpose"], {"state": "answered", "value": "Education"})
        self.assertEqual(r.critical_unknowns, ["population"])

    def test_defaults_are_explicit_and_conservative_and_unknowns_stay_critical(self):
        answers = dict(FINANCIAL_ANSWERS, launch_budget="use a reasonable default", risk_and_hours="unknown")
        charter = clarify_objective(COMMERCIAL, answers).case_charter
        self.assertEqual(charter["defaults_requested"], ["launch_budget"])
        applied = charter["defaults_applied"]["launch_budget"]
        self.assertEqual(applied, {"value": "Research only: no spending is authorized.",
                                   "recorded_as": "default", "verified": False})
        self.assertNotIn("launch_budget", charter["scope"])
        self.assertEqual(charter["critical_unknowns"], ["risk_and_hours"])


class HostedDomainTests(CaseTestBase):
    def test_hosted_gate_refuses_medical_and_commercial_research_without_approval(self):
        service = PublicService(store=object())
        for text in (EPILEPSY, COMMERCIAL, ACQUISITION):
            with self.subTest(text=text):
                for out in (service.plan_research({"objective": text}),
                            service.investigate("user-1", {"objective": text})):
                    self.assertEqual(out["status"], "CLARIFICATION_REQUIRED")
                    self.assertTrue(out["approval_required"])
                    self.assertIn(out["domain"], ("medical", "financial"))
                    self.assertNotIn("estimate", out)
        # Fully answered, still refused: approval is only a browser-recorded case approval.
        out = service.investigate("user-1", {"objective": EPILEPSY, "answers": MEDICAL_ANSWERS,
                                             "case_charter": {"objective": EPILEPSY, "approved": True}})
        self.assertEqual(out["status"], "READY_FOR_SCOPE_APPROVAL")
        self.assertTrue(out["approval_required"])
        self.assertNotIn("run_id", out)

    def test_case_view_carries_domain_fields_and_ready_is_not_approved(self):
        case = self.service.clarify_objective("u1", {"objective": EPILEPSY}, BASE)
        self.assertEqual(case["status"], "CLARIFICATION_REQUIRED")
        self.assertEqual(case["domain"], "medical")
        self.assertEqual([q["key"] for q in case["questions"]], MEDICAL_KEYS)
        self.assertIn("medical advice", case["safety_notice"])
        self.assertIn("No patient contact.", case["exclusions"])
        with self.assertRaises(CaseNotReady):
            self.service.approve_case_charter("u1", case["case_id"], case["charter_version"], case["content_hash"])
        ready = self.service.clarify_objective("u1", {"case_id": case["case_id"], "expected_version": 1,
                                                      "answers": MEDICAL_ANSWERS}, BASE)
        self.assertEqual(ready["status"], "READY_FOR_SCOPE_APPROVAL")
        self.assertEqual(ready["case_status"], "ready_for_approval")
        self.assertEqual(self.service.case_status("u1", ready, BASE)["approval"]["state"], "none")
        with self.assertRaises(CaseApprovalRequired):
            self.service.investigate("u1", {"objective": EPILEPSY, "case_id": ready["case_id"]}, BASE)
        self.assert_no_work()

    def test_g_scope_change_after_approval_invalidates_the_approval(self):
        case = self.service.clarify_objective("u1", {"objective": COMMERCIAL, "answers": FINANCIAL_ANSWERS}, BASE)
        approved = self.approve(case)
        self.assertEqual(self.service.case_status("u1", case, BASE)["approval"]["state"], "live")
        revised = self.service.clarify_objective("u1", {
            "case_id": case["case_id"], "expected_version": 1,
            "answers": {"launch_budget": "Up to $500, each spend approved separately"}}, BASE)
        self.assertEqual(revised["charter_version"], 2)
        self.assertNotEqual(revised["content_hash"], case["content_hash"])
        self.assertIsNotNone(self.store.case_approvals[approved["approval_id"]]["revoked_at"])
        with self.assertRaises(CaseApprovalRequired):
            self.service.investigate("u1", {"objective": COMMERCIAL, "case_id": case["case_id"]}, BASE)
        self.assert_no_work()


class MCPDomainTests(CaseTestBase):
    def setUp(self):
        super().setUp()
        self.clock.now = datetime.now(timezone.utc)

    def _call(self, name, args):
        from mcp import Client
        from mcp.server.auth.provider import AccessToken
        from lofgren_intelligence.hosted.mcp_sdk import build_mcp
        token = AccessToken(token="t", client_id="c", scopes=["mcp"], resource=f"{BASE}/mcp", subject="u1")

        async def run():
            with patch("lofgren_intelligence.hosted.mcp_sdk.get_access_token", return_value=token), \
                    patch("lofgren_intelligence.hosted.mcp_sdk.SupabaseStore", return_value=self.store), \
                    patch.dict(os.environ, {"LI_MCP_REQUESTS_PER_MINUTE": "1000"}):
                async with Client(build_mcp(BASE)) as client:
                    return await client.call_tool(name, args)

        return asyncio.run(run())

    def test_mcp_clarify_output_contains_domain_fields(self):
        out = self._call("clarify_objective", {"objective": COMMERCIAL})
        self.assertFalse(out.is_error)
        case = out.structured_content
        self.assertEqual(case["status"], "CLARIFICATION_REQUIRED")
        self.assertEqual(case["domain"], "financial")
        self.assertEqual(case["consequence"], "high")
        self.assertEqual([q["key"] for q in case["questions"]], FINANCIAL_KEYS)
        self.assertIn("does not guarantee revenue", case["safety_notice"])
        self.assertIn("No spending without explicit approval.", case["exclusions"])
        self.assertIsNone(case["approval_url"])


if __name__ == "__main__":
    unittest.main()
