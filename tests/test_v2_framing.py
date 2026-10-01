"""V2 steps 2-3: problem framing, prior art, gaps, constraints, assumptions, evidence requirements, schemas.

Fixtures are fictional (tests/fixtures/discovery).
"""

import json
import unittest
import warnings
from pathlib import Path

from lofgren_intelligence.discovery import (
    ALL_TYPES,
    Candidate,
    DiscoveryOutcome,
    FalseNovelty,
    GapBasis,
    GapType,
    InvalidScope,
    MalformedInput,
    PriorArtAssessment,
    PriorArtConclusion,
    PromotionRefused,
    UncertaintyReason,
    UnitMismatch,
    UnknownReference,
    UnknownStatus,
    from_dict,
)
from lofgren_intelligence.discovery.context import DiscoveryContext
from lofgren_intelligence.discovery.expr import Const, Mul, Relation, Var
from lofgren_intelligence.discovery.frame import frame_problem
from lofgren_intelligence.discovery.gaps import detect_gaps
from lofgren_intelligence.discovery.principles import ground_constraint, state_assumption
from lofgren_intelligence.discovery.prior_art import (
    FixturePriorArtProvider,
    PriorArtProvider,
    ProviderCoverage,
    assess_prior_art,
)
from lofgren_intelligence.discovery.requirements import evidence_requirement, requirement_for_gap
from lofgren_intelligence.discovery.schemas import render, validate
from lofgren_intelligence.discovery.types import NOVELTY_WORDS, SEARCH_ABSENCE_MAX_CONFIDENCE

from .test_v2_context_integrity import AT, FIXTURES, load, objective
from .test_v2_foundations import sample_objects

SCHEMA_DIR = Path(__file__).resolve().parent.parent / "schemas" / "discovery"


def corpus_provider() -> FixturePriorArtProvider:
    corpus = json.loads((FIXTURES / "prior_art_corpus.json").read_text(encoding="utf-8"))
    c = corpus["coverage"]
    coverage = ProviderCoverage(tuple(c["sources"]), tuple(c["domains"]), tuple(c["time_range"]),
                                tuple(c["limitations"]))
    return FixturePriorArtProvider(corpus["records"], coverage)


COVERED = ("2015-01-01", "2026-09-30")


def build(name: str = "knowledge_map_phoenix.json"):
    ctx = DiscoveryContext(load(name))
    return ctx, frame_problem(ctx, objective(ctx), ["occupancy >= 0.9"], at=AT)


class FrameTests(unittest.TestCase):
    def test_frame_copies_v1_unchanged(self):
        ctx, framed = build()
        self.assertIsNone(framed.outcome)
        self.assertEqual(len(framed.known_facts), len(ctx.claims("known")))
        self.assertEqual(len(framed.uncertainties), len(ctx.claims("uncertain")) + len(ctx.claims("contradicted")))
        self.assertEqual(len(framed.missing_evidence), len(ctx.entities("unknowns")))
        for fact in framed.known_facts:
            rec = next(c for c in ctx.claims("known") if c["id"] == fact.claim_id)
            self.assertEqual(fact.statement, rec["statement"])
            self.assertEqual(fact.confidence_kind.value, "evidence")
            self.assertEqual(ctx.scope_of(rec), fact.scope)  # scope flows through unchanged
        f = framed.frame
        self.assertEqual(f.contradiction_ids, sorted(cx["id"] for cx in ctx.entities("contradictions")))
        self.assertEqual((f.scope.valid_from, f.scope.valid_to, f.scope.geography),
                         ("2026-01-01", "2026-12-31", "Phoenix"))
        self.assertIn("unchanged", f.scope_note)
        self.assertEqual(f.success_metrics, ["occupancy >= 0.9"])

    def test_uncertainty_reasons(self):
        ctx, framed = build()
        by_claim = {u.claim_id: u for u in framed.uncertainties}
        for c in ctx.claims("contradicted"):
            self.assertEqual(by_claim[c["id"]].reason, UncertaintyReason.CONTESTED)
        for c in ctx.claims("uncertain"):
            self.assertIn(by_claim[c["id"]].reason, (UncertaintyReason.POLICY_UNMET, UncertaintyReason.STALE,
                                                     UncertaintyReason.INSUFFICIENT))

    def test_scope_widening_is_recorded(self):
        m = load()
        m["findings"][0]["scope"] = {**m["findings"][0]["scope"], "geography": "Tucson"}
        ctx = DiscoveryContext(m)
        framed = frame_problem(ctx, objective(ctx), at=AT)
        self.assertIn("Widened", framed.frame.scope_note)
        self.assertIn("Tucson", framed.frame.scope_note)

    def test_empty_evidence_state_is_insufficient(self):
        ctx, framed = build("knowledge_map_empty.json")
        self.assertEqual(framed.outcome, DiscoveryOutcome.INSUFFICIENT_EVIDENCE)
        self.assertIsNone(framed.frame)
        self.assertEqual(len(framed.missing_evidence), len(ctx.entities("unknowns")))
        self.assertTrue(framed.missing_evidence)
        gaps = detect_gaps(ctx, framed, at=AT).gaps
        self.assertEqual({g.basis for g in gaps}, {GapBasis.UNKNOWN})
        self.assertEqual(len(gaps), len(framed.missing_evidence))

    def test_framing_is_deterministic_and_idempotent(self):
        ctx1, a = build()
        ctx2, b = build()
        self.assertEqual(a.frame.id, b.frame.id)
        self.assertEqual([o.to_dict() for o in ctx1.objects()], [o.to_dict() for o in ctx2.objects()])
        again = frame_problem(ctx1, objective(ctx1), ["occupancy >= 0.9"], at=AT)
        self.assertEqual(again.frame.id, a.frame.id)
        self.assertEqual(len(ctx1.objects()), len(ctx2.objects()))


