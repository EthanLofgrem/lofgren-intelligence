"""V2 discovery engines: connections, hypotheses, simulation, sensitivity, optimization, candidates, verifier,
receipt, handoff and pipeline. Hand-computed expectations wherever a number is checked. All data is fictional."""

from __future__ import annotations

import copy
import json
import unittest

import lofgren_intelligence.certification as C
from lofgren_intelligence.discovery import (
    DiscoveryContext,
    DiscoveryObjective,
    MalformedInput,
    UnknownReference,
    detect_gaps,
    frame_problem,
)
from lofgren_intelligence.discovery import fixtures as F
from lofgren_intelligence.discovery.candidates import DecisionRule, DesignSpace, dominates, evaluate_candidates
from lofgren_intelligence.discovery.connections import find_connections
from lofgren_intelligence.discovery.errors import DiscoveryError, NonFiniteValue, UnitMismatch, UnsupportedAlgorithm
from lofgren_intelligence.discovery.expr import Relation, from_json
from lofgren_intelligence.discovery.handoff import HandoffInvalid, check_handoff, validate_handoff
from lofgren_intelligence.discovery.hypotheses import COUNTER_THRESHOLD, generate_hypotheses
from lofgren_intelligence.discovery.optimize import (
    NotLinear,
    linear_form,
    minimal_infeasible_set,
    optimize,
    problem_from_json,
    verify_solution,
)
from lofgren_intelligence.discovery.pipeline import discover_from_run, reverify, run_discovery
from lofgren_intelligence.discovery.receipt import (
    LEDGER_KINDS,
    DiscoveryLedger,
    objects_match_receipt,
    verify_discovery_receipt,
)
from lofgren_intelligence.discovery.report import discovery_summary, render_discovery_markdown
from lofgren_intelligence.discovery.simulate import ExpressionModel, MonteCarloModel, check_distribution, simulate
from lofgren_intelligence.discovery.types import (
    Candidate,
    CandidateStatus,
    ConnectionStrength,
    DiscoveryOutcome,
    Hypothesis,
    HypothesisStatus,
    OptimizationStatus,
    Robustness,
    Scenario,
)
from lofgren_intelligence.discovery.verifier import ISSUE_CODES, verify_discovery
from lofgren_intelligence.kernel import export_knowledge_map, verify_receipt
from lofgren_intelligence.models.provider import HeuristicProvider

V, K, add, sub, mul, div, rel = F.V, F.K, F.add, F.sub, F.mul, F.div, F.rel
AT = F.AT


def phoenix():
    return C._run(C.OBJECTIVE, C._docs(C.AGREE_AND_CONFLICT))


class Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.v1 = phoenix()

    def ctx(self) -> DiscoveryContext:
        return DiscoveryContext(export_knowledge_map(self.v1), self.v1.receipt)

    def framed(self, ctx: DiscoveryContext):
        return frame_problem(ctx, ctx.ensure(DiscoveryObjective("Test objective (fictional)", ctx.research_id,
                                                                created_at=AT)), at=AT)


class Connections(Base):
    def test_rules_decide_strength(self):
        ctx = self.ctx()
        fr = self.framed(ctx)
        gaps = detect_gaps(ctx, fr, at=AT).gaps
        res = find_connections(ctx, fr, gaps, at=AT)
        observed = res.by_strength(ConnectionStrength.OBSERVED)
        self.assertTrue(observed)
        for c in observed:
            self.assertTrue(c.evidence_ids)
            self.assertTrue(all(e.startswith("EV-") for e in c.evidence_ids))
        for c in res.by_strength(ConnectionStrength.SPECULATIVE):
            self.assertFalse(c.evidence_ids)
        again = find_connections(ctx, fr, gaps, at=AT)
        self.assertEqual([c.id for c in res.connections], [c.id for c in again.connections])

    def test_temporal_order_and_shared_variables_are_derived(self):
        run = C._run(C.OBJECTIVE, C._docs(C.SCOPED))
        ctx = DiscoveryContext(export_knowledge_map(run), run.receipt)
        fr = self.framed(ctx)
        d = F.warehouse_design()
        d["constraints"].append({"name": "minimum size", "kind": "resource",
                                 "relation": rel(V("sqft"), ">=", K(1000, "sqft")), "variable_units": {"sqft": "sqft"},
                                 "assumption": "max_sqft"})
        res = evaluate_candidates(ctx, fr, DesignSpace.from_json(d), at=AT)
        conns = find_connections(ctx, fr, constraints=res.constraints, at=AT)
        shared = [c for c in conns.connections if c.relation.startswith("constrain the same variable")]
        self.assertEqual(len(shared), 1)
        self.assertEqual(shared[0].strength, ConnectionStrength.DERIVED)


