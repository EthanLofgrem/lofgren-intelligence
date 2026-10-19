"""Interface defects found by capability testing (D7, D8, D9, D11, D12).

D7  hosted MCP refusals keep their reason as a typed, sanitized tool error.
D8  the local stdio MCP validates arguments against its inputSchema, size limits and the local file policy.
D9  design assumptions appear in the discovery report, the JSON summary and the produced README.
D11 every operator-selected manifest source is recorded with its status (used, no_passages, failed).
D12 the CLI refuses cleanly (no traceback) and states each surface's real max_records cap.

No test makes a network call. All data is fictional.
"""

from __future__ import annotations

import contextlib
import io
import json
import logging
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from mcp import Client

from lofgren_intelligence.adapters.manifest import (
    STATUS_FAILED,
    STATUS_NO_PASSAGES,
    STATUS_USED,
    ManifestError,
    OperatorSourcesAdapter,
    parse_manifest,
)
from lofgren_intelligence.adapters.net import UnsafeURL
from lofgren_intelligence.cli import main
from lofgren_intelligence.discovery.report import discovery_summary, render_discovery_markdown
from lofgren_intelligence.execution.core import ExecutionError
from lofgren_intelligence.hosted import mcp_sdk
from lofgren_intelligence.hosted.mcp_sdk import build_mcp
from lofgren_intelligence.hosted.security import PublicInputError
from lofgren_intelligence.hosted.service import PublicService
from lofgren_intelligence.improvement.core import ImprovementError
from lofgren_intelligence.mcp import inputs
from lofgren_intelligence.mcp.server import TOOLS, Server
from lofgren_intelligence.production import build_artifact, codegen
from lofgren_intelligence.production.certification import _fixture

from .test_public_ops_hardening import INTERNAL_MARKERS, RUN_ENV, TOKEN, _discovered_store
from .test_public_sources import NOW, FakeResponse, client, contract, question

ROOT = Path(__file__).resolve().parent.parent
SAMPLE_SOURCES = ROOT / "examples" / "sample-sources"
OBJECTIVE = "Is industrial construction in the Phoenix metro increasing?"


def _text(result) -> str:
    return "\n".join(getattr(c, "text", "") for c in result.content)


# ---------------------------------------------------------------------------------------------------------------
class HostedRefusalMappingTests(unittest.TestCase):
    """D7: the wrapper itself, one exception type at a time."""

    def _refusal(self, exc: BaseException) -> str:
        def tool():
            raise exc

        with self.assertRaises(mcp_sdk.ToolError) as ctx:
            mcp_sdk._public_errors(tool)()
        return str(ctx.exception)

    def test_each_expected_refusal_has_a_stable_code_and_its_reason(self):
        cases = (
            (UnsafeURL("local hostnames are not allowed"), "UNSAFE_URL"),
            (ManifestError("source manifest must be a JSON object"), "MANIFEST_INVALID"),
            (PublicInputError("too many URLs"), "INPUT_INVALID"),
            (ExecutionError("local webhook targets are not allowed"), "EXECUTION_REFUSED"),
            (ImprovementError("baseline and candidate must be distinct"), "IMPROVEMENT_REFUSED"),
        )
        for exc, code in cases:
            with self.subTest(code=code):
                self.assertEqual(self._refusal(exc), f"{code}: {exc}")

    def test_message_is_one_bounded_printable_line(self):
        text = self._refusal(PublicInputError("bad\ninput\x00\x1b[31m" + "x" * 1000))
        self.assertNotIn("\n", text)
        self.assertTrue(text.isprintable())
        self.assertLessEqual(len(text), len("INPUT_INVALID: ") + mcp_sdk.MAX_REFUSAL_MESSAGE_CHARS)

    def test_unexpected_exceptions_are_not_mapped(self):
        for exc in (RuntimeError("internal detail"), KeyError("secret"), ValueError("raw value error")):
            with self.subTest(exc=type(exc).__name__):
                def tool():
                    raise exc

                with self.assertRaises(type(exc)):
                    mcp_sdk._public_errors(tool)()
                self.assertIsNone(mcp_sdk.refusal_code(exc))

    def test_hosted_max_records_cap_is_typed(self):
        with self.assertRaises(PublicInputError) as ctx:
            mcp_sdk._source_args(europepmc="zelvapril", max_records=21)
        self.assertIn("between 1 and 20", str(ctx.exception))
        self.assertEqual(mcp_sdk._source_args(max_records=20)["max_records"], 20)


