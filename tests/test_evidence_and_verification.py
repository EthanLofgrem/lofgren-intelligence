import tempfile
import unittest
from pathlib import Path

from lofgren_intelligence.evidence import (
    Claim,
    ClaimOrigin,
    ClaimStatus,
    Evidence,
    EvidenceGraph,
    EvidenceKind,
    Source,
    SourceKind,
)
from lofgren_intelligence.models import HeuristicProvider, parse_value
from lofgren_intelligence.verification import Calibrator, Verifier, relation


def claim(text, **kw):
    value, unit = parse_value(text)
    return Claim(text, value=kw.pop("value", value), unit=kw.pop("unit", unit), **kw)


def graph_with(*items):
    """items: (publisher, statement) pairs; each gets its own source and evidence."""
    g = EvidenceGraph()
    ids = []
    for publisher, text in items:
        s = g.add_source(Source(SourceKind.DOCUMENT, f"{publisher} doc", uri=f"inline:{publisher}:{text[:10]}",
                                publisher=publisher, quality=0.6))
        e = g.add_evidence(Evidence(s.id, EvidenceKind.DOCUMENT, text))
        ids.append(g.add_claim(claim(text), supported_by=[e.id]).id)
    return g, ids


class GraphTests(unittest.TestCase):
    def test_provenance_trace_and_round_trip(self):
        g, (cid,) = graph_with(("A", "Warehouse space in Phoenix reached 14 million square feet."))
        trace = g.trace(cid)
        self.assertEqual(trace["supporting"][0]["source"]["publisher"], "A")
        with tempfile.TemporaryDirectory() as d:
            g.save(Path(d) / "g.json")
            g2 = EvidenceGraph.load(Path(d) / "g.json")
        self.assertEqual(g2.claims[cid].statement, g.claims[cid].statement)
        self.assertEqual(len(g2.edges), len(g.edges))

    def test_evidence_requires_known_source(self):
        with self.assertRaises(KeyError):
            EvidenceGraph().add_evidence(Evidence("SRC-missing", EvidenceKind.DOCUMENT, "x"))


class RelationTests(unittest.TestCase):
    def test_opposite_trends_contradict(self):
        rel, _ = relation(claim("Industrial construction in the Phoenix metro increased in 2026."),
                          claim("Industrial construction in the Phoenix metro decreased in 2026."))
        self.assertEqual(rel, "contradicts")

    def test_different_subjects_do_not_match(self):
        rel, _ = relation(claim("Industrial construction in the Phoenix metro increased in 2026."),
                          claim("Industrial vacancy in the Phoenix metro rose in 2026."))
        self.assertIsNone(rel)

    def test_values_agree_within_tolerance(self):
        a = claim("Completed warehouse space in the Phoenix metro reached 14 million square feet.")
        b = claim("Completed warehouse space in the Phoenix metro reached 14.2 million square feet.")
        self.assertEqual(relation(a, b)[0], "supports")

    def test_values_disagree(self):
        a = claim("Completed warehouse space in the Phoenix metro reached 14 million square feet.")
        b = claim("Completed warehouse space in the Phoenix metro reached 9 million square feet.")
        self.assertEqual(relation(a, b)[0], "contradicts")

    def test_negation_contradicts(self):
        a = claim("The Loop 303 corridor has rail access for warehouse tenants.")
        b = claim("The Loop 303 corridor has no rail access for warehouse tenants.", polarity=-1)
        self.assertEqual(relation(a, b)[0], "contradicts")


class VerifierTests(unittest.TestCase):
    def test_independent_confirmation_verifies(self):
        g, (a, b) = graph_with(
            ("Journal", "Completed warehouse space in the Phoenix metro reached 14 million square feet."),
            ("County", "Completed warehouse space in the Phoenix metro reached 14.2 million square feet."),
        )
        Verifier().verify(g)
        self.assertEqual(g.claims[a].status, ClaimStatus.VERIFIED)
        self.assertGreaterEqual(g.claims[a].confidence, 0.7)

    def test_same_publisher_is_not_independent(self):
        g, (a, _) = graph_with(
            ("Journal", "Completed warehouse space in the Phoenix metro reached 14 million square feet."),
            ("Journal", "Completed warehouse space in the Phoenix metro reached 14.2 million square feet."),
        )
        Verifier().verify(g)
        self.assertNotEqual(g.claims[a].status, ClaimStatus.VERIFIED)

    def test_contradiction_is_recorded_and_contested(self):
        g, (a, b) = graph_with(
            ("Journal", "Industrial construction in the Phoenix metro increased in 2026."),
            ("Broker", "Industrial construction in the Phoenix metro decreased in 2026."),
        )
        v = Verifier()
        v.verify(g)
        self.assertEqual(len(g.contradictions), 1)
        self.assertEqual(g.claims[a].status, ClaimStatus.CONTESTED)
        self.assertEqual(v.factors[a].contradicting_sources, 1)

    def test_single_observation_can_verify(self):
        g = EvidenceGraph()
        s = g.add_source(Source(SourceKind.ORBITAL, "elements", quality=0.9))
        e = g.add_evidence(Evidence(s.id, EvidenceKind.CALCULATION, "Sentinel-2A passes over Phoenix twice."))
        c = g.add_claim(Claim("Sentinel-2A passes over Phoenix twice.", origin=ClaimOrigin.OBSERVED),
                        supported_by=[e.id])
        Verifier().verify(g)
        self.assertEqual(c.status, ClaimStatus.VERIFIED)


class CalibratorTests(unittest.TestCase):
    def test_empty_calibrator_is_identity(self):
        self.assertEqual(Calibrator().calibrate(0.8), 0.8)

    def test_overconfidence_is_pulled_down(self):
        cal = Calibrator()
        for i in range(40):
            cal.record(0.85, i % 2 == 0)  # stated 85%, right only half the time
        self.assertLess(cal.calibrate(0.85), 0.6)
        self.assertAlmostEqual(cal.expected_calibration_error(), 0.35, places=2)

    def test_rejects_out_of_range(self):
        with self.assertRaises(ValueError):
            Calibrator().record(1.2, True)


class ExtractionTests(unittest.TestCase):
    def test_heuristic_extracts_factual_sentences(self):
        text = ("Industrial construction in Phoenix increased in 2026. What should we do next? "
                "Vacancy in Phoenix rose to 11 percent.")
        claims = HeuristicProvider().extract_claims(text, "Phoenix industrial construction vacancy")
        statements = [c["statement"] for c in claims]
        self.assertEqual(len(statements), 2)
        self.assertEqual(claims[1]["value"], 11.0)
        self.assertEqual(claims[1]["unit"], "%")

    def test_years_are_not_values(self):
        self.assertEqual(parse_value("Construction increased in 2026."), (None, ""))
        self.assertEqual(parse_value("Rents rose to $1,250 in 2026."), (1250.0, "usd"))


if __name__ == "__main__":
    unittest.main()
