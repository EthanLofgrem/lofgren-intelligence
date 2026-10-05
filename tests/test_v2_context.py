"""V2 context tests: immutable V1 handoff and reference integrity."""
import copy, unittest
from lofgren_intelligence.discovery import DiscoveryContext, DuplicateId, MalformedInput, PromotionRefused, UnknownReference

def knowledge_map():
    return {
        "schema":"lofgren.knowledge-map/1","research_id":"RR-0123456789abcdef0123","objective":"Reduce spoilage",
        "mode":"investigate","questions":[],
        "known":[{"id":"CL-known","statement":"Spoilage is four percent.","type":"numeric","status":"verified",
                  "confidence":0.8,"confidence_status":"provisional","value":4.0,"unit":"%","scope":{},
                  "policy":"two independent sources","evidence":["EV-a","EV-b"],"issues":[],"calculation_id":None}],
        "uncertain":[{"id":"CL-uncertain","statement":"Zone 7 runs hot.","type":"state","status":"supported",
                      "confidence":0.5,"confidence_status":"provisional","value":None,"unit":"","scope":{},
                      "policy":"physical","evidence":["EV-c"],"issues":[],"calculation_id":None}],
        "contradicted":[],"contradictions":[{"id":"CX-1","claim_a":"CL-known","claim_b":"CL-uncertain"}],
        "unknowns":[{"id":"UNK-1","question_id":"Q-1","description":"Need temperature logs"}],
        "calculations":[{"id":"CALC-1","expression":"4 / 100"}],
        "findings":[{"id":"F-1","question_id":"Q-1","statement":"Spoilage is measurable"}],
        "rule":"V2 may hypothesize from this map; it may never promote a hypothesis into a verified finding."}

class DiscoveryContextTests(unittest.TestCase):
    def test_fingerprint_deterministic_across_key_order(self):
        a=knowledge_map(); b={k:a[k] for k in reversed(list(a))}
        self.assertEqual(DiscoveryContext(a).knowledge_map_fingerprint, DiscoveryContext(b).knowledge_map_fingerprint)
    def test_input_and_snapshot_mutation_cannot_mutate_context(self):
        source=knowledge_map(); ctx=DiscoveryContext(source); before=ctx.knowledge_map_fingerprint
        source["known"][0]["statement"]="tampered"; snap=ctx.snapshot(); snap["known"][0]["statement"]="also tampered"
        self.assertEqual("Spoilage is four percent.",ctx.resolve("CL-known").value["statement"])
        self.assertEqual(before,ctx.knowledge_map_fingerprint)
    def test_every_exported_v1_kind_resolves(self):
        ctx=DiscoveryContext(knowledge_map())
        for ref in ("CL-known","CL-uncertain","CX-1","UNK-1","CALC-1","F-1"):
            self.assertEqual(ref,ctx.resolve(ref).id)
    def test_dangling_wrong_kind_and_v2_ids_fail_closed(self):
        ctx=DiscoveryContext(knowledge_map())
        with self.assertRaises(UnknownReference): ctx.resolve("CL-missing")
        with self.assertRaises(UnknownReference): ctx.resolve("CL-known",("UNK",))
        with self.assertRaises(PromotionRefused): ctx.resolve("HYP-not-v1")
    def test_map_v1_evidence_is_attachment_only(self):
        ctx=DiscoveryContext(knowledge_map())
        self.assertEqual(("CL-known",),ctx.evidence_claims("EV-a"))
        with self.assertRaises(UnknownReference): ctx.resolve("EV-a")
        with self.assertRaises(UnknownReference): ctx.evidence_claims("EV-missing")
    def test_contexts_do_not_mix(self):
        a=DiscoveryContext(knowledge_map()); other=knowledge_map(); other["research_id"]="RR-fedcba98765432100123"
        with self.assertRaises(UnknownReference): a.assert_same_context(DiscoveryContext(other))
        changed=knowledge_map(); changed["objective"]="Different objective"
        with self.assertRaises(UnknownReference): a.assert_same_context(DiscoveryContext(changed))
    def test_duplicate_and_wrong_section_ids_fail_closed(self):
        bad=knowledge_map(); bad["known"].append(copy.deepcopy(bad["known"][0]))
        with self.assertRaises(DuplicateId): DiscoveryContext(bad)
        bad=knowledge_map(); bad["unknowns"][0]["id"]="CL-not-an-unknown"
        with self.assertRaises(MalformedInput): DiscoveryContext(bad)
    def test_non_json_and_nonfinite_maps_fail_closed(self):
        bad=knowledge_map(); bad["extra"]=object()
        with self.assertRaises(MalformedInput): DiscoveryContext(bad)
        bad=knowledge_map(); bad["known"][0]["confidence"]=float("nan")
        with self.assertRaises(MalformedInput): DiscoveryContext(bad)
    def test_resolve_many_rejects_duplicates(self):
        with self.assertRaises(DuplicateId): DiscoveryContext(knowledge_map()).resolve_many(["CL-known","CL-known"])
if __name__=="__main__": unittest.main()
