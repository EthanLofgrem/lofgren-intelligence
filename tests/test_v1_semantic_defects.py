"""V1 semantic-integrity defects, pinned before any fix (hardening/v1-semantic-integrity, commit 1).

Each `@known_defect` test states the REQUIRED behaviour. While the defect exists, its assertion fails and the
decorator records that and lets the test pass. When a fix makes the requirement hold, the decorator fails the
test ("no longer reproduces"), so the fix must delete the decorator: the spec then becomes a plain regression
test and the history keeps the evidence that the defect existed. Any exception other than AssertionError is a
real error, so a spec cannot pass by failing for the wrong reason.

Tests without the decorator pin behaviour that is correct today and must survive the fixes, or document what
knowledge-map/1 can and cannot show (it stays a lower-assurance format by design).

All data is fictional.
"""

from __future__ import annotations

import dataclasses
import functools
import unittest
from types import SimpleNamespace

from lofgren_intelligence.evidence import (
    Claim,
    ClaimStatus,
    Evidence,
    EvidenceGraph,
    EvidenceKind,
    Scope,
    Source,
    SourceKind,
)
from lofgren_intelligence.evidence.types import Finding
from lofgren_intelligence.intent.compiler import Question
from lofgren_intelligence.kernel import export_state
from lofgren_intelligence.kernel.answers import answer
from lofgren_intelligence.kernel.findings import build_findings

KNOWN_DEFECTS: dict[str, str] = {}  # defect id -> the assertion message observed on the current code


def known_defect(defect_id: str):
    def wrap(fn):
        @functools.wraps(fn)
        def run(self):
            try:
                fn(self)
            except AssertionError as exc:
                KNOWN_DEFECTS[defect_id] = str(exc).splitlines()[0]
                return
            self.fail(f"{defect_id} no longer reproduces: the requirement now holds. Remove @known_defect so this "
                      "spec becomes a regression test.")
        run.defect_id = defect_id
        return run
    return wrap


class KnownDefectDecoratorTests(unittest.TestCase):
    """The decorator cannot hide a fixed defect or a spec that fails for the wrong reason."""

    def test_fixed_defect_fails_loudly(self):
        spec = known_defect("SELF-TEST-FIXED")(lambda self: None)
        with self.assertRaises(AssertionError) as ctx:
            spec(self)
        self.assertIn("no longer reproduces", str(ctx.exception))

    def test_wrong_reason_is_an_error(self):
        def broken(self):
            raise KeyError("setup bug")
        with self.assertRaises(KeyError):
            known_defect("SELF-TEST-ERROR")(broken)(self)

    def test_reproduced_defect_is_recorded(self):
        def failing(self):
            self.assertEqual(1, 2, "observed")
        known_defect("SELF-TEST-REPRODUCED")(failing)(self)
        self.assertIn("observed", KNOWN_DEFECTS.pop("SELF-TEST-REPRODUCED"))


PHOENIX_2026 = Scope("2026-01-01", "2026-12-31", "Phoenix")
TUCSON_2026 = Scope("2026-01-01", "2026-12-31", "Tucson")
PHOENIX_2024 = Scope("2024-01-01", "2024-12-31", "Phoenix")
VACANCY = "Industrial warehouse vacancy rose (fictional)."


def graph_with_source() -> tuple[EvidenceGraph, Source]:
    g = EvidenceGraph()
    return g, g.add_source(Source(SourceKind.DOCUMENT, "fictional broker note", uri="inline:broker", quality=0.8))


