"""V2 adversarial suite (directive section 22) and randomized malformed input (section 18).

Each case asserts the correct outcome, which is often insufficient_evidence, contradicted, infeasible,
unsupported, unknown or requires_research, or a typed refusal, rather than any answer at all. All data is fictional.
"""

from __future__ import annotations

import copy
import csv
import json
import random
import tempfile
import unittest
from pathlib import Path

import lofgren_intelligence.certification as C
from lofgren_intelligence.adapters import AdapterRegistry, SensorAdapter
from lofgren_intelligence.discovery import DiscoveryContext
from lofgren_intelligence.discovery import fixtures as F
from lofgren_intelligence.discovery.errors import (
    DiscoveryError,
    FalseNovelty,
    MalformedInput,
    PromotionRefused,
    UnitMismatch,
    UnknownReference,
)
from lofgren_intelligence.discovery.optimize import optimize, problem_from_json
from lofgren_intelligence.discovery.pipeline import discover_from_run, run_discovery
from lofgren_intelligence.discovery.receipt import check_discovery_receipt, verify_discovery_receipt
from lofgren_intelligence.discovery.types import (
    CandidateStatus,
    DiscoveryFinding,
    DiscoveryOutcome,
    FindingKind,
    Hypothesis,
    HypothesisStatus,
    OptimizationStatus,
    UncertaintyReason,
)
from lofgren_intelligence.discovery.verifier import verify_discovery
from lofgren_intelligence.kernel import export_knowledge_map

V, K, add, mul, rel = F.V, F.K, F.add, F.mul, F.rel
AT = F.AT


def run_of(docs: dict, objective: str = C.OBJECTIVE):
    return C._run(objective, C._docs(docs))


