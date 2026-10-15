"""Regression tests for V1 verification defects found by capability testing on 33a3188.

D1  false corroboration between different quantities (revenue vs costs; rated 48 vs delivered 52 L/min)
D2  opposite statements without a negation word ("fixed" vs "still occurs") not flagged
D3  finding headlines said "(N confirmed claim(s))" for single-source, partially verified claims
D10 trial registrations presented as "Evidence supports" for an efficacy question

Each end-to-end repro below is the failing input from the capability test, kept as written.
"""

import sys
import unittest
from pathlib import Path

from lofgren_intelligence.certification import _docs, _run
from lofgren_intelligence.evidence import Claim, ClaimStatus
from lofgren_intelligence.models import parse_value
from lofgren_intelligence.report.markdown import render_markdown
from lofgren_intelligence.verification import relation
from lofgren_intelligence.verification.engine import (
    is_approximate,
    scope_mismatch,
    stance,
    value_resolution,
    values_agree,
)

sys.path.insert(0, str(Path(__file__).parent))


def claim(text, **kw):
    value, unit = parse_value(text)
    return Claim(text, value=kw.pop("value", value), unit=kw.pop("unit", unit), **kw)


def by_statement(result, fragment):
    return next(c for c in result.graph.claims.values() if fragment in c.statement)


def publishers(result, c):
    return sorted(s.title for s in result.graph.sources_for_claim(c.id))


LEDGER_MEMO = {
    "ledger": "Acme Corp total revenue for fiscal year 2026 was 410000 dollars. "
              "Acme Corp total costs for fiscal year 2026 were 380000 dollars.",
    "memo": "Acme Corp was profitable in fiscal year 2026. "
            "Acme Corp total revenue for fiscal year 2026 was 410000 dollars.",
}
PUMP = {
    "datasheet": "The Model X pump flow is rated 48 L/min at full speed.",
    "test report": "In bench testing the Model X pump flow delivered 52 L/min at full speed.",
}
DEADLOCK = {
    "release notes": "The foo 2.1 release fixed the deadlock in the scheduler.",
    "issue tracker": "Users report the deadlock still occurs in foo 2.1 in the scheduler.",
}


