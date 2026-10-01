"""V2 step 1: object model, typed errors, structured expressions."""

import json
import math
import unittest

from lofgren_intelligence.discovery import (
    ALL_TYPES,
    Assumption,
    Candidate,
    CandidateStatus,
    Connection,
    Constraint,
    CounterHypothesis,
    DecisionVariable,
    DiscoveryDecision,
    DiscoveryFinding,
    DiscoveryObjective,
    DiscoveryOutcome,
    DuplicateId,
    EvidenceRequirement,
    Gap,
    Hypothesis,
    ImpossibleTimestamp,
    InputTooLarge,
    InvalidScope,
    KnownFact,
    MalformedInput,
    MissingEvidence,
    NegativeCost,
    NonFiniteValue,
    OptimizationProblem,
    OptimizationResult,
    PriorArt,
    ProblemFrame,
    PromotionRefused,
    Scenario,
    SensitivityResult,
    Simulation,
    Uncertainty,
    UnitMismatch,
    UnknownReference,
    UnknownStatus,
    UnsafeName,
    add_hypothesis,
    from_dict,
    promote,
)
from lofgren_intelligence.discovery.expr import (
    Add,
    Const,
    Div,
    Max,
    Min,
    Mul,
    Neg,
    Pow,
    Relation,
    Sub,
    Unit,
    Var,
    env_of,
    from_json,
)
from lofgren_intelligence.evidence import (
    ClaimStatus,
    Evidence,
    EvidenceGraph,
    EvidenceKind,
    Source,
    SourceKind,
)
from lofgren_intelligence.verification import Verifier

BUDGET = Relation(Mul(Var("units"), Var("unit_cost")), "<=", Var("budget"))


def sample_objects() -> dict:
    """One valid instance of every V2 type."""
    rel = BUDGET.to_json()
    return {
        "discovery_objective": DiscoveryObjective("Cut cold-storage spoilage", "RR-abc"),
        "known_fact": KnownFact("CL-1", "Spoilage was 4% in 2026.", {"valid_from": "2026-01-01",
                                                                       "valid_to": "2026-12-31",
                                                                       "geography": "Phoenix"}, 4.0, "%"),
        "uncertainty": Uncertainty("CL-2", "Compressor runs hot.", "single_source"),
        "missing_evidence": MissingEvidence("UNK-1", "No temperature logs for Zone 7", "sensor", ["IoT"], 0.6, 0.03),
        "problem_frame": ProblemFrame("DOBJ-1", ["KF-1"], ["UNC-1"], [], ["MISS-1"]),
        "prior_art": PriorArt("cold storage spoilage sensors", ["fixture-patents"],
                              limitations=["fixture only; no live patent search"]),
        "gap": Gap("measurement", "Zone 7 temperature", "spoilage cause unknown", "unknown"),
        "assumption": Assumption("Energy costs $0.14/kWh", "price", 0.14, "usd/kWh", "utility tariff not yet sourced"),
        "constraint": Constraint("budget", "economic", rel, source_assumption_id="ASM-1"),
        "connection": Connection("KF-1", "KF-2", "same_place_and_period", "derived"),
        "evidence_requirement": EvidenceRequirement("Hourly Zone 7 temperature for 30 days", "sensor",
                                                    "Phoenix", ("2026-09-01", "2026-09-30"), rel),
        "hypothesis": Hypothesis("Door seals in Zone 7 leak.", originating=["UNK-1"], test="REQ-1"),
        "counter_hypothesis": CounterHypothesis("Spoilage comes from late deliveries.", counters="HYP-1"),
        "candidate": Candidate("Replace Zone 7 door seals", originating_gap="GAP-1", novelty=0.1,
                               technical_feasibility=0.9, economic_feasibility=0.8, expected_value=1200.0),
        "scenario": Scenario("CAND-1", {"units": {"value": 4, "unit": "unit"}}),
        "simulation": Simulation("CAND-1", "expression", "1", {"units": 4}, seed=7, iterations=100),
        "sensitivity_result": SensitivityResult("CAND-1", {"unit_cost": 1.0}),
        "optimization_problem": OptimizationProblem([DecisionVariable("units", "unit", 0, 10, True)],
                                                    Var("units"), "maximize"),
        "optimization_result": OptimizationResult("OPT-1", "optimal", {"units": 10}, 10.0,
                                                  proof="exhaustive over 11 points", verified=True),
        "discovery_finding": DiscoveryFinding("Simulated spoilage falls to 2%.", "simulation_result", ["SIM-1"]),
        "discovery_decision": DiscoveryDecision("max expected value s.t. feasibility >= 0.5", "CAND-1",
                                                outcome="candidate_selected"),
    }