class Adversarial(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.v1 = run_of(C.AGREE_AND_CONFLICT)

    def ctx(self, run=None):
        run = run or self.v1
        return DiscoveryContext(export_knowledge_map(run), run.receipt)

    def test_conflicting_sources(self):
        r = discover_from_run(run_of(F.CONTRADICTED_DOCS, "Is warehouse vacancy in Tucson rising?"), "x (fictional)",
                              design=F.warehouse_design(), at=AT)
        self.assertEqual(r.outcome, DiscoveryOutcome.CONTRADICTED)
        self.assertIsNone(r.selected)
        selectable = [c for c in r.candidates.candidates if c.status == CandidateStatus.VIABLE]
        self.assertTrue(selectable, "the fixture should have candidates that would otherwise be selectable")
        for c in selectable:  # they are held back by the evidence state, and say so
            self.assertIn("contested", r.decision.alternatives[c.id])

    def test_copied_sources(self):
        r = discover_from_run(run_of(C.SYNDICATED, "Is industrial vacancy in the Phoenix metro falling?"),
                              "x (fictional)", at=AT)
        self.assertFalse(r.framed.known_facts)

    def test_outdated_evidence(self):
        r = discover_from_run(run_of(C.STALE), "x (fictional)", at=AT)
        self.assertTrue(any(u.reason == UncertaintyReason.STALE for u in r.framed.uncertainties))

    def test_future_dated_evidence(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "s.csv"
            with path.open("w", newline="", encoding="utf-8") as fh:
                w = csv.writer(fh)
                w.writerow(["timestamp", "sensor_id", "metric", "value", "unit", "calibrated_at"])
                w.writerow(["2031-09-01T00:00:00Z", "t-1", "temperature", "40", "C", "2026-06-01T00:00:00Z"])
                w.writerow(["2031-09-02T00:00:00Z", "t-1", "temperature", "44", "C", "2026-06-01T00:00:00Z"])
            reg = AdapterRegistry()
            reg.register(SensorAdapter([path], authorized=True))
            run = C._run("Is warehouse temperature too high?", reg)
        r = discover_from_run(run, "x (fictional)", at=AT)
        self.assertFalse(r.framed.known_facts)

    def test_wrong_locations(self):
        r = discover_from_run(run_of(C.SCOPED), "x (fictional)", at=AT)
        f = r.framed.frame.scope
        self.assertTrue(all(h.scope.geography == f.geography for h in r.hypotheses.all))
        self.assertNotIn("scope-mismatch", r.verifier.codes())
        elsewhere = Hypothesis("Elsewhere (fictional)", scope={"geography": "Atlantis"},
                               supporting_claim_ids=[next(iter(self.v1.graph.claims))], created_at=AT)
        frame = discover_from_run(self.v1, "x (fictional)", at=AT).framed.frame
        rep = verify_discovery(self.ctx(), frame, [elsewhere])
        self.assertIn("scope-mismatch", rep.codes())
        self.assertEqual(elsewhere.status, HypothesisStatus.CHALLENGED)

    def test_wrong_and_ambiguous_units(self):
        d = F.warehouse_design()
        d["constraints"][0]["variable_units"] = {"sqft": "sqft", "max_sqft": "usd"}
        with self.assertRaises(UnitMismatch):
            discover_from_run(self.v1, "x (fictional)", design=d, at=AT)
        d = F.warehouse_design()
        d["model"] = {**d["model"], "units": {**d["model"]["units"], "rent": "dollars per ???"}}
        with self.assertRaises(MalformedInput):
            discover_from_run(self.v1, "x (fictional)", design=d, at=AT)

    def test_fabricated_citations(self):
        d = F.warehouse_design()
        d["candidates"][0]["evidence_support"] = ["CL-0000000000"]
        with self.assertRaises(UnknownReference):
            discover_from_run(self.v1, "x (fictional)", design=d, at=AT)
        d = F.warehouse_design()
        d["constraints"][0] = {**d["constraints"][0], "fact": "CL-0000000000", "assumption": None}
        with self.assertRaises(UnknownReference):
            discover_from_run(self.v1, "x (fictional)", design=d, at=AT)

    def test_unsupported_hypotheses(self):
        class Provider(C.HeuristicProvider):
            def propose_hypotheses(self, frame):
                return [{"statement": "Demand will triple (fictional)"}]

        r = discover_from_run(self.v1, "x (fictional)", provider=Provider(), at=AT)
        h = next(h for h in r.hypotheses.all if h.origin == "provider:heuristic")
        self.assertEqual(h.status, HypothesisStatus.REQUIRES_RESEARCH)
        self.assertNotIn(h.id, {f.rests_on[0] for f in r.findings if f.kind == FindingKind.HYPOTHESIS})

    def test_circular_hypotheses(self):
        a = Hypothesis("A (fictional)", created_at=AT)
        b = Hypothesis("B (fictional)", derived_from=[a.id], created_at=AT)
        object.__setattr__(a, "derived_from", [b.id])
        self.assertIn("circular-reasoning", verify_discovery(self.ctx(), None, [a, b]).codes())

    def test_false_novelty_claims(self):
        for words in ("a first-ever cold store", "an unprecedented warehouse", "never been attempted"):
            d = F.warehouse_design()
            d["candidates"][0]["description"] = f"Build {words} (fictional)"
            with self.subTest(words=words), self.assertRaises(FalseNovelty):
                discover_from_run(self.v1, "x (fictional)", design=d, at=AT)

    def test_impossible_constraints(self):
        r = discover_from_run(self.v1, "x (fictional)", design=F.infeasible_design(), at=AT)
        self.assertEqual(r.outcome, DiscoveryOutcome.INFEASIBLE)
        self.assertTrue(all(c.status == CandidateStatus.INFEASIBLE for c in r.candidates.candidates))

    def test_degenerate_lp(self):
        ctx = self.ctx()
        deg = {"variables": [{"name": "x", "lower": 0}, {"name": "y", "lower": 0}], "objective": add(V("x"), V("y")),
               "direction": "maximize", "constraints": [rel(V("x"), "<=", K(0)), rel(V("y"), "<=", K(0)),
                                                        rel(add(V("x"), V("y")), "<=", K(0))]}
        res = optimize(ctx, ctx.ensure(problem_from_json(deg, AT)), at=AT)
        self.assertEqual((res.status, res.objective_value), (OptimizationStatus.OPTIMAL, 0.0))

    def test_simulation_labelled_observed(self):
        for word in ("observed", "measured", "verified"):
            with self.subTest(word=word), self.assertRaises(MalformedInput):
                DiscoveryFinding(f"Profit was {word} at 5 USD", FindingKind.SIMULATION_RESULT, ["SIM-1"])

    def test_empty_map(self):
        r = discover_from_run(run_of(F.EMPTY_DOCS, "Is warehouse vacancy in Tucson rising?"), "x (fictional)",
                              design=F.warehouse_design(), at=AT)
        self.assertEqual(r.outcome, DiscoveryOutcome.INSUFFICIENT_EVIDENCE)
        self.assertIsNone(r.handoff)

    def test_very_weak_evidence(self):
        run = run_of({"blog (fictional)": "Someone said Phoenix warehouses might be busy."})
        r = discover_from_run(run, "x (fictional)", at=AT)
        self.assertIn(r.outcome, (DiscoveryOutcome.INSUFFICIENT_EVIDENCE, DiscoveryOutcome.REQUIRES_RESEARCH))
        self.assertFalse(r.framed.known_facts)

    def test_high_confidence_false_inputs(self):
        ctx = self.ctx()
        contested = ctx.claims("contradicted")[0]
        d = F.warehouse_design()
        d["facts"] = {"rent_seen": contested["id"]}
        with self.assertRaises((PromotionRefused, MalformedInput)):
            run_discovery(ctx, "x (fictional)", design=d, at=AT)
        d = F.warehouse_design()
        d["constraints"][0] = {**d["constraints"][0], "fact": contested["id"], "assumption": None}
        with self.assertRaises((PromotionRefused, MalformedInput)):
            run_discovery(self.ctx(), "x (fictional)", design=d, at=AT)

    def test_malformed_and_tampered_receipts(self):
        r = discover_from_run(self.v1, "x (fictional)", design=F.warehouse_design(), at=AT)
        for bad in (None, [], "DR-1", {"schema": "lofgren.discovery-receipt/1"}, {**r.receipt, "ledger": "x"}):
            self.assertFalse(verify_discovery_receipt(bad))
        t = copy.deepcopy(r.receipt)
        t["outcome"] = "candidate_selected" if r.outcome.value != "candidate_selected" else "unknown"
        with self.assertRaises(DiscoveryError):
            check_discovery_receipt(t)


class RandomizedMalformedInput(unittest.TestCase):
    """Seeded random corruption of real design spaces: every result is a typed refusal or a valid discovery."""

    def test_every_outcome_is_typed_or_valid(self):
        v1 = run_of(C.AGREE_AND_CONFLICT)
        base = F.warehouse_design(simulation={"seed": 3, "iterations": 50})
        rng = random.Random(20261004)
        junk = [None, -1, 1e308, float("nan"), "", "x" * 6000, [], {}, {"op": "eval"}, True, "__import__('os')"]

        def corrupt(node, depth=0):
            if isinstance(node, dict) and node and depth < 6:
                key = rng.choice(sorted(node))
                if rng.random() < 0.4:
                    node[key] = rng.choice(junk)
                else:
                    corrupt(node[key], depth + 1)
            elif isinstance(node, list) and node and depth < 6:
                i = rng.randrange(len(node))
                if rng.random() < 0.4:
                    node[i] = rng.choice(junk)
                else:
                    corrupt(node[i], depth + 1)

        outcomes = {"typed_refusal": 0, "valid": 0}
        for i in range(60):
            design = copy.deepcopy(base)
            for _ in range(rng.randint(1, 3)):
                corrupt(design)
            try:
                json.dumps(design, allow_nan=True)
                r = discover_from_run(v1, "x (fictional)", design=design, at=AT)
            except (DiscoveryError, ValueError) as exc:  # typed errors (discovery errors are ValueErrors too)
                self.assertIsInstance(exc, (DiscoveryError, ValueError))
                outcomes["typed_refusal"] += 1
                continue
            self.assertTrue(verify_discovery_receipt(r.receipt), f"case {i}")
            self.assertIsInstance(r.outcome, DiscoveryOutcome)
            outcomes["valid"] += 1
        self.assertEqual(sum(outcomes.values()), 60)
        self.assertGreater(outcomes["typed_refusal"], 0)


if __name__ == "__main__":
    unittest.main()