class D1DifferentQuantitiesNeverCorroborate(unittest.TestCase):
    def test_costs_are_not_supported_by_revenue(self):
        r = _run("Is Acme Corp profitable given its revenue and costs?", _docs(LEDGER_MEMO))
        costs = by_statement(r, "total costs")
        # before the fix: VERIFIED 0.83, supported by ['ledger', 'memo']
        self.assertEqual(publishers(r, costs), ["ledger"])
        self.assertNotEqual(costs.status, ClaimStatus.VERIFIED)
        profitable = by_statement(r, "was profitable")
        self.assertEqual(publishers(r, profitable), ["memo"], "a statement without the figure cannot confirm it")
        self.assertNotEqual(profitable.status, ClaimStatus.VERIFIED)
        revenue = by_statement(r, "total revenue")  # the same figure from two lineages still corroborates
        self.assertEqual(publishers(r, revenue), ["ledger", "memo"])
        self.assertEqual(revenue.status, ClaimStatus.VERIFIED)

    def test_rated_48_and_delivered_52_do_not_corroborate(self):
        r = _run("What flow rate does the Model X pump deliver?", _docs(PUMP))
        rated, delivered = by_statement(r, "rated 48"), by_statement(r, "delivered 52")
        # before the fix: both VERIFIED 0.83, each supported by the other
        self.assertEqual(publishers(r, rated), ["datasheet"])
        self.assertEqual(publishers(r, delivered), ["test report"])
        for c in (rated, delivered):
            self.assertNotEqual(c.status, ClaimStatus.VERIFIED)
        (cx,) = r.graph.contradictions.values()  # recorded as a scoped disagreement, not dropped
        self.assertEqual(cx.kind, "scope_mismatch")
        self.assertIn("different measurement basis", cx.scope_note)
        self.assertIn("48 vs 52", cx.reason)

    def test_48_vs_52_on_the_same_basis_contradict(self):
        a = claim("The Model X pump flow measured 48 L/min at full speed.")
        b = claim("The Model X pump flow measured 52 L/min at full speed.")
        self.assertEqual(relation(a, b)[0], "contradicts")

    def test_revenue_vs_costs_are_unrelated(self):
        a = claim("Acme Corp total revenue for fiscal year 2026 was 410000 dollars.")
        b = claim("Acme Corp total costs for fiscal year 2026 were 380000 dollars.")
        self.assertEqual(relation(a, b), (None, ""))

    def test_rated_vs_delivered_is_a_basis_mismatch_even_when_values_agree(self):
        a = claim("The Model X pump flow is rated 48 L/min at full speed.")
        b = claim("In bench testing the Model X pump flow delivered 48 L/min at full speed.")
        self.assertTrue(scope_mismatch(a, b).startswith("different measurement basis"))

    def test_precision_rule(self):
        # The certification "agree" fixture: 14 vs 14.2 million sq ft agree at whole-million precision.
        a = claim("Completed warehouse space in the Phoenix metro reached 14 million square feet.")
        b = claim("Completed warehouse space in the Phoenix metro reached 14.2 million square feet.")
        self.assertEqual((value_resolution(a), value_resolution(b)), (1e6, 1e5))
        self.assertTrue(values_agree(a, b))
        self.assertEqual(relation(a, b)[0], "supports")
        # 14.2 vs 14.6 million: both stated to 0.1 million, so they disagree.
        c = claim("Completed warehouse space in the Phoenix metro reached 14.6 million square feet.")
        self.assertFalse(values_agree(b, c))
        self.assertEqual(relation(b, c)[0], "contradicts")
        # An approximate value agrees within 15%.
        d = claim("Completed warehouse space in the Phoenix metro reached about 13 million square feet.")
        self.assertTrue(is_approximate(d))
        self.assertTrue(values_agree(b, d))
        self.assertEqual(value_resolution(claim("Vacancy was 6.50 percent in the corridor.")), 0.01)


class D2OppositeStatementsAreFlagged(unittest.TestCase):
    def test_fixed_vs_still_occurs(self):
        r = _run("Did foo 2.1 fix the deadlock?", _docs(DEADLOCK))
        # before the fix: 0 contradictions, both partially verified
        (cx,) = r.graph.contradictions.values()
        self.assertEqual(cx.kind, "incompatible")
        self.assertIn("opposite statements", cx.reason)
        self.assertNotIn("relative difference", cx.reason, "a version number is not a measured value")
        for c in r.graph.claims.values():
            self.assertEqual(c.status, ClaimStatus.CONTESTED)

    def test_negated_resolution_agrees_with_occurrence(self):
        a = claim("The foo 2.1 release did not fix the deadlock in the scheduler.", polarity=-1)
        b = claim("Users report the deadlock still occurs in foo 2.1 in the scheduler.")
        self.assertEqual(stance(a), stance(b))
        self.assertNotEqual(relation(a, b)[0], "contradicts")

    def test_different_versions_are_not_opposed(self):
        a = claim("The foo 2.1 release fixed the deadlock in the scheduler.")
        b = claim("Users report the deadlock still occurs in foo 2.0 in the scheduler.")
        self.assertIsNone(relation(a, b)[0])

    def test_other_frames_and_mixed_statements(self):
        self.assertEqual(relation(claim("The permit application for the Loop 303 warehouse was approved."),
                                  claim("The permit application for the Loop 303 warehouse was rejected."))[0],
                         "contradicts")
        self.assertIsNone(stance(claim("The patch fixed the crash but the deadlock still occurs in foo 2.1.")))