class HostedRefusalMCPTests(unittest.IsolatedAsyncioTestCase):
    """D7 repros through the official SDK client."""

    @classmethod
    def setUpClass(cls):
        cls.store, cls.run_id, cls.discovery_id = _discovered_store()
        with patch.dict(os.environ, RUN_ENV, clear=False):
            cls.artifact_id = PublicService(cls.store).build_artifact(
                "u1", {"discovery_id": cls.discovery_id})["artifact_id"]

    async def _call(self, name, args):
        with (
            patch("lofgren_intelligence.hosted.mcp_sdk.get_access_token", return_value=TOKEN),
            patch("lofgren_intelligence.hosted.mcp_sdk.SupabaseStore", return_value=self.store),
            patch.dict(os.environ, RUN_ENV, clear=False),
        ):
            async with Client(build_mcp("https://li.example")) as c:
                return await c.call_tool(name, args)

    async def _refused(self, name, args, code):
        with self.assertLogs("mcp.server.mcpserver.server", level="INFO") as logs:
            result = await self._call(name, args)
        self.assertTrue(result.is_error)
        text = _text(result)
        self.assertIn(f"{code}: ", text)
        for marker in INTERNAL_MARKERS:
            self.assertNotIn(marker, text)
        # Logged as one informational line: no traceback, no ERROR record.
        self.assertTrue(all(r.levelno < logging.ERROR and r.exc_info is None for r in logs.records))
        self.assertFalse(any("Traceback" in line for line in logs.output))
        return text

    async def test_investigate_localhost_url_is_unsafe_url(self):
        text = await self._refused("investigate", {"objective": OBJECTIVE, "urls": ["http://localhost/"]},
                                   "UNSAFE_URL")
        self.assertIn("local hostnames are not allowed", text)

    async def test_propose_action_to_loopback_is_execution_refused(self):
        text = await self._refused("propose_action", {"artifact_id": self.artifact_id,
                                                      "target": "https://127.0.0.1/hook", "payload": {}},
                                   "EXECUTION_REFUSED")
        self.assertIn("non-public", text)
        self.assertEqual(self.store.actions, {})  # fail closed: nothing was proposed

    async def test_max_records_over_the_hosted_cap_is_input_invalid(self):
        text = await self._refused("investigate", {"objective": OBJECTIVE, "europepmc": "zelvapril",
                                                   "max_records": 50}, "INPUT_INVALID")
        self.assertIn("between 1 and 20", text)

    async def test_bad_inline_manifest_is_manifest_invalid(self):
        await self._refused("verify_claim", {"claim": "x", "sources_manifest": {"schema": "nope"}},
                            "MANIFEST_INVALID")

    async def test_oversized_objective_is_input_invalid(self):
        await self._refused("plan_research", {"objective": "x" * 9000}, "INPUT_INVALID")

    async def test_unexpected_failure_stays_generic(self):
        with patch.object(PublicService, "usage_status", side_effect=RuntimeError("internal detail 42")):
            with self.assertLogs("mcp.server.mcpserver.server", level="ERROR"):
                result = await self._call("usage_status", {})
        self.assertTrue(result.is_error)
        self.assertNotIn("internal detail 42", _text(result))
        self.assertIn("Error executing tool usage_status", _text(result))


