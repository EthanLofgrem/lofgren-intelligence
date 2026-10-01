"""V2 step 1 hardening: identity, references, lifecycles and fail-closed parsing.

Each test pins a behaviour the step 1 foundations did not yet enforce.
"""

import copy
import json
import random
import re
import unittest
from pathlib import Path

from lofgren_intelligence.discovery import (
    Candidate,
    CandidateStatus,
    Connection,
    Constraint,
    DependencyCycle,
    DiscoveryDecision,
    DiscoveryError,
    DuplicateId,
    EvidenceRequirement,
    Hypothesis,
    HypothesisStatus,
    InputTooLarge,
    InvalidScope,
    InvalidTransition,
    KnownFact,
    MalformedInput,
    NonFiniteValue,
    OptimizationResult,
    PriorArt,
    ProblemFrame,
    PromotionRefused,
    Scenario,
    Simulation,
    UnknownStatus,
    from_dict,
)
from lofgren_intelligence.discovery.expr import Add, Const, Neg, Relation, Var, env_of, from_json
from lofgren_intelligence.research.planner import DependencyCycle as V1DependencyCycle

from .test_v2_foundations import BUDGET, sample_objects

DISCOVERY_DIR = Path(__file__).resolve().parent.parent / "lofgren_intelligence" / "discovery"


class IdentityTests(unittest.TestCase):
    def test_supplied_id_must_match_content(self):
        d = json.loads(json.dumps(sample_objects()["hypothesis"].to_dict()))
        d["statement"] = "Edited after the id was computed."
        with self.assertRaises(MalformedInput):
            from_dict("hypothesis", d)
        with self.assertRaises(MalformedInput):
            Candidate("x", "GAP-1", id="CAND-0000000000")

    def test_round_trip_keeps_every_id(self):
        for name, obj in sample_objects().items():
            with self.subTest(type=name):
                again = from_dict(name, json.loads(json.dumps(obj.to_dict())))
                self.assertEqual(again.id, obj.id)
                self.assertEqual(again.id, again.compute_id())

    def test_ids_ignore_key_order_and_number_spelling(self):
        a = Scenario("CAND-1", {"units": {"value": 4, "unit": "unit"}, "price": {"value": 2.5, "unit": "usd"}})
        b = Scenario("CAND-1", {"price": {"unit": "usd", "value": 2.5}, "units": {"unit": "unit", "value": 4}})
        self.assertEqual(a.id, b.id)
        self.assertEqual(Const(2, "usd"), Const(2.0, "usd"))
        c1 = Constraint("cap", "economic", Relation(Var("x"), "<=", Const(2, "usd")), source_assumption_id="ASM-1")
        c2 = Constraint("cap", "economic", Relation(Var("x"), "<=", Const(2.0, "usd")), source_assumption_id="ASM-1")
        self.assertEqual(c1.id, c2.id)

    def test_hypothesis_identity_includes_scope(self):
        phx = Hypothesis("Construction increased.", scope={"valid_from": "2024-01-01", "geography": "Phoenix"})
        tuc = Hypothesis("Construction increased.", scope={"valid_from": "2026-01-01", "geography": "Tucson"})
        self.assertNotEqual(phx.id, tuc.id)
        self.assertEqual(phx.id, Hypothesis("Construction increased.",
                                            scope={"valid_from": "2024-01-01", "geography": "Phoenix"}).id)

    def test_identity_fields_are_fixed_after_construction(self):
        hyp, sim = sample_objects()["hypothesis"], sample_objects()["simulation"]
        for obj, name, value in ((hyp, "id", "HYP-0000000000"), (sim, "kind", "observed"),
                                 (sim, "confidence_kind", "evidence"), (hyp, "created_at", "2020-01-01")):
            with self.subTest(field=name), self.assertRaises(MalformedInput):
                setattr(obj, name, value)
        self.assertEqual(sim.kind, "simulated")

    def test_self_derivation_is_a_cycle(self):
        hyp = Hypothesis("Door seals leak.")
        with self.assertRaises(DependencyCycle):
            Hypothesis("Door seals leak.", derived_from=[hyp.id])
        with self.assertRaises(V1DependencyCycle):  # also catchable as the V1 planner error
            Hypothesis("Door seals leak.", parent_ids=[hyp.id])