class ClaimIdentityDefects(unittest.TestCase):
    """Under identity v1, Claim.id was make_id("CL", statement.lower().strip()), so scope and structure played no
    part. Claim identity v2 fixed the five identity specs below; they are now regression tests."""

    def test_identical_proposition_keeps_one_identity(self):
        # Must survive the fix: the same proposition is the same claim.
        self.assertEqual(Claim(VACANCY, scope=PHOENIX_2026).id, Claim(VACANCY.upper(), scope=PHOENIX_2026).id)

    # Fixed by claim identity v2 (was CLAIM-ID-GEOGRAPHY).
    def test_different_geography_is_a_different_proposition(self):
        a, b = Claim(VACANCY, scope=PHOENIX_2026), Claim(VACANCY, scope=TUCSON_2026)
        self.assertNotEqual(a.id, b.id, f"Phoenix and Tucson claims share id {a.id}")

    # Fixed by claim identity v2 (was CLAIM-ID-PERIOD).
    def test_different_period_is_a_different_proposition(self):
        a, b = Claim(VACANCY, scope=PHOENIX_2026), Claim(VACANCY, scope=PHOENIX_2024)
        self.assertNotEqual(a.id, b.id, f"2026 and 2024 claims share id {a.id}")

    # Fixed by claim identity v2 (was CLAIM-ID-VALUE).
    def test_different_structured_value_is_a_different_proposition(self):
        a = Claim(VACANCY, value=11.0, unit="%", scope=PHOENIX_2026)
        b = Claim(VACANCY, value=14.0, unit="%", scope=PHOENIX_2026)
        self.assertNotEqual(a.id, b.id, f"value 11 and value 14 share id {a.id}")

    # Fixed by claim identity v2 (was CLAIM-ID-UNIT).
    def test_different_structured_unit_is_a_different_proposition(self):
        a = Claim(VACANCY, value=11.0, unit="%", scope=PHOENIX_2026)
        b = Claim(VACANCY, value=11.0, unit="points", scope=PHOENIX_2026)
        self.assertNotEqual(a.id, b.id, f"units % and points share id {a.id}")

    # Fixed by claim identity v2 (was CLAIM-ID-SUBJECT).
    def test_different_structured_subject_is_a_different_proposition(self):
        a = Claim(VACANCY, subject="vacancy:phoenix-industrial", scope=PHOENIX_2026)
        b = Claim(VACANCY, subject="vacancy:phoenix-flex", scope=PHOENIX_2026)
        self.assertNotEqual(a.id, b.id, f"subjects phoenix-industrial and phoenix-flex share id {a.id}")

    def test_question_does_not_change_proposition_identity(self):
        # Must survive the fix: question association is not part of what a proposition says.
        self.assertEqual(Claim(VACANCY, scope=PHOENIX_2026, question_id="Q-a").id,
                         Claim(VACANCY, scope=PHOENIX_2026, question_id="Q-b").id)

    @known_defect("CLAIM-QUESTION-ASSOCIATION")
    def test_shared_proposition_keeps_every_question_association(self):
        g, _ = graph_with_source()
        g.add_claim(Claim(VACANCY, scope=PHOENIX_2026, question_id="Q-a"))
        g.add_claim(Claim(VACANCY, scope=PHOENIX_2026, question_id="Q-b"))
        claim = next(iter(g.claims.values()))
        associations = set(getattr(claim, "question_ids", None) or {claim.question_id})
        self.assertEqual(associations, {"Q-a", "Q-b"}, f"only {sorted(associations)} survives")


