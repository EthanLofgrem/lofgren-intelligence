"""DiscoveryContext over knowledge-map/2 (hardening commit 6): assurance, provenance, receipt binding, references.

Every V1 run here is local and deterministic: document fixtures, a local sensor log and the heuristic provider (no
network, no model). All data is fictional.
"""

from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path

import lofgren_intelligence.certification as cert
from lofgren_intelligence.discovery import (
    ContextMismatch,
    DiscoveryObjective,
    KnownFact,
    MalformedInput,
    MissingEvidence,
    NonFiniteValue,
    PromotionRefused,
    Uncertainty,
    UnknownReference,
)
from lofgren_intelligence.discovery.context import AssuranceLevel, DiscoveryContext, KnowledgeMapRefused, thaw
from lofgren_intelligence.discovery.frame import frame_problem, uncertainty_reason
from lofgren_intelligence.discovery.gaps import detect_gaps
from lofgren_intelligence.discovery.types import UncertaintyReason
from lofgren_intelligence.evidence import Claim, ClaimOrigin
from lofgren_intelligence.kernel import export_knowledge_map, export_state, verify_receipt
from lofgren_intelligence.kernel.knowledge_map import compute_fingerprints, knowledge_state_hash
from lofgren_intelligence.kernel.pipeline import _finish
from lofgren_intelligence.models import HeuristicProvider

from .test_v1_knowledge_map2 import phoenix_run, sensor_run

AT = "2026-09-30T12:00:00+00:00"


def refingerprint(m: dict) -> dict:
    m["receipt"]["knowledge_state_hash"] = knowledge_state_hash(m)
    m["fingerprint"] = compute_fingerprints(m)
    return m


def objective(ctx: DiscoveryContext) -> DiscoveryObjective:
    return DiscoveryObjective("Find a warehouse opportunity (fictional)", ctx.research_id, created_at=AT)


