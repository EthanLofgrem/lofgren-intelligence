"""knowledge-map/2: export, canonical form, fingerprints, validation and receipt binding (hardening commit 5).

All data is fictional.
"""

from __future__ import annotations

import copy
import csv
import json
import random
import tempfile
import unittest
from pathlib import Path

from lofgren_intelligence.adapters import AdapterRegistry, DocumentAdapter, SensorAdapter
from lofgren_intelligence.certification import _run
from lofgren_intelligence.evidence import Evidence, EvidenceKind, Source, SourceKind
from lofgren_intelligence.intent.compiler import compile_intent
from lofgren_intelligence.kernel import export_state, run_investigation
from lofgren_intelligence.kernel.knowledge_map import (
    ENTITY_KEYS,
    MAP_SCHEMA,
    SECTIONS,
    KnowledgeMapError,
    canonicalize,
    compute_fingerprints,
    export_knowledge_map,
    json_schema,
    knowledge_map_problems,
    validate_knowledge_map,
)

from .helpers import TEXTS

PHOENIX = "Is industrial construction in the Phoenix metro increasing?"


def phoenix_run():
    reg = AdapterRegistry()
    reg.register(DocumentAdapter(texts=TEXTS))
    return run_investigation(compile_intent(PHOENIX), reg)


def sensor_run(path: Path):
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["timestamp", "sensor_id", "metric", "value", "unit", "calibrated_at"])
        w.writerow(["2026-09-01T00:00:00Z", "t-1", "temperature", "212", "F", "2026-06-01T00:00:00Z"])
        w.writerow(["2026-09-02T00:00:00Z", "t-1", "temperature", "32", "F", "2026-06-01T00:00:00Z"])
    reg = AdapterRegistry()
    reg.register(SensorAdapter([path], authorized=True))
    return _run("Is warehouse temperature changing?", reg)


def refingerprint(m: dict) -> dict:
    """What an attacker who knows the algorithm would do after editing a map."""
    m["fingerprint"] = compute_fingerprints(m)
    return m


def reorder_keys(value):
    if isinstance(value, dict):
        return {k: reorder_keys(value[k]) for k in reversed(list(value))}
    if isinstance(value, list):
        return [reorder_keys(v) for v in value]
    return value


