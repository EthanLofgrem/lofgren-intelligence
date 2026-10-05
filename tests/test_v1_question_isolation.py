"""Question isolation and finding derivation (hardening/v1-semantic-integrity, commit 4).

Rule B: a question may use the claims gathered for it and the claims gathered for the questions it DIRECTLY
declares in depends_on. Nothing is followed transitively, nothing undeclared is used, and using a dependency's
claim never re-associates that claim. Cross-question synthesis is explicit and names every question it combines.

All data is fictional.
"""

from __future__ import annotations

import copy
import json
import unittest
from types import SimpleNamespace

from lofgren_intelligence.evidence import (
    Claim,
    ClaimStatus,
    Evidence,
    EvidenceGraph,
    EvidenceKind,
    Source,
    SourceKind,
)
from lofgren_intelligence.evidence.types import Contradiction, Finding, question_associations
from lofgren_intelligence.intent.compiler import Question
from lofgren_intelligence.kernel import export_state
from lofgren_intelligence.kernel.answers import answer, claim_scope, declared_dependencies
from lofgren_intelligence.kernel.findings import (
    FindingIntegrityError,
    build_findings,
    finding_problems,
    synthesis_finding,
    validate_findings,
)
from lofgren_intelligence.kernel.receipt import verify_receipt


def question(qid: str, role: str = "state", depends_on: tuple[str, ...] = ()) -> Question:
    return Question(f"Fictional question {qid}?", ["text"], role=role, depends_on=list(depends_on), id=qid)


class Run:
    """A minimal run: a graph, the contract's questions, and helpers to add claims gathered for a question."""

    def __init__(self, *questions: Question):
        self.graph = EvidenceGraph()
        self.source = self.graph.add_source(Source(SourceKind.DOCUMENT, "fictional note", uri="inline:note",
                                                   quality=0.8))
        self.gaps: list[str] = []
        self.unknowns: list = []
        self.calibrated = False
        self.contract = SimpleNamespace(questions=list(questions))
        self.findings: list[Finding] = []

    def claim(self, statement: str, question_id: str, confidence: float = 0.8,
              status: ClaimStatus = ClaimStatus.VERIFIED) -> Claim:
        ev = self.graph.add_evidence(Evidence(self.source.id, EvidenceKind.DOCUMENT, f"{statement} (source text)"))
        return self.graph.add_claim(Claim(statement, question_id=question_id, status=status, confidence=confidence),
                                    supported_by=[ev.id])

    def finding(self, qid: str) -> Finding:
        return next(f for f in build_findings(self) if f.question_id == qid)

    def build(self) -> list[Finding]:
        self.findings = build_findings(self)
        return self.findings


def snapshot(claim: Claim) -> str:
    return json.dumps(claim.__dict__, default=str, sort_keys=True)


class OwnClaimsOnly(unittest.TestCase):
    # 1. A=.70 vs unrelated B=.99: answering A selects A.
    def test_unrelated_stronger_claim_does_not_win(self):
        run = Run(question("Q-A"), question("Q-B"))
        a = run.claim("Phoenix warehouse vacancy rose (fictional).", "Q-A", 0.70)
        b = run.claim("Tucson recorded 40 cold-storage permits (fictional).", "Q-B", 0.99)
        self.assertEqual([c.id for c in answer(run.contract.questions[0], run).claims], [a.id])
        f = run.finding("Q-A")
        self.assertEqual((f.claim_ids, f.confidence, f.derivation, f.question_ids), ([a.id], 0.70, "direct", ["Q-A"]))
        self.assertNotIn(b.id, f.claim_ids)

    # 3. A does NOT depend on B: A may not use B's claim.
    def test_no_declared_dependency_no_access(self):
        run = Run(question("Q-A"), question("Q-B"))
        b = run.claim("A claim gathered for B only (fictional).", "Q-B")
        qa = run.contract.questions[0]
        self.assertEqual(answer(qa, run).claims, [])
        self.assertEqual(claim_scope(qa, run), {})
        f = run.finding("Q-A")
        self.assertEqual((f.claim_ids, f.question_ids, f.derivation), ([], ["Q-A"], "direct"))
        self.assertNotIn(b.id, f.claim_ids)

    def test_dependency_outside_the_contract_grants_nothing(self):
        run = Run(question("Q-A", depends_on=("Q-ghost", "Q-A")))
        run.claim("A claim gathered for a question not in the contract (fictional).", "Q-ghost")
        qa = run.contract.questions[0]
        self.assertEqual(declared_dependencies(qa, run), [])
        self.assertEqual(answer(qa, run).claims, [])


