import json
import tempfile
import unittest
from pathlib import Path

from lofgren_intelligence.adapters import canonical_url, classify_source, normalize_scene, normalize_unit
from lofgren_intelligence.certification import run_certification
from lofgren_intelligence.discovery import Candidate, Hypothesis, PromotionRefused, promote
from lofgren_intelligence.evidence import (
    Claim,
    ClaimOrigin,
    ClaimType,
    Evidence,
    EvidenceGraph,
    EvidenceKind,
    Scope,
    Source,
    SourceKind,
)
from lofgren_intelligence.evidence.lineage import resolve_lineage
from lofgren_intelligence.evidence.scope import infer_scope, period_from_text, place_from_text
from lofgren_intelligence.intent import Question, compile_intent
from lofgren_intelligence.kernel import CostLedger, export_state, run_investigation, verify_receipt
from lofgren_intelligence.adapters import AdapterRegistry, DocumentAdapter
from lofgren_intelligence.models import HeuristicProvider, ReasoningProvider, parse_claims_json
from lofgren_intelligence.research import DependencyCycle, question_depths
from lofgren_intelligence.schemas import schema_for, validate_shape
from lofgren_intelligence.verification import Verifier, brier_score, reliability_table
from lofgren_intelligence.verification.calibration import PredictionLog
from lofgren_intelligence.verification.policies import classify_claim, policy_for
from lofgren_intelligence.verification.skeptic import review

from .helpers import TEXTS

OBJECTIVE = "Is industrial construction in the Phoenix metro increasing?"


def registry(texts=TEXTS):
    reg = AdapterRegistry()
    reg.register(DocumentAdapter(texts=texts))
    return reg


class ScopeTests(unittest.TestCase):
    def test_periods(self):
        self.assertEqual(period_from_text("Vacancy rose in 2026."), ("2026-01-01", "2026-12-31"))
        self.assertEqual(period_from_text("From 2020 to 2024 prices rose."), ("2020-01-01", "2024-12-31"))
        self.assertEqual(period_from_text("In September 2026 rents fell."), ("2026-09-01", "2026-09-30"))
        self.assertEqual(period_from_text("On 2026-03-04 the plant opened."), ("2026-03-04", "2026-03-04"))
        self.assertEqual(period_from_text("Rents fell."), (None, None))

    def test_places(self):
        self.assertEqual(place_from_text("Vacancy in the Phoenix metro rose."), "Phoenix")
        self.assertEqual(place_from_text("Rents rose downtown.", "Tempe"), None)
        self.assertEqual(place_from_text("Rents in tempe rose.", "Tempe"), "Tempe")

    def test_evidence_window_wins(self):
        ev = Evidence("SRC-x", EvidenceKind.TIME_SERIES, "reading", valid_from="2026-09-01T00:00:00Z",
                      valid_to="2026-09-29T00:00:00Z")
        s = infer_scope("averaged 25 in 2019", ev)
        self.assertEqual((s.valid_from, s.valid_to), ("2026-09-01", "2026-09-29"))

    def test_overlap(self):
        a, b = Scope("2024-01-01", "2024-12-31"), Scope("2026-01-01", "2026-12-31")
        self.assertFalse(a.overlaps_time(b))
        self.assertIsNone(a.overlaps_time(Scope()))


class PolicyTests(unittest.TestCase):
    def test_classification(self):
        self.assertEqual(classify_claim(Claim("The county reported that permits rose.")), ClaimType.ATTRIBUTION)
        self.assertEqual(classify_claim(Claim("Rents reached 1250 dollars.", value=1250.0)), ClaimType.QUANTITATIVE)
        self.assertEqual(classify_claim(Claim("Construction increased in Phoenix.")), ClaimType.TREND)
        self.assertEqual(classify_claim(Claim("Pass predicted.", origin=ClaimOrigin.OBSERVED)), ClaimType.PHYSICAL)

    def test_policy_bars(self):
        self.assertEqual(policy_for(ClaimType.ATTRIBUTION).min_independent_sources, 1)
        self.assertTrue(policy_for(ClaimType.PHYSICAL).requires_observation)
        self.assertEqual(policy_for(ClaimType.TREND, 3).min_independent_sources, 3)


