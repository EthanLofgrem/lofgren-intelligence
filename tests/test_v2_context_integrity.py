"""V2 step 2: DiscoveryContext map validation, fingerprint, registry and reference integrity.

Complements tests/test_v2_context.py (the context API contract). Fixtures are fictional
(tests/fixtures/discovery).
"""

import copy
import json
import random
import unittest
from pathlib import Path

from lofgren_intelligence.adapters import AdapterRegistry, DocumentAdapter
from lofgren_intelligence.discovery import (
    ContextMismatch,
    DiscoveryError,
    DiscoveryObjective,
    DuplicateId,
    Gap,
    InvalidScope,
    KnownFact,
    MalformedInput,
    MissingEvidence,
    NonFiniteValue,
    ProblemFrame,
    PromotionRefused,
    UnknownReference,
    from_dict,
)
from lofgren_intelligence.discovery.context import (
    FINGERPRINT_ALGORITHM,
    OPTIONAL_DEFAULTS,
    DiscoveryContext,
    knowledge_map_fingerprint,
    thaw,
)
from lofgren_intelligence.discovery.frame import frame_problem
from lofgren_intelligence.discovery.gaps import detect_gaps
from lofgren_intelligence.intent.compiler import compile_intent
from lofgren_intelligence.kernel import export_state, run_investigation, verify_receipt

from .helpers import TEXTS

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "discovery"
AT = "2026-09-30T12:00:00+00:00"
OTHER_RR = "RR-" + "f" * 20


def load(name: str = "knowledge_map_phoenix.json") -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def objective(ctx: DiscoveryContext) -> DiscoveryObjective:
    return DiscoveryObjective("Find a warehouse opportunity (fictional)", ctx.research_id, created_at=AT)


class FingerprintTests(unittest.TestCase):
    def test_fingerprint_is_sha256_of_canonical_json(self):
        import hashlib

        ctx = DiscoveryContext(load())
        self.assertEqual(FINGERPRINT_ALGORITHM, "sha256/canonical-json-1")
        self.assertEqual(ctx.knowledge_map_fingerprint,
                         "KMF-" + hashlib.sha256(ctx.canonical_json().encode("utf-8")).hexdigest())
        self.assertEqual(ctx.knowledge_map_fingerprint, knowledge_map_fingerprint(load()))
        self.assertEqual(DiscoveryContext(json.dumps(load())).knowledge_map_fingerprint, ctx.knowledge_map_fingerprint)

    def test_reordered_but_identical_maps_share_a_fingerprint(self):
        original = load()
        shuffled = copy.deepcopy(original)
        rng = random.Random(3)
        for key in ("known", "uncertain", "contradicted", "contradictions", "unknowns", "findings", "questions"):
            rng.shuffle(shuffled[key])
        shuffled = {k: shuffled[k] for k in reversed(list(shuffled))}  # different key order
        shuffled["known"] = [{k: c[k] for k in reversed(list(c))} for c in shuffled["known"]]
        for c in shuffled["known"]:
            if isinstance(c["value"], (int, float)):
                c["value"] = float(c["value"])  # 14000000 vs 14000000.0
        self.assertEqual(knowledge_map_fingerprint(shuffled), knowledge_map_fingerprint(original))

    def test_any_material_change_changes_the_fingerprint(self):
        base = knowledge_map_fingerprint(load())
        edits = [
            lambda m: m["known"][0].__setitem__("statement", m["known"][0]["statement"] + " (edited)"),
            lambda m: m["known"][0].__setitem__("confidence", 0.5),
            lambda m: m["known"][0]["scope"].__setitem__("geography", "Tucson"),
            lambda m: m["known"][0]["evidence"].reverse(),  # inner arrays keep their order
            lambda m: m.__setitem__("objective", "Something else"),
        ]
        for i, edit in enumerate(edits):
            m = load()
            if i == 3 and len(m["known"][0]["evidence"]) < 2:
                continue
            edit(m)
            with self.subTest(edit=i):
                self.assertNotEqual(knowledge_map_fingerprint(m), base)

    def test_limitations_are_explicit(self):
        ctx = DiscoveryContext(load())
        joined = " ".join(ctx.limitations)
        self.assertIn("not V1's receipt state_hash", joined)
        self.assertIn("provenance", joined)
        self.assertNotIn("state_hash", [a for a in dir(ctx) if not a.startswith("_")])