class ReferenceTests(unittest.TestCase):
    def test_fact_fields_refuse_v2_ideas(self):
        cases = [
            lambda: KnownFact("HYP-1", "Door seals leak."),
            lambda: Hypothesis("B", supporting_claim_ids=["HYP-2"]),
            lambda: Hypothesis("B", contradicting_claim_ids=["SIM-1"]),
            lambda: Candidate("x", "GAP-1", evidence_support=["CAND-2"]),
            lambda: ProblemFrame("DOBJ-1", known_ids=["HYP-1"]),
            lambda: Constraint("cap", "economic", BUDGET, source_fact_id="SIM-1"),
        ]
        for i, make in enumerate(cases):
            with self.subTest(case=i), self.assertRaises(PromotionRefused):
                make()

    def test_missing_evidence_is_not_negative_evidence(self):
        with self.assertRaises(MalformedInput):
            Hypothesis("x", contradicting_claim_ids=["UNK-1"])
        with self.assertRaises(MalformedInput):
            Candidate("x", "GAP-1", evidence_against=["MISS-1"])
        with self.assertRaises(MalformedInput):
            KnownFact("UNK-1", "x")

    def test_wrong_id_kinds(self):
        with self.assertRaises(MalformedInput):
            Scenario("SIM-1", {})
        with self.assertRaises(MalformedInput):
            OptimizationResult("CAND-1", "unknown")
        with self.assertRaises(MalformedInput):
            DiscoveryDecision("rule", "HYP-1", outcome="candidate_selected")
        with self.assertRaises(MalformedInput):
            Hypothesis("x", predicted_observations=["free text, not a requirement id"])

    def test_duplicates(self):
        with self.assertRaises(DuplicateId):
            ProblemFrame("DOBJ-1", known_ids=["KF-1", "KF-1"])
        with self.assertRaises(DuplicateId):
            Hypothesis("x", derived_from=["UNK-1", "UNK-1"])
        with self.assertRaises(DuplicateId):
            Candidate("x", "GAP-1", constraints_satisfied=["CON-1", "CON-1"])
        with self.assertRaises(MalformedInput):
            ProblemFrame("DOBJ-1", known_ids=["CL-1"], uncertain_ids=["CL-1"])
        with self.assertRaises(MalformedInput):
            DiscoveryDecision("rule", "CAND-1", {"CAND-1": "also rejected"}, outcome="candidate_selected")
        with self.assertRaises(MalformedInput):
            Connection("KF-1", "KF-1", "self", "derived")


class LifecycleTests(unittest.TestCase):
    def test_hypothesis_lifecycle(self):
        hyp = Hypothesis("Door seals leak.")
        hyp.transition("survives")
        hyp.status = HypothesisStatus.CHALLENGED  # assignment goes through the same rules
        with self.assertRaises(InvalidTransition):
            hyp.status = "survives"  # the verifier only lowers standing
        hyp.transition("requires_research")
        with self.assertRaises(InvalidTransition):
            hyp.transition("proposed")
        for word in ("verified", "fact", "observed"):
            with self.subTest(word=word), self.assertRaises(UnknownStatus):
                hyp.status = word
        self.assertEqual(hyp.status, HypothesisStatus.REQUIRES_RESEARCH)

    def test_candidate_lifecycle(self):
        c = Candidate("Replace door seals", "GAP-1")
        with self.assertRaises(InvalidTransition):
            c.status = "selected"  # must be viable first
        c.transition("viable").transition("selected")
        self.assertEqual(c.status, CandidateStatus.SELECTED)
        c.transition("infeasible")
        with self.assertRaises(InvalidTransition):
            c.transition("viable")

    def test_status_change_keeps_identity(self):
        hyp = Hypothesis("Door seals leak.")
        before = hyp.id
        hyp.transition("challenged")
        self.assertEqual(hyp.id, before)
        self.assertEqual(from_dict("hypothesis", json.loads(json.dumps(hyp.to_dict()))).status,
                         HypothesisStatus.CHALLENGED)


class ValueTests(unittest.TestCase):
    def test_calendar_dates(self):
        for bad in ({"valid_from": "2026-13-45"}, {"valid_from": "2026-02-30"}, {"bogus": 1}):
            with self.subTest(scope=bad), self.assertRaises(InvalidScope):
                KnownFact("CL-1", "x", bad)
        with self.assertRaises(InvalidScope):
            EvidenceRequirement("temps", period=("2026-09-30", "2026-09-01"))
        with self.assertRaises(InvalidScope):
            PriorArt("q", ["fixture"], time_range=("2026-02-30", None), limitations=["fixture"])
        with self.assertRaises(MalformedInput):
            EvidenceRequirement("temps", period=("2026-09-01",))

    def test_non_finite_values_inside_mappings(self):
        with self.assertRaises(NonFiniteValue):
            Simulation("CAND-1", "expression", "1", {}, outcomes={"spoilage": {"p50": float("nan")}})
        with self.assertRaises(NonFiniteValue):
            Candidate("x", "GAP-1", costs={"capex": {"value": float("inf"), "unit": "usd"}})
        with self.assertRaises(MalformedInput):
            Simulation("CAND-1", "expression", "1", {"units": object()})

    def test_integer_fields(self):
        with self.assertRaises(MalformedInput):
            Simulation("CAND-1", "expression", "1", {}, seed=True, iterations=10)
        with self.assertRaises(MalformedInput):
            Simulation("CAND-1", "expression", "1", {}, seed=-1, iterations=10)
        with self.assertRaises(InputTooLarge):
            Simulation("CAND-1", "expression", "1", {}, seed=1, iterations=10 ** 9)

    def test_feasible_and_optimal_need_independent_verification(self):
        with self.assertRaises(MalformedInput):
            OptimizationResult("OPT-1", "optimal", {"units": 10}, 10.0, proof="exhaustive over 11 points")
        with self.assertRaises(MalformedInput):
            OptimizationResult("OPT-1", "feasible", {"units": 10}, 10.0)
        OptimizationResult("OPT-1", "feasible", {"units": 10}, 10.0, verified=True)


