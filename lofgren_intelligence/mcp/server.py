"""MCP server: exposes Lofgren Intelligence as tools to any MCP client.

Claude Code, Codex, and other MCP-capable agents call these tools, so the
evidence layer sits underneath the AI a person already uses. Tools return
structured data (structuredContent) validated state, not narrative: clients
are consumers of Lofgren Intelligence's findings, never an alternate source
of truth. Only `render_report` returns prose, and it is a view of that state.

Transport: JSON-RPC 2.0 over stdio, one message per line (MCP stdio
transport). Standard library only.

    claude mcp add lofgren -- lofgren mcp
"""

from __future__ import annotations

import json
import sys
import traceback
from datetime import datetime, timezone
from typing import Any, Callable, TextIO

from .. import __version__, build_registry
from ..billing.pricing import PLANS, cheapest_plan, estimate, monthly_bill
from ..evidence.types import to_dict
from ..intent.compiler import compile_intent
from ..kernel.pipeline import RunResult, estimate_run, run_investigation
from ..kernel.receipt import verify_receipt
from ..kernel.state import export_state
from ..models.provider import default_provider
from ..orbital.catalog import IMAGING_SATELLITES, fetch_tles, load_tles
from ..orbital.propagate import PROPAGATOR, find_passes
from ..orbital.tle import parse_tle_text
from ..report.markdown import render_markdown
from ..research.planner import gap_unknowns, plan_research

SUPPORTED_PROTOCOLS = ("2025-06-18", "2025-03-26", "2024-11-05")
INSTRUCTIONS = (
    "Lofgren Intelligence is an evidence layer. Typical flow: `compile_objective` -> `plan_research` "
    "(shows price) -> `investigate` (returns a run_id and typed findings) -> `get_finding`, "
    "`find_contradictions`, `find_gaps`, `trace_claim`, `get_receipt`, `export_state`. `verify_claim` tests one "
    "statement. `render_report` gives a human-readable view. Confidence is provisional until calibrated; treat "
    "'contested' and 'supported' (single-source) findings as unsettled, and never present a hypothesis as a finding."
)

_SOURCE_PROPS: dict[str, Any] = {
    "files": {"type": "array", "items": {"type": "string"}, "description": "Local documents or folders to use as evidence."},
    "texts": {"type": "object", "additionalProperties": {"type": "string"},
              "description": "Inline documents: title -> text."},
    "urls": {"type": "array", "items": {"type": "string"}, "description": "Public web pages to read."},
    "search": {"type": "string", "enum": ["brave"], "description": "Discover web pages (needs BRAVE_API_KEY)."},
    "lat": {"type": "number"},
    "lon": {"type": "number"},
    "tle_path": {"type": "string", "description": "File of orbital elements for pass prediction."},
    "fetch_orbits": {"type": "boolean", "description": "Fetch current elements from CelesTrak."},
    "imagery": {"type": "boolean", "description": "Search open Sentinel-2 / Landsat catalogs."},
    "plan": {"type": "string", "enum": sorted(PLANS)},
    "max_spend_usd": {"type": "number", "description": "Research spend cap in USD (default 5)."},
}
_RUN = {"run_id": {"type": "string", "description": "The research ID returned by investigate or verify_claim."}}


def _obj(required: list[str], props: dict) -> dict:
    return {"type": "object", "required": required, "properties": props}


_RUN_SUMMARY_SCHEMA = {
    "type": "object",
    "required": ["run_id", "completed", "findings", "unknowns"],
    "properties": {
        "run_id": {"type": ["string", "null"]},
        "completed": {"type": "boolean"},
        "stopped_reason": {"type": "string"},
        "findings": {"type": "array"},
        "contradictions": {"type": "integer"},
        "unknowns": {"type": "array"},
        "charge_usd": {"type": "number"},
        "confidence_status": {"type": "string"},
    },
}