class PriorArtTests(unittest.TestCase):
    def setUp(self):
        self.ctx, self.framed = build()
        self.provider = corpus_provider()

    def test_match_found(self):
        a, meta = assess_prior_art(self.ctx, "desert cold storage hub", ["cold storage warehouse desert"],
                                   self.provider, ["US"], COVERED, at=AT)
        self.assertEqual(a.conclusion, PriorArtConclusion.MATCH_FOUND)
        self.assertEqual(a.matches[0]["title"], "Cold chain hub for desert logistics (fictional)")
        self.assertTrue(a.statement.startswith("Matching prior art found:"))
        self.assertEqual((meta.name, meta.queries_failed), ("fixture", 0))
        self.assertFalse({"provider", "provider_name", "provider_version"} & set(a.to_dict()))  # receipt-only data

    def test_no_match_is_not_novelty(self):
        a, _ = assess_prior_art(self.ctx, "drone-delivered pallet racking", ["drone pallet racking"], self.provider,
                                ["US"], COVERED, at=AT)
        self.assertEqual(a.conclusion, PriorArtConclusion.NO_MATCH_WITHIN_COVERAGE)
        self.assertEqual(a.statement, "No matching prior art was found within the searched sources "
                                      "(fixture-patents (fictional); domains: US; period: 2015-01-01 to 2026-09-30) "
                                      "for drone-delivered pallet racking; this does not establish novelty.")
        self.assertTrue(a.limitations)
        gaps = detect_gaps(self.ctx, self.framed, [a], at=AT).gaps
        absence = [g for g in gaps if g.basis == GapBasis.SEARCH_ABSENCE]
        self.assertEqual(len(absence), 1)
        self.assertLessEqual(absence[0].confidence, SEARCH_ABSENCE_MAX_CONFIDENCE)
        self.assertEqual(absence[0].coverage_statement, a.statement)
        rendered = [a.statement, *a.limitations, *(g.missing for g in absence), *(g.why_it_matters for g in absence),
                    *(w for g in absence for w in g.ways_to_close)]
        for s in rendered:
            self.assertIsNone(NOVELTY_WORDS.search(s), s)

    def test_incomplete_coverage_concludes_nothing(self):
        a, _ = assess_prior_art(self.ctx, "drone-delivered pallet racking", ["drone pallet racking"], self.provider,
                                ["US", "EU"], COVERED, at=AT)
        self.assertEqual(a.conclusion, PriorArtConclusion.INCOMPLETE)
        self.assertIn("domain EU: not covered by fixture-patents (fictional)", a.unsearched_areas)
        b, _ = assess_prior_art(self.ctx, "drone-delivered pallet racking", ["drone pallet racking"], self.provider,
                                ["US"], ("2001-01-01", None), at=AT)
        self.assertEqual(b.conclusion, PriorArtConclusion.INCOMPLETE)
        self.assertEqual(len(b.unsearched_areas), 2)
        result = detect_gaps(self.ctx, self.framed, [a, b], at=AT)
        self.assertFalse([g for g in result.gaps if g.basis == GapBasis.SEARCH_ABSENCE])
        self.assertEqual(len(result.notes), 2)
        self.assertIn("incomplete", a.statement)

    def test_provider_failure_is_recorded(self):
        class Failing(PriorArtProvider):
            def coverage(self):
                return corpus_provider().coverage()

            def search(self, query, domains, time_range):
                raise TimeoutError("upstream timed out")

        a, meta = assess_prior_art(self.ctx, "pallet racking", ["pallet racking"], Failing(), ["US"], COVERED, at=AT)
        self.assertEqual(a.conclusion, PriorArtConclusion.INCOMPLETE)
        self.assertEqual(meta.queries_failed, 1)
        self.assertIn("search failed (TimeoutError)", a.unsearched_areas[0])

    def test_malformed_results_fail_closed(self):
        bad_hits = [[{"source": "x"}], [{"title": "t", "source": "s", "rank": 1}],
                    [{"title": "t", "source": "s", "published": "2026-02-30"}], [{"title": 5, "source": "s"}], "hits",
                    [None]]
        for hits in bad_hits:
            class Bad(PriorArtProvider):
                def coverage(self):
                    return corpus_provider().coverage()

                def search(self, query, domains, time_range, hits=hits):
                    return hits

            with self.subTest(hits=hits), self.assertRaises((MalformedInput, InvalidScope)):
                assess_prior_art(self.ctx, "pallet racking", ["pallet racking"], Bad(), ["US"], COVERED, at=AT)
        with self.assertRaises(MalformedInput):
            ProviderCoverage(("s",), (), (None, None), ())  # a provider must state its limitations
        with self.assertRaises(MalformedInput):
            FixturePriorArtProvider([{"title": "t"}], corpus_provider().coverage())

    def test_false_novelty_language_is_refused(self):
        for subject in ("a new cross-dock design", "the first solar canopy", "an unprecedented layout",
                        "a NOVEL hub", "something never attempted before"):
            with self.subTest(subject=subject), self.assertRaises(FalseNovelty):
                assess_prior_art(self.ctx, subject, ["drone pallet racking"], self.provider, ["US"], COVERED, at=AT)
        with self.assertRaises(FalseNovelty):
            assess_prior_art(self.ctx, "pallet racking", ["drone pallet racking"], self.provider, ["US"], COVERED,
                             distinctive_features=["first of its kind"], at=AT)
        self.assertTrue(issubclass(FalseNovelty, MalformedInput))

    def test_assessment_contract(self):
        base = dict(subject="pallet racking", queries=["q"], search_ids=["PA-1"], sources_searched=["s"])
        with self.assertRaises(MalformedInput):
            PriorArtAssessment(conclusion="no_match_within_coverage", **base)  # no limitations
        with self.assertRaises(MalformedInput):
            PriorArtAssessment(conclusion="match_found", limitations=["x"], **base)  # no matches
        with self.assertRaises(MalformedInput):
            PriorArtAssessment(conclusion="incomplete", limitations=["x"], **base)  # nothing named as unsearched
        with self.assertRaises(MalformedInput):
            PriorArtAssessment(conclusion="no_match_within_coverage", limitations=["x"],
                               matches=[{"title": "t", "source": "s"}], **base)
        with self.assertRaises(UnknownStatus):
            PriorArtAssessment(conclusion="novel", limitations=["x"], **base)

    def test_assessment_must_match_its_searches(self):
        a, _ = assess_prior_art(self.ctx, "pallet racking", ["drone pallet racking"], self.provider, ["US"], COVERED,
                                at=AT)
        forged = PriorArtAssessment("pallet racking", "no_match_within_coverage", ["something else"], a.search_ids,
                                    a.sources_searched, limitations=a.limitations, created_at=AT)
        with self.assertRaises(MalformedInput):
            self.ctx.register(forged)

    def test_candidate_novelty_is_deprecated(self):
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            Candidate("x", "GAP-1", novelty=0.5)
        self.assertTrue(any(issubclass(w.category, DeprecationWarning) for w in caught))
        a, _ = assess_prior_art(self.ctx, "pallet racking", ["drone pallet racking"], self.provider, ["US"], COVERED,
                                at=AT)
        with warnings.catch_warnings():
            warnings.simplefilter("error")
            c = Candidate("Pallet racking retrofit", "GAP-1", prior_art_assessment_id=a.id)
        self.assertIsNone(c.novelty)
        with self.assertRaises(MalformedInput):
            Candidate("x", "GAP-1", prior_art_assessment_id="PA-1")