class Hypotheses(Base):
    def test_competing_explanations_and_counters(self):
        ctx = self.ctx()
        fr = self.framed(ctx)
        res = generate_hypotheses(ctx, fr, detect_gaps(ctx, fr, at=AT).gaps, at=AT)
        for cx in fr.frame.contradiction_ids:
            self.assertGreaterEqual(len([h for h in res.hypotheses if cx in h.originating]), 2)
        for h in res.hypotheses:
            self.assertEqual(h.confidence_kind.value, "hypothesis")
            self.assertEqual(h.scope, fr.frame.scope)
            if (h.provisional_score or 0) >= COUNTER_THRESHOLD:
                self.assertTrue(any(c.counters == h.id for c in res.counters), h.statement)
        self.assertEqual(len({h.statement for h in res.hypotheses}), len(res.hypotheses))

    def test_score_is_the_stated_heuristic(self):
        ctx = self.ctx()
        fr = self.framed(ctx)
        res = generate_hypotheses(ctx, fr, at=AT)
        for h in res.hypotheses:
            if h.supporting_claim_ids and h.origin == "contradiction-explaining":
                s = sum(ctx.reference(c)["confidence"] for c in h.supporting_claim_ids) / len(h.supporting_claim_ids)
                k = sum(ctx.reference(c)["confidence"] for c in h.contradicting_claim_ids) / \
                    max(1, len(h.contradicting_claim_ids))
                self.assertAlmostEqual(h.provisional_score, round(min(0.95, s * (1 - 0.5 * k)), 3))

    def test_provider_text_only(self):
        class Provider(HeuristicProvider):
            def propose_hypotheses(self, frame):
                assert set(frame) == {"objective", "known", "uncertain", "contradictions"}
                return [{"statement": "Rail access raises occupancy (fictional)", "evidence": ["EV-x"],
                         "claim_ids": ["CL-x"]}, "not an object", {"statement": ""}]

        ctx = self.ctx()
        fr = self.framed(ctx)
        res = generate_hypotheses(ctx, fr, provider=Provider(), at=AT)
        mine = [h for h in res.hypotheses if h.origin == "provider:heuristic"]
        self.assertEqual(len(mine), 1)
        self.assertEqual(mine[0].supporting_claim_ids, [])
        self.assertEqual(res.provider["proposals"], 1)
        self.assertEqual(len(res.notes), 2)

    def test_failing_provider_keeps_engine_strategies(self):
        class Down(HeuristicProvider):
            def propose_hypotheses(self, frame):
                raise RuntimeError("down")

        ctx = self.ctx()
        fr = self.framed(ctx)
        res = generate_hypotheses(ctx, fr, provider=Down(), at=AT)
        self.assertIn("RuntimeError", res.provider["error"])
        self.assertTrue(res.hypotheses)

    def test_no_frame_no_hypotheses(self):
        run = C._run("Is warehouse vacancy in Tucson rising?", C._docs(F.EMPTY_DOCS))
        ctx = DiscoveryContext(export_knowledge_map(run), run.receipt)
        res = generate_hypotheses(ctx, self.framed(ctx), at=AT)
        self.assertEqual(res.all, [])


