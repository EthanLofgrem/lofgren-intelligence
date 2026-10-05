"""The V2 invariants not covered in test_v2_foundations (01, 02, 08-11 live there), one named test each, run against
the full discovery pipeline. All data is fictional."""

from __future__ import annotations

import ast
import copy
import json
import unittest
from pathlib import Path

import lofgren_intelligence.certification as C
from lofgren_intelligence.discovery import DiscoveryContext, MalformedInput
from lofgren_intelligence.discovery import fixtures as F
from lofgren_intelligence.discovery.errors import DiscoveryError
from lofgren_intelligence.discovery.handoff import validate_handoff
from lofgren_intelligence.discovery.pipeline import discover_from_run, run_discovery
from lofgren_intelligence.discovery.receipt import verify_discovery_receipt
from lofgren_intelligence.discovery.report import render_discovery_markdown
from lofgren_intelligence.discovery.types import NOVELTY_CLAIMS, GapBasis, Hypothesis
from lofgren_intelligence.kernel import export_knowledge_map, verify_receipt
from lofgren_intelligence.models.provider import HeuristicProvider

AT = F.AT
DISCOVERY = Path(__file__).resolve().parent.parent / "lofgren_intelligence" / "discovery"


class Invariants(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.v1 = C._run(C.OBJECTIVE, C._docs(C.AGREE_AND_CONFLICT))
        cls.result = discover_from_run(cls.v1, "Choose a warehouse size (fictional)", design=F.warehouse_design(), at=AT)

    def test_invariant_03_generated_text_cannot_create_evidence(self):
        class Fabricator(HeuristicProvider):
            def propose_hypotheses(self, frame):
                return [{"statement": "Rents doubled (fictional)", "evidence": [{"id": "EV-fake", "content": "x"}],
                         "supporting_claim_ids": ["CL-fake"]}]

        ctx = DiscoveryContext(export_knowledge_map(self.v1), self.v1.receipt)
        before = [e["id"] for e in ctx.entities("evidence")]
        r = run_discovery(ctx, "x (fictional)", provider=Fabricator(), at=AT)
        self.assertEqual([e["id"] for e in ctx.entities("evidence")], before)
        self.assertFalse(any("EV-fake" in json.dumps(o.to_dict()) or "CL-fake" in json.dumps(o.to_dict())
                             for o in r.objects))

    def test_invariant_04_missing_evidence_cannot_become_negative_evidence(self):
        r = discover_from_run(self.v1, "x (fictional)", prior_art=F.PRIOR_ART_NONE, at=AT)
        for g in r.gaps.gaps:
            if g.basis == GapBasis.SEARCH_ABSENCE:
                self.assertLessEqual(g.confidence, 0.4)
                self.assertTrue(g.coverage_statement)
        with self.assertRaises(MalformedInput):
            Hypothesis("x", contradicting_claim_ids=["UNK-abc"])

    def test_invariant_05_copied_sources_cannot_create_independence(self):
        run = C._run("Is industrial vacancy in the Phoenix metro falling?", C._docs(C.SYNDICATED))
        r = discover_from_run(run, "x (fictional)", at=AT)
        self.assertEqual(r.framed.known_facts, ())
        self.assertTrue(run.lineage)

    def test_invariant_06_temporal_scope_is_preserved(self):
        run = C._run(C.OBJECTIVE, C._docs(C.SCOPED))
        r = discover_from_run(run, "x (fictional)", at=AT)
        f = r.framed.frame.scope
        for h in r.hypotheses.all:
            self.assertEqual((h.scope.valid_from, h.scope.valid_to), (f.valid_from, f.valid_to))
        for q in r.requirements:
            self.assertEqual(q.period, (f.valid_from, f.valid_to))

    def test_invariant_07_geographic_scope_is_preserved(self):
        f = self.result.framed.frame.scope
        for h in self.result.hypotheses.all:
            self.assertEqual(h.scope.geography, f.geography)
        for q in self.result.requirements:
            self.assertEqual(q.place, f.geography)

    def test_invariant_12_a_prior_art_miss_cannot_prove_novelty(self):
        r = discover_from_run(self.v1, "x (fictional)", prior_art=F.PRIOR_ART_NONE, at=AT)
        text = render_discovery_markdown(r) + json.dumps([a.to_dict() for a in r.prior_art])
        self.assertIsNone(NOVELTY_CLAIMS.search(text.replace("does not establish novelty", "")))
        self.assertIn("does not establish novelty", r.prior_art[0].statement)

    def test_invariant_13_every_discovery_conclusion_traces_to_structured_inputs(self):
        ids = {o.id for o in self.result.objects}
        ctx = self.result.context
        for f in self.result.findings:
            self.assertTrue(f.rests_on)
            for rid in f.rests_on:
                self.assertTrue(rid in ids or ctx.reference(rid) is not None)
        self.assertEqual(self.result.decision.selected_candidate_id, self.result.receipt["selected_candidate_id"])

    def test_invariant_14_every_cost_traces_to_an_operation(self):
        led = self.result.receipt["ledger"]
        self.assertAlmostEqual(sum(e["usd"] for e in led["entries"]), led["total_usd"])
        self.assertTrue(all(e["kind"] and e["actor"] and e["stage"] for e in led["entries"]))

    def test_invariant_15_every_receipt_detects_material_tampering(self):
        for key in ("objective", "evidence", "config", "objects", "ledger", "decision_rule", "outcome", "seeds"):
            t = copy.deepcopy(self.result.receipt)
            t[key] = {"tampered": True}
            self.assertFalse(verify_discovery_receipt(t), key)

    def test_invariant_16_switching_providers_does_not_change_the_domain_schema(self):
        class Canned(HeuristicProvider):
            name = "canned"

            def propose_hypotheses(self, frame):
                return [{"statement": "Rail access helps (fictional)"}]

        shapes = set()
        for p in (None, HeuristicProvider(), Canned()):
            r = discover_from_run(self.v1, "Choose a warehouse size (fictional)", design=F.warehouse_design(),
                                  provider=p, at=AT)
            shapes.add(json.dumps([sorted(r.hypotheses.all[0].to_dict()), sorted(r.selected.to_dict())]))
        self.assertEqual(len(shapes), 1)

    def test_invariant_17_unsupported_input_fails_closed(self):
        for bad in ({"model": {"name": "m"}}, {"candidates": [{"description": 5}], "model": F.PROFIT_MODEL},
                    {"constraints": [{"name": "c", "kind": "magic", "relation": {}}]}):
            with self.subTest(design=bad), self.assertRaises(DiscoveryError):
                discover_from_run(self.v1, "x (fictional)", design=bad, at=AT)

    def test_invariant_18_v2_cannot_invoke_execution_authority(self):
        forbidden = {"authority", "adapters", "requests", "urllib", "subprocess", "socket", "smtplib", "http"}
        for path in DISCOVERY.glob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom):
                    mod = (node.module or "").split(".")
                    if path.name in ("fixtures.py", "certification.py") and mod[:1] == ["adapters"]:
                        continue  # read-only fixtures and certification build V1 runs from local data
                    self.assertFalse(set(mod) & forbidden, f"{path.name} imports {node.module}")
                elif isinstance(node, ast.Import):
                    for a in node.names:
                        self.assertNotIn(a.name.split(".")[0], forbidden, f"{path.name} imports {a.name}")

    def test_invariant_19_the_v3_handoff_contains_no_hidden_untyped_assumptions(self):
        h = copy.deepcopy(self.result.handoff)
        self.assertEqual(validate_handoff(h, self.result.receipt, self.result.context), [])
        h["selected_candidate"]["description"] += " with 37 percent contingency"  # 37 is typed nowhere
        self.assertTrue(any("hidden untyped" in p for p in validate_handoff(h, self.result.receipt)))

    def test_invariant_20_v1_evidence_state_is_immutable_from_v2(self):
        run = C._run(C.OBJECTIVE, C._docs(C.AGREE_AND_CONFLICT))
        state = (json.dumps(run.graph.to_json(), sort_keys=True), run.receipt["state_hash"],
                 run.receipt["knowledge_state_hash"])
        r = discover_from_run(run, "x (fictional)", design=F.resource_design(), at=AT)
        self.assertEqual((json.dumps(run.graph.to_json(), sort_keys=True), run.receipt["state_hash"],
                          run.receipt["knowledge_state_hash"]), state)
        self.assertTrue(verify_receipt(run.receipt))
        self.assertEqual(r.receipt["evidence"]["state_hash"], run.receipt["state_hash"])


if __name__ == "__main__":
    unittest.main()
