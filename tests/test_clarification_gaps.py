"""Clarification gaps found by capability testing (D4, D6).

Recorded behavior on e232ced (before this fix):

  D4. "Is it worth it?" -> READY_FOR_SCOPE_APPROVAL, round 0, 0 questions (the run then
      hit 8 unknowns because the objective never names its subject).
  D6. A medical + legal + financial objective -> domain "medical" only: the 7 medical
      questions, the medical safety notice and the medical exclusions; the legal and
      financial notices and exclusions were dropped.

No test makes a model, network, Stripe or Supabase call.
"""

from __future__ import annotations

import unittest

from lofgren_intelligence.hosted.service import PublicService
from lofgren_intelligence.intent import classify_objective
from lofgren_intelligence.intent.clarification import (
    DOMAIN_EXCLUSIONS,
    NOTICE_SCOPE,
    SAFETY_NOTICES,
    clarify_objective,
    requires_clarification,
    to_dict,
)

VAGUE = (
    "Is it worth it?",
    "Should I do this?",
    "What does that mean?",
    "Is this a good idea?",
    "Can they do that?",
    "Is it worth buying?",
)
WELL_FORMED = (
    "What is the capital of France?",
    "When was the transistor invented?",
    "Research the history of desalination membranes.",
    "How tall is Mount Everest?",
    "Who wrote Pride and Prejudice?",
    "Is it true that the transistor was invented in 1947?",
    "What is the boiling point of water at sea level?",
    "Desalination",
)
MIXED = (
    "Should my clinic's patients sign a liability waiver contract before we charge them for an "
    "experimental therapy, and what revenue risk does that create?"
)
MIXED_FOUR = "Assess the legal liability, revenue impact and patient safety hazards of a chemical spill at a hospital."
SPECIALISED = ("medical", "legal", "financial", "safety")


def keys(result):
    return [q.key for q in result.questions]


class UnderspecifiedObjectiveTests(unittest.TestCase):
    """D4: a referent-less objective is clarified for subject, purpose and scope."""

    def test_d4_vague_objectives_require_general_clarification(self):
        for text in VAGUE:
            with self.subTest(text=text):
                self.assertTrue(requires_clarification(text))
                profile = classify_objective(text)
                self.assertTrue(profile.underspecified)
                self.assertEqual((profile.domain, profile.consequence), ("general", "standard"))
                r = clarify_objective(text)
                self.assertEqual((r.status, r.round), ("CLARIFICATION_REQUIRED", 1))
                self.assertEqual(keys(r), ["subject", "purpose", "scope"])
                self.assertIsNone(r.case_charter)
                self.assertTrue(r.classification["underspecified"])
                for q in r.questions:
                    self.assertTrue(q.why and q.examples and q.default)

    def test_d4_signals_name_the_rule(self):
        signals = classify_objective("Is it worth it?").signals
        self.assertIn("underspecified:no_subject_noun", signals)
        self.assertIn("underspecified:deictic_without_referent", signals)
        # One subject word is not enough when the objective leans on "it".
        self.assertIn("underspecified:deictic_without_referent", classify_objective("Is it legal?").signals)

    def test_simple_well_formed_questions_still_skip(self):
        for text in WELL_FORMED:
            with self.subTest(text=text):
                self.assertFalse(requires_clarification(text))
                profile = classify_objective(text)
                self.assertFalse(profile.underspecified)
                r = clarify_objective(text)
                self.assertEqual((r.status, r.round, r.questions), ("READY_FOR_SCOPE_APPROVAL", 0, []))

    def test_underspecified_domain_objective_asks_subject_first_within_bound(self):
        r = clarify_objective("Is it legal?")
        self.assertEqual(r.domain, "legal")
        self.assertEqual(keys(r)[0], "subject")
        self.assertIn("jurisdiction", keys(r))
        self.assertTrue(3 <= len(r.questions) <= 7)
        self.assertEqual(len(set(keys(r))), len(keys(r)))

    def test_answered_subject_produces_a_charter_for_approval(self):
        answers = {"subject": "A used 2019 hybrid car at the listed price",
                   "purpose": "Decide whether to buy it", "scope": "use a reasonable default"}
        r = clarify_objective("Is it worth it?", answers)
        self.assertEqual(r.status, "READY_FOR_SCOPE_APPROVAL")
        self.assertEqual(r.case_charter["scope"]["subject"], answers["subject"])
        self.assertEqual(r.case_charter["defaults_requested"], ["scope"])
        self.assertTrue(r.case_charter["approval_required_before_research"])

    def test_hosted_gate_refuses_vague_objective_before_work(self):
        service = PublicService(store=object())
        for out in (service.plan_research({"objective": "Is it worth it?"}),
                    service.investigate("user-1", {"objective": "Is it worth it?"})):
            self.assertEqual(out["status"], "CLARIFICATION_REQUIRED")
            self.assertEqual([q["key"] for q in out["questions"]], ["subject", "purpose", "scope"])
            self.assertNotIn("estimate", out)