class Simulation(Base):
    def setUp(self):
        self.model = ExpressionModel.from_json(F.PROFIT_MODEL)

    def candidate(self, ctx):
        fr = self.framed(ctx)
        cand = ctx.register(Candidate("Sim test (fictional)", "x", created_at=AT))
        params = {"sqft": {"value": 1000, "unit": "sqft"}, "rent": {"value": 12, "unit": "usd/sqft"},
                  "occupancy": {"value": 0.85, "unit": ""}, "build_cost": {"value": 8, "unit": "usd/sqft"}}
        scn = ctx.register(Scenario(cand.id, params, created_at=AT))
        del fr
        return cand, scn

    def test_deterministic_model_value(self):
        ctx = self.ctx()
        cand, scn = self.candidate(ctx)
        sim = simulate(ctx, cand, scn, self.model, at=AT)
        self.assertEqual(sim.iterations, 1)
        self.assertEqual(sim.kind, "simulated")
        self.assertAlmostEqual(sim.outcomes["profit"]["mean"], 1000 * (12 * 0.85 - 8))  # 2200
        self.assertEqual(sim.outcomes["profit"]["unit"], "usd")

    def test_monte_carlo_is_seeded(self):
        mc = MonteCarloModel(self.model, {"occupancy": {"dist": "uniform", "low": 0.6, "high": 0.95}})
        a = simulate(self.ctx_a(), *self.candidate(self.ctx_a()), mc, seed=5, iterations=300, at=AT)
        ctx = self.ctx()
        b = simulate(ctx, *self.candidate(ctx), mc, seed=5, iterations=300, at=AT)
        self.assertEqual(json.dumps(a.outcomes, sort_keys=True), json.dumps(b.outcomes, sort_keys=True))
        s = a.outcomes["profit"]
        self.assertLessEqual(s["p5"], s["p50"])
        self.assertLessEqual(s["p50"], s["p95"])
        with self.assertRaises(MalformedInput):
            ctx2 = self.ctx()
            simulate(ctx2, *self.candidate(ctx2), mc, seed=None, iterations=10, at=AT)

    def ctx_a(self):
        if not hasattr(self, "_a"):
            self._a = self.ctx()
        return self._a

    def test_models_fail_closed(self):
        with self.assertRaises(UnitMismatch):
            ExpressionModel("bad", "1", {"y": add(V("a"), V("b"))}, {"a": "usd", "b": "sqft"})
        with self.assertRaises(MalformedInput):
            ExpressionModel("bad", "1", {"y": V("a")}, {})
        with self.assertRaises(UnsupportedAlgorithm):
            check_distribution("x", {"dist": "cauchy"})
        with self.assertRaises(MalformedInput):
            check_distribution("x", {"dist": "uniform", "low": 2, "high": 1})
        with self.assertRaises(NonFiniteValue):
            ExpressionModel("div", "1", {"y": div(V("a"), V("b"))}, {"a": "", "b": ""}).evaluate({"a": 1, "b": 0})


class Sensitivity(Base):
    def test_hand_computed_elasticity_and_break_even(self):
        r = discover_from_run(self.v1, "Choose a warehouse size (fictional)", design=F.warehouse_design(), at=AT)
        s = {x.candidate_id: x for x in r.candidates.sensitivities}[r.selected.id]
        # profit = sqft*(rent*occ - build); elasticity wrt rent = rent*occ/(rent*occ - build) = 10.2/2.2
        self.assertAlmostEqual(s.elasticities["rent"], 10.2 / 2.2, places=5)
        self.assertAlmostEqual(s.elasticities["build_cost"], -8 / 2.2, places=5)
        self.assertAlmostEqual(s.elasticities["sqft"], 1.0, places=5)
        self.assertAlmostEqual(s.break_even["rent"]["value"], 8 / 0.85, places=3)
        self.assertAlmostEqual(s.break_even["build_cost"]["value"], 10.2, places=3)
        self.assertIn("rent", s.high_sensitivity)

    def test_fragile(self):
        r = discover_from_run(self.v1, "Choose a lease (fictional)", design=F.fragile_design(), at=AT)
        fragile = [c for c in r.candidates.candidates if c.robustness == Robustness.FRAGILE]
        self.assertEqual(len(fragile), 1)
        self.assertNotEqual(fragile[0].status, CandidateStatus.SELECTED)