class GapTests(unittest.TestCase):
    def test_gaps_from_unknowns_and_contradictions(self):
        ctx, framed = build()
        result = detect_gaps(ctx, framed, at=AT)
        unknown_gaps = [g for g in result.gaps if g.basis == GapBasis.UNKNOWN]
        self.assertEqual(len(unknown_gaps), len(ctx.entities("unknowns")))
        caps = {u["id"]: u["capability"] for u in ctx.entities("unknowns")}
        for g in unknown_gaps:
            expected = GapType.DATA if caps[g.evidence_ids[0]] in ("sensor", "orbital_passes", "imagery_catalog") \
                else GapType.EVIDENCE
            self.assertEqual(g.type, expected)
            self.assertEqual(g.confidence, 1.0)
        incompatible = [cx for cx in ctx.entities("contradictions") if cx["kind"] == "incompatible"]
        knowledge = [g for g in result.gaps if g.basis == GapBasis.CONTRADICTION]
        self.assertEqual(len(knowledge), len(incompatible))
        for g in result.gaps:
            for ref in g.evidence_ids:
                ctx.reference(ref)  # every cited id is real in this context

    def test_scope_mismatch_is_a_lead_not_a_gap(self):
        m = load()
        m["contradictions"][0]["kind"] = "scope_mismatch"
        ctx = DiscoveryContext(m)
        framed = frame_problem(ctx, objective(ctx), at=AT)
        result = detect_gaps(ctx, framed, at=AT)
        self.assertEqual(len([g for g in result.gaps if g.basis == GapBasis.CONTRADICTION]),
                         len(m["contradictions"]) - 1)
        self.assertTrue(any("lead" in n for n in result.notes))