class DeclaredDependencies(unittest.TestCase):
    # 2. A depends on B: A may use B's claim, recorded as coming through B.
    def test_declared_dependency_is_used_and_recorded(self):
        run = Run(question("Q-A", depends_on=("Q-B",)), question("Q-B"))
        b = run.claim("A claim gathered for B (fictional).", "Q-B")
        self.assertEqual([c.id for c in answer(run.contract.questions[0], run).claims], [b.id])
        f = run.finding("Q-A")
        self.assertEqual(f.derivation, "dependency")
        self.assertEqual(f.question_ids, ["Q-A", "Q-B"])
        self.assertEqual(f.claim_scopes, {b.id: ["Q-B"]})  # consumer Q-A -> declared Q-B -> claim b
        self.assertEqual(b.question_ids, ["Q-B"])

    def test_own_and_dependency_claims_make_a_mixed_finding(self):
        run = Run(question("Q-A", depends_on=("Q-B",)), question("Q-B"))
        a = run.claim("A claim gathered for A (fictional).", "Q-A", 0.9)
        b = run.claim("A claim gathered for B (fictional).", "Q-B", 0.8)
        f = run.finding("Q-A")
        self.assertEqual(f.derivation, "mixed")
        self.assertEqual(f.claim_scopes, {a.id: ["Q-A"], b.id: ["Q-B"]})
        self.assertEqual(f.question_ids, ["Q-A", "Q-B"])

    def test_claim_shared_by_consumer_and_dependency_is_direct(self):
        run = Run(question("Q-A", depends_on=("Q-B",)), question("Q-B"))
        run.claim("A shared claim (fictional).", "Q-B")
        shared = run.claim("A shared claim (fictional).", "Q-A")
        self.assertEqual(shared.question_ids, ["Q-A", "Q-B"])
        f = run.finding("Q-A")
        self.assertEqual((f.derivation, f.question_ids, f.claim_scopes), ("direct", ["Q-A"], {}))

    # 4. A -> B -> C: A uses B, not C.
    def test_dependencies_are_not_transitive(self):
        run = Run(question("Q-A", depends_on=("Q-B",)), question("Q-B", depends_on=("Q-C",)), question("Q-C"))
        b = run.claim("A claim gathered for B (fictional).", "Q-B")
        c = run.claim("A claim gathered for C (fictional).", "Q-C", 0.99)
        qa, qb = run.contract.questions[:2]
        self.assertEqual([x.id for x in answer(qa, run).claims], [b.id])
        self.assertEqual({x.id for x in answer(qb, run).claims}, {b.id, c.id})
        f = run.finding("Q-A")
        self.assertNotIn(c.id, f.claim_ids)
        self.assertNotIn("Q-C", f.question_ids)

    # 5. Cyclic A -> B -> A: bounded and deterministic.
    def test_cycle_is_bounded_and_deterministic(self):
        run = Run(question("Q-A", depends_on=("Q-B",)), question("Q-B", depends_on=("Q-A",)))
        a = run.claim("A claim gathered for A (fictional).", "Q-A", 0.7)
        b = run.claim("A claim gathered for B (fictional).", "Q-B", 0.9)
        first = [json.dumps(f.__dict__, default=str, sort_keys=True) for f in run.build()]
        second = [json.dumps(f.__dict__, default=str, sort_keys=True) for f in build_findings(run)]
        self.assertEqual(first, second)
        fa, fb = run.findings
        self.assertEqual((fa.claim_scopes, fb.claim_scopes), ({a.id: ["Q-A"], b.id: ["Q-B"]},
                                                              {a.id: ["Q-A"], b.id: ["Q-B"]}))
        validate_findings(run)

    # 7. Consuming a dependency's claim does not change the claim.
    def test_dependency_consumption_does_not_mutate_the_claim(self):
        run = Run(question("Q-A", depends_on=("Q-B",)), question("Q-B"))
        b = run.claim("A claim gathered for B (fictional).", "Q-B")
        before = snapshot(b)
        graph_before = json.dumps(run.graph.to_json(), sort_keys=True)
        answer(run.contract.questions[0], run)
        run.build()
        validate_findings(run)
        self.assertEqual(snapshot(b), before)
        self.assertEqual(json.dumps(run.graph.to_json(), sort_keys=True), graph_before)