class GraphIntegrityDefects(unittest.TestCase):
    # Was GRAPH-FIRST-WINS. Identity v2 gives the Phoenix and Tucson claims different ids, so this scenario no
    # longer collides; first-wins itself remains for claims with the same identity (see the spec below).
    def test_claims_with_different_scope_are_both_kept(self):
        g, _ = graph_with_source()
        first = g.add_claim(Claim(VACANCY, scope=PHOENIX_2026))
        second = g.add_claim(Claim(VACANCY, scope=TUCSON_2026))
        self.assertIsNot(first, second)
        self.assertEqual(sorted(c.scope.geography for c in g.claims.values()), ["Phoenix", "Tucson"])

    # Fixed by commit 3 (was GRAPH-FIRST-WINS-SAME-PROPOSITION): add_claim used to return the stored claim, so a
    # hypothesis with a verified claim's proposition came back as that verified claim.
    def test_hypothesis_never_becomes_an_existing_verified_claim(self):
        from lofgren_intelligence.discovery import Hypothesis, add_hypothesis
        from lofgren_intelligence.evidence.graph import HypothesisCollision

        g, _ = graph_with_source()
        verified = g.add_claim(Claim(VACANCY, scope=PHOENIX_2026, status=ClaimStatus.VERIFIED, confidence=0.9))
        with self.assertRaises(HypothesisCollision) as ctx:
            add_hypothesis(g, Hypothesis(VACANCY, scope=dataclasses.asdict(PHOENIX_2026)))
        self.assertEqual((ctx.exception.claim_id, ctx.exception.existing_status), (verified.id, ClaimStatus.VERIFIED))
        self.assertEqual((len(g.claims), verified.origin, verified.status),
                         (1, verified.origin, ClaimStatus.VERIFIED))  # nothing changed

    # Fixed by commit 3 (was GRAPH-UNKNOWN-RELATION): link() filed any non-"supports" relation as contradicting.
    def test_unknown_relation_fails_closed(self):
        from lofgren_intelligence.evidence.graph import UnknownRelation

        g, src = graph_with_source()
        ev = g.add_evidence(Evidence(src.id, EvidenceKind.DOCUMENT, "Vacancy rose in Phoenix (fictional)."))
        claim = g.add_claim(Claim(VACANCY, scope=PHOENIX_2026))
        for relation in ("mentions", "provided_by", "conflicts", "derived_from", "SUPPORTS", ""):
            with self.subTest(relation=relation), self.assertRaises(UnknownRelation):
                g.link(ev.id, claim.id, relation)
        self.assertEqual((claim.supporting, claim.contradicting), ([], []))

    def test_supports_relation_still_supports(self):
        # Must survive the fix.
        g, src = graph_with_source()
        ev = g.add_evidence(Evidence(src.id, EvidenceKind.DOCUMENT, "Vacancy rose in Phoenix (fictional)."))
        claim = g.add_claim(Claim(VACANCY, scope=PHOENIX_2026), supported_by=[ev.id])
        self.assertEqual((claim.supporting, claim.contradicting), ([ev.id], []))


class EvidenceIdentityDefects(unittest.TestCase):
    """Under evidence identity v1, Evidence.id was make_id("EV", source_id, content[:200], observed_at), so the tail
    of the content was ignored. Evidence identity v2 fixed this."""

    PREFIX = "Fictional sensor log. " + "x" * 178  # exactly 200 characters

    def test_prefix_is_two_hundred_characters(self):
        self.assertEqual(len(self.PREFIX), 200)

    # Fixed by evidence identity v2 (was EVIDENCE-ID-PREFIX).
    def test_different_content_after_200_characters_is_different_evidence(self):
        g, src = graph_with_source()
        a = Evidence(src.id, EvidenceKind.DOCUMENT, self.PREFIX + " reading 21 C", observed_at="2026-09-01T00:00:00Z")
        b = Evidence(src.id, EvidenceKind.DOCUMENT, self.PREFIX + " reading 48 C", observed_at="2026-09-01T00:00:00Z")
        g.add_evidence(a)
        g.add_evidence(b)
        self.assertNotEqual(a.id, b.id, f"different readings share id {a.id}; the graph keeps {len(g.evidence)} of 2")
        self.assertEqual(len(g.evidence), 2)

    def test_identical_evidence_keeps_one_identity(self):
        # Must survive the fix.
        _, src = graph_with_source()
        a = Evidence(src.id, EvidenceKind.DOCUMENT, self.PREFIX + " reading 21 C", observed_at="2026-09-01T00:00:00Z")
        b = Evidence(src.id, EvidenceKind.DOCUMENT, self.PREFIX + " reading 21 C", observed_at="2026-09-01T00:00:00Z")
        self.assertEqual(a.id, b.id)


def two_question_run():
    """Question A has the right claim at 0.70; question B has an unrelated claim at 0.99."""
    qa = Question("What happened to Phoenix warehouse vacancy in 2026? (fictional)", ["text"], role="state")
    qb = Question("How many cold-storage permits were filed in Tucson? (fictional)", ["text"], role="state")
    g, src = graph_with_source()
    ev_a = g.add_evidence(Evidence(src.id, EvidenceKind.DOCUMENT, "Phoenix vacancy rose (fictional)."))
    ev_b = g.add_evidence(Evidence(src.id, EvidenceKind.DOCUMENT, "Tucson permits: 40 (fictional)."))
    claim_a = g.add_claim(Claim("Phoenix warehouse vacancy rose in 2026 (fictional).", question_id=qa.id,
                                status=ClaimStatus.VERIFIED, confidence=0.70), supported_by=[ev_a.id])
    claim_b = g.add_claim(Claim("Tucson recorded 40 cold-storage permits (fictional).", question_id=qb.id,
                                status=ClaimStatus.VERIFIED, confidence=0.99), supported_by=[ev_b.id])
    run = SimpleNamespace(graph=g, gaps=[], unknowns=[], calibrated=False,
                          contract=SimpleNamespace(questions=[qa, qb]))
    return run, qa, qb, claim_a, claim_b


