"""V3 Production Intelligence: specification compiler, planner, code generation, probes, independent verifier,
upstream binding, receipt and V4 handoff, disk round trip, CLI and MCP. All data is fictional."""

from __future__ import annotations

import contextlib
import copy
import io
import json
import tempfile
import unittest
from pathlib import Path

from lofgren_intelligence.certification import AGREE_AND_CONFLICT, OBJECTIVE
from lofgren_intelligence.discovery import fixtures as F
from lofgren_intelligence.discovery.expr import Relation, env_of
from lofgren_intelligence.mcp.server import PRODUCTION_TOOLS, Server
from lofgren_intelligence.production import (
    AUTHORITY_REQUIRED_ACTIONS,
    KIND_FILES,
    ProductionError,
    SpecificationError,
    build_artifact,
    check_production_receipt,
    compile_specification,
    validate_v4_handoff,
    verify_artifact,
    verify_directory,
    write_artifact,
)
from lofgren_intelligence.production import codegen
from lofgren_intelligence.production.certification import _domain, _fixture, _rehash
from lofgren_intelligence.production.sandbox import run_tests
from lofgren_intelligence.production.upstream import handoff_integrity_problems


def _file(artifact: dict, path: str) -> dict:
    return next(f for f in artifact["files"] if f["path"] == path)


class Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.context, cls.discovery = _fixture()
        cls.handoff = cls.discovery.handoff
        cls.result = cls.build()

    @classmethod
    def build(cls, kind="structured_bundle", handoff=None):
        return build_artifact(handoff or cls.handoff, discovery_receipt=cls.discovery.receipt, context=cls.context,
                              kind=kind)


class SpecificationTests(Base):
    def test_requirements_are_numbered_by_kind_and_stable(self):
        spec = compile_specification(self.handoff)
        ids = spec.ids()
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual(spec.ids("specification")[:2], ["REQ-SPEC-001", "REQ-SPEC-002"])
        self.assertEqual(spec.ids("acceptance"), ["REQ-ACC-001", "REQ-ACC-002"])
        self.assertEqual(spec.ids("constraint"), ["REQ-CON-001"])
        self.assertEqual(spec.ids("test"), ["REQ-TST-001"])
        self.assertEqual(compile_specification(self.handoff).as_list(), spec.as_list())

    def test_values_cover_specifications_and_simulated_outcomes(self):
        spec = compile_specification(self.handoff)
        self.assertEqual(spec.values["sqft"], (150000.0, "sqft"))
        self.assertEqual(spec.values["profit"][1], "usd")

    def test_constraints_over_specified_values_become_checks(self):
        spec = compile_specification(self.handoff)
        self.assertEqual([k for _, k, _, _ in spec.checks], ["acceptance", "acceptance", "constraint"])

    def test_constraint_over_an_unspecified_variable_stays_documented(self):
        h = copy.deepcopy(self.handoff)
        h["constraints"].append({"id": "CON-x", "name": "design-time", "relation": {
            "lhs": {"op": "var", "name": "design_only"}, "op": "<=", "rhs": {"op": "const", "value": 1}}})
        spec = compile_specification(h)
        self.assertEqual(len(spec.ids("constraint")), 2)
        self.assertEqual(len([c for c in spec.checks if c[1] == "constraint"]), 1)

    def test_malformed_specifications_are_refused_with_every_problem(self):
        h = copy.deepcopy(self.handoff)
        h["specifications"].append(dict(h["specifications"][0]))
        h["specifications"][1]["value"] = float("nan")
        with self.assertRaises(SpecificationError) as cm:
            compile_specification(h)
        self.assertIn("duplicate", str(cm.exception))
        self.assertIn("not finite", str(cm.exception))
        self.assertTrue(issubclass(SpecificationError, ProductionError))

    def test_criterion_units_must_agree(self):
        h = copy.deepcopy(self.handoff)
        h["acceptance_criteria"][0]["relation"]["rhs"]["unit"] = "sqft"
        with self.assertRaises(SpecificationError):
            compile_specification(h)