class QuestionAssociations(unittest.TestCase):
    # 6. A claim with several associations keeps all of them, deterministically.
    def test_every_association_is_kept_in_canonical_order(self):
        orders = []
        for first, second in (("Q-b", "Q-a"), ("Q-a", "Q-b")):
            g = EvidenceGraph()
            g.add_claim(Claim("A shared proposition (fictional).", question_id=first))
            g.add_claim(Claim("A shared proposition (fictional).", question_id=second))
            claim = next(iter(g.claims.values()))
            orders.append(claim.question_ids)
            self.assertEqual(claim.question_id, first)  # the first association is kept as the primary one
            self.assertEqual(g.problems(), [])
        self.assertEqual(orders, [["Q-a", "Q-b"], ["Q-a", "Q-b"]])

    def test_associations_are_normalized(self):
        self.assertEqual(question_associations(["Q-b", "Q-a", "Q-b"], "Q-c"), ["Q-a", "Q-b", "Q-c"])
        self.assertEqual(Claim("A claim (fictional).", question_id="Q-a").question_ids, ["Q-a"])
        self.assertEqual(Claim("A claim (fictional).").question_ids, [])
        with self.assertRaises(ValueError):
            question_associations([7])

    def test_association_is_not_identity(self):
        self.assertEqual(Claim("A claim (fictional).", question_ids=["Q-a"]).id,
                         Claim("A claim (fictional).", question_ids=["Q-a", "Q-b"]).id)

    def test_graph_rejects_tampered_associations(self):
        g = EvidenceGraph()
        claim = g.add_claim(Claim("A claim (fictional).", question_id="Q-a"))
        claim.question_ids = ["Q-b"]
        self.assertTrue(any("Q-a" in p for p in g.problems()), g.problems())


class ContradictionScope(unittest.TestCase):
    def contested_run(self, *questions: Question) -> tuple[Run, Contradiction]:
        run = Run(*questions)
        x = run.claim("Vacancy rose (fictional).", "Q-B", 0.6, ClaimStatus.CONTESTED)
        y = run.claim("Vacancy fell (fictional).", "Q-B", 0.6, ClaimStatus.CONTESTED)
        return run, run.graph.add_contradiction(Contradiction(x.id, y.id, "opposite directions"))

    # 8. Contradictions between another question's claims cannot leak in.
    def test_unrelated_contradiction_does_not_leak(self):
        run, cx = self.contested_run(question("Q-A", role="contradict"), question("Q-B"))
        f = run.finding("Q-A")
        self.assertEqual((f.claim_ids, f.contradiction_ids), ([], []))
        self.assertIn(cx.id, run.finding("Q-B").contradiction_ids)

    def test_declared_dependency_contradiction_is_visible(self):
        run, cx = self.contested_run(question("Q-A", role="contradict", depends_on=("Q-B",)), question("Q-B"))
        f = run.finding("Q-A")
        self.assertEqual(f.contradiction_ids, [cx.id])
        self.assertEqual(f.derivation, "dependency")

    def test_tampered_contradiction_is_refused(self):
        run, cx = self.contested_run(question("Q-A", role="contradict"), question("Q-B"))
        run.build()
        run.findings[0].contradiction_ids.append(cx.id)
        self.assertTrue(any("contradiction" in p for p in finding_problems(run.findings, run)))


class Synthesis(unittest.TestCase):
    # 9. Explicit synthesis combines only the named scopes and records every question and claim.
    def test_synthesis_combines_only_named_questions(self):
        run = Run(question("Q-A"), question("Q-B"), question("Q-C"), question("Q-D"))
        a = run.claim("A claim gathered for A (fictional).", "Q-A", 0.7)
        b = run.claim("A claim gathered for B (fictional).", "Q-B", 0.8)
        c = run.claim("A claim gathered for C (fictional).", "Q-C", 0.99)
        f = synthesis_finding(run, ["Q-B", "Q-A", "Q-D"])
        self.assertEqual(f.derivation, "synthesis")
        self.assertEqual(f.question_ids, ["Q-A", "Q-B", "Q-D"])  # Q-D is named even though it contributed nothing
        self.assertEqual(set(f.claim_ids), {a.id, b.id})
        self.assertNotIn(c.id, f.claim_ids)
        self.assertEqual(f.claim_scopes, {a.id: ["Q-A"], b.id: ["Q-B"]})
        run.findings = run.build() + [f]
        validate_findings(run)
        self.assertEqual(synthesis_finding(run, ["Q-A", "Q-B", "Q-D"]).id, f.id)  # order of naming is irrelevant

    def test_synthesis_must_name_real_questions(self):
        run = Run(question("Q-A"), question("Q-B"))
        with self.assertRaises(FindingIntegrityError):
            synthesis_finding(run, ["Q-A"])
        with self.assertRaises(FindingIntegrityError):
            synthesis_finding(run, ["Q-A", "Q-ghost"])

    def test_tampered_synthesis_claim_is_refused(self):
        run = Run(question("Q-A"), question("Q-B"), question("Q-C"))
        run.claim("A claim gathered for A (fictional).", "Q-A")
        c = run.claim("A claim gathered for C (fictional).", "Q-C")
        f = synthesis_finding(run, ["Q-A", "Q-B"])
        f.claim_ids.append(c.id)
        run.findings = [f]
        self.assertTrue(any(c.id in p for p in finding_problems(run.findings, run)))