class ExpressionTests(unittest.TestCase):
    def test_arithmetic_and_units(self):
        env = env_of({"units": (4, "unit"), "unit_cost": (250, "usd/unit"), "budget": (1200, "usd")})
        total = Mul(Var("units"), Var("unit_cost")).evaluate(env)
        self.assertEqual(total.value, 1000)
        self.assertEqual(str(total.unit), "usd")
        check = BUDGET.check(env)
        self.assertTrue(check.satisfied)
        self.assertEqual(check.slack, 200)

    def test_every_operator(self):
        env = env_of({"x": 6.0, "y": 3.0})
        x, y = Var("x"), Var("y")
        self.assertEqual(Add(x, y).evaluate(env).value, 9)
        self.assertEqual(Sub(x, y).evaluate(env).value, 3)
        self.assertEqual(Div(x, y).evaluate(env).value, 2)
        self.assertEqual(Min(x, y).evaluate(env).value, 3)
        self.assertEqual(Max(x, y).evaluate(env).value, 6)
        self.assertEqual(Neg(x).evaluate(env).value, -6)
        self.assertEqual(Pow(y, 2).evaluate(env).value, 9)

    def test_unit_mismatch_fails_closed(self):
        env = env_of({"a": (1, "usd"), "b": (1, "kWh")})
        with self.assertRaises(UnitMismatch):
            Add(Var("a"), Var("b")).evaluate(env)
        with self.assertRaises(UnitMismatch):
            Relation(Var("a"), "<=", Var("b")).check(env)
        with self.assertRaises(UnitMismatch):
            Add(Var("a"), Var("b")).unit({"a": Unit.parse("usd"), "b": Unit.parse("kWh")})

    def test_unit_canonicalization(self):
        self.assertEqual(Unit.parse("percent"), Unit.parse("%"))
        self.assertEqual(Unit.parse("$/units"), Unit.parse("usd/unit"))
        self.assertEqual(Unit.parse("usd/unit") * Unit.parse("unit"), Unit.parse("usd"))

    def test_non_finite_values_fail_closed(self):
        with self.assertRaises(NonFiniteValue):
            Div(Const(1), Const(0)).evaluate({})
        with self.assertRaises(NonFiniteValue):
            Mul(Const(1e308), Const(10)).evaluate({})
        with self.assertRaises(NonFiniteValue):
            Const(math.nan)
        with self.assertRaises(NonFiniteValue):
            env_of({"x": math.inf})
        with self.assertRaises(NonFiniteValue):
            Pow(Const(0), -1).evaluate({})

    def test_missing_variable(self):
        with self.assertRaises(UnknownReference):
            Var("ghost").evaluate({})

    def test_unsafe_names(self):
        for bad in ("__import__('os')", "a.b", "1x", "", "x" * 65):
            with self.assertRaises(UnsafeName):
                Var(bad)

    def test_json_round_trip_and_rejection(self):
        e = Div(Add(Var("a"), Const(2, "usd")), Pow(Var("b"), 2))
        self.assertEqual(from_json(json.loads(json.dumps(e.to_json()))).to_json(), e.to_json())
        self.assertEqual(Relation.from_json(BUDGET.to_json()).to_json(), BUDGET.to_json())
        for bad in ({"op": "__import__", "args": []}, {"op": "add", "args": [{"op": "const", "value": 1}]},
                    {"op": "const", "value": "1"}, {"op": "pow", "args": [{"op": "var", "name": "x"}], "exponent": 2.5},
                    "1 + 1", None):
            with self.assertRaises((MalformedInput, UnsafeName)):
                from_json(bad)

    def test_size_limits(self):
        node = {"op": "var", "name": "x"}
        for _ in range(70):
            node = {"op": "neg", "args": [node]}
        with self.assertRaises(InputTooLarge):
            from_json(node)

    def test_relation_operators(self):
        with self.assertRaises(MalformedInput):
            Relation(Var("x"), "<", Var("y"))
        env = env_of({"x": 1.0, "y": 1.0 + 1e-12})
        self.assertTrue(Relation(Var("x"), "==", Var("y")).check(env).satisfied)
        self.assertFalse(Relation(Var("x"), ">=", Const(2)).check(env).satisfied)