class Optimization(Base):
    def solve(self, d, **kw):
        ctx = self.ctx()
        return optimize(ctx, ctx.ensure(problem_from_json(d, AT)), at=AT, **kw)

    def test_linear_form(self):
        c, k = linear_form(from_json(add(mul(K(3), V("x")), sub(K(2), div(V("y"), K(4))))))
        self.assertEqual((float(c["x"]), float(c["y"]), float(k)), (3.0, -0.25, 2.0))
        for e in (mul(V("x"), V("y")), div(K(1), V("x")), {"op": "max", "args": [V("x"), V("y")]}):
            with self.subTest(expr=e), self.assertRaises(NotLinear):
                linear_form(from_json(e))

    def test_textbook_lp(self):
        r = self.solve(F.resource_design()["optimization"])
        self.assertEqual((r.status, r.solution, r.objective_value, r.verified),
                         (OptimizationStatus.OPTIMAL, {"x": 2.0, "y": 6.0}, 36.0, True))
        self.assertIn("reduced costs", r.proof)

    def test_infeasible_with_minimal_set(self):
        lp = F.resource_design()["optimization"]
        lp = {**lp, "constraints": lp["constraints"] + [rel(add(V("x"), V("y")), ">=", K(100))]}
        r = self.solve(lp)
        self.assertEqual(r.status, OptimizationStatus.INFEASIBLE)
        self.assertEqual(r.solution, {})
        ctx = self.ctx()
        self.assertEqual(minimal_infeasible_set(ctx.ensure(problem_from_json(lp, AT))), [2, 3])

    def test_unbounded_degenerate_free(self):
        unb = {"variables": [{"name": "x", "lower": 0}, {"name": "y", "lower": 0}], "objective": add(V("x"), V("y")),
               "direction": "maximize", "constraints": [rel(V("x"), ">=", K(1))]}
        self.assertEqual(self.solve(unb).status, OptimizationStatus.UNBOUNDED)
        deg = {"variables": [{"name": "x", "lower": 0}, {"name": "y", "lower": 0}], "objective": add(V("x"), V("y")),
               "direction": "maximize", "constraints": [rel(V("x"), "<=", K(1)), rel(V("y"), "<=", K(1)),
                                                        rel(add(V("x"), V("y")), "<=", K(2)),
                                                        rel(add(mul(K(2), V("x")), V("y")), "<=", K(3))]}
        r = self.solve(deg)
        self.assertEqual((r.status, r.objective_value), (OptimizationStatus.OPTIMAL, 2.0))
        free = {"variables": [{"name": "x"}], "objective": V("x"), "direction": "minimize",
                "constraints": [rel(V("x"), ">=", K(-5))]}
        self.assertEqual(self.solve(free).solution, {"x": -5.0})

    def test_integer_ties_and_grid(self):
        intp = {"variables": [{"name": "a", "lower": 0, "upper": 3, "integer": True},
                              {"name": "b", "lower": 0, "upper": 3, "integer": True}],
                "objective": add(V("a"), V("b")), "direction": "maximize",
                "constraints": [rel(add(V("a"), V("b")), "<=", K(3))]}
        r = self.solve(intp)
        self.assertEqual((r.status, r.solution), (OptimizationStatus.OPTIMAL, {"a": 0.0, "b": 3.0}))
        self.assertIn("4 points tie", r.proof)
        nl = {"variables": [{"name": "x", "lower": 0, "upper": 4}], "objective": mul(V("x"), sub(K(4), V("x"))),
              "direction": "maximize"}
        r = self.solve(nl)
        self.assertEqual(r.status, OptimizationStatus.FEASIBLE)
        self.assertNotIn("optimal", r.proof.replace("does not establish optimality", ""))

    def test_unsupported_and_timeout(self):
        nl_unbounded = {"variables": [{"name": "x", "lower": 0}], "objective": mul(V("x"), V("x")),
                        "direction": "maximize"}
        self.assertEqual(self.solve(nl_unbounded).status, OptimizationStatus.UNSUPPORTED)
        lp = {**F.resource_design()["optimization"], "solver": "exhaustive"}
        self.assertEqual(self.solve(lp).status, OptimizationStatus.UNSUPPORTED)
        self.assertEqual(self.solve({**lp, "solver": "quantum"}).status, OptimizationStatus.UNSUPPORTED)
        big = {"variables": [{"name": "x", "lower": 0, "upper": 1000}, {"name": "y", "lower": 0, "upper": 1000}],
               "objective": mul(V("x"), V("y")), "direction": "maximize", "solver": "grid"}
        self.assertEqual(self.solve(big, deadline_s=0.0).status, OptimizationStatus.TIMEOUT)

    def test_independent_recheck(self):
        ctx = self.ctx()
        p = ctx.ensure(problem_from_json(F.resource_design()["optimization"], AT))
        self.assertTrue(verify_solution(p, {"x": 2.0, "y": 6.0}, 36.0)[0])
        for bad in ({"x": 4.0, "y": 6.0}, {"x": -1.0, "y": 0.0}, {"x": 2.0}):
            self.assertFalse(verify_solution(p, bad)[0], bad)
        self.assertFalse(verify_solution(p, {"x": 2.0, "y": 6.0}, 37.0)[0])