class FindingShape(unittest.TestCase):
    def test_direct_finding_names_one_question(self):
        with self.assertRaises(ValueError):
            Finding("Q-A", "?", "answer", question_ids=["Q-A", "Q-B"])

    def test_derived_finding_needs_a_scope_for_every_claim(self):
        with self.assertRaises(ValueError):
            Finding("Q-A", "?", "answer", claim_ids=["CL-x"], question_ids=["Q-A", "Q-B"], derivation="dependency")
        with self.assertRaises(ValueError):
            Finding("Q-A", "?", "answer", claim_ids=["CL-x"], question_ids=["Q-A", "Q-B"], derivation="dependency",
                    claim_scopes={"CL-x": ["Q-C"]})
        with self.assertRaises(ValueError):
            Finding("Q-A", "?", "answer", derivation="inferred")

    def test_mislabelled_scope_is_refused(self):
        run = Run(question("Q-A", depends_on=("Q-B", "Q-C")), question("Q-B"), question("Q-C"))
        b = run.claim("A claim gathered for B (fictional).", "Q-B")
        run.build()
        f = run.findings[0]
        f.claim_scopes[b.id] = ["Q-C"]  # says the claim came through C; it was gathered for B
        self.assertTrue(any("coming through Q-C" in p for p in finding_problems(run.findings, run)))

    def test_undeclared_question_named_is_refused(self):
        run = Run(question("Q-A"), question("Q-B"))
        run.build()
        run.findings[0].question_ids.append("Q-B")
        self.assertTrue(any("without a declared dependency" in p for p in finding_problems(run.findings, run)))


class ReceiptIntegrity(unittest.TestCase):
    def setUp(self):
        from lofgren_intelligence.adapters import AdapterRegistry, DocumentAdapter
        from lofgren_intelligence.intent.compiler import compile_intent
        from lofgren_intelligence.kernel import run_investigation

        from .helpers import TEXTS

        reg = AdapterRegistry()
        reg.register(DocumentAdapter(texts=TEXTS))
        self.run = run_investigation(compile_intent("Is industrial construction in the Phoenix metro increasing?"), reg)

    def test_real_run_findings_are_valid(self):
        self.assertEqual(finding_problems(self.run.findings, self.run), [])
        for f in self.run.findings:
            for cid in f.claim_ids:
                self.assertTrue(set(self.run.graph.claims[cid].question_ids) & set(f.question_ids))

    # 10. A tampered finding (a claim outside its scope) gets no receipt, and a tampered receipt fails verification.
    def test_tampered_finding_gets_no_receipt(self):
        from lofgren_intelligence.kernel.pipeline import _finish
        from lofgren_intelligence.models import HeuristicProvider

        old_receipt = dict(self.run.receipt)
        foreign = self.run.graph.add_claim(Claim("A claim gathered elsewhere (fictional).", question_id="Q-elsewhere"))
        self.run.findings[0].claim_ids.append(foreign.id)
        with self.assertRaises(FindingIntegrityError):
            _finish(self.run, HeuristicProvider())
        self.assertEqual(self.run.receipt, old_receipt)

    def test_tampered_receipt_finding_fails_verification(self):
        receipt = copy.deepcopy(self.run.receipt)
        self.assertTrue(verify_receipt(receipt))
        receipt["findings"][0]["claim_scopes"] = {"CL-foreign": ["Q-elsewhere"]}
        self.assertFalse(verify_receipt(receipt))

    def test_knowledge_map_v1_stays_frozen(self):
        exported = export_state(self.run)
        for f in exported["findings"]:
            self.assertFalse({"question_ids", "derivation", "claim_scopes"} & set(f), sorted(f))
        for section in ("known", "uncertain", "contradicted"):
            for c in exported[section]:
                self.assertNotIn("question_ids", c)


if __name__ == "__main__":
    unittest.main()