class CodegenTests(Base):
    def test_expression_source_matches_v2_on_every_operator(self):
        V = {"a": 3.0, "b": -2.0}
        nodes = {
            "add": ({"op": "add", "args": [{"op": "var", "name": "a"}, {"op": "var", "name": "b"}]}, 1.0),
            "sub": ({"op": "sub", "args": [{"op": "var", "name": "a"}, {"op": "var", "name": "b"}]}, 5.0),
            "mul": ({"op": "mul", "args": [{"op": "var", "name": "a"}, {"op": "var", "name": "b"}]}, -6.0),
            "div": ({"op": "div", "args": [{"op": "var", "name": "a"}, {"op": "var", "name": "b"}]}, -1.5),
            "min": ({"op": "min", "args": [{"op": "var", "name": "a"}, {"op": "var", "name": "b"}]}, -2.0),
            "max": ({"op": "max", "args": [{"op": "var", "name": "a"}, {"op": "var", "name": "b"}]}, 3.0),
            "neg": ({"op": "neg", "args": [{"op": "var", "name": "a"}]}, -3.0),
            "pow": ({"op": "pow", "args": [{"op": "var", "name": "b"}], "exponent": 3}, -8.0),
            "const": ({"op": "const", "value": 7}, 7.0),
        }
        for op, (node, want) in nodes.items():
            with self.subTest(op=op):
                self.assertEqual(eval(codegen.expr_source(node), {"V": V}), want)  # noqa: S307 - generated, trusted

    def test_relation_tolerance_matches_v2(self):
        for op in ("<=", ">=", "=="):
            rel = {"lhs": {"op": "var", "name": "x"}, "op": op, "rhs": {"op": "const", "value": 1}}
            body = "\n".join("    " + line for line in codegen.relation_source(rel))
            ns = {"TOLERANCE": 1e-9}
            exec(f"def f(V):\n{body}", ns)  # noqa: S102 - generated from a validated relation
            for x in (0.0, 1.0 - 1e-10, 1.0, 1.0 + 1e-10, 2.0):
                with self.subTest(op=op, x=x):
                    want = Relation.from_json(rel).check(env_of({"x": x})).satisfied
                    self.assertEqual(ns["f"]({"x": x}), want)

    def test_bad_expression_is_refused(self):
        with self.assertRaises(Exception):
            codegen.expr_source({"op": "exec", "args": []})

    def test_each_kind_has_its_files(self):
        for kind, paths in KIND_FILES.items():
            with self.subTest(kind=kind):
                r = self.build(kind)
                self.assertEqual([f["path"] for f in r.artifact["files"]], list(paths))

    def test_probes_include_expected_failures_for_every_check(self):
        acc = json.loads(_file(self.result.artifact, "acceptance.json")["content"])
        self.assertEqual(set(acc["falsified_by_probes"]), {x.requirement_id for x in self.result.acceptance})
        self.assertTrue(any(not ok for p in acc["probes"] for ok in p["expected"].values()))


class VerifierTests(Base):
    def test_generated_tests_ran(self):
        t = self.result.verification.tests
        manifest = json.loads(_file(self.result.artifact, "artifact.json")["content"])
        self.assertTrue(t["ok"])
        self.assertEqual(t["ran"], codegen.expected_test_count(manifest))
        self.assertIsNone(self.build("markdown").verification.tests)

    def test_rehashed_edits_are_caught_by_regeneration(self):
        for path in ("README.md", "artifact.py", "test_artifact.py", "plan.json", "acceptance.json"):
            with self.subTest(path=path):
                bad = copy.deepcopy(self.result.artifact)
                _file(bad, path)["content"] += "\n"
                check = verify_artifact(_rehash(bad))
                self.assertFalse(check.passed)
                self.assertTrue(any("regeneration" in p for p in check.problems), check.problems)

    def test_manifest_edit_with_rebuilt_files_still_fails_fingerprint_or_checks(self):
        bad = copy.deepcopy(self.result.artifact)
        m = json.loads(_file(bad, "artifact.json")["content"])
        m["objective"] = "Something else"
        for path, media, content in codegen.generate(m, bad["kind"]):
            f = _file(bad, path)
            f["content"], f["media_type"] = content, media
        check = verify_artifact(_rehash(bad))
        self.assertFalse(check.passed)
        self.assertIn("artifact fingerprint does not match its contents", check.problems)

    def test_broken_check_code_fails_in_the_sandbox(self):
        files = copy.deepcopy(self.result.artifact["files"])
        py = _file({"files": files}, "artifact.py")
        py["content"] = py["content"].replace("return rhs - lhs >= -TOLERANCE", "return True")
        run = run_tests(files)
        self.assertFalse(run.ok)  # the probe test catches a vacuous check
        self.assertGreaterEqual(run.failures, 1)

    def test_verifier_never_raises_on_garbage(self):
        for junk in (None, [], {}, {"files": None}, {"schema": "lofgren.artifact/1", "files": [{"path": 3}]}):
            with self.subTest(junk=junk):
                self.assertFalse(verify_artifact(junk).passed)