class LineageTests(unittest.TestCase):
    def _graph(self, a_text, b_text, b_publisher="B"):
        g = EvidenceGraph()
        for pub, text in (("A", a_text), (b_publisher, b_text)):
            s = g.add_source(Source(SourceKind.WEB, pub, uri=f"https://{pub.lower()}.example/x", publisher=pub))
            g.add_evidence(Evidence(s.id, EvidenceKind.DOCUMENT, text))
        return g

    def test_syndication_merges(self):
        text = "Industrial vacancy in the Phoenix metro fell to six percent this year as tenants absorbed space."
        g = self._graph(text, text)
        links = resolve_lineage(g)
        self.assertEqual(links[0].relation, "syndicated")
        self.assertEqual(len({s.independence_group for s in g.sources.values()}), 1)

    def test_quotes_merge(self):
        g = self._graph("Vacancy fell to six percent.", "Vacancy fell, according to A, to six percent.")
        self.assertEqual(resolve_lineage(g)[0].relation, "quotes")

    def test_different_texts_stay_independent(self):
        g = self._graph("Vacancy fell to six percent.", "Construction starts paused across the valley.")
        self.assertEqual(resolve_lineage(g), [])
        self.assertEqual(len({s.independence_group for s in g.sources.values()}), 2)


class SkepticTests(unittest.TestCase):
    def test_paraphrase_not_in_evidence_is_flagged(self):
        class Inventive(ReasoningProvider):
            name = "inventive"

            def extract_claims(self, text, objective):
                return [{"statement": "Industrial construction in the Phoenix metro tripled to 40 million square feet.",
                         "value": 40e6, "unit": "", "polarity": 1}]

        r = run_investigation(compile_intent(OBJECTIVE), registry(), Inventive())
        c = next(iter(r.graph.claims.values()))
        self.assertTrue(any(i.startswith("unsupported") for i in c.issues))
        self.assertNotEqual(c.status.value, "verified")

    def test_review_counts(self):
        g = EvidenceGraph()
        s = g.add_source(Source(SourceKind.DOCUMENT, "d", uri="inline:d"))
        e = g.add_evidence(Evidence(s.id, EvidenceKind.DOCUMENT, "Model guessed this."))
        g.add_claim(Claim("Rents will rise.", origin=ClaimOrigin.INFERRED), supported_by=[e.id])
        self.assertGreaterEqual(review(g).count, 1)


class GraphAndPlanningTests(unittest.TestCase):
    def test_question_depths_and_cycle(self):
        c = compile_intent("Find a warehouse business opportunity in Phoenix")
        depths = question_depths(c.questions)
        self.assertEqual(sorted(depths.values()), list(range(7)))
        a, b = Question("A?", ["text"]), Question("B?", ["text"])
        a.depends_on, b.depends_on = [b.id], [a.id]
        with self.assertRaises(DependencyCycle):
            question_depths([a, b])

    def test_tasks_follow_dependencies(self):
        r = run_investigation(compile_intent(OBJECTIVE), registry())
        depths = [t.depth for t in r.plan.tasks]
        self.assertEqual(depths, sorted(depths))


class ReceiptLedgerStateTests(unittest.TestCase):
    def setUp(self):
        self.r = run_investigation(compile_intent(OBJECTIVE), registry())

    def test_receipt_integrity(self):
        self.assertTrue(verify_receipt(self.r.receipt))
        self.assertFalse(verify_receipt({**self.r.receipt, "objective": "something else"}))
        self.assertEqual(len(self.r.receipt["evidence"]), len(self.r.graph.evidence))
        json.dumps(self.r.receipt)

    def test_ledger(self):
        led = self.r.ledger
        self.assertIn("retrieval", led.by_kind())
        self.assertAlmostEqual(led.total_units, sum(e.work_units for e in led.entries))
        self.assertIn("documents", led.yield_by_actor())
        solo = CostLedger(0.0312)
        solo.record("sense", "retrieval", "x", 2, external_usd=1.0)
        self.assertAlmostEqual(solo.total_usd, 1.0624)

    def test_state_export(self):
        state = export_state(self.r)
        self.assertEqual(state["schema"], "lofgren.knowledge-map/1")
        self.assertTrue(state["known"])
        self.assertTrue(state["contradicted"])
        known_ids = {k["id"] for k in state["known"]}
        self.assertFalse(known_ids & {c["id"] for c in state["contradicted"]})

    def test_findings_validate_against_schema(self):
        from lofgren_intelligence.evidence import to_dict

        for f in self.r.findings:
            self.assertEqual(validate_shape("finding", to_dict(f)), [])
        self.assertIn("claim_ids", schema_for("finding")["properties"])