class ConstraintTests(unittest.TestCase):
    def setUp(self):
        self.ctx, self.framed = build()
        self.asm = state_assumption(self.ctx, "Land costs 40 USD per square foot (fictional)",
                                    "No land-price claim in the knowledge map", "land_cost", 40.0, "usd/sqft", at=AT)
        self.rel = Relation(Var("land_cost"), "<=", Const(50, "usd/sqft"))

    def test_grounded_constraints(self):
        c = ground_constraint(self.ctx, "land_budget", "economic", self.rel, {"land_cost": "usd/sqft"},
                              assumption=self.asm.id, at=AT)
        self.assertIn(c.id, self.ctx)
        known = self.ctx.claims("known")[0]["id"]
        fact = ground_constraint(self.ctx, "built_area", "physical",
                                 Relation(Var("area"), ">=", Const(1, "sqft")), {"area": "sqft"}, fact=known, at=AT)
        self.assertEqual(fact.source_fact_id, known)

    def test_unsupported_constraints(self):
        uncertain = self.ctx.claims("contradicted")[0]["id"]
        units = {"land_cost": "usd/sqft"}
        with self.assertRaises(PromotionRefused):
            ground_constraint(self.ctx, "a", "economic", self.rel, units, fact=uncertain, at=AT)
        with self.assertRaises(UnknownReference):
            ground_constraint(self.ctx, "b", "economic", self.rel, units, fact="CL-0000000000", at=AT)
        with self.assertRaises(UnknownReference):
            ground_constraint(self.ctx, "c", "economic", self.rel, units, assumption="ASM-0000000000", at=AT)
        with self.assertRaises(MalformedInput):
            ground_constraint(self.ctx, "d", "economic", self.rel, units, at=AT)
        with self.assertRaises(MalformedInput):
            ground_constraint(self.ctx, "e", "economic", self.rel, units, fact=self.ctx.claims("known")[0]["id"],
                              assumption=self.asm.id, at=AT)

    def test_units_are_checked_at_construction(self):
        with self.assertRaises(UnitMismatch):
            ground_constraint(self.ctx, "f", "economic", self.rel, {"land_cost": "usd"}, assumption=self.asm.id,
                              at=AT)
        with self.assertRaises(MalformedInput):
            ground_constraint(self.ctx, "g", "economic", self.rel, {}, assumption=self.asm.id, at=AT)
        with self.assertRaises(MalformedInput):
            ground_constraint(self.ctx, "h", "economic", self.rel, {"land_cost": "usd/sqft", "spare": "usd"},
                              assumption=self.asm.id, at=AT)
        other = state_assumption(self.ctx, "Land costs 400 USD per acre (fictional)", "illustration", "land_cost",
                                 400.0, "usd/acre", at=AT)
        with self.assertRaises(UnitMismatch):  # the assumption sets the variable in another unit
            ground_constraint(self.ctx, "i", "economic", Relation(Var("land_cost"), "<=", Const(50, "usd/sqft")),
                              {"land_cost": "usd/sqft"}, assumption=other.id, at=AT)
        with self.assertRaises(UnitMismatch):
            ground_constraint(self.ctx, "j", "economic",
                              Relation(Mul(Var("units"), Var("unit_cost")), "<=", Const(10, "usd")),
                              {"units": "unit", "unit_cost": "usd/h"}, assumption=self.asm.id, at=AT)