class UpstreamTests(Base):
    def test_every_domain_binds(self):
        for name in ("software", "resource", "engineering"):
            d = _domain(name)
            with self.subTest(domain=name):
                self.assertEqual(handoff_integrity_problems(d.handoff, d.receipt, d.context), [])

    def test_edited_handoff_is_refused_before_building(self):
        bad = copy.deepcopy(self.handoff)
        rent = next(s for s in bad["specifications"] if s["name"] == "rent")
        rent["value"] = 1.0
        with self.assertRaises(ProductionError) as cm:
            self.build(handoff=bad)
        self.assertIn("rent", str(cm.exception))

    def test_specification_citing_another_source_is_refused(self):
        bad = copy.deepcopy(self.handoff)
        bad["specifications"][0]["source_id"] = bad["specifications"][1]["source_id"]
        self.assertTrue(handoff_integrity_problems(bad, self.discovery.receipt, self.context))


class ReceiptAndHandoffTests(Base):
    def test_receipt_is_bound_to_its_artifact(self):
        self.assertEqual(check_production_receipt(self.result.receipt, self.result.artifact), [])
        other = self.build("python_module")
        self.assertTrue(check_production_receipt(other.receipt, self.result.artifact))

    def test_v4_handoff_grants_nothing(self):
        h = self.result.v4_handoff
        self.assertEqual(validate_v4_handoff(h, self.result.receipt, self.result.artifact), [])
        self.assertEqual(sorted(h["authority"]["actions_requiring_authority"]), sorted(AUTHORITY_REQUIRED_ACTIONS))
        self.assertEqual(h["open_verification_work"], ["Pre-leasing commitments for the first phase"])
        for change in ({"requested_actions": ["publish"]}, {"authority": {"granted": True}},
                       {"files": []}, {"production_receipt_hash": "PR-x"}, {"artifact_verified": False}):
            with self.subTest(change=change):
                self.assertTrue(validate_v4_handoff({**h, **change}, self.result.receipt, self.result.artifact))

    def test_build_is_deterministic_across_processes_inputs(self):
        self.assertEqual(self.build().receipt, self.result.receipt)


class StoreTests(Base):
    def test_round_trip_and_refusals(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "a"
            write_artifact(self.result, target)
            self.assertTrue(verify_directory(target)["passed"])
            with self.assertRaises(ProductionError):
                write_artifact(self.result, target)
            (target / "__pycache__").mkdir()
            (target / "__pycache__" / "x.pyc").write_bytes(b"")
            self.assertTrue(verify_directory(target)["passed"])  # running the tests leaves bytecode behind
            record = json.loads((target / "lofgren-artifact.json").read_text(encoding="utf-8"))
            record["files"][0]["path"] = "../outside.json"
            (target / "lofgren-artifact.json").write_text(json.dumps(record), encoding="utf-8")
            out = verify_directory(target)
            self.assertFalse(out["passed"])
            self.assertIn("unsafe", out["problems"][0])

    def test_missing_directory(self):
        self.assertFalse(verify_directory(Path(tempfile.gettempdir()) / "lofgren-no-such-artifact")["passed"])


class InterfaceTests(unittest.TestCase):
    def test_cli_produce_refuses_without_a_selection(self):
        from lofgren_intelligence.cli import main

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "docs").mkdir()
            for i, (title, text) in enumerate(AGREE_AND_CONFLICT.items()):
                (root / "docs" / f"d{i}.txt").write_text(f"{title}\n\n{text}", encoding="utf-8")
            (root / "design.json").write_text(json.dumps(F.infeasible_design()), encoding="utf-8")
            err = io.StringIO()
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
                rc = main(["produce", OBJECTIVE, "--files", str(root / "docs"), "--design", str(root / "design.json"),
                           "--out-dir", str(root / "art")])
            self.assertEqual(rc, 2)
            self.assertIn("infeasible", err.getvalue())
            self.assertFalse((root / "art").exists())

    def test_mcp_tools_listed_with_schemas(self):
        srv = Server()
        listed = {t["name"]: t for t in srv.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})["result"]["tools"]}
        for t in PRODUCTION_TOOLS:
            self.assertIn(t["name"], listed)
            self.assertEqual(listed[t["name"]]["inputSchema"]["type"], "object")
            self.assertIn("outputSchema", listed[t["name"]])

    def test_mcp_unknown_ids_are_errors(self):
        srv = Server()
        for name, args in (("build_artifact", {"discovery_id": "DR-none"}),
                           ("get_production_receipt", {"artifact_id": "AF-none"}),
                           ("create_v4_handoff", {"artifact_id": "AF-none"})):
            out = srv.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                              "params": {"name": name, "arguments": args}})["result"]
            self.assertTrue(out["isError"], name)


if __name__ == "__main__":
    unittest.main()
