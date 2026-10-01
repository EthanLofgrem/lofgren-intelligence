"""Evidence identity v2 and evidence-graph integrity (hardening commit 3). All data is fictional."""

from __future__ import annotations

import copy
import json
import unittest

from lofgren_intelligence.evidence import (
    Claim,
    ClaimOrigin,
    ClaimStatus,
    Evidence,
    EvidenceGraph,
    EvidenceKind,
    Scope,
    Source,
    SourceKind,
)
from lofgren_intelligence.evidence.graph import (
    ClaimConflict,
    Edge,
    EdgeRelation,
    GraphIntegrityError,
    GraphValidationError,
    HypothesisCollision,
    UnknownRelation,
)
from lofgren_intelligence.evidence.types import (
    EVIDENCE_IDENTITY_SCHEMA,
    EVIDENCE_IDENTITY_VERSION,
    EvidenceIdentityError,
    Location,
    evidence_id,
    evidence_identity_key,
    make_id,
)

SRC = "SRC-0000000001"
TEXT = "Fictional sensor log for bay 7."
T0 = "2026-09-01T00:00:00Z"
PHOENIX = Scope("2026-01-01", "2026-12-31", "Phoenix")
S = "Industrial warehouse vacancy rose (fictional)."


def ev(**kw) -> Evidence:
    base = dict(source_id=SRC, kind=EvidenceKind.MEASUREMENT, content=TEXT, data={"reading_c": 21.5},
                observed_at=T0)
    base.update(kw)
    return Evidence(**base)


def graph() -> tuple[EvidenceGraph, Source]:
    g = EvidenceGraph()
    src = g.add_source(Source(SourceKind.DOCUMENT, "fictional source", uri="inline:a", quality=0.8))
    return g, src