TOOLS: list[dict[str, Any]] = [
    {"name": "compile_objective",
     "description": "Turn an objective into an Outcome Contract: linked research questions, constraints, evidence "
                    "standard, spend cap and actions that need approval.",
     "inputSchema": _obj(["objective"], {"objective": {"type": "string"}, "max_spend_usd": {"type": "number"},
                                          "lat": {"type": "number"}, "lon": {"type": "number"}})},
    {"name": "plan_research",
     "description": "Plan an investigation without running it: gather tasks in dependency order, capability gaps "
                    "as acquisition plans, and the price estimate.",
     "inputSchema": _obj(["objective"], {"objective": {"type": "string"}, **_SOURCE_PROPS})},
    {"name": "investigate",
     "description": "Run the evidence loop on an objective. Returns a run_id, typed findings and open unknowns.",
     "inputSchema": _obj(["objective"], {"objective": {"type": "string"}, **_SOURCE_PROPS,
                                          "approve": {"type": "boolean"}}),
     "outputSchema": _RUN_SUMMARY_SCHEMA},
    {"name": "verify_claim",
     "description": "Check one factual claim against the supplied evidence. Returns a run_id and typed findings.",
     "inputSchema": _obj(["claim"], {"claim": {"type": "string"}, **_SOURCE_PROPS}),
     "outputSchema": _RUN_SUMMARY_SCHEMA},
    {"name": "get_finding",
     "description": "One finding with its claims, evidence ids, contradictions, unknowns, scope and confidence.",
     "inputSchema": _obj(["run_id"], {**_RUN, "finding_id": {"type": "string"},
                                       "question_index": {"type": "integer", "description": "1-based"}})},
    {"name": "find_contradictions",
     "description": "The contradiction graph of a run: conflicting claims, kind, scope and what would resolve each.",
     "inputSchema": _obj(["run_id"], _RUN)},
    {"name": "find_gaps",
     "description": "Open unknowns of a run as acquisition plans: sources, expected gain, cost, approval needed.",
     "inputSchema": _obj(["run_id"], _RUN)},
    {"name": "trace_claim",
     "description": "Full provenance of one claim: claim -> evidence -> source.",
     "inputSchema": _obj(["run_id", "claim_id"], {**_RUN, "claim_id": {"type": "string"}})},
    {"name": "get_receipt",
     "description": "The research receipt of a run (hashes, operations, sources, versions, cost) and whether it "
                    "is intact.",
     "inputSchema": _obj(["run_id"], _RUN)},
    {"name": "export_state",
     "description": "The run's knowledge map for downstream reasoning: known, uncertain, contradicted, unknowns, "
                    "calculations.",
     "inputSchema": _obj(["run_id"], _RUN)},
    {"name": "render_report",
     "description": "Human-readable Markdown report of a run (a view of its typed state).",
     "inputSchema": _obj(["run_id"], _RUN)},
    {"name": "satellite_passes",
     "description": "Predict when open-data imaging satellites (Sentinel, Landsat, Terra, Aqua, VIIRS) pass over a "
                    "location, from public orbital elements. Predictions, not acquisition schedules.",
     "inputSchema": _obj(["lat", "lon"], {"lat": {"type": "number"}, "lon": {"type": "number"},
                                          "hours": {"type": "number"}, "min_elevation_deg": {"type": "number"},
                                          "tle_path": {"type": "string"}, "tle_text": {"type": "string"},
                                          "fetch": {"type": "boolean"}})},
    {"name": "pricing",
     "description": "Plans, and the monthly bill for a given usage on each plan.",
     "inputSchema": _obj([], {"standard_units": {"type": "number"}, "heavy_jobs": {"type": "integer"}})},
]
# Older tool names kept working for existing clients.
ALIASES = {"compile_intent": "compile_objective", "estimate_cost": "plan_research"}


class ToolError(Exception):
    pass


def _out(data: Any) -> dict:
    structured = json.loads(json.dumps(data, default=str))
    return {"text": json.dumps(structured, indent=2), "structured": structured}