class ObjectModelTests(unittest.TestCase):
    def test_every_type_has_a_sample_and_round_trips(self):
        samples = sample_objects()
        self.assertEqual(set(samples), set(ALL_TYPES))
        for name, obj in samples.items():
            d = json.loads(json.dumps(obj.to_dict()))
            again = from_dict(name, d)
            self.assertEqual(again.id, obj.id, name)
            self.assertEqual(again.to_dict(), obj.to_dict(), name)

    def test_ids_are_content_derived(self):
        a = Hypothesis("Door seals in Zone 7 leak.")
        b = Hypothesis("Door seals in Zone 7 leak.")
        c = Hypothesis("Compressor is undersized.")
        self.assertEqual(a.id, b.id)
        self.assertNotEqual(a.id, c.id)

    def test_unknown_fields_and_types_fail_closed(self):
        with self.assertRaises(MalformedInput):
            from_dict("hypothesis", {"statement": "x", "verified": True})
        with self.assertRaises(MalformedInput):
            from_dict("fact", {})

    def test_numbers(self):
        with self.assertRaises(NonFiniteValue):
            KnownFact("CL-1", "x", value=math.nan)
        with self.assertRaises(NegativeCost):
            MissingEvidence("UNK-1", "x", est_cost_usd=-1)
        with self.assertRaises(MalformedInput):
            Candidate("x", "GAP-1", novelty=1.5)
        with self.assertRaises(MalformedInput):
            KnownFact("CL-1", "x", value=True)

    def test_timestamps(self):
        for bad in ("yesterday", "2026-13-45", "2999-01-01T00:00:00+00:00", "1066-10-14"):
            with self.assertRaises(ImpossibleTimestamp):
                Hypothesis("x", created_at=bad)

    def test_scopes(self):
        with self.assertRaises(InvalidScope):
            KnownFact("CL-1", "x", {"valid_from": "2026-12-31", "valid_to": "2026-01-01"})
        with self.assertRaises(InvalidScope):
            KnownFact("CL-1", "x", {"lat": 120.0})
        with self.assertRaises(InvalidScope):
            KnownFact("CL-1", "x", {"valid_from": "last year"})

    def test_statuses(self):
        with self.assertRaises(UnknownStatus):
            Hypothesis("x", status="verified")  # there is no such status
        with self.assertRaises(UnknownStatus):
            Candidate("x", "GAP-1", status="verified")
        with self.assertRaises(UnknownStatus):
            Gap("bogus", "x", "y", "unknown")

    def test_search_absence_is_weak(self):
        with self.assertRaises(MalformedInput):
            Gap("market", "no competitor", "opportunity", "search_absence", confidence=0.9)
        g = Gap("market", "no competitor", "opportunity", "search_absence", confidence=0.9,
                coverage_statement="fixture directory, Phoenix only")
        self.assertLessEqual(g.confidence, 0.4)

    def test_prior_art_must_state_coverage(self):
        with self.assertRaises(MalformedInput):
            PriorArt("query", ["fixture"])
        with self.assertRaises(MalformedInput):
            PriorArt("query", [], limitations=["x"])

    def test_connections_and_constraints_need_grounding(self):
        with self.assertRaises(MalformedInput):
            Connection("KF-1", "KF-2", "same mechanism", "observed")
        with self.assertRaises(MalformedInput):
            Constraint("budget", "economic", BUDGET)
        with self.assertRaises(MalformedInput):
            Assumption("Price is 0.14", "price", 0.14, "usd/kWh")

    def test_optimization_problem_validation(self):
        with self.assertRaises(UnknownReference):
            OptimizationProblem([DecisionVariable("x")], Add(Var("x"), Var("y")), "minimize")
        with self.assertRaises(DuplicateId):
            OptimizationProblem([DecisionVariable("x"), DecisionVariable("x")], Var("x"), "minimize")
        with self.assertRaises(MalformedInput):
            DecisionVariable("x", lower=5, upper=1)
        with self.assertRaises(MalformedInput):
            OptimizationProblem([DecisionVariable("x")], Var("x"), "best")

    def test_simulation_rules(self):
        with self.assertRaises(MalformedInput):
            Simulation("CAND-1", "mc", "1", {}, iterations=100)  # stochastic without a seed
        with self.assertRaises(MalformedInput):
            Simulation("CAND-1", "mc", "1", {}, kind="observed")

    def test_decision_consistency(self):
        with self.assertRaises(MalformedInput):
            DiscoveryDecision("rule", None, outcome="candidate_selected")
        with self.assertRaises(MalformedInput):
            DiscoveryDecision("rule", "CAND-1", outcome="infeasible")
        DiscoveryDecision("rule", None, outcome="insufficient_evidence")

    def test_findings_trace_to_inputs(self):
        with self.assertRaises(MalformedInput):
            DiscoveryFinding("Something.", "hypothesis", [])