class D3HeadlinesUseRealStatusCounts(unittest.TestCase):
    def test_single_source_is_never_headlined_as_confirmed(self):
        r = _run("Did foo 2.1 fix the deadlock?", _docs({"release notes": DEADLOCK["release notes"]}))
        state = r.findings[0]
        # before the fix: "Evidence supports: ... (1 confirmed claim(s))."
        self.assertNotIn("confirmed claim", state.answer)
        self.assertIn("1 single-source claim, not confirmed", state.answer)
        self.assertFalse(state.answer.startswith("Evidence supports"))
        for f in r.findings:
            self.assertNotIn("confirmed claim", f.answer)

    def test_verified_claims_are_counted_as_verified(self):
        r = _run("Is Acme Corp profitable given its revenue and costs?", _docs(LEDGER_MEMO))
        answers = [f.answer for f in r.findings]
        self.assertIn("Evidence supports: Acme Corp total revenue for fiscal year 2026 was 410000 dollars "
                      "(1 verified claim; 1 single-source claim, not confirmed).", answers)
        self.assertIn("Evidence suggests, unconfirmed: Acme Corp total costs for fiscal year 2026 were 380000 "
                      "dollars (1 single-source claim, not confirmed).", answers)


class D10RegistrationsAreNotEfficacyEvidence(unittest.TestCase):
    def run_trials(self, objective):
        import test_public_sources as tps
        from lofgren_intelligence.adapters import AdapterRegistry
        from lofgren_intelligence.adapters.clinicaltrials import ClinicalTrialsAdapter
        from lofgren_intelligence.intent.compiler import compile_intent
        from lofgren_intelligence.kernel.pipeline import run_investigation
        from lofgren_intelligence.verification.engine import Verifier

        c, _, _ = tps.client("clinicaltrials", [tps.ok("clinicaltrials_page1.json"),
                                                tps.ok("clinicaltrials_page2.json")])
        reg = AdapterRegistry()
        reg.register(ClinicalTrialsAdapter("zelvapril hypertension", client=c, clock=lambda: tps.NOW,
                                           max_records=10))
        return run_investigation(compile_intent(objective), reg, plan_id="payg",
                                 verifier=Verifier(now=tps.VERIFY_AT))

    def test_registry_records_never_support_an_efficacy_question(self):
        r = self.run_trials("Does zelvapril lower blood pressure in children with hypertension?")
        roles = {q.id: q.role for q in r.contract.questions}
        for f in r.findings:
            if roles[f.question_id] not in ("state", "claim", "support"):
                continue
            # before the fix: "Evidence supports: ClinicalTrials.gov reports that trial NCT90000001 ..."
            self.assertFalse(f.answer.startswith("Evidence supports"), f.answer)
            self.assertEqual(f.claim_ids, [])
            self.assertIn("Registered trials (not efficacy evidence): 3 trial registration(s)", f.answer)
            self.assertIn("do not match the question's population", f.answer)
        md = render_markdown(r)
        section = md[md.index("## Registered trials (not efficacy evidence)"):md.index("## Contradiction graph")]
        for nct in ("NCT90000001", "NCT90000002", "NCT90000003"):
            self.assertIn(f"**{nct}**", section)
        self.assertIn("population mismatch: the question concerns children; the trial registers adults", section)

    def test_matching_population_is_not_flagged(self):
        r = self.run_trials("Does zelvapril lower blood pressure in adults with hypertension?")
        md = render_markdown(r)
        section = md[md.index("## Registered trials (not efficacy evidence)"):md.index("## Contradiction graph")]
        nct1 = section[section.index("**NCT90000001**"):section.index("**NCT90000002**")]
        self.assertNotIn("mismatch", nct1)
        nct2 = section[section.index("**NCT90000002**"):section.index("**NCT90000003**")]
        self.assertIn("population mismatch", nct2)  # older adults only: narrower than the question

    def test_a_non_efficacy_question_may_still_use_the_registry(self):
        r = self.run_trials("Which zelvapril hypertension trials are registered?")
        self.assertTrue(any(f.claim_ids for f in r.findings if f.answer.startswith("Evidence supports")))
        self.assertNotIn("## Registered trials (not efficacy evidence)", render_markdown(r))


if __name__ == "__main__":
    unittest.main()