class ExpressionHardeningTests(unittest.TestCase):
    def test_nodes_check_their_children(self):
        for make in (lambda: Add(1, 2), lambda: Neg("x"), lambda: Relation(1, "<=", Var("x"))):
            with self.assertRaises(MalformedInput):
                make()

    def test_direct_construction_is_bounded(self):
        node = Var("x")
        with self.assertRaises(InputTooLarge):
            for _ in range(200):
                node = Neg(node)  # stops at the depth limit instead of a RecursionError at evaluation

    def test_unit_syntax(self):
        for bad in ("usd/h/d", "kg^0", "m^99", "a b", "us$d!", 5):
            with self.subTest(unit=bad), self.assertRaises((MalformedInput, InputTooLarge)):
                Const(1.0, bad)
        self.assertEqual(str(Const(1.0, "usd/kWh").unit({})), "usd/kWh")

    def test_json_nodes_have_exact_keys(self):
        for bad in ({"op": "var", "name": "x", "unit": "usd"}, {"op": "const", "value": 1, "eval": "x"},
                    {"op": "add", "args": [{"op": "var", "name": "x"}, {"op": "var", "name": "y"}], "extra": 1},
                    {"op": ["add"], "args": []}):
            with self.subTest(node=bad), self.assertRaises(MalformedInput):
                from_json(bad)
        with self.assertRaises(MalformedInput):
            Relation.from_json({**BUDGET.to_json(), "note": "x"})

    def test_environment_shape(self):
        with self.assertRaises(MalformedInput):
            env_of({"x": (1.0,)})
        with self.assertRaises(MalformedInput):
            env_of([("x", 1.0)])

    def test_evaluation_is_deterministic(self):
        e = Add(Var("a"), Const(0.1, "usd"))
        env = env_of({"a": (0.2, "usd")})
        self.assertEqual({repr(e.evaluate(env).value) for _ in range(50)}, {repr(0.2 + 0.1)})

    def test_no_eval_or_exec_in_discovery(self):
        call = re.compile(r"(?<![\w.])(eval|exec|compile|__import__)\s*\(")
        for path in DISCOVERY_DIR.glob("*.py"):
            with self.subTest(file=path.name):
                self.assertIsNone(call.search(path.read_text(encoding="utf-8")))


class FuzzTests(unittest.TestCase):
    """Seeded corruption of every type: always a typed error or a valid, round-trippable object."""

    JUNK = [None, "", "x", "verified", -1, 0, 1.5, 1e308, float("nan"), float("inf"), True, [], {}, [None], [1, 1],
            {"op": "neg"}, "CL-1", "HYP-1", "UNK-1", "x" * 6000, "2999-01-01T00:00:00+00:00", 2 ** 80, ["a", "a"]]

    def _corrupt(self, rng, value, depth=0):
        if isinstance(value, dict) and value and depth < 4 and rng.random() < 0.7:
            key = rng.choice(sorted(value))
            out = dict(value)
            roll = rng.random()
            if roll < 0.15:
                out.pop(key)
            elif roll < 0.25:
                out["extra_" + key] = rng.choice(self.JUNK)
            else:
                out[key] = self._corrupt(rng, value[key], depth + 1)
            return out
        if isinstance(value, list) and value and depth < 4 and rng.random() < 0.7:
            out = list(value)
            i = rng.randrange(len(out))
            out[i] = self._corrupt(rng, out[i], depth + 1)
            return out
        return copy.deepcopy(rng.choice(self.JUNK))

    def test_corrupted_objects_fail_closed(self):
        rng = random.Random(20260930)
        base = {name: json.loads(json.dumps(obj.to_dict())) for name, obj in sample_objects().items()}
        counts = {"error": 0, "valid": 0}
        for _ in range(4000):
            name = rng.choice(sorted(base))
            data = self._corrupt(rng, base[name])
            try:
                obj = from_dict(name, data)
            except DiscoveryError:
                counts["error"] += 1
                continue
            again = from_dict(name, json.loads(json.dumps(obj.to_dict())))
            self.assertEqual(again.id, obj.id, name)
            counts["valid"] += 1
        self.assertGreater(counts["error"], 1000)
        self.assertGreater(counts["valid"], 100)

    def test_corrupted_expressions_fail_closed(self):
        rng = random.Random(7)
        base = BUDGET.to_json()
        for _ in range(2000):
            data = self._corrupt(rng, copy.deepcopy(base))
            try:
                Relation.from_json(data)
            except DiscoveryError:
                pass


if __name__ == "__main__":
    unittest.main()