class InvariantTests(unittest.TestCase):
    """Named invariants from the V2 directive that step 1 can already enforce."""

    def test_invariant_01_hypothesis_cannot_become_verified(self):
        g = EvidenceGraph()
        s = g.add_source(Source(SourceKind.DOCUMENT, "d", uri="inline:d", quality=1.0))
        e = g.add_evidence(Evidence(s.id, EvidenceKind.DOCUMENT, "Door seals in Zone 7 leak."))
        hyp = Hypothesis("Door seals in Zone 7 leak.")
        claim = add_hypothesis(g, hyp)
        g.link(e.id, claim.id, "supports")
        Verifier().verify(g)
        self.assertEqual(claim.status, ClaimStatus.UNVERIFIED)
        with self.assertRaises(PromotionRefused):
            promote(hyp)
        with self.assertRaises(UnknownStatus):
            hyp.status = "verified"
            Hypothesis(**{**hyp.__dict__, "id": ""})

    def test_invariant_02_candidate_cannot_become_verified_claim(self):
        g = EvidenceGraph()
        cand = sample_objects()["candidate"]
        with self.assertRaises(TypeError):
            g.add_claim(cand)
        with self.assertRaises(PromotionRefused):
            promote(cand)
        self.assertEqual(len(g.claims), 0)

    def test_invariant_08_simulated_is_never_observed(self):
        with self.assertRaises(MalformedInput):
            DiscoveryFinding("Spoilage was observed to fall to 2%.", "simulation_result", ["SIM-1"])
        with self.assertRaises(MalformedInput):
            DiscoveryFinding("Spoilage falls to 2%.", "verified_fact", ["SIM-1"])
        f = DiscoveryFinding("Simulated spoilage falls to 2%.", "simulation_result", ["SIM-1"])
        self.assertEqual(f.confidence_kind.value, "simulation_uncertainty")

    def test_invariant_09_infeasible_cannot_be_labelled_feasible(self):
        with self.assertRaises(MalformedInput):
            OptimizationResult("OPT-1", "feasible", {"x": 1.0}, 1.0, violations=["budget"])

    def test_invariant_10_unproven_solution_cannot_be_optimal(self):
        with self.assertRaises(MalformedInput):
            OptimizationResult("OPT-1", "optimal", {"x": 1.0}, 1.0)

    def test_invariant_11_speculative_connection_stays_speculative(self):
        c = Connection("KF-1", "HYP-1", "possible shared mechanism", "speculative")
        self.assertEqual(c.strength.value, "speculative")
        with self.assertRaises(MalformedInput):
            Connection("KF-1", "HYP-1", "possible shared mechanism", "observed")
        with self.assertRaises(PromotionRefused):
            promote(c)

    def test_invariant_selected_candidate_satisfies_constraints(self):
        with self.assertRaises(MalformedInput):
            Candidate("x", "GAP-1", constraints_violated=["CON-1"], status=CandidateStatus.SELECTED)

    def test_outcomes_are_first_class(self):
        for outcome in ("insufficient_evidence", "contradicted", "infeasible", "unsupported", "unknown",
                        "requires_research"):
            self.assertEqual(DiscoveryDecision("rule", None, outcome=outcome).outcome, DiscoveryOutcome(outcome))


if __name__ == "__main__":
    unittest.main()