class Contexts(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.result = phoenix_run()
        cls.map = export_knowledge_map(cls.result)
        cls.ctx = DiscoveryContext(cls.map, cls.result.receipt)
        cls.v1 = DiscoveryContext(export_state(cls.result))

    def m(self) -> dict:
        return copy.deepcopy(self.map)

    def refused(self, m, receipt=None, error=KnowledgeMapRefused, fragment: str = ""):
        with self.assertRaises(error) as caught:
            DiscoveryContext(m, self.result.receipt if receipt is None else receipt)
        self.assertIn(fragment, str(caught.exception))
        return caught.exception

    # 1, 2, 29: two schemas, two assurance levels that cannot be confused.
    def test_v2_is_validated_and_v1_is_degraded(self):
        a, d = self.ctx.assurance, self.v1.assurance
        self.assertEqual((a.level, a.receipt_bound, a.provenance_inspectable), (AssuranceLevel.VALIDATED_V2, True,
                                                                                True))
        self.assertEqual((d.level, d.receipt_bound, d.provenance_inspectable), (AssuranceLevel.DEGRADED_V1, False,
                                                                                False))
        self.assertEqual((self.ctx.version, self.v1.version), (2, 1))
        self.assertEqual(self.ctx.schema, "lofgren.knowledge-map/2")
        self.assertEqual(self.ctx.fingerprint_algorithm, "sha256/canonical-json-2")
        self.assertEqual(self.v1.fingerprint_algorithm, "sha256/canonical-json-1")
        with self.assertRaises((UnknownReference, ContextMismatch)):
            self.ctx.assert_same_context(self.v1)

    def test_fingerprints_keep_their_meanings(self):
        fp = self.map["fingerprint"]
        self.assertEqual((self.ctx.knowledge_map_fingerprint, self.ctx.content_fingerprint), (fp["map"], fp["content"]))
        self.assertTrue(self.v1.knowledge_map_fingerprint.startswith("KMF-"))
        self.assertIsNone(self.v1.content_fingerprint)
        self.assertIsNone(self.v1.receipt)
        receipt = self.result.receipt
        self.assertEqual(dict(self.ctx.receipt), {k: receipt[k] for k in ("schema", "research_id", "contract_hash",
                                                                            "inputs_hash", "state_hash",
                                                                            "knowledge_state_hash")})
        distinct = {self.ctx.knowledge_map_fingerprint, self.ctx.content_fingerprint, self.v1.knowledge_map_fingerprint,
                    receipt["state_hash"], receipt["inputs_hash"]}
        self.assertEqual(len(distinct), 5)

    def test_limitations_come_from_the_map(self):
        self.assertEqual(list(self.ctx.limitations), self.map["limitations"])
        self.assertTrue(any("inputs_hash" in x for x in self.ctx.limitations))
        self.assertTrue(any("does not export" in x or "lists evidence ids" in x for x in self.v1.limitations))

    # 3: /1 keeps attachment-only evidence and no sources.
    def test_v1_does_not_gain_evidence_or_sources(self):
        ev = next(iter(self.result.graph.evidence))
        src = next(iter(self.result.graph.sources))
        for ref in (ev, src):
            with self.assertRaises(UnknownReference):
                self.v1.resolve(ref)
        with self.assertRaises(UnknownReference):
            self.v1.evidence(ev)
        with self.assertRaises(UnknownReference):
            self.v1.lineage()
        self.assertEqual(set(self.v1.reference(ev)), {"id", "claims"})  # an attachment, not a record

    # 4, 5, 6: evidence and sources resolve, and evidence names its source.
    def test_evidence_and_sources_resolve(self):
        g = self.result.graph
        for e in g.evidence.values():
            rec = self.ctx.resolve(e.id)
            self.assertEqual((rec.kind, rec.section), ("EV", "evidence"))
            self.assertEqual((rec.value["content_hash"], rec.value["source_id"]), (e.content_hash, e.source_id))
            self.assertEqual(self.ctx.source_of(e.id)["id"], e.source_id)
            self.assertEqual(self.ctx.reference(e.id)["content_hash"], e.content_hash)
        for s in g.sources.values():
            rec = self.ctx.resolve(s.id)
            self.assertEqual((rec.kind, rec.value["uri"], rec.value["publisher"]), ("SRC", s.uri, s.publisher))
            self.assertEqual(self.ctx.source(s.id)["independence_group"], s.independence_group)
        q = self.result.contract.questions[0]
        self.assertEqual(self.ctx.resolve(q.id).value["text"], q.text)

    def test_claims_are_not_flattened(self):
        for c in self.result.graph.claims.values():
            rec = self.ctx.resolve(c.id).value
            self.assertEqual((rec["origin"], rec["polarity"], rec["identity_version"], rec["question_ids"],
                              rec["supporting"], rec["contradicting"]),
                             (c.origin.value, c.polarity, c.identity_version, c.question_ids, c.supporting,
                              c.contradicting))
            self.assertIn("assessment", rec)
        for cat in ("known", "uncertain", "contradicted"):  # the category view agrees with /1
            self.assertEqual([c["id"] for c in self.ctx.claims(cat)], [c["id"] for c in self.v1.claims(cat)])

    # 8, 9: question associations and dependencies round-trip.
    def test_question_associations_and_dependencies(self):
        for q in self.result.contract.questions:
            self.assertEqual(list(self.ctx.resolve(q.id).value["depends_on"]), q.depends_on)
        for c in self.ctx.entities("claims"):
            self.assertEqual(list(c["question_ids"]), self.result.graph.claims[c["id"]].question_ids)

    # 12-17: the map and its receipt must belong together, intact.
    def test_v2_requires_its_receipt(self):
        with self.assertRaises(MalformedInput):
            DiscoveryContext(self.m())
        with self.assertRaises(MalformedInput):
            DiscoveryContext(export_state(self.result), self.result.receipt)  # /1 cannot be receipt-bound

    def test_wrong_research_id(self):
        m = self.m()
        m["research_id"] = m["receipt"]["research_id"] = "RR-" + "0" * 20
        self.refused(refingerprint(m), fragment="another run's map")

    def test_wrong_receipt(self):
        other = sensor_run(Path(tempfile.mkdtemp()) / "s.csv")
        self.refused(self.m(), other.receipt, fragment="does not match the receipt")

    def test_tampered_receipt(self):
        receipt = copy.deepcopy(self.result.receipt)
        receipt["claims"][0]["confidence"] = 0.01
        self.assertFalse(verify_receipt(receipt))
        self.refused(self.m(), receipt, fragment="not intact")

    def test_tampered_fingerprints(self):
        m = self.m()
        m["fingerprint"]["map"] = "KM2-" + "0" * 64
        self.refused(m, fragment="fingerprint.map")
        m = self.m()
        m["fingerprint"]["content"] = "KM2C-" + "0" * 64
        self.refused(m, fragment="fingerprint.content")
        m = self.m()
        m["claims"][0]["issues"].append("edited")
        self.refused(m, fragment="altered after export")
        self.refused(refingerprint(m), fragment="issues differ from the receipt")  # recomputing does not help

    def test_unsupported_schema(self):
        for schema in ("lofgren.knowledge-map/3", "lofgren.knowledge-map/2.1"):
            m = self.m()
            m["schema"] = schema
            with self.assertRaises(MalformedInput):
                DiscoveryContext(m, self.result.receipt)

    # 18-23: references, hypotheses, question scope and derivation fail closed.
    def test_dangling_references(self):
        edits = {
            "EV": lambda m: m["claims"][0]["supporting"].append("EV-missing00"),
            "SRC": lambda m: m["evidence"][0].update(source_id="SRC-missing0"),
            "CL": lambda m: m["contradictions"].append({**copy.deepcopy(m["contradictions"][0]), "id": "CX-new0000000",
                                                        "claim_b": "CL-missing00"}),
            "CX": lambda m: m["findings"][0]["contradiction_ids"].append("CX-missing00"),
            "UNK": lambda m: m["findings"][0]["unknown_ids"].append("UNK-missing0"),
            "CALC": lambda m: m["claims"][0].update(calculation_id="CALC-missing"),
            "Q": lambda m: m["questions"][0]["depends_on"].append("Q-missing000"),
        }
        for kind, edit in edits.items():
            with self.subTest(kind=kind):
                m = self.m()
                edit(m)
                self.refused(refingerprint(m), fragment="is not in this map")
        m = self.m()
        m["findings"].append({**copy.deepcopy(m["findings"][0]), "id": "F-missing000", "question_id": "Q-missing000"})
        self.refused(refingerprint(m), fragment="Q-missing000 is not in this map")

    def test_promoted_hypothesis(self):
        m = self.m()
        m["claims"][0].update(origin="hypothesis", status="verified")
        self.refused(refingerprint(m), fragment="a hypothesis is never verified")

    def test_non_finite_and_malformed_dates(self):
        m = self.m()
        m["claims"][0]["confidence"] = float("nan")
        with self.assertRaises(NonFiniteValue):
            DiscoveryContext(m, self.result.receipt)
        text = json.dumps(self.map).replace('"quality": ', '"quality": Infinity, "x": ', 1)
        with self.assertRaises(NonFiniteValue):
            DiscoveryContext(text, self.result.receipt)
        m = self.m()
        m["evidence"][0]["observed_at"] = "2026-02-30"
        self.refused(refingerprint(m), fragment="not an ISO 8601 date")

    # 26, 27, 28: immutability and reproducibility.
    def test_inputs_and_outputs_cannot_mutate_the_context(self):
        m, receipt = self.m(), copy.deepcopy(self.result.receipt)
        ctx = DiscoveryContext(m, receipt)
        before = ctx.canonical_json()
        m["claims"][0]["statement"] = "changed"
        m["sources"].clear()
        receipt["state_hash"] = "0" * 64
        snap = ctx.snapshot()
        snap["claims"][0]["statement"] = "changed"
        resolved = ctx.resolve(ctx.entities("claims")[0]["id"])
        resolved.value["statement"] = "changed"
        thawed = thaw(ctx.evidence(ctx.entities("evidence")[0]["id"]))
        thawed["content_hash"] = "changed"
        self.assertEqual(ctx.canonical_json(), before)
        self.assertEqual(ctx.receipt["state_hash"], self.result.receipt["state_hash"])
        with self.assertRaises(TypeError):
            ctx.evidence(ctx.entities("evidence")[0]["id"])["content_hash"] = "changed"
        with self.assertRaises(MalformedInput):
            ctx._map = {}

    def test_two_contexts_reproduce_fingerprints(self):
        again = DiscoveryContext(json.dumps(self.map), self.result.receipt)
        self.assertEqual((again.knowledge_map_fingerprint, again.content_fingerprint, again.canonical_json()),
                         (self.ctx.knowledge_map_fingerprint, self.ctx.content_fingerprint, self.ctx.canonical_json()))
        rerun = phoenix_run()  # a rerun of identical inputs: same content, whatever its research id
        other = DiscoveryContext(export_knowledge_map(rerun), rerun.receipt)
        self.assertEqual(other.content_fingerprint, self.ctx.content_fingerprint)

    # 30, 31: the V2 registry works over /2 and keeps its promotion rules.
    def test_v2_framing_and_gaps(self):
        ctx = DiscoveryContext(self.map, self.result.receipt)
        framed = frame_problem(ctx, objective(ctx), at=AT)
        legacy = DiscoveryContext(export_state(self.result))
        framed_v1 = frame_problem(legacy, objective(legacy), at=AT)
        self.assertEqual([f.claim_id for f in framed.known_facts], [f.claim_id for f in framed_v1.known_facts])
        self.assertEqual([(u.claim_id, u.reason) for u in framed.uncertainties],
                         [(u.claim_id, u.reason) for u in framed_v1.uncertainties])
        self.assertEqual([m.unknown_id for m in framed.missing_evidence],
                         [m.unknown_id for m in framed_v1.missing_evidence])
        gaps = detect_gaps(ctx, framed, at=AT)
        self.assertEqual(len(gaps.gaps), len(detect_gaps(legacy, framed_v1, at=AT).gaps))

    def test_promotion_rules_hold(self):
        ctx = DiscoveryContext(self.map, self.result.receipt)
        uncertain = ctx.claims("uncertain")[0]
        with self.assertRaises(PromotionRefused):
            ctx.register(KnownFact(uncertain["id"], uncertain["statement"], thaw(uncertain["scope"]),
                                   uncertain["value"], uncertain["unit"], uncertain["policy"], uncertain["confidence"],
                                   created_at=AT))
        known = ctx.claims("known")[0]
        with self.assertRaises(ContextMismatch):
            ctx.register(KnownFact(known["id"], known["statement"] + " (edited)", thaw(known["scope"]), known["value"],
                                   known["unit"], known["policy"], known["confidence"], created_at=AT))
        with self.assertRaises(UnknownReference):
            ctx.register(KnownFact("CL-missing00", "x", {}, None, "", "", 0.5, created_at=AT))


class Hypotheses(unittest.TestCase):
    """An unverified V1 claim (here a V1 hypothesis) is visible under /2 but can never be cited."""

    @classmethod
    def setUpClass(cls):
        cls.result = phoenix_run()
        cls.hyp = cls.result.graph.add_claim(Claim("A fictional untested hypothesis about warehouses.",
                                                   origin=ClaimOrigin.HYPOTHESIS))
        _finish(cls.result, HeuristicProvider())  # issue the receipt again, now including the hypothesis
        cls.ctx = DiscoveryContext(export_knowledge_map(cls.result), cls.result.receipt)

    def test_hypothesis_is_inspectable_not_citable(self):
        rec = self.ctx.resolve(self.hyp.id).value
        self.assertEqual((rec["origin"], rec["status"]), ("hypothesis", "unverified"))
        self.assertNotIn(self.hyp.id, [c["id"] for cat in ("known", "uncertain", "contradicted")
                                       for c in self.ctx.claims(cat)])
        for accept in (("claim",), ("claim:uncertain",), ("any",)):
            with self.subTest(accept=accept), self.assertRaises(PromotionRefused):
                self.ctx.reference(self.hyp.id, accept)
        with self.assertRaises(PromotionRefused):
            self.ctx.register(Uncertainty(self.hyp.id, self.hyp.statement, UncertaintyReason.POLICY_UNMET, {}, 0.0,
                                          created_at=AT))


class Derivation(unittest.TestCase):
    """10, 11: finding derivation and claim scopes survive into V2 (sensor run: a support question that answers
    through its declared dependency)."""

    @classmethod
    def setUpClass(cls):
        cls.dir = tempfile.TemporaryDirectory()
        cls.result = sensor_run(Path(cls.dir.name) / "sensors.csv")
        cls.map = export_knowledge_map(cls.result)
        cls.ctx = DiscoveryContext(cls.map, cls.result.receipt)

    @classmethod
    def tearDownClass(cls):
        cls.dir.cleanup()

    def test_derivation_round_trips(self):
        for f in self.result.findings:
            rec = self.ctx.resolve(f.id).value
            self.assertEqual((rec["derivation"], list(rec["question_ids"]), thaw(rec["claim_scopes"])),
                             (f.derivation, f.question_ids, f.claim_scopes))
        self.assertTrue(any(f.derivation == "dependency" for f in self.result.findings))

    def test_invalid_dependency_derivation(self):
        m = copy.deepcopy(self.map)
        f = next(f for f in m["findings"] if f["derivation"] == "dependency")
        next(q for q in m["questions"] if q["id"] == f["question_id"])["depends_on"] = []
        with self.assertRaises(KnowledgeMapRefused):
            DiscoveryContext(refingerprint(m), self.result.receipt)
        m = copy.deepcopy(self.map)
        f = next(f for f in m["findings"] if f["derivation"] == "dependency")
        f["claim_scopes"][next(iter(f["claim_scopes"]))] = [f["question_id"]]
        with self.assertRaises(KnowledgeMapRefused) as caught:
            DiscoveryContext(refingerprint(m), self.result.receipt)
        self.assertIn("came through", str(caught.exception))

    def test_cross_question_scope_violation(self):
        m = copy.deepcopy(self.map)
        questions = {q["id"]: q for q in m["questions"]}
        f, foreign = next((f, c["id"]) for f in m["findings"] for c in m["claims"]
                          if not set(c["question_ids"]) & ({f["question_id"]} |
                                                            set(questions[f["question_id"]]["depends_on"])))
        f["claim_ids"].append(foreign)
        with self.assertRaises(KnowledgeMapRefused) as caught:
            DiscoveryContext(refingerprint(m), self.result.receipt)
        self.assertIn("outside the questions this finding may use", str(caught.exception))

    def test_not_transitive(self):
        # A->B->C: the context adds no access of its own; every finding's claims come through declared edges only.
        questions = {q["id"]: q for q in self.ctx.entities("questions")}
        for f in self.ctx.entities("findings"):
            allowed = {f["question_id"], *questions[f["question_id"]]["depends_on"]}
            if f["derivation"] != "synthesis":
                for via in f["claim_scopes"].values():
                    self.assertTrue(set(via) <= allowed)


class Lineage(unittest.TestCase):
    """7: source lineage is inspectable (the certification syndication fixture)."""

    def test_lineage_is_preserved(self):
        result = cert._run("Is industrial vacancy in the Phoenix metro falling?", cert._docs(cert.SYNDICATED))
        ctx = DiscoveryContext(export_knowledge_map(result), result.receipt)
        self.assertEqual([dict(link) for link in ctx.lineage()],
                         sorted((dict(link.__dict__) for link in result.lineage), key=lambda d: json.dumps(d,
                                                                                                           sort_keys=True)))
        link = ctx.lineage()[0]
        self.assertEqual(link["relation"], "syndicated")
        self.assertEqual(ctx.source(link["source"])["independence_group"],
                         ctx.source(link["derived_from"])["independence_group"])


class UncertaintyReasons(unittest.TestCase):
    """Under /2 the assessment can show that one independent source was the only unmet requirement."""

    def record(self, sources: int, calibrated: float, status: str = "partially_verified") -> dict:
        return {"status": status, "issues": [], "assessment": {
            "factors": {"independent_sources": sources, "calibrated": calibrated},
            "requirements": {"min_independent_sources": 2, "min_confidence": 0.7}}}

    def test_single_source(self):
        self.assertEqual(uncertainty_reason(self.record(1, 0.8)), UncertaintyReason.SINGLE_SOURCE)
        self.assertEqual(uncertainty_reason(self.record(1, 0.66)), UncertaintyReason.POLICY_UNMET)  # two shortfalls
        self.assertEqual(uncertainty_reason(self.record(2, 0.6)), UncertaintyReason.POLICY_UNMET)
        self.assertEqual(uncertainty_reason(self.record(1, 0.8, "contested")), UncertaintyReason.CONTESTED)
        self.assertEqual(uncertainty_reason({"status": "partially_verified", "issues": []}),
                         UncertaintyReason.POLICY_UNMET)  # /1: no assessment, no inference

    def test_closed_unknown_is_not_missing_evidence(self):
        result = phoenix_run()
        if not result.unknowns:
            self.skipTest("fixture run raised no unknowns")
        result.unknowns[0].status = "resolved"
        _finish(result, HeuristicProvider())
        ctx = DiscoveryContext(export_knowledge_map(result), result.receipt)
        u = ctx.resolve(result.unknowns[0].id).value
        framed = frame_problem(ctx, objective(ctx), at=AT)
        self.assertNotIn(u["id"], [m.unknown_id for m in framed.missing_evidence])
        with self.assertRaises(ContextMismatch):
            ctx.register(MissingEvidence(u["id"], u["description"], u["capability"], list(u["source_types"]),
                                         u["expected_gain"], u["est_cost_usd"], u["needs_approval"], created_at=AT))


class EndToEnd(unittest.TestCase):
    def test_v1_run_to_discovery_context(self):
        result = phoenix_run()
        m = export_knowledge_map(result)
        ctx = DiscoveryContext(json.dumps(m), result.receipt)
        claim = ctx.claims("known")[0]
        evidence = ctx.resolve(claim["supporting"][0])
        source = ctx.source_of(evidence.id)
        self.assertEqual(source["id"], result.graph.evidence[evidence.id].source_id)
        self.assertEqual(list(claim["question_ids"]), result.graph.claims[claim["id"]].question_ids)
        finding = next(f for f in ctx.entities("findings") if claim["id"] in f["claim_ids"])
        self.assertIn(finding["derivation"], ("direct", "dependency", "mixed"))
        fact = ctx.register(KnownFact(claim["id"], claim["statement"], thaw(claim["scope"]), claim["value"],
                                      claim["unit"], claim["policy"], claim["confidence"], created_at=AT))
        self.assertIn(fact.id, ctx)
        self.assertEqual(ctx.receipt["state_hash"], result.receipt["state_hash"])
        self.assertEqual(ctx.knowledge_map_fingerprint, m["fingerprint"]["map"])
        self.assertTrue(verify_receipt(result.receipt))
        self.assertEqual(ctx.research_id, result.receipt["research_id"])


if __name__ == "__main__":
    unittest.main()