# ---------------------------------------------------------------------------------------------------------------
class LocalMCPValidationTests(unittest.TestCase):
    """D8: the stdio server checks inputSchema, limits and the local file policy before any handler runs."""

    def setUp(self):
        self.server = Server()

    def call(self, name, args):
        reply = self.server.handle({"jsonrpc": "2.0", "id": 7, "method": "tools/call",
                                    "params": {"name": name, "arguments": args}})
        self.assertNotIn("error", reply, reply)  # never a -32603 internal error
        return reply["result"]

    def refused(self, name, args, fragment):
        result = self.call(name, args)
        self.assertTrue(result["isError"], result)
        text = result["content"][0]["text"]
        self.assertTrue(text.startswith("INPUT_INVALID: "), text)
        self.assertIn(fragment, text)
        return text

    def test_capability_repros_are_refused(self):
        self.refused("plan_research", {"objective": 5}, "objective must be of type string")
        self.refused("satellite_passes", {"lat": 200, "lon": 0, "tle_text": "x"}, "lat must be <= 90")
        self.refused("pricing", {"standard_units": -5}, "standard_units must be >= 0")
        self.refused("pricing", {"heavy_jobs": -1}, "heavy_jobs must be >= 0")
        self.refused("investigate", {"objective": "x" * 200_000}, "at most 8000 characters")
        self.refused("investigate", {"objective": "Is it?", "texts": {"a": "y" * 5_600_000}},
                     "at most 250000 characters")

    def test_aggregate_and_count_limits(self):
        many = {f"doc {i}": "text" for i in range(21)}
        self.refused("investigate", {"objective": "Is it?", "texts": many}, "at most 20 entries")
        split = {f"doc {i}": "y" * 20_000 for i in range(15)}
        self.refused("investigate", {"objective": "Is it?", "texts": split}, "the limit is 250000")
        self.refused("investigate", {"objective": "Is it?", "urls": ["https://example.org/"] * 13}, "at most 12")
        self.refused("investigate", {"objective": "Is it?", "max_records": 101}, "max_records must be <= 100")
        self.refused("investigate", {"objective": "Is it?", "max_spend_usd": -1}, "max_spend_usd must be >= 0")
        self.refused("satellite_passes", {"lat": 1, "lon": 1, "hours": 0, "tle_text": "x"}, "hours must be > 0")
        self.refused("get_finding", {"run_id": "RR-x", "question_index": 0}, "question_index must be >= 1")
        self.refused("investigate", {"objective": "Is it?", "approve": "yes"}, "approve must be of type boolean")
        self.refused("investigate", {"objective": "Is it?", "max_records": True}, "must be of type integer")

    def test_arguments_must_be_an_object(self):
        self.refused("pricing", ["not", "an", "object"], "arguments must be of type object")

    def test_unsupported_local_file_type_is_refused(self):
        with tempfile.TemporaryDirectory() as d:
            ini = Path(d) / "win.ini"
            ini.write_text("[fonts]\n", encoding="utf-8")
            self.refused("investigate", {"objective": "Is it?", "files": [str(ini)]}, "not a supported document type")
            self.refused("investigate", {"objective": "Is it?", "files": [str(Path(d) / "missing.md")]},
                         "does not exist")

    def test_oversized_and_too_many_local_files_are_refused(self):
        with tempfile.TemporaryDirectory() as d:
            big = Path(d) / "big.txt"
            big.write_bytes(b"x" * (inputs.MAX_LOCAL_FILE_BYTES + 1))
            self.refused("investigate", {"objective": "Is it?", "files": [str(big)]}, "the limit is")
            many = Path(d) / "many"
            many.mkdir()
            for i in range(21):
                (many / f"n{i}.md").write_text("note", encoding="utf-8")
            self.refused("investigate", {"objective": "Is it?", "files": [str(many)]}, "more than 20 documents")

    def test_non_regular_file_is_refused(self):
        target = Path(os.devnull)  # a character device on POSIX, NUL on Windows: never a regular file
        with self.assertRaises(inputs.InputInvalid) as ctx:
            inputs._regular_file(target, "file", 10, None)
        self.assertTrue("not a regular file" in str(ctx.exception) or "does not exist" in str(ctx.exception))

    def test_files_root_confines_local_paths(self):
        with tempfile.TemporaryDirectory() as d:
            inside = Path(d) / "inside"
            inside.mkdir()
            (inside / "note.md").write_text("Industrial construction in Phoenix increased.", encoding="utf-8")
            with patch.dict(os.environ, {inputs.FILES_ROOT_ENV: str(inside)}):
                self.refused("investigate", {"objective": OBJECTIVE, "files": [str(SAMPLE_SOURCES)]},
                             f"outside {inputs.FILES_ROOT_ENV}")
                self.refused("investigate", {"objective": OBJECTIVE, "files": [str(inside / ".." / "x.md")]},
                             f"outside {inputs.FILES_ROOT_ENV}")
                inputs.check_local_paths({"files": [str(inside / "note.md")]})  # allowed

    def test_valid_local_call_still_runs(self):
        with patch.dict(os.environ, {"LOFGREN_PROVIDER": "heuristic"}):
            result = self.call("investigate", {"objective": OBJECTIVE, "files": [str(SAMPLE_SOURCES)]})
        self.assertFalse(result["isError"])
        self.assertTrue(result["structuredContent"]["run_id"])

    def test_schemas_advertise_the_limits(self):
        by_name = {t["name"]: t["inputSchema"]["properties"] for t in TOOLS}
        self.assertEqual(by_name["investigate"]["objective"]["maxLength"], inputs.MAX_OBJECTIVE_CHARS)
        self.assertEqual(by_name["investigate"]["texts"]["maxProperties"], inputs.MAX_DOCUMENTS)
        self.assertEqual(by_name["investigate"]["max_records"]["maximum"], 100)
        self.assertIn("hosted service caps it at 20", by_name["investigate"]["max_records"]["description"])
        self.assertEqual(by_name["satellite_passes"]["lat"], {"type": "number", "minimum": -90, "maximum": 90})
        self.assertEqual(by_name["pricing"]["standard_units"]["minimum"], 0)