class Server:
    def __init__(self) -> None:
        self.runs: dict[str, RunResult] = {}
        self.handlers: dict[str, Callable[[dict], dict]] = {
            "compile_objective": self.t_compile_objective,
            "plan_research": self.t_plan_research,
            "investigate": self.t_investigate,
            "verify_claim": self.t_verify_claim,
            "get_finding": self.t_get_finding,
            "find_contradictions": self.t_find_contradictions,
            "find_gaps": self.t_find_gaps,
            "trace_claim": self.t_trace_claim,
            "get_receipt": self.t_get_receipt,
            "export_state": self.t_export_state,
            "render_report": self.t_render_report,
            "satellite_passes": self.t_satellite_passes,
            "pricing": self.t_pricing,
        }
        for alias, target in ALIASES.items():
            self.handlers[alias] = self.handlers[target]

    # -- helpers -------------------------------------------------------------
    @staticmethod
    def _location(a: dict) -> dict | None:
        if a.get("lat") is not None and a.get("lon") is not None:
            return {"lat": float(a["lat"]), "lon": float(a["lon"]), "name": None}
        return None

    @staticmethod
    def _registry(a: dict):
        return build_registry(files=a.get("files"), texts=a.get("texts"), urls=a.get("urls"),
                              tle_path=a.get("tle_path"), fetch_orbits=bool(a.get("fetch_orbits")),
                              imagery=bool(a.get("imagery")), search=a.get("search"))

    def _contract(self, objective: str, a: dict):
        return compile_intent(objective, max_spend_usd=float(a.get("max_spend_usd", 5.0)), location=self._location(a))

    def _get_run(self, a: dict) -> RunResult:
        run = self.runs.get(a.get("run_id", ""))
        if run is None:
            raise ToolError(f"unknown run_id {a.get('run_id')}; runs live for this server session only")
        return run

    def _run(self, objective: str, a: dict) -> dict:
        result = run_investigation(self._contract(objective, a), self._registry(a), default_provider(),
                                   a.get("plan", "payg"), approved=bool(a.get("approve")))
        run_id = result.receipt.get("research_id")
        self.runs[run_id] = result
        q_index = {q.id: i + 1 for i, q in enumerate(result.contract.questions)}
        return _out({
            "run_id": run_id,
            "completed": result.completed,
            "stopped_reason": result.stopped_reason,
            "findings": [{"id": f.id, "question_index": q_index[f.question_id], "question": f.question,
                          "answer": f.answer, "confidence": f.confidence, "claims": len(f.claim_ids),
                          "contradictions": len(f.contradiction_ids), "issues": f.issues}
                         for f in result.findings],
            "contradictions": len(result.graph.contradictions),
            "unknowns": [{"id": u.id, "description": u.description, "expected_gain": u.expected_gain}
                         for u in result.unknowns if u.status == "open"],
            "charge_usd": result.charge.total_usd if result.charge else 0.0,
            "confidence_status": "calibrated" if result.calibrated else "provisional",
        })

    # -- tools ---------------------------------------------------------------
    def t_compile_objective(self, a: dict) -> dict:
        return _out(to_dict(self._contract(a["objective"], a)))

    def t_plan_research(self, a: dict) -> dict:
        c = self._contract(a["objective"], a)
        plan = plan_research(c, self._registry(a))
        est = estimate_run(plan, a.get("plan", "payg"))
        return _out({"estimate": est.as_dict(), "spend_cap_usd": c.max_spend_usd,
                     "tasks": [t.__dict__ for t in plan.tasks],
                     "unknowns": [to_dict(u) for u in gap_unknowns(plan, c, PLANS[a.get("plan", "payg")].rate)]})

    def t_investigate(self, a: dict) -> dict:
        return self._run(a["objective"], a)

    def t_verify_claim(self, a: dict) -> dict:
        return self._run(f"Verify the claim: {a['claim']}", a)

    def t_get_finding(self, a: dict) -> dict:
        run = self._get_run(a)
        f = None
        if a.get("finding_id"):
            f = next((x for x in run.findings if x.id == a["finding_id"]), None)
        elif a.get("question_index"):
            i = int(a["question_index"]) - 1
            f = run.findings[i] if 0 <= i < len(run.findings) else None
        if f is None:
            raise ToolError("give a valid finding_id or question_index")
        d = to_dict(f)
        d["claims"] = [{"id": c.id, "statement": c.statement, "status": c.status.value, "confidence": c.confidence,
                        "type": c.claim_type.value, "policy": c.sufficiency, "scope": to_dict(c.scope),
                        "issues": c.issues} for c in (run.graph.claims[i] for i in f.claim_ids)]
        return _out(d)

    def t_find_contradictions(self, a: dict) -> dict:
        run = self._get_run(a)
        g = run.graph
        return _out({"contradictions": [{**to_dict(cx), "claim_a_statement": g.claims[cx.claim_a].statement,
                                         "claim_b_statement": g.claims[cx.claim_b].statement}
                                        for cx in g.contradictions.values()]})

    def t_find_gaps(self, a: dict) -> dict:
        run = self._get_run(a)
        return _out({"unknowns": [to_dict(u) for u in sorted(run.unknowns, key=lambda u: -u.expected_gain)
                                  if u.status == "open"]})

    def t_trace_claim(self, a: dict) -> dict:
        run = self._get_run(a)
        if a.get("claim_id") not in run.graph.claims:
            raise ToolError(f"unknown claim_id {a.get('claim_id')}")
        return _out(run.graph.trace(a["claim_id"]))

    def t_get_receipt(self, a: dict) -> dict:
        run = self._get_run(a)
        return _out({"intact": verify_receipt(run.receipt), "receipt": run.receipt})

    def t_export_state(self, a: dict) -> dict:
        return _out(export_state(self._get_run(a)))

    def t_render_report(self, a: dict) -> dict:
        run = self._get_run(a)
        md = render_markdown(run)
        return {"text": md, "structured": {"run_id": a["run_id"], "format": "markdown", "report": md}}

    def t_satellite_passes(self, a: dict) -> dict:
        if a.get("tle_text"):
            tles = parse_tle_text(a["tle_text"])
        elif a.get("tle_path"):
            tles = load_tles(a["tle_path"])
        elif a.get("fetch"):
            tles = fetch_tles([s.norad_id for s in IMAGING_SATELLITES])
        else:
            raise ToolError("provide tle_text, tle_path, or fetch=true")
        start = datetime.now(timezone.utc)
        passes = []
        for t in tles:
            passes += find_passes(t, float(a["lat"]), float(a["lon"]), start, float(a.get("hours", 24)),
                                  float(a.get("min_elevation_deg", 30)))
        passes.sort(key=lambda p: p.rise)
        return _out({"propagator": PROPAGATOR, "kind": "prediction (not a provider acquisition schedule)",
                     "from": start.isoformat(), "passes": [p.to_dict() for p in passes]})

    def t_pricing(self, a: dict) -> dict:
        d: dict[str, Any] = {"plans": {k: p.__dict__ for k, p in PLANS.items()}}
        if a.get("standard_units") is not None:
            su, hj = float(a["standard_units"]), int(a.get("heavy_jobs", 0))
            d["bills"] = {k: monthly_bill(k, su, hj) for k in PLANS if k != "free"}
            d["cheapest"] = cheapest_plan(su, hj)
        d["example_estimate"] = estimate("payg", 40).as_dict()
        return _out(d)

    # -- protocol ------------------------------------------------------------
    def handle(self, msg: dict) -> dict | None:
        method, mid = msg.get("method"), msg.get("id")
        if mid is None:  # notification
            return None
        try:
            if method == "initialize":
                requested = (msg.get("params") or {}).get("protocolVersion")
                version = requested if requested in SUPPORTED_PROTOCOLS else SUPPORTED_PROTOCOLS[0]
                result: Any = {"protocolVersion": version, "capabilities": {"tools": {"listChanged": False}},
                               "serverInfo": {"name": "lofgren-intelligence", "version": __version__},
                               "instructions": INSTRUCTIONS}
            elif method == "ping":
                result = {}
            elif method == "tools/list":
                result = {"tools": TOOLS}
            elif method == "tools/call":
                params = msg.get("params") or {}
                name, args = params.get("name"), params.get("arguments") or {}
                if name not in self.handlers:
                    return _error(mid, -32602, f"unknown tool: {name}")
                try:
                    out = self.handlers[name](args)
                    result = {"content": [{"type": "text", "text": out["text"]}],
                              "structuredContent": out["structured"], "isError": False}
                except (ToolError, KeyError, ValueError, FileNotFoundError) as exc:
                    result = {"content": [{"type": "text", "text": f"error: {exc}"}], "isError": True}
            else:
                return _error(mid, -32601, f"method not found: {method}")
            return {"jsonrpc": "2.0", "id": mid, "result": result}
        except Exception as exc:  # never crash the server on one bad call
            traceback.print_exc(file=sys.stderr)
            return _error(mid, -32603, f"internal error: {exc}")


def _error(mid: Any, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": mid, "error": {"code": code, "message": message}}


def serve(stdin: TextIO = sys.stdin, stdout: TextIO = sys.stdout) -> None:
    server = Server()
    for line in stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            stdout.write(json.dumps(_error(None, -32700, "parse error")) + "\n")
            stdout.flush()
            continue
        reply = server.handle(msg)
        if reply is not None:
            stdout.write(json.dumps(reply) + "\n")
            stdout.flush()