class CalibrationLogTests(unittest.TestCase):
    def test_log_resolve_and_scores(self):
        with tempfile.TemporaryDirectory() as d:
            log = PredictionLog(Path(d) / "p.jsonl")
            log.append([{"claim_id": "CL-1", "confidence": 0.8, "raw": 0.8}, {"claim_id": "CL-2", "confidence": 0.3, "raw": 0.3}])
            self.assertEqual(log.resolve("CL-1", True), 1)
            self.assertEqual(len(log.pending()), 1)
            self.assertAlmostEqual(log.summary()["brier"], 0.04)
        self.assertAlmostEqual(brier_score([(1.0, True), (0.0, False)]), 0.0)
        self.assertEqual(reliability_table([(0.85, True), (0.85, False)])[0]["observed"], 0.5)


class DiscoveryContractTests(unittest.TestCase):
    def test_hypothesis_never_verified_or_promoted(self):
        g = EvidenceGraph()
        s = g.add_source(Source(SourceKind.DOCUMENT, "d", uri="inline:d", quality=1.0))
        e = g.add_evidence(Evidence(s.id, EvidenceKind.DOCUMENT, "Data centers drive construction."))
        claim = g.add_claim(Hypothesis("Data centers drive construction.", originating=[]).as_claim(),
                            supported_by=[e.id])
        Verifier().verify(g)
        self.assertEqual(claim.status.value, "unverified")
        with self.assertRaises(PromotionRefused):
            promote(claim)

    def test_candidate_keeps_measures_separate(self):
        c = Candidate("Cold storage co-op", originating_gap="UNK-1", novelty=0.9, economic_feasibility=0.2)
        self.assertFalse(hasattr(c, "score"))


class AcquisitionNormalizationTests(unittest.TestCase):
    def test_canonical_urls(self):
        self.assertEqual(canonical_url("https://www.Example.com/a/?utm_source=x&id=2#top"), "https://example.com/a?id=2")

    def test_source_classes(self):
        self.assertEqual(classify_source("https://phoenix.gov/x")[0], "government")
        self.assertEqual(classify_source("https://asu.edu/x")[0], "academic")
        self.assertEqual(classify_source("https://reddit.com/r/x")[0], "social")

    def test_units(self):
        unit, conv = normalize_unit("°F")
        self.assertEqual(unit, "°C")
        self.assertAlmostEqual(conv(212), 100.0)
        self.assertIsNone(normalize_unit(""))
        self.assertIsNone(normalize_unit("furlongs"))

    def test_scene_normalization(self):
        s = normalize_scene({"id": "x", "bbox": [0, 0, 1, 1], "properties": {"datetime": "2026-09-01T00:00:00Z",
                                                                              "eo:cloud_cover": 1.0}}, "landsat-c2-l2")
        self.assertEqual(s["gsd_m"], 30.0)
        self.assertEqual(s["access"], "open")

    def test_provider_json_parsing(self):
        out = parse_claims_json('Here: [{"statement": "A rose.", "value": "n/a", "polarity": -1}]')
        self.assertIsNone(out[0]["value"])
        self.assertEqual(out[0]["polarity"], -1)


class CertificationTests(unittest.TestCase):
    def test_v1_gate_passes(self):
        cert = run_certification()
        failed = [s for s in cert["scenarios"] if not s["passed"]]
        self.assertEqual(failed, [])
        self.assertTrue(cert["v1_ready"])


if __name__ == "__main__":
    unittest.main()