class MixedDomainTests(unittest.TestCase):
    """D6: every applicable domain contributes its notice, exclusions and questions."""

    def test_d6_domains_are_detected_and_recorded(self):
        profile = classify_objective(MIXED)
        self.assertEqual(profile.domain, "medical")  # primary still follows priority
        self.assertEqual(profile.domains, ("medical", "legal", "financial"))
        self.assertIn("mixed_domain:medical+legal+financial", profile.signals)
        r = clarify_objective(MIXED)
        out = to_dict(r)
        self.assertEqual(out["domains"], ["medical", "legal", "financial"])
        self.assertEqual(out["classification"]["domains"], ["medical", "legal", "financial"])
        self.assertTrue(out["classification"]["mixed_domain"])

    def test_d6_every_safety_notice_is_carried(self):
        notice = clarify_objective(MIXED).safety_notice
        self.assertIn("does not diagnose, treat or give medical advice", notice)
        self.assertIn("legal research, not legal advice", notice)
        self.assertIn("does not guarantee revenue", notice)
        self.assertTrue(notice.endswith(NOTICE_SCOPE))
        self.assertEqual(notice.count(NOTICE_SCOPE.strip()), 1)

    def test_d6_every_exclusion_is_carried_without_duplicates(self):
        exclusions = clarify_objective(MIXED).exclusions
        for domain in ("medical", "legal", "financial"):
            for line in DOMAIN_EXCLUSIONS[domain]:
                self.assertIn(line, exclusions)
        self.assertEqual(len(exclusions), len(set(exclusions)))
        self.assertTrue(any("own situation" in line for line in exclusions))

    def test_d6_question_set_is_bounded_unique_and_covers_each_domain(self):
        for text, domains in ((MIXED, ("medical", "legal", "financial")),
                              (MIXED_FOUR, SPECIALISED)):
            with self.subTest(text=text):
                r = clarify_objective(text)
                self.assertEqual(r.status, "CLARIFICATION_REQUIRED")
                self.assertEqual(tuple(r.domains), domains)
                ks = keys(r)
                self.assertTrue(3 <= len(ks) <= 7, ks)
                self.assertEqual(len(ks), len(set(ks)))
                self.assertEqual(ks[:2], ["purpose", "scope"])
                for q in r.questions:
                    self.assertTrue(q.why and q.examples and q.default)
                    self.assertNotIn("{", q.prompt)
                coverage = {"medical": {"population", "focus", "evidence_types"},
                            "legal": {"jurisdiction", "parties", "time"},
                            "financial": {"launch_budget", "revenue_definition", "risk_and_hours"},
                            "safety": {"jurisdiction", "standards", "affected_population"}}
                for d in domains:
                    self.assertTrue(coverage[d] & set(ks), d)
                for d in domains:
                    self.assertIn(d, r.questions[1].prompt)  # scope names every domain
        self.assertEqual(keys(clarify_objective(MIXED)),
                         ["purpose", "scope", "population", "jurisdiction", "launch_budget", "focus", "parties"])

    def test_d6_complete_answers_produce_a_charter_recording_every_domain(self):
        bank = clarify_objective(MIXED).questions
        answers = {q.key: "use a reasonable default" for q in bank}
        answers["population"] = "adults"
        r = clarify_objective(MIXED, answers)
        self.assertEqual(r.status, "READY_FOR_SCOPE_APPROVAL")
        charter = r.case_charter
        self.assertEqual(charter["domains"], ["medical", "legal", "financial"])
        self.assertEqual(charter["safety_notice"], r.safety_notice)
        self.assertEqual(charter["exclusions"], r.exclusions)
        self.assertEqual(charter["scope"], {"population": "adults"})
        self.assertEqual(set(charter["defaults_applied"]), set(answers) - {"population"})
        self.assertNotIn("approved", charter)

    def test_single_domain_objectives_are_unchanged(self):
        epilepsy = clarify_objective("Design a new treatment protocol for drug-resistant epilepsy.")
        self.assertEqual(epilepsy.domains, ["medical"])
        self.assertEqual(len(epilepsy.questions), 7)
        self.assertEqual(epilepsy.safety_notice, SAFETY_NOTICES["medical"])
        shop = clarify_objective("Determine the strongest defensible zero-owned-inventory business that could "
                                 "operate on Shopify, targeting $75,000-$100,000 in monthly recurring revenue "
                                 "by day 30.")
        self.assertEqual(shop.domains, ["financial"])
        self.assertEqual(len(shop.questions), 7)
        self.assertEqual(shop.safety_notice, SAFETY_NOTICES["financial"])
        self.assertFalse(to_dict(shop)["classification"]["mixed_domain"])


if __name__ == "__main__":
    unittest.main()