# ---------------------------------------------------------------------------------------------------------------
class AssumptionsVisibleTests(unittest.TestCase):
    """D9: assumptions in the discovery report, the JSON summary and the produced README."""

    @classmethod
    def setUpClass(cls):
        cls.context, cls.discovery = _fixture()
        cls.assumptions = cls.discovery.candidates.assumptions
        assert cls.assumptions, "the certification fixture states design assumptions"

    def test_report_has_an_explicit_assumptions_section(self):
        md = render_discovery_markdown(self.discovery)
        self.assertIn("## Assumptions (stated, not verified)", md)
        for a in self.assumptions:
            self.assertIn(a.statement, md)
            self.assertIn(a.id, md)

    def test_summary_lists_assumptions(self):
        rows = discovery_summary(self.discovery)["assumptions"]
        self.assertEqual({r["id"] for r in rows}, {a.id for a in self.assumptions})
        for r in rows:
            self.assertEqual(r["kind"], "assumption")
            self.assertTrue(r["why_assumed"])
        json.dumps(rows)

    def test_produced_readme_has_assumptions_and_still_verifies(self):
        r = build_artifact(self.discovery.handoff, discovery_receipt=self.discovery.receipt, context=self.context,
                           kind="structured_bundle")
        readme = next(f for f in r.artifact["files"] if f["path"] == "README.md")["content"]
        self.assertIn("## Assumptions", readme)
        for a in self.assumptions:
            self.assertIn(a.statement, readme)
        self.assertTrue(r.verification.passed, r.verification.problems)

    def test_codegen_1_manifests_regenerate_unchanged(self):
        r = build_artifact(self.discovery.handoff, discovery_receipt=self.discovery.receipt, context=self.context,
                           kind="structured_bundle")
        manifest = json.loads(next(f for f in r.artifact["files"] if f["path"] == "artifact.json")["content"])
        self.assertEqual(manifest["codegen"], codegen.CODEGEN_VERSION)
        manifest["codegen"] = "production.codegen/1"
        files = {p: c for p, _, c in codegen.generate(manifest, "structured_bundle")}
        self.assertNotIn("## Assumptions", files["README.md"])
        self.assertEqual(json.loads(files["plan.json"])["codegen"], "production.codegen/1")
        self.assertIn("production.codegen/1", files["artifact.py"].splitlines()[0])
        self.assertIn("production.codegen/1", codegen.SUPPORTED_CODEGEN_VERSIONS)


# ---------------------------------------------------------------------------------------------------------------
class ManifestSourceStatusTests(unittest.TestCase):
    """D11: every operator-selected source has a recorded status."""

    PAGE = (b"<html><body><p>Zelvapril sales to adults with hypertension reached 40 million units in 2025, "
            b"and blood pressure clinics reported that zelvapril is prescribed widely.</p></body></html>")
    EMPTY = b"<html><body><h1>Example Domain</h1><p>This domain is for use in documentation.</p></body></html>"

    def _adapter(self, script, urls):
        m = parse_manifest(json.dumps({"schema": "lofgren.sources-manifest/1",
                                       "sources": [{"url": u} for u in urls]}).encode())
        c, _, _ = client("operator_sources", script)
        return OperatorSourcesAdapter(m, client=c, clock=lambda: NOW)

    def test_used_no_passages_and_failed_are_all_recorded(self):
        a = self._adapter([FakeResponse(body=self.PAGE, ctype="text/html"),
                           FakeResponse(body=self.EMPTY, ctype="text/html"),
                           FakeResponse(404)],
                          ["https://a.example.org/z.html", "https://example.com/", "https://c.example.org/gone"])
        out = a.gather(question(), contract())
        self.assertTrue(out.items)
        for _, ev in out.items:
            self.assertEqual(ev.data["source_status"], STATUS_USED)
        note = next(n for n in out.notes if "operator-selected source status" in n)
        self.assertIn(f"entry 0 https://a.example.org/z.html {STATUS_USED}", note)
        self.assertIn(f"entry 1 https://example.com/ {STATUS_NO_PASSAGES}", note)
        self.assertIn(f"entry 2 https://c.example.org/gone {STATUS_FAILED}", note)
        # The failure keeps exactly one provider_unavailable note.
        self.assertEqual(sum("provider_unavailable" in n for n in out.notes), 1)
        statuses = {s["url"]: s["status"] for s in a.source_statuses()}
        self.assertEqual(statuses, {"https://a.example.org/z.html": STATUS_USED,
                                    "https://example.com/": STATUS_NO_PASSAGES,
                                    "https://c.example.org/gone": STATUS_FAILED})

    def test_fetched_page_without_passages_reaches_the_run_record(self):
        from lofgren_intelligence.adapters import AdapterRegistry
        from lofgren_intelligence.kernel.pipeline import run_investigation

        a = self._adapter([FakeResponse(body=self.EMPTY, ctype="text/html")], ["https://example.com/"])
        reg = AdapterRegistry()
        reg.register(a)
        result = run_investigation(contract(), reg, plan_id="payg")
        recorded = [u["description"] for u in result.receipt["unknowns"]]
        self.assertTrue(any("https://example.com/ no_passages" in d for d in recorded), recorded)

    def test_all_used_sources_add_no_gap_note(self):
        a = self._adapter([FakeResponse(body=self.PAGE, ctype="text/html")], ["https://a.example.org/z.html"])
        out = a.gather(question(), contract())
        self.assertTrue(out.items)
        self.assertFalse(any("operator-selected source status" in n for n in out.notes))
        self.assertEqual(a.source_statuses()[0]["status"], STATUS_USED)