class QuestionIsolationDefects(unittest.TestCase):
    @known_defect("ANSWER-CROSS-QUESTION-LEAK")
    def test_direct_answer_ranks_only_its_own_claims(self):
        run, qa, _, claim_a, claim_b = two_question_run()
        top = answer(qa, run).claims[0]
        self.assertEqual(top.id, claim_a.id, f"question A's top claim is question B's claim at {top.confidence}")

    @known_defect("ANSWER-FOREIGN-CLAIMS-LISTED")
    def test_direct_answer_lists_no_foreign_claims(self):
        run, qa, _, _, claim_b = two_question_run()
        listed = [c.id for c in answer(qa, run).claims]
        self.assertNotIn(claim_b.id, listed, f"question A's answer lists question B's claim {claim_b.id}")

    @known_defect("FINDING-CROSS-QUESTION-LEAK")
    def test_finding_rests_on_its_own_question(self):
        run, qa, _, claim_a, claim_b = two_question_run()
        finding = next(f for f in build_findings(run) if f.question_id == qa.id)
        self.assertEqual((finding.claim_ids[0], finding.confidence), (claim_a.id, 0.70),
                         f"question A's finding leads with {finding.claim_ids[0]} at confidence {finding.confidence}")

    def test_answer_for_b_selects_b(self):
        # Control: the defect is about foreign claims winning, not about B's own answer.
        run, _, qb, _, claim_b = two_question_run()
        self.assertEqual(answer(qb, run).claims[0].id, claim_b.id)


class MultiQuestionSynthesisRequirement(unittest.TestCase):
    """Synthesis across questions is legitimate when explicit. The fix for leakage must keep it possible."""

    @known_defect("FINDING-NO-MULTI-QUESTION")
    def test_a_finding_can_record_every_question_it_synthesizes(self):
        fields = {f.name for f in dataclasses.fields(Finding)}
        self.assertIn("question_ids", fields, f"Finding records a single question_id only; fields: {sorted(fields)}")


class KnowledgeMapV1Limits(unittest.TestCase):
    """What knowledge-map/1 can and cannot show. /1 stays a lower-assurance format; knowledge-map/2 must add these."""

    @classmethod
    def setUpClass(cls):
        from lofgren_intelligence.adapters import AdapterRegistry, DocumentAdapter
        from lofgren_intelligence.intent.compiler import compile_intent
        from lofgren_intelligence.kernel import run_investigation

        from .helpers import TEXTS

        reg = AdapterRegistry()
        reg.register(DocumentAdapter(texts=TEXTS))
        cls.v1_run = run_investigation(compile_intent("Is industrial construction in the Phoenix metro increasing?"), reg)
        cls.map = export_state(cls.v1_run)

    def test_evidence_ids_are_listed(self):
        cited = {e for c in self.map["known"] + self.map["uncertain"] + self.map["contradicted"] for e in c["evidence"]}
        self.assertTrue(cited)
        self.assertTrue(cited <= set(self.v1_run.graph.evidence))

    def test_evidence_sources_and_lineage_are_not_exported(self):
        self.assertFalse({"evidence", "sources", "lineage", "edges"} & set(self.map))
        for c in self.map["known"] + self.map["uncertain"] + self.map["contradicted"]:
            self.assertFalse({"sources", "source_ids", "independence_groups", "lineage"} & set(c))

    def test_receipt_state_is_not_exported(self):
        self.assertTrue(self.v1_run.receipt.get("state_hash"))
        self.assertFalse({"state_hash", "inputs_hash", "identity_versions"} & set(self.map))

    def test_independence_cannot_be_reconstructed_from_the_map(self):
        # V1 knows which sources share an independence group; the map does not carry it.
        groups = {s.independence_group for s in self.v1_run.graph.sources.values()}
        self.assertTrue(groups)
        exported = repr(self.map)
        self.assertFalse(any(g and g in exported for g in groups))


if __name__ == "__main__":
    unittest.main()