class Export(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.result = phoenix_run()
        cls.map = export_knowledge_map(cls.result)

    def m(self) -> dict:
        return copy.deepcopy(self.map)

    def assertRefused(self, m, fragment: str, receipt=None):
        problems = knowledge_map_problems(m, receipt)
        self.assertTrue(any(fragment in p for p in problems), problems)
        with self.assertRaises(KnowledgeMapError):
            validate_knowledge_map(m, receipt)

    def test_export_is_valid_against_its_receipt(self):
        self.assertEqual(self.map["schema"], MAP_SCHEMA)
        self.assertEqual(knowledge_map_problems(self.map, self.result.receipt), [])
        self.assertEqual(self.map["receipt"]["state_hash"], self.result.receipt["state_hash"])

    def test_export_covers_the_whole_run(self):
        g = self.result.graph
        self.assertEqual({c["id"] for c in self.map["claims"]}, set(g.claims))
        self.assertEqual({e["id"] for e in self.map["evidence"]}, set(g.evidence))
        self.assertEqual({s["id"] for s in self.map["sources"]}, set(g.sources))
        self.assertEqual({f["id"] for f in self.map["findings"]}, {f.id for f in self.result.findings})
        self.assertEqual(len(self.map["unknowns"]), len(self.result.unknowns))
        self.assertTrue(self.map["limitations"])

    def test_published_schema_matches_the_export(self):
        schema = json_schema()
        self.assertEqual(set(self.map), set(schema["required"]))
        for key in SECTIONS:
            for e in self.map[key]:
                self.assertEqual(set(e), set(ENTITY_KEYS[key]), key)
        published = Path(__file__).resolve().parent.parent / "schemas" / "knowledge-map-2.schema.json"
        self.assertEqual(json.loads(published.read_text(encoding="utf-8")), schema)

    def test_json_round_trip(self):
        text = json.dumps(self.map)
        self.assertEqual(validate_knowledge_map(text, self.result.receipt), self.map)
        self.assertEqual(validate_knowledge_map(text.encode("utf-8")), self.map)

    def test_export_does_not_change_the_run(self):
        run = phoenix_run()
        before = (json.dumps(run.graph.to_json(), sort_keys=True), json.dumps(run.receipt, sort_keys=True, default=str),
                  json.dumps(export_state(run), sort_keys=True, default=str))
        export_knowledge_map(run)
        self.assertEqual((json.dumps(run.graph.to_json(), sort_keys=True),
                          json.dumps(run.receipt, sort_keys=True, default=str),
                          json.dumps(export_state(run), sort_keys=True, default=str)), before)

    def test_evidence_manifest_has_no_content_or_data(self):
        for e in self.map["evidence"]:
            self.assertFalse({"content", "data"} & set(e))
            self.assertEqual(e["content_hash"], self.result.graph.evidence[e["id"]].content_hash)

    # Question associations round-trip; the legacy single question_id does not travel.
    def test_question_associations_round_trip(self):
        for c in validate_knowledge_map(json.dumps(self.map))["claims"]:
            self.assertEqual(c["question_ids"], self.result.graph.claims[c["id"]].question_ids)
            self.assertNotIn("question_id", c)

    # Why a claim fell short is readable: the verifier's factors next to its policy's thresholds.
    def test_assessment_records_what_the_verifier_used(self):
        self.assertEqual(self.map["evidence_standard"], {"min_independent_sources": 2, "min_confidence": 0.7})
        for c in self.map["claims"]:
            factors = self.result.factors[c["id"]]
            self.assertEqual(c["assessment"]["factors"], canonicalize(dict(factors.__dict__)))
            req = c["assessment"]["requirements"]
            self.assertEqual(req["name"], c["policy"])
            if c["status"] == "verified" and not req["requires_observation"]:
                self.assertGreaterEqual(factors.independent_sources, req["min_independent_sources"])
                self.assertGreaterEqual(factors.calibrated, req["min_confidence"])

    def test_tampered_assessment_is_refused(self):
        m = self.m()
        m["claims"][0]["assessment"]["factors"]["independent_sources"] = 9
        self.assertRefused(m, "altered after export")
        m = self.m()
        m["claims"][0]["assessment"]["requirements"]["name"] = "lenient-1"
        self.assertRefused(refingerprint(m), "is not the claim's policy")
        m = self.m()
        m["claims"][0]["assessment"]["requirements"] = None
        self.assertRefused(refingerprint(m), "only a hypothesis is held to no evidence policy")

    # ---- fingerprint ----
    def test_key_order_does_not_matter(self):
        m = reorder_keys(self.m())
        self.assertEqual(compute_fingerprints(m), self.map["fingerprint"])
        self.assertEqual(knowledge_map_problems(m, self.result.receipt), [])

    def test_collection_order_does_not_matter(self):
        m = self.m()
        rng = random.Random(7)
        for key in ("questions", "claims", "evidence", "sources", "unknowns", "findings", "lineage"):
            rng.shuffle(m[key])
        self.assertEqual(compute_fingerprints(m), self.map["fingerprint"])
        self.assertEqual(knowledge_map_problems(m, self.result.receipt), [])

    def test_ranking_order_matters(self):
        m = self.m()
        f = next(f for f in m["findings"] if len(f["claim_ids"]) >= 2)
        f["claim_ids"].reverse()
        self.assertNotEqual(compute_fingerprints(m)["map"], self.map["fingerprint"]["map"])
        self.assertRefused(m, "altered after export")
        # Re-fingerprinted, it is a well-formed map, but not the map of this run's receipt.
        self.assertEqual(knowledge_map_problems(refingerprint(m)), [])
        self.assertRefused(m, "claim_ids differ from the receipt", self.result.receipt)

    def test_tampering_changes_the_fingerprint(self):
        edits = {
            "claim status": lambda m: m["claims"][0].update(status="verified" if m["claims"][0]["status"] != "verified"
                                                            else "supported"),
            "claim confidence": lambda m: m["claims"][0].update(confidence=0.123),
            "evidence hash": lambda m: m["evidence"][0].update(content_hash="0" * 64),
            "source uri": lambda m: m["sources"][0].update(uri="inline:elsewhere"),
            "finding answer": lambda m: m["findings"][0].update(answer="Something else."),
            "limitations": lambda m: m["limitations"].pop(),
        }
        for name, edit in edits.items():
            with self.subTest(edit=name):
                m = self.m()
                edit(m)
                self.assertRefused(m, "altered after export")

    def test_content_fingerprint_ignores_only_per_run_fields(self):
        m = self.m()
        for s in m["sources"]:
            s["retrieved_at"] = "2030-01-01T00:00:00+00:00"
        fp = compute_fingerprints(m)
        self.assertEqual(fp["content"], self.map["fingerprint"]["content"])
        self.assertNotEqual(fp["map"], self.map["fingerprint"]["map"])

    def test_rerun_reproduces_the_content_fingerprint(self):
        again = export_knowledge_map(phoenix_run())
        self.assertEqual(again["fingerprint"]["content"], self.map["fingerprint"]["content"])

    def test_malformed_fingerprint(self):
        m = self.m()
        m["fingerprint"]["map"] = "KM2-xyz"
        self.assertRefused(m, "malformed fingerprint")
        m = self.m()
        m["fingerprint"]["algorithm"] = "md5"
        self.assertRefused(m, "fingerprint.algorithm")

    # ---- identity of the run and the protocol ----
    def test_wrong_version(self):
        for schema in ("lofgren.knowledge-map/1", "lofgren.knowledge-map/3", None):
            with self.subTest(schema=schema):
                m = self.m()
                m["schema"] = schema
                self.assertRefused(refingerprint(m), "expected 'lofgren.knowledge-map/2'")

    def test_wrong_research_id(self):
        m = self.m()
        m["research_id"] = "RR-" + "0" * 20
        self.assertRefused(refingerprint(m), "does not match the map's research_id")
        m["receipt"]["research_id"] = m["research_id"]
        self.assertEqual(knowledge_map_problems(refingerprint(m)), [])
        self.assertRefused(m, "another run's map", self.result.receipt)
        m["research_id"] = m["receipt"]["research_id"] = "RR-not-an-id"
        self.assertRefused(refingerprint(m), "must be a V1 research id")

    # A re-fingerprinted edit is well formed, but every field the receipt records must still match it.
    def test_receipt_binds_every_field_it_records(self):
        edits = {
            "question dependency": (lambda m: next(q for q in m["questions"] if q["depends_on"])["depends_on"].clear(),
                                    "depends_on differ"),
            "question text": (lambda m: m["questions"][0].update(text="Another question?"), "text differ"),
            "unknown status": (lambda m: m["unknowns"][0].update(status="closed"), "status differ"),
            "source title": (lambda m: m["sources"][0].update(title="Another title"), "title differ"),
            "evidence time": (lambda m: m["evidence"][0].update(observed_at="2020-01-01"), "observed_at differ"),
            "claim issues": (lambda m: m["claims"][0]["issues"].append("added"), "issues differ"),
            "confidence status": (lambda m: m["claims"][0].update(confidence_status="calibrated"),
                                  "does not match the receipt's verifier"),
            "evidence standard": (lambda m: m["evidence_standard"].update(min_confidence=0.1),
                                  "does not match the receipt's contract"),
            "lineage": (lambda m: m["lineage"].append({"source": m["sources"][0]["id"],
                                                       "derived_from": m["sources"][1]["id"],
                                                       "relation": "declared", "detail": ""}), "lineage"),
        }
        for name, (edit, fragment) in edits.items():
            with self.subTest(edit=name):
                m = self.m()
                edit(m)
                self.assertRefused(refingerprint(m), fragment, self.result.receipt)

    def test_claim_fields_are_bound_through_claim_identity(self):
        for field_name, value in (("value", 99.0), ("unit", "furlongs"), ("polarity", -1), ("subject", "other"),
                                  ("statement", "Something else (fictional).")):
            with self.subTest(field=field_name):
                m = self.m()
                self.assertNotEqual(m["claims"][0][field_name], value)
                m["claims"][0][field_name] = value
                self.assertRefused(refingerprint(m), "does not match the claim it describes")

    def test_another_runs_receipt_is_refused(self):
        other = sensor_run(Path(tempfile.mkdtemp()) / "s.csv")
        self.assertRefused(self.m(), "does not match the receipt", other.receipt)

    def test_tampered_receipt_is_refused(self):
        receipt = copy.deepcopy(self.result.receipt)
        receipt["state_hash"] = "0" * 64
        self.assertRefused(self.m(), "not intact", receipt)

    def test_graph_changed_after_receipt_is_not_exported(self):
        run = phoenix_run()
        claim = next(iter(run.graph.claims.values()))
        src = run.graph.add_source(Source(SourceKind.DOCUMENT, "later note", uri="inline:later", quality=0.5))
        ev = run.graph.add_evidence(Evidence(src.id, EvidenceKind.DOCUMENT, "A later, fictional note."))
        run.graph.link(ev.id, claim.id, "supports")
        with self.assertRaises(KnowledgeMapError):
            export_knowledge_map(run)

    def test_unfinished_run_is_not_exported(self):
        run = phoenix_run()
        run.receipt = {}
        with self.assertRaises(KnowledgeMapError):
            export_knowledge_map(run)

    # ---- values ----
    def test_malformed_dates(self):
        for where, bad in (("evidence.observed_at", "2026-13-45"), ("sources.retrieved_at", "yesterday"),
                           ("claims.scope.valid_from", "2026-02-30"), ("evidence.valid_to", 20260101)):
            with self.subTest(where=where, value=bad):
                m = self.m()
                section, *path = where.split(".")
                target = m[section][0]
                for p in path[:-1]:
                    target = target[p]
                target[path[-1]] = bad
                self.assertRefused(refingerprint(m), path[-1])

    def test_valid_dates_are_accepted(self):
        m = self.m()
        m["evidence"][0]["observed_at"] = "2026-09-02T00:00:00Z"
        m["claims"][0]["scope"]["valid_from"] = "2026-01-01"
        self.assertEqual(knowledge_map_problems(refingerprint(m)), [])

    def test_non_finite_values(self):
        text = json.dumps(self.map).replace(f'"confidence": {json.dumps(self.map["claims"][0]["confidence"])}',
                                            '"confidence": NaN', 1)
        self.assertIn("NaN", text)
        self.assertRefused(text, "not a finite number")
        m = self.m()
        m["claims"][0]["value"] = float("inf")
        self.assertRefused(m, "not finite")
        with self.assertRaises(KnowledgeMapError):
            refingerprint(m)

    def test_numbers_have_one_spelling(self):
        self.assertEqual(canonicalize({"a": 14000000.0}), {"a": 14000000})
        with self.assertRaises(KnowledgeMapError):
            canonicalize(2 ** 60)

    def test_strict_shape(self):
        m = self.m()
        m["extra"] = 1
        self.assertRefused(refingerprint(m), "keys not defined")
        m = self.m()
        del m["limitations"]
        self.assertRefused(m, "missing keys")
        m = self.m()
        m["claims"].append(copy.deepcopy(m["claims"][0]))
        self.assertRefused(refingerprint(m), "duplicate id")

    # ---- references ----
    def test_dangling_references(self):
        edits = {
            "claim evidence": (lambda m: m["claims"][0]["supporting"].append("EV-missing00"), "EV-missing00"),
            "claim question": (lambda m: m["claims"][0]["question_ids"].append("Q-zzzzmissing"), "Q-zzzzmissing"),
            "evidence source": (lambda m: m["evidence"][0].update(source_id="SRC-missing0"), "SRC-missing0"),
            "finding claim": (lambda m: m["findings"][0]["claim_ids"].append("CL-missing00"), "CL-missing00"),
            "finding unknown": (lambda m: m["findings"][0]["unknown_ids"].append("UNK-missing0"), "UNK-missing0"),
            "question dependency": (lambda m: m["questions"][0]["depends_on"].append("Q-missing000"), "Q-missing000"),
        }
        for name, (edit, ref) in edits.items():
            with self.subTest(edit=name):
                m = self.m()
                edit(m)
                self.assertRefused(refingerprint(m), f"{ref} is not in this map")

    def test_dangling_contradiction_endpoint(self):
        m = self.m()
        m["contradictions"].append({"id": "CX-fictional0", "claim_a": m["claims"][0]["id"], "claim_b": "CL-missing00",
                                    "reason": "", "kind": "incompatible", "scope_note": "", "resolution": "",
                                    "severity": 1})
        self.assertRefused(refingerprint(m), "CL-missing00 is not in this map")

    def test_promoted_hypothesis_is_refused(self):
        m = self.m()
        m["claims"][0].update(origin="hypothesis", status="verified")
        self.assertRefused(refingerprint(m), "a hypothesis is never verified")

    def test_finding_evidence_must_come_from_its_claims(self):
        m = self.m()
        f, stray = next((f, e["id"]) for f in m["findings"] for e in m["evidence"]
                        if not any(e["id"] in c["supporting"] for c in m["claims"] if c["id"] in f["claim_ids"]))
        f["evidence_ids"].append(stray)
        self.assertRefused(refingerprint(m), "lists evidence none of its claims rests on")

    def test_v1_map_is_frozen(self):
        state = export_state(self.result)
        self.assertEqual(state["schema"], "lofgren.knowledge-map/1")
        self.assertEqual(set(state), {"schema", "research_id", "objective", "mode", "questions", "known", "uncertain",
                                      "contradicted", "contradictions", "unknowns", "calculations", "findings", "rule"})
        for f in state["findings"]:
            self.assertFalse({"question_ids", "derivation", "claim_scopes"} & set(f))
        for c in state["known"] + state["uncertain"] + state["contradicted"]:
            self.assertFalse({"question_ids", "origin", "polarity", "identity_version"} & set(c))


class Derivation(unittest.TestCase):
    """A support question that declares depends_on gets its answer through that dependency (sensor run)."""

    @classmethod
    def setUpClass(cls):
        cls.dir = tempfile.TemporaryDirectory()
        cls.path = Path(cls.dir.name) / "sensors.csv"
        cls.result = sensor_run(cls.path)
        cls.map = export_knowledge_map(cls.result)

    @classmethod
    def tearDownClass(cls):
        cls.dir.cleanup()

    def dependency_finding(self, m: dict) -> dict:
        return next(f for f in m["findings"] if f["derivation"] == "dependency")

    def test_dependency_derivation_round_trips(self):
        m = validate_knowledge_map(json.dumps(self.map), self.result.receipt)
        f = self.dependency_finding(m)
        q = next(q for q in m["questions"] if q["id"] == f["question_id"])
        self.assertTrue(f["claim_scopes"])
        for cid, via in f["claim_scopes"].items():
            claim = next(c for c in m["claims"] if c["id"] == cid)
            # consumer question -> declared depends_on -> source question -> claim
            self.assertTrue(set(via) <= set(q["depends_on"]))
            self.assertTrue(set(via) <= set(claim["question_ids"]))
            self.assertNotIn(f["question_id"], claim["question_ids"])
        original = next(x for x in self.result.findings if x.id == f["id"])
        self.assertEqual((f["question_ids"], f["claim_scopes"]), (original.question_ids, original.claim_scopes))

    def test_removing_the_dependency_edge_is_refused(self):
        m = copy.deepcopy(self.map)
        f = self.dependency_finding(m)
        next(q for q in m["questions"] if q["id"] == f["question_id"])["depends_on"] = []
        self.assertTrue(any("without a declared dependency" in p for p in knowledge_map_problems(refingerprint(m))))

    def test_mislabelled_derivation_is_refused(self):
        m = copy.deepcopy(self.map)
        f = self.dependency_finding(m)
        f.update(derivation="direct")
        self.assertTrue(any("direct finding" in p for p in knowledge_map_problems(refingerprint(m))))
        m = copy.deepcopy(self.map)
        f = self.dependency_finding(m)
        cid = next(iter(f["claim_scopes"]))
        f["claim_scopes"][cid] = [f["question_id"]]
        self.assertTrue(any("came through" in p for p in knowledge_map_problems(refingerprint(m))))

    # The certification run that looked nondeterministic (Run 9) reads its sensor log from a fresh temporary
    # directory each time. A source's identity includes its URI, so the inputs really differ.
    def test_same_file_same_place_reproduces(self):
        again = export_knowledge_map(sensor_run(self.path))
        self.assertEqual(again["fingerprint"]["content"], self.map["fingerprint"]["content"])

    def test_same_content_elsewhere_is_another_source(self):
        with tempfile.TemporaryDirectory() as d:
            moved = export_knowledge_map(sensor_run(Path(d) / "sensors.csv"))
        self.assertNotEqual(moved["fingerprint"]["content"], self.map["fingerprint"]["content"])
        self.assertNotEqual([s["uri"] for s in moved["sources"]], [s["uri"] for s in self.map["sources"]])
        self.assertEqual([e["content_hash"] for e in moved["evidence"]],
                         [e["content_hash"] for e in self.map["evidence"]])
        # The receipt's inputs_hash does not see the difference: a stated limitation of the receipt.
        self.assertEqual(moved["receipt"]["inputs_hash"], self.map["receipt"]["inputs_hash"])
        self.assertTrue(any("inputs_hash" in x for x in self.map["limitations"]))


if __name__ == "__main__":
    unittest.main()