class Candidates(Base):
    def test_design_space_validation(self):
        for bad in ({"unknown": 1}, {"value_metric": "profit"}, {"model": F.PROFIT_MODEL, "value_metric": "loss"},
                    {"distributions": {"x": {"dist": "uniform", "low": 0, "high": 1}}},
                    {"model": F.PROFIT_MODEL, "simulation": {"seed": -1}}, {"candidates": "all of them"}):
            with self.subTest(design=bad), self.assertRaises((MalformedInput, DiscoveryError)):
                DesignSpace.from_json(bad)
        with self.assertRaises(UnsupportedAlgorithm):
            DesignSpace.from_json({"model": F.PROFIT_MODEL, "distributions": {"occupancy": {"dist": "cauchy"}}})
        self.assertIn("ties go to the smallest candidate id", DecisionRule().text)
        with self.assertRaises(MalformedInput):
            DecisionRule.from_json({"maximize": "novelty"})

    def test_dominance(self):
        a = Candidate("A (fictional)", "x", technical_feasibility=1.0, expected_value=10.0, created_at=AT)
        b = Candidate("B (fictional)", "x", technical_feasibility=1.0, expected_value=5.0, created_at=AT)
        c = Candidate("C (fictional)", "x", technical_feasibility=0.5, expected_value=20.0, created_at=AT)
        self.assertTrue(dominates(a, b))
        self.assertFalse(dominates(a, c))
        self.assertFalse(dominates(c, a))
        self.assertFalse(dominates(a, a))

    def test_facts_bind_known_values_only(self):
        run = F.sensor_run()
        cid = next(iter(run.graph.claims))
        r = discover_from_run(run, "Size cooling (fictional)", design=F.engineering_design(cid), at=AT)
        self.assertIn(cid, r.selected.evidence_support)
        d = F.engineering_design(cid)
        d["candidates"][0]["parameters"]["temp"] = [30, "°C"]
        with self.assertRaises(MalformedInput):
            discover_from_run(run, "x (fictional)", design=d, at=AT)
        d = F.engineering_design("CL-notinmap0")
        with self.assertRaises(UnknownReference):
            discover_from_run(run, "x (fictional)", design=d, at=AT)


class Verifier(Base):
    def test_codes_are_known_and_only_lower(self):
        r = discover_from_run(self.v1, "Choose a warehouse size (fictional)", design=F.warehouse_design(), at=AT)
        for i in r.verifier.issues:
            self.assertIn(i.code, ISSUE_CODES)
        self.assertTrue(r.verifier.to_json()["checked"] > 0)

    def test_constraint_recheck_and_scope(self):
        ctx = self.ctx()
        fr = self.framed(ctx)
        res = evaluate_candidates(ctx, fr, DesignSpace.from_json(F.warehouse_design()), decide_now=False, at=AT)
        viable = next(c for c in res.candidates if c.status == CandidateStatus.VIABLE)
        scn = next(s for s in res.scenarios if s.candidate_id == viable.id)
        object.__setattr__(scn, "parameters", {**scn.parameters, "sqft": {"value": 999999, "unit": "sqft"}})
        rep = verify_discovery(ctx, fr.frame, ctx.objects(), scenarios=res.scenarios)
        self.assertIn("constraint-violation", rep.codes())
        self.assertEqual(viable.status, CandidateStatus.INFEASIBLE)
        wrong = Hypothesis("Elsewhere (fictional)", scope={"geography": "Tucson"}, supporting_claim_ids=[],
                           created_at=AT)
        rep = verify_discovery(ctx, fr.frame, [wrong])
        self.assertIn("missing-evidence", rep.codes())