# ---------------------------------------------------------------------------------------------------------------
class CLIRefusalTests(unittest.TestCase):
    """D12."""

    def _run(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                rc = main(argv)
            except SystemExit as exc:
                rc = exc.code
        return rc, out.getvalue(), err.getvalue()

    def test_ssrf_refusal_is_one_clean_line_and_exit_2(self):
        rc, _, err = self._run(["estimate", OBJECTIVE, "--url", "http://localhost/"])
        self.assertEqual(rc, 2)
        self.assertNotIn("Traceback", err)
        self.assertEqual(err.strip(), "lofgren estimate: error: unsafe URL: local hostnames are not allowed")

    def test_invalid_manifest_is_clean_and_exit_2(self):
        with tempfile.TemporaryDirectory() as d:
            bad = Path(d) / "m.json"
            bad.write_text("{\"schema\": \"nope\", \"sources\": []}", encoding="utf-8")
            rc, _, err = self._run(["estimate", OBJECTIVE, "--sources-manifest", str(bad)])
        self.assertEqual(rc, 2)
        self.assertNotIn("Traceback", err)
        self.assertIn("invalid source manifest", err)

    def test_calibration_with_unknown_claim_fails(self):
        with tempfile.TemporaryDirectory() as d:
            log = Path(d) / "log.jsonl"
            log.write_text(json.dumps({"claim_id": "C-known", "confidence": 0.7, "outcome": None}) + "\n",
                           encoding="utf-8")
            before = log.read_text(encoding="utf-8")
            rc, out, err = self._run(["calibration", "--log", str(log), "--claim", "C-unknown", "--correct", "yes"])
            self.assertEqual(rc, 1)
            self.assertIn("no unresolved prediction for claim 'C-unknown'", err)
            self.assertNotIn("recorded", out)
            self.assertEqual(log.read_text(encoding="utf-8"), before)
            rc, _, err = self._run(["calibration", "--log", str(log), "--claim", "C-known"])
            self.assertEqual(rc, 2)
            self.assertIn("--correct", err)
            rc, out, _ = self._run(["calibration", "--log", str(log), "--claim", "C-known", "--correct", "yes"])
            self.assertEqual(rc, 0)
            self.assertIn("recorded outcome for 1 prediction(s) of C-known", out)

    def test_help_states_each_surface_cap(self):
        rc, out, _ = self._run(["investigate", "--help"])
        self.assertEqual(rc, 0)
        flat = " ".join(out.split())
        self.assertIn("1-100 for this local CLI", flat)
        self.assertIn("hosted MCP service caps it at 20", flat)

    def test_bad_public_query_is_an_argument_error(self):
        rc, _, err = self._run(["estimate", OBJECTIVE, "--europepmc", "   "])
        self.assertEqual(rc, 2)
        self.assertNotIn("Traceback", err)
        self.assertIn("--europepmc must be non-empty text", err)


if __name__ == "__main__":
    unittest.main()