class ImmutabilityTests(unittest.TestCase):
    def test_context_does_not_share_state_with_its_input(self):
        data = load()
        ctx = DiscoveryContext(data)
        before = ctx.knowledge_map_fingerprint
        data["known"][0]["statement"] = "changed by the caller after the context was built"
        data["known"].clear()
        self.assertEqual(ctx.knowledge_map_fingerprint, before)
        self.assertTrue(ctx.claims("known"))
        self.assertEqual(knowledge_map_fingerprint(thaw(ctx.knowledge_map)), before)

    def test_map_and_context_are_frozen(self):
        ctx = DiscoveryContext(load())
        with self.assertRaises(TypeError):
            ctx.knowledge_map["known"][0]["statement"] = "x"  # type: ignore[index]
        with self.assertRaises(AttributeError):
            ctx.knowledge_map["known"].append({})  # type: ignore[attr-defined]
        with self.assertRaises(MalformedInput):
            ctx.research_id = OTHER_RR  # type: ignore[misc]

    def test_v1_run_is_untouched(self):
        reg = AdapterRegistry()
        reg.register(DocumentAdapter(texts=TEXTS))
        run = run_investigation(compile_intent("Is industrial construction in the Phoenix metro increasing?"), reg)
        exported = copy.deepcopy(export_state(run))
        receipt = copy.deepcopy(run.receipt)
        ctx = DiscoveryContext(export_state(run))
        framed = frame_problem(ctx, objective(ctx), at=AT)
        detect_gaps(ctx, framed, at=AT)
        self.assertEqual(export_state(run), exported)
        self.assertEqual(run.receipt, receipt)
        self.assertTrue(verify_receipt(run.receipt))


class MapValidationTests(unittest.TestCase):
    def test_malformed_maps_fail_closed(self):
        def edited(fn):
            m = load()
            fn(m)
            return m

        cases = [
            (edited(lambda m: m.__setitem__("schema", "lofgren.knowledge-map/2")), MalformedInput),
            (edited(lambda m: m.__setitem__("research_id", "RR-123")), MalformedInput),
            (edited(lambda m: m.__setitem__("objective", 5)), MalformedInput),  # a recognized field, wrong type
            (edited(lambda m: m.pop("unknowns")), MalformedInput),
            (edited(lambda m: m["known"][0].__setitem__("status", "contested")), MalformedInput),
            (edited(lambda m: m["known"][0].pop("evidence")), MalformedInput),  # a field V2 reads
            (edited(lambda m: m["unknowns"][0].__setitem__("expected_gain", "high")), MalformedInput),
            (edited(lambda m: m["contradictions"][0].__setitem__("kind", "maybe")), MalformedInput),
            (edited(lambda m: m["known"][0].__setitem__("extension", float("inf"))), NonFiniteValue),
            (edited(lambda m: m["known"][0].__setitem__("confidence", 1.7)), MalformedInput),
            (edited(lambda m: m["known"][0]["scope"].__setitem__("valid_from", "2026-02-30")), InvalidScope),
            (edited(lambda m: m["uncertain"].append(copy.deepcopy(m["known"][0]))), (DuplicateId, MalformedInput)),
            (edited(lambda m: m["unknowns"][0].__setitem__("id", "CL-1")), MalformedInput),
            (edited(lambda m: m["known"][0]["evidence"].append("CL-1")), MalformedInput),
            ([], MalformedInput),
            ("{not json", MalformedInput),
            (json.dumps(load()).replace('"confidence": 0.', '"confidence": NaN, "x": 0.', 1), NonFiniteValue),
        ]
        for i, (data, err) in enumerate(cases):
            with self.subTest(case=i), self.assertRaises(err):
                DiscoveryContext(data)

    def test_additive_extensions_are_accepted_and_listed(self):
        m = load()
        m["known"][0]["exporter_note"] = "added by a newer V1 exporter"
        m["exporter_version"] = "1.1"
        ctx = DiscoveryContext(m)
        self.assertNotEqual(ctx.knowledge_map_fingerprint, knowledge_map_fingerprint(load()))  # still covered
        joined = " ".join(ctx.limitations)
        self.assertIn("exporter_note", joined)
        self.assertIn("exporter_version", joined)
        self.assertNotIn("exporter_note", ctx.claims("known")[0].get("statement", ""))

    def test_missing_optional_fields_use_documented_defaults(self):
        m = load()
        for u in m["unknowns"]:
            for k in ("capability", "expected_gain", "est_cost_usd"):
                u.pop(k)
        for cx in m["contradictions"]:
            cx.pop("kind")
        ctx = DiscoveryContext(m)
        u = ctx.entities("unknowns")[0]
        self.assertEqual((u["capability"], u["expected_gain"]), (OPTIONAL_DEFAULTS["unknowns"]["capability"],
                                                                 OPTIONAL_DEFAULTS["unknowns"]["expected_gain"]))
        self.assertEqual(ctx.entities("contradictions")[0]["kind"], "incompatible")
        joined = " ".join(ctx.limitations)
        self.assertIn("'expected_gain' not exported", joined)
        self.assertIn("'kind' not exported", joined)
        self.assertNotIn("kind", ctx.snapshot()["contradictions"][0])  # the map itself is unchanged
        framed = frame_problem(ctx, objective(ctx), at=AT)
        self.assertEqual(len(framed.missing_evidence), len(m["unknowns"]))

    def test_duplicate_ids_across_collections(self):
        m = load()
        m["contradicted"].append({**copy.deepcopy(m["known"][0]), "status": "contested", "contradictions": []})
        with self.assertRaises(DuplicateId):
            DiscoveryContext(m)

    def test_fuzzed_maps_fail_closed(self):
        rng = random.Random(1993)
        junk = [None, "", "x", -1, 2.5, True, [], {}, "CL-x", "RR-0", float("nan"), "2026-13-01", [None]]
        base = load()
        outcomes = {"error": 0, "valid": 0}
        for _ in range(1500):
            m = copy.deepcopy(base)
            target = m
            path = []
            for _ in range(rng.randint(1, 4)):
                if isinstance(target, dict) and target:
                    k = rng.choice(sorted(target))
                elif isinstance(target, list) and target:
                    k = rng.randrange(len(target))
                else:
                    break
                path.append((target, k))
                target = target[k]
            if not path:
                continue
            parent, key = path[-1]
            parent[key] = copy.deepcopy(rng.choice(junk))
            try:
                DiscoveryContext(m)
                outcomes["valid"] += 1
            except DiscoveryError:
                outcomes["error"] += 1
        self.assertGreater(outcomes["error"], 500)