class Receipt(Base):
    def test_reproducible_and_tamper_evident(self):
        a = discover_from_run(self.v1, "Choose a warehouse size (fictional)", design=F.warehouse_design(), at=AT)
        b = discover_from_run(self.v1, "Choose a warehouse size (fictional)", design=F.warehouse_design(),
                              at="2026-09-30T23:00:00+00:00")
        self.assertEqual(a.receipt["discovery_fingerprint"], b.receipt["discovery_fingerprint"])
        self.assertNotEqual(a.receipt["discovery_id"], b.receipt["discovery_id"])  # timestamps differ
        self.assertTrue(verify_discovery_receipt(a.receipt))
        self.assertEqual(objects_match_receipt(a.receipt, a.context.objects()), [])
        for key in list(a.receipt):
            t = copy.deepcopy(a.receipt)
            t[key] = None
            self.assertFalse(verify_discovery_receipt(t), key)
        cand = a.selected
        cand.risks = cand.risks + ["edited after issue"]
        self.assertTrue(objects_match_receipt(a.receipt, a.context.objects()))
        self.assertEqual(a.receipt["evidence"]["state_hash"], self.v1.receipt["state_hash"])
        self.assertEqual(a.receipt["evidence"]["knowledge_state_hash"], self.v1.receipt["knowledge_state_hash"])

    def test_ledger(self):
        led = DiscoveryLedger(0.01)
        led.record("s", "simulation", "x", 2.0)
        self.assertEqual(led.total_usd, 0.02)
        with self.assertRaises(MalformedInput):
            led.record("s", "teleport", "x")
        with self.assertRaises(MalformedInput):
            led.record("s", "compute", "x", -1)
        self.assertIn("hypothesis_generation", LEDGER_KINDS)


class Handoff(Base):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.result = discover_from_run(cls.v1, "Choose a warehouse size (fictional)", design=F.warehouse_design(),
                                       at=AT)

    def test_valid(self):
        h = self.result.handoff
        self.assertEqual(validate_handoff(h, self.result.receipt, self.result.context), [])
        self.assertEqual(h["selected_candidate"]["id"], self.result.receipt["selected_candidate_id"])
        self.assertTrue(all(e["kind"] == "verified_fact" for e in h["verified_evidence"]))
        self.assertTrue(all(o["kind"] == "simulated" for o in h["expected_outcomes"]))

    def test_documents_match_their_schemas(self):
        from lofgren_intelligence.discovery.schemas import validate as validate_schema
        r = json.loads(json.dumps(self.result.receipt))
        h = json.loads(json.dumps(self.result.handoff))
        self.assertEqual(validate_schema("discovery_receipt", r), [])
        self.assertEqual(validate_schema("v3_handoff", h), [])
        self.assertNotEqual(validate_schema("v3_handoff", {**h, "discovery_receipt_id": "DR-x"}), [])
        self.assertNotEqual(validate_schema("discovery_receipt", {**r, "outcome": "novel"}), [])

    def test_refusals(self):
        cases = {
            "schema": (lambda h: h.update(schema="x"), "not a"),
            "hidden number": (lambda h: h["dependencies"].append("needs 42 more permits"), "hidden untyped"),
            "hypothesis evidence": (lambda h: h["verified_evidence"].append({"claim_id": "HYP-1", "kind": "hypothesis"}),
                                    "only V1 known claims"),
            "criterion": (lambda h: h["acceptance_criteria"].append({"name": "x", "relation": {"op": "?"}}),
                          "not machine-evaluable"),
            "untyped criterion": (lambda h: h["acceptance_criteria"].append(
                {"name": "x", "relation": rel(V("zzz"), ">=", K(0))}), "untyped variables"),
            "receipt id": (lambda h: h.update(discovery_receipt_id="DR-0"), "does not match the receipt"),
            "status": (lambda h: h["selected_candidate"].update(status="viable"), "not selected"),
        }
        for name, (edit, fragment) in cases.items():
            with self.subTest(case=name):
                h = copy.deepcopy(self.result.handoff)
                edit(h)
                problems = validate_handoff(h, self.result.receipt, self.result.context)
                self.assertTrue(any(fragment in p for p in problems), problems)
                with self.assertRaises(HandoffInvalid):
                    check_handoff(h, self.result.receipt, self.result.context)

    def test_numbers_typed_elsewhere_are_allowed(self):
        h = copy.deepcopy(self.result.handoff)
        h["risks"].append("the plan assumes 150000 sqft")
        self.assertEqual(validate_handoff(h, self.result.receipt, self.result.context), [])