class RequirementTests(unittest.TestCase):
    def test_requirements_follow_the_frame(self):
        ctx, framed = build()
        gaps = detect_gaps(ctx, framed, at=AT).gaps
        for g in gaps:
            r = requirement_for_gap(ctx, g, framed, at=AT)
            self.assertEqual((r.place, r.period), ("Phoenix", ("2026-01-01", "2026-12-31")))
            self.assertEqual(r.derived_from, [g.id])
        orbital = next(g for g in gaps if g.basis == GapBasis.UNKNOWN and "orbital_passes" in g.missing)
        self.assertEqual(requirement_for_gap(ctx, orbital, framed, at=AT).capability, "orbital_passes")

    def test_thresholds_are_machine_evaluable_with_units(self):
        ctx, _ = build()
        rel = Relation(Var("permits"), ">=", Const(100, "count")).to_json()
        r = evidence_requirement(ctx, "Permit counts for 2026 (fictional)", "text", "Phoenix",
                                 ("2026-01-01", "2026-12-31"), rel, {"permits": "count"}, at=AT)
        self.assertEqual(r.threshold, rel)
        with self.assertRaises(MalformedInput):
            evidence_requirement(ctx, "Permits", threshold=rel, at=AT)
        with self.assertRaises(UnitMismatch):
            evidence_requirement(ctx, "Permits", threshold=rel, variable_units={"permits": "usd"}, at=AT)
        with self.assertRaises(MalformedInput):
            evidence_requirement(ctx, "Permits", threshold={"lhs": "permits > 100"}, at=AT)


class SchemaTests(unittest.TestCase):
    def test_committed_schemas_match_the_types(self):
        committed = {p.name for p in SCHEMA_DIR.glob("*.schema.json")}
        self.assertEqual(committed, {f"{n}.schema.json" for n in ALL_TYPES})
        for name in ALL_TYPES:
            with self.subTest(type=name):
                self.assertEqual((SCHEMA_DIR / f"{name}.schema.json").read_text(encoding="utf-8"), render(name))

    def test_objects_validate_against_their_schemas(self):
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            samples = sample_objects()
        ctx, framed = build()
        a, _ = assess_prior_art(ctx, "drone-delivered pallet racking", ["drone pallet racking"], corpus_provider(),
                                ["US"], COVERED, at=AT)
        detect_gaps(ctx, framed, [a], at=AT)
        objs = list(samples.items()) + [(next(k for k, c in ALL_TYPES.items() if type(o) is c), o)
                                        for o in ctx.objects()]
        for name, obj in objs:
            with self.subTest(type=name, id=obj.id):
                data = json.loads(json.dumps(obj.to_dict()))
                self.assertEqual(validate(name, data), [])
                self.assertEqual(from_dict(name, data).id, obj.id)

    def test_schemas_reject_invalid_instances(self):
        good = json.loads(json.dumps(sample_objects()["gap"].to_dict()))
        self.assertTrue(validate("gap", {**good, "extra": 1}))
        self.assertTrue(validate("gap", {**good, "basis": "hunch"}))
        self.assertTrue(validate("gap", {k: v for k, v in good.items() if k != "missing"}))
        self.assertTrue(validate("gap", {**good, "confidence": "high"}))
        self.assertTrue(validate("gap", {**good, "version": "lofgren.discovery/0"}))
        con = json.loads(json.dumps(sample_objects()["constraint"].to_dict()))
        con["relation"]["lhs"] = {"op": "call", "args": []}
        self.assertTrue(validate("constraint", con))


if __name__ == "__main__":
    unittest.main()