class ReferenceIntegrityTests(unittest.TestCase):
    def setUp(self):
        self.ctx = DiscoveryContext(load())
        self.framed = frame_problem(self.ctx, objective(self.ctx), at=AT)
        self.known = self.ctx.claims("known")[0]
        self.uncertain = self.ctx.claims("uncertain")[0]

    def test_dangling_ids_are_rejected(self):
        with self.assertRaises(UnknownReference):
            self.ctx.resolve("CL-0000000000")
        with self.assertRaises(UnknownReference):
            self.ctx.register(MissingEvidence("UNK-0000000000", "made up", created_at=AT))
        with self.assertRaises(UnknownReference):
            self.ctx.register(Gap("data", "x", "y", "unknown", ["UNK-0000000000"], 1.0, created_at=AT))
        with self.assertRaises(UnknownReference):
            self.ctx.register(Gap("data", "x", "y", "unknown", ["HYP-0000000000"], 1.0, created_at=AT))

    def test_wrong_kinds_are_rejected(self):
        unk = self.ctx.entities("unknowns")[0]["id"]
        cx = self.ctx.entities("contradictions")[0]["id"]
        with self.assertRaises(MalformedInput):
            self.ctx.reference(unk, ("claim",))
        with self.assertRaises(UnknownReference):
            self.ctx.resolve(unk, ("CL",))
        with self.assertRaises(MalformedInput):
            self.ctx.register(ProblemFrame(self.framed.objective.id, contradiction_ids=[self.known["id"]],
                                           created_at=AT))
        with self.assertRaises(MalformedInput):
            self.ctx.register(ProblemFrame(self.framed.objective.id, known_ids=[self.framed.uncertainties[0].id],
                                           created_at=AT))
        with self.assertRaises(MalformedInput):
            self.ctx.register(ProblemFrame(self.framed.missing_evidence[0].id, created_at=AT))
        self.assertEqual(self.ctx.resolve(cx, ("CX",)).kind, "CX")
        self.assertEqual(self.ctx.reference(cx, ("contradiction",))["id"], cx)

    def test_uncertain_claims_cannot_stand_as_known(self):
        rec = self.uncertain
        with self.assertRaises(PromotionRefused):
            self.ctx.register(KnownFact(rec["id"], rec["statement"], thaw(rec["scope"]), rec["value"], rec["unit"],
                                        rec["policy"], rec["confidence"], created_at=AT))
        with self.assertRaises(PromotionRefused):
            self.ctx.register(ProblemFrame(self.framed.objective.id, known_ids=[rec["id"]], created_at=AT))

    def test_references_from_another_research_context(self):
        other = DiscoveryContext(load("knowledge_map_empty.json"))
        with self.assertRaises(ContextMismatch):
            other.register(self.framed.objective)
        with self.assertRaises(UnknownReference):
            other.register(self.framed.known_facts[0])  # its claim is not in the other map
        with self.assertRaises(UnknownReference):
            other.register(ProblemFrame(self.framed.frame.objective_id, created_at=AT))  # objective not here
        twin = load()
        twin["research_id"] = OTHER_RR  # same claims, another research run
        ctx2 = DiscoveryContext(twin)
        with self.assertRaises(ContextMismatch):
            ctx2.register(self.framed.objective)
        with self.assertRaises(UnknownReference):
            ctx2.register(self.framed.frame)  # its V2 parts were registered in another context

    def test_altered_maps_reject_objects_built_from_the_original(self):
        altered = load()
        altered["known"] = [c if c["id"] != self.known["id"] else {**c, "statement": c["statement"] + " (altered)"}
                            for c in altered["known"]]
        ctx2 = DiscoveryContext(altered)
        self.assertNotEqual(ctx2.knowledge_map_fingerprint, self.ctx.knowledge_map_fingerprint)
        fact = next(f for f in self.framed.known_facts if f.claim_id == self.known["id"])
        with self.assertRaises(ContextMismatch):
            ctx2.register(fact)

    def test_tampered_serialized_objects(self):
        fact = self.framed.known_facts[0]
        d = json.loads(json.dumps(fact.to_dict()))
        d["statement"] = "Something V1 never said."
        tampered = from_dict("known_fact", d)  # its id rests on the claim id, so it still parses
        fresh = DiscoveryContext(load())
        with self.assertRaises(ContextMismatch):
            fresh.register(tampered)
        miss = json.loads(json.dumps(self.framed.missing_evidence[0].to_dict()))
        miss["est_cost_usd"] = 0.0 if miss["est_cost_usd"] else 1.0
        with self.assertRaises(ContextMismatch):
            fresh.register(from_dict("missing_evidence", miss))
        frame = json.loads(json.dumps(self.framed.frame.to_dict()))
        frame["known_ids"] = frame["known_ids"][:-1]
        with self.assertRaises(MalformedInput):  # the id no longer matches the content
            from_dict("problem_frame", frame)

    def test_duplicate_references_and_registrations(self):
        with self.assertRaises(DuplicateId):
            ProblemFrame(self.framed.objective.id, known_ids=[self.framed.known_facts[0].id] * 2)
        with self.assertRaises(DuplicateId):
            self.ctx.register(self.framed.known_facts[0])
        fact = self.framed.known_facts[0]
        twin = KnownFact(fact.claim_id, fact.statement, fact.scope, fact.value, fact.unit, fact.policy,
                         fact.confidence, derived_from=[fact.claim_id], created_at=AT)
        with self.assertRaises(DuplicateId):
            self.ctx.ensure(twin)

    def test_internal_map_gaps_are_recorded_not_resolved(self):
        m = load()
        cx = m["contradictions"][0]
        dropped = cx["claim_a"]
        for key in ("known", "uncertain", "contradicted"):
            m[key] = [c for c in m[key] if c["id"] != dropped]
        ctx = DiscoveryContext(m)
        self.assertTrue(any(dropped in note and cx["id"] in note for note in ctx.limitations))
        with self.assertRaises(UnknownReference):
            ctx.resolve(dropped)

    def test_evidence_ids_resolve_without_provenance(self):
        ev = self.known["evidence"][0]
        self.assertIn(self.known["id"], self.ctx.evidence_claims(ev))
        self.assertIn(self.known["id"], self.ctx.reference(ev, ("evidence",))["claims"])
        with self.assertRaises(UnknownReference):
            self.ctx.resolve(ev)  # attachment only: no evidence record to resolve
        with self.assertRaises(MalformedInput):
            self.ctx.reference(ev, ("claim",))


if __name__ == "__main__":
    unittest.main()