class Pipeline(Base):
    def test_outcomes(self):
        cases = {
            DiscoveryOutcome.CANDIDATE_SELECTED: (self.v1, F.warehouse_design()),
            DiscoveryOutcome.INFEASIBLE: (self.v1, F.infeasible_design()),
            DiscoveryOutcome.UNSUPPORTED: (self.v1, F.warehouse_design(
                distributions={"occupancy": {"dist": "cauchy", "x0": 0, "gamma": 1}})),
            DiscoveryOutcome.REQUIRES_RESEARCH: (self.v1, None),
            DiscoveryOutcome.CONTRADICTED: (C._run("Is warehouse vacancy in Tucson rising?",
                                                   C._docs(F.CONTRADICTED_DOCS)), F.warehouse_design()),
            DiscoveryOutcome.INSUFFICIENT_EVIDENCE: (C._run("Is warehouse vacancy in Tucson rising?",
                                                            C._docs(F.EMPTY_DOCS)), None),
        }
        for outcome, (run, design) in cases.items():
            with self.subTest(outcome=outcome.value):
                r = discover_from_run(run, "Test (fictional)", design=design, at=AT)
                self.assertEqual(r.outcome, outcome)
                self.assertEqual(r.handoff is not None, outcome == DiscoveryOutcome.CANDIDATE_SELECTED)
                self.assertTrue(verify_discovery_receipt(r.receipt))
        r = discover_from_run(self.v1, "Test (fictional)", design=F.warehouse_design(decision_rule={
            "min_technical_feasibility": 1.0, "min_economic_feasibility": 1.0}), at=AT)
        self.assertEqual(r.outcome, DiscoveryOutcome.UNKNOWN)

    def test_stages_and_reports(self):
        r = discover_from_run(self.v1, "Plan production (fictional)", design=F.resource_design(), at=AT)
        self.assertEqual(r.stages, {"imagine": "done", "simulate": "done", "optimize": "done"})
        md = render_discovery_markdown(r)
        self.assertIn("predictions", md)
        self.assertIn("not established", md)
        s = discovery_summary(r)
        self.assertEqual(s["outcome"], "candidate_selected")
        self.assertTrue(all(c["expected_value_kind"] == "simulated" for c in s["candidates"]))

    def test_reverify_is_a_separate_v1_run(self):
        r = discover_from_run(self.v1, "Test (fictional)", at=AT)
        req = r.requirements[0]
        before = json.dumps(self.v1.graph.to_json(), sort_keys=True)
        new = reverify(req, C._docs(C.AGREE_AND_CONFLICT))
        self.assertNotEqual(new.receipt["research_id"], self.v1.receipt["research_id"])
        self.assertTrue(verify_receipt(new.receipt))
        self.assertEqual(json.dumps(self.v1.graph.to_json(), sort_keys=True), before)
        self.assertFalse(any(c.origin.value == "hypothesis" for c in new.graph.claims.values()))

    def test_runs_on_a_v1_context_too(self):
        from lofgren_intelligence.kernel import export_state

        ctx = DiscoveryContext(export_state(self.v1))
        r = run_discovery(ctx, "Choose a warehouse size (fictional)", design=F.warehouse_design(), at=AT)
        self.assertEqual(r.receipt["evidence"]["assurance"], "degraded_v1")
        self.assertIsNone(r.receipt["evidence"]["state_hash"])
        self.assertEqual(r.outcome, DiscoveryOutcome.CANDIDATE_SELECTED)

    def test_relation_type(self):
        self.assertIsInstance(Relation.from_json(rel(V("a"), "<=", K(1))), Relation)
        self.assertEqual(HypothesisStatus.REQUIRES_RESEARCH.value, "requires_research")


if __name__ == "__main__":
    unittest.main()