class EvidenceIdentityTests(unittest.TestCase):
    def test_key_definition(self):
        e = ev(location=Location(33.45, -112, "Bay 7"), valid_from="2026-09-01", valid_to="2026-09-30")
        key = evidence_identity_key(e)
        self.assertEqual(key, {
            "identity": EVIDENCE_IDENTITY_SCHEMA, "source_id": SRC, "kind": "measurement",
            "content_sha256": e.content_hash, "data": {"reading_c": 21.5}, "observed_at": T0,
            "valid_from": "2026-09-01", "valid_to": "2026-09-30",
            "location": {"lat": 33.45, "lon": -112.0, "name": "Bay 7"}})
        canonical = json.dumps(key, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        self.assertEqual(e.id, make_id("EV", canonical))
        self.assertEqual((EVIDENCE_IDENTITY_VERSION, e.identity_version), (2, 2))

    def test_a_identical_observation_same_id(self):
        self.assertEqual(len({ev().id for _ in range(20)}), 1)

    def test_b_content_after_200_characters_matters(self):
        prefix = "x" * 200
        self.assertNotEqual(ev(content=prefix + " 21 C").id, ev(content=prefix + " 48 C").id)

    def test_c_structured_data_matters(self):
        self.assertNotEqual(ev(data={"reading_c": 21.5}).id, ev(data={"reading_c": 48.0}).id)

    def test_d_key_order_does_not_matter(self):
        a = ev(data={"sensor": "bay7", "reading_c": 21.5, "meta": {"a": 1, "b": 2}})
        b = ev(data={"meta": {"b": 2, "a": 1}, "reading_c": 21.5, "sensor": "bay7"})
        self.assertEqual(a.id, b.id)
        self.assertEqual(ev(data={"reading_c": 21}).id, ev(data={"reading_c": 21.0}).id)  # one number, one spelling

    def test_e_list_order_matters(self):
        self.assertNotEqual(ev(data={"readings": [21, 22]}).id, ev(data={"readings": [22, 21]}).id)

    def test_f_to_j_each_observation_field_matters(self):
        base = ev()
        variants = {
            "source": ev(source_id="SRC-0000000002"),
            "kind": ev(kind=EvidenceKind.OBSERVATION),
            "observed_at": ev(observed_at="2026-09-02T00:00:00Z"),
            "valid_from": ev(valid_from="2026-09-01"),
            "valid_to": ev(valid_from="2026-09-01", valid_to="2026-09-30"),
            "location": ev(location=Location(33.45, -112.07)),
            "location name": ev(location=Location(name="Bay 7")),
        }
        for name, other in variants.items():
            with self.subTest(field=name):
                self.assertNotEqual(other.id, base.id)

    def test_transformations_are_provenance_not_identity(self):
        self.assertEqual(ev().id, ev(transformations=["unit normalized"]).id)

    def test_k_l_malformed_structured_data_fails_closed(self):
        for data in ({"reading": float("nan")}, {"nested": {"readings": [1.0, float("inf")]}}, {"x": object()},
                     {"x": {1, 2}}, {1: "non-text key"}, {"big": 2 ** 60}):
            with self.subTest(data=repr(data)), self.assertRaises(EvidenceIdentityError):
                ev(data=data)

    def test_malformed_location_and_time_fail_closed(self):
        for kw in (dict(location=Location(95.0, 0.0)), dict(location=Location(0.0, 200.0)),
                   dict(location=Location(float("inf"), 0.0)), dict(location=Location(True, 0.0)),
                   dict(location=Location(1.0, 1.0, 7)), dict(observed_at="yesterday"),
                   dict(valid_from="2026-02-30"), dict(valid_to=20260901), dict(data=[1, 2])):
            with self.subTest(kw=repr(kw)), self.assertRaises(EvidenceIdentityError):
                ev(**kw)

    def test_unsupported_versions_fail_closed(self):
        for bad in (0, 3, "2", True, None):
            with self.subTest(version=bad), self.assertRaises(EvidenceIdentityError):
                ev(identity_version=bad)

    def test_v2_refuses_a_content_hash_that_does_not_match(self):
        with self.assertRaises(EvidenceIdentityError):
            ev(content_hash="0" * 64)

    def test_m_version_one_reproduces_the_historical_rule(self):
        legacy = ev(identity_version=1, content="y" * 200 + " tail A")
        self.assertEqual(legacy.id, make_id("EV", SRC, ("y" * 200 + " tail A")[:200], T0))
        self.assertEqual(legacy.id, ev(identity_version=1, content="y" * 200 + " tail B").id)  # unchanged, flawed

    def test_n_o_legacy_saved_evidence_loads_as_version_one_with_its_id(self):
        g, src = graph()
        stored = g.add_evidence(ev(source_id=src.id, identity_version=1))
        data = g.to_json()
        data["evidence"][0].pop("identity_version")
        loaded = EvidenceGraph.from_json(data)
        self.assertEqual((loaded.evidence[stored.id].identity_version, loaded.evidence[stored.id].id), (1, stored.id))
        loaded.validate()

    def test_p_no_clock_or_randomness_in_identity(self):
        a = evidence_identity_key(ev())
        b = evidence_identity_key(copy.deepcopy(ev()))
        self.assertEqual(a, b)
        self.assertFalse(any("retrieved" in k or "created" in k for k in a))

    def test_version_two_round_trips_through_json(self):
        g, src = graph()
        e = g.add_evidence(ev(source_id=src.id, location=Location(33.4, -112.0, "Bay 7")))
        loaded = EvidenceGraph.from_json(json.loads(json.dumps(g.to_json())))
        self.assertEqual((loaded.evidence[e.id].identity_version, evidence_id(loaded.evidence[e.id])), (2, e.id))


class MergeTests(unittest.TestCase):
    def setUp(self):
        self.g, src = graph()
        self.e1 = self.g.add_evidence(Evidence(src.id, EvidenceKind.DOCUMENT, "Vacancy rose (fictional source 1)."))
        self.e2 = self.g.add_evidence(Evidence(src.id, EvidenceKind.DOCUMENT, "Vacancy rose (fictional source 2)."))

    def test_corroborating_evidence_both_survive(self):
        a = self.g.add_claim(Claim(S, scope=PHOENIX), supported_by=[self.e1.id])
        b = self.g.add_claim(Claim(S, scope=PHOENIX), supported_by=[self.e2.id])
        self.assertIs(a, b)
        self.assertEqual(a.supporting, [self.e1.id, self.e2.id])
        self.g.validate()

    def test_repeated_association_is_idempotent(self):
        c = self.g.add_claim(Claim(S, scope=PHOENIX), supported_by=[self.e1.id])
        self.g.add_claim(Claim(S, scope=PHOENIX), supported_by=[self.e1.id])
        self.g.link(self.e1.id, c.id, "supports")
        self.assertEqual(c.supporting, [self.e1.id])
        self.assertEqual(sum(1 for e in self.g.edges if e.relation == "supports"), 1)

    def test_arrival_order_does_not_change_the_result(self):
        def build(first, second):
            g = copy.deepcopy(self.g)
            g.add_claim(Claim(S, scope=PHOENIX), supported_by=[first])
            claim = g.add_claim(Claim(S, scope=PHOENIX), supported_by=[second])
            return claim
        ab, ba = build(self.e1.id, self.e2.id), build(self.e2.id, self.e1.id)
        self.assertEqual(ab.id, ba.id)
        self.assertEqual(set(ab.supporting), set(ba.supporting))
        self.assertEqual((ab.status, ab.origin, ab.confidence), (ba.status, ba.origin, ba.confidence))

    def test_incoming_evidence_lists_are_merged(self):
        self.g.add_claim(Claim(S, scope=PHOENIX), supported_by=[self.e1.id])
        merged = self.g.add_claim(Claim(S, scope=PHOENIX, supporting=[self.e2.id]))
        self.assertEqual(merged.supporting, [self.e1.id, self.e2.id])

    def test_question_difference_still_merges(self):
        # Question association is not identity and is not a conflict; it is the pinned commit-4 defect.
        a = self.g.add_claim(Claim(S, scope=PHOENIX, question_id="Q-a"))
        b = self.g.add_claim(Claim(S, scope=PHOENIX, question_id="Q-b"))
        self.assertIs(a, b)

    def test_incompatible_protected_state_is_refused(self):
        self.g.add_claim(Claim(S, scope=PHOENIX), supported_by=[self.e1.id])
        before = json.dumps(self.g.to_json(), sort_keys=True)
        for field, kw in (("origin", dict(origin=ClaimOrigin.USER)), ("status", dict(status=ClaimStatus.VERIFIED)),
                          ("confidence", dict(confidence=0.9)), ("claim_type", dict(claim_type="trend")),
                          ("sufficiency", dict(sufficiency="general-2"))):
            with self.subTest(field=field), self.assertRaises(ClaimConflict) as ctx:
                self.g.add_claim(Claim(S, scope=PHOENIX, **kw), supported_by=[self.e2.id])
            self.assertEqual(ctx.exception.field, field)
        self.assertEqual(json.dumps(self.g.to_json(), sort_keys=True), before)  # nothing merged on refusal

    def test_conflicting_calculations_are_refused(self):
        self.g.add_claim(Claim(S, scope=PHOENIX, calculation_id="CALC-a"))
        with self.assertRaises(ClaimConflict):
            self.g.add_claim(Claim(S, scope=PHOENIX, calculation_id="CALC-b"))

    def test_unknown_evidence_is_refused_before_any_change(self):
        with self.assertRaises(KeyError):
            self.g.add_claim(Claim(S, scope=PHOENIX), supported_by=["EV-missing0000"])
        self.assertEqual(self.g.claims, {})

    def test_evidence_cannot_both_support_and_contradict(self):
        c = self.g.add_claim(Claim(S, scope=PHOENIX), supported_by=[self.e1.id])
        with self.assertRaises(GraphIntegrityError):
            self.g.link(self.e1.id, c.id, "contradicts")
        self.assertEqual(c.contradicting, [])


class HypothesisBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.g, src = graph()
        self.e = self.g.add_evidence(Evidence(src.id, EvidenceKind.DOCUMENT, "Vacancy rose (fictional)."))

    def test_hypothesis_cannot_receive_a_verified_claim(self):
        verified = self.g.add_claim(Claim(S, scope=PHOENIX, status=ClaimStatus.VERIFIED, confidence=0.9),
                                    supported_by=[self.e.id])
        with self.assertRaises(HypothesisCollision) as ctx:
            self.g.add_claim(Claim(S, scope=PHOENIX, origin=ClaimOrigin.HYPOTHESIS))
        self.assertEqual((ctx.exception.claim_id, ctx.exception.existing_origin, ctx.exception.existing_status),
                         (verified.id, ClaimOrigin.EXTRACTED, ClaimStatus.VERIFIED))
        self.assertEqual((verified.origin, verified.status), (ClaimOrigin.EXTRACTED, ClaimStatus.VERIFIED))

    def test_evidence_claim_cannot_join_a_stored_hypothesis(self):
        hyp = self.g.add_claim(Claim(S, scope=PHOENIX, origin=ClaimOrigin.HYPOTHESIS))
        with self.assertRaises(HypothesisCollision):
            self.g.add_claim(Claim(S, scope=PHOENIX), supported_by=[self.e.id])
        self.assertEqual((hyp.origin, hyp.supporting), (ClaimOrigin.HYPOTHESIS, []))

    def test_matching_hypotheses_share_one_hypothesis_claim(self):
        a = self.g.add_claim(Claim(S, scope=PHOENIX, origin=ClaimOrigin.HYPOTHESIS))
        b = self.g.add_claim(Claim(S, scope=PHOENIX, origin=ClaimOrigin.HYPOTHESIS))
        self.assertIs(a, b)

    def test_unrelated_hypothesis_is_unaffected(self):
        self.g.add_claim(Claim(S, scope=PHOENIX, status=ClaimStatus.VERIFIED, confidence=0.9))
        hyp = self.g.add_claim(Claim(S, scope=Scope("2027-01-01", "2027-12-31", "Phoenix"),
                                     origin=ClaimOrigin.HYPOTHESIS))
        self.assertEqual((hyp.origin, hyp.status), (ClaimOrigin.HYPOTHESIS, ClaimStatus.UNVERIFIED))
        self.g.validate()


class RelationTests(unittest.TestCase):
    def test_link_accepts_supports_and_contradicts_only(self):
        g, src = graph()
        e = g.add_evidence(Evidence(src.id, EvidenceKind.DOCUMENT, "Vacancy rose (fictional)."))
        f = g.add_evidence(Evidence(src.id, EvidenceKind.DOCUMENT, "Vacancy fell (fictional)."))
        c = g.add_claim(Claim(S, scope=PHOENIX))
        g.link(e.id, c.id, EdgeRelation.SUPPORTS)
        g.link(f.id, c.id, "contradicts")
        self.assertEqual((c.supporting, c.contradicting), ([e.id], [f.id]))
        for bad in ("mentions", "provided_by", "conflicts", "derived_from", "derived_from:syndicated", "Supports"):
            with self.subTest(relation=bad), self.assertRaises(UnknownRelation):
                g.link(e.id, c.id, bad)


class ValidationTests(unittest.TestCase):
    def setUp(self):
        from lofgren_intelligence.adapters import AdapterRegistry, DocumentAdapter
        from lofgren_intelligence.intent.compiler import compile_intent
        from lofgren_intelligence.kernel import run_investigation

        from .helpers import TEXTS

        reg = AdapterRegistry()
        reg.register(DocumentAdapter(texts=TEXTS))
        self.v1_run = run_investigation(compile_intent("Is industrial construction in the Phoenix metro increasing?"),
                                        reg)
        self.g = self.v1_run.graph

    def assertProblem(self, fragment: str):
        problems = self.g.problems()
        self.assertTrue(any(fragment in p for p in problems), problems)
        with self.assertRaises(GraphValidationError):
            self.g.validate()

    def test_real_run_graph_is_valid(self):
        self.assertEqual(self.g.problems(), [])

    def test_validation_has_no_side_effects(self):
        before = json.dumps(self.g.to_json(), sort_keys=True)
        next(iter(self.g.claims.values())).supporting.append("EV-missing0000")
        after_corruption = json.dumps(self.g.to_json(), sort_keys=True)
        self.g.problems()
        with self.assertRaises(GraphValidationError):
            self.g.validate()
        self.assertEqual(json.dumps(self.g.to_json(), sort_keys=True), after_corruption)
        self.assertNotEqual(before, after_corruption)

    def test_dangling_evidence(self):
        next(iter(self.g.claims.values())).supporting.append("EV-missing0000")
        self.assertProblem("cites missing supporting evidence EV-missing0000")

    def test_dangling_contradiction_endpoint(self):
        cx = next(iter(self.g.contradictions.values()))
        cx.claim_b = "CL-missing0000"
        self.assertProblem("cites missing claim CL-missing0000")

    def test_tampered_dictionary_key(self):
        key, claim = next(iter(self.g.claims.items()))
        self.g.claims["CL-wrongkey000"] = self.g.claims.pop(key)
        self.assertProblem("stored under key 'CL-wrongkey000'")

    def test_tampered_claim(self):
        claim = next(iter(self.g.claims.values()))
        claim.statement += " (edited)"
        self.assertProblem("does not match its proposition")

    def test_tampered_evidence(self):
        e = next(iter(self.g.evidence.values()))
        e.content += " (edited)"
        self.assertProblem("does not match its content")

    def test_missing_source(self):
        self.g.sources.clear()
        self.assertProblem("cites missing source")

    def test_unknown_edge_relation(self):
        self.g.edges.append(Edge("EV-a", "CL-b", "mentions"))
        self.assertProblem("unknown relation 'mentions'")

    def test_promoted_hypothesis(self):
        hyp = Claim("A fictional hypothesis.", origin=ClaimOrigin.HYPOTHESIS, status=ClaimStatus.VERIFIED)
        self.g.claims[hyp.id] = hyp
        self.assertProblem("hypotheses are never verified")

    def test_evidence_on_both_sides(self):
        claim = next(c for c in self.g.claims.values() if c.supporting)
        claim.contradicting.append(claim.supporting[0])
        self.assertProblem("both supports and contradicts")

    def test_issued_receipt_does_not_change_with_the_graph(self):
        # Was: build_receipt stored the claims' own supporting/contradicting/issues lists, so evidence linked after
        # the receipt was issued silently rewrote it and verify_receipt then refused it.
        from lofgren_intelligence.kernel.receipt import verify_receipt

        issued = json.dumps(self.v1_run.receipt, sort_keys=True, default=str)
        claim = next(c for c in self.g.claims.values() if c.supporting)
        src = self.g.add_source(Source(SourceKind.DOCUMENT, "later note", uri="inline:later", quality=0.5))
        ev = self.g.add_evidence(Evidence(src.id, EvidenceKind.DOCUMENT, "A later, fictional note."))
        self.g.link(ev.id, claim.id, "supports")
        claim.issues.append("raised later")
        self.assertEqual(json.dumps(self.v1_run.receipt, sort_keys=True, default=str), issued)
        self.assertTrue(verify_receipt(self.v1_run.receipt))

    def test_no_receipt_from_an_invalid_graph(self):
        from lofgren_intelligence.kernel.pipeline import _finish
        from lofgren_intelligence.models import HeuristicProvider

        old_receipt = dict(self.v1_run.receipt)
        next(iter(self.g.claims.values())).supporting.append("EV-missing0000")
        with self.assertRaises(GraphValidationError):
            _finish(self.v1_run, HeuristicProvider())
        self.assertEqual(self.v1_run.receipt, old_receipt)


if __name__ == "__main__":
    unittest.main()
