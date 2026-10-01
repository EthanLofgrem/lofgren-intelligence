"""MCP server: exposes Lofgren Intelligence as tools to any MCP client.

Claude Code, Codex, and other MCP-capable agents can call these tools, so the
evidence layer sits underneath the AI a person already uses.

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
from ..models.provider import default_provider
from ..orbital.catalog import IMAGING_SATELLITES, fetch_tles, load_tles
from ..orbital.propagate import PROPAGATOR, find_passes
from ..orbital.tle import parse_tle_text
from ..report.markdown import render_markdown
from ..research.planner import plan_research

SUPPORTED_PROTOCOLS = ("2025-06-18", "2025-03-26", "2024-11-05")
INSTRUCTIONS = (
    "Lofgren Intelligence is an evidence layer. Use `investigate` to turn an objective into a cited, "
    "cross-checked report; `verify_claim` to test one statement; `estimate_cost` before large jobs; "
    "`satellite_passes` for when open-data imaging satellites pass over a location; `trace_claim` to see "
    "the full provenance of a finding. Treat 'Contested' and 'Single-source' findings as unsettled."
)

_SOURCE_PROPS: dict[str, Any] = {
    "files": {"type": "array", "items": {"type": "string"}, "description": "Local documents or folders to use as evidence."},
    "texts": {"type": "object", "additionalProperties": {"type": "string"},
              "description": "Inline documents: title -> text."},
    "urls": {"type": "array", "items": {"type": "string"}, "description": "Public web pages to read."},
    "lat": {"type": "number"},
    "lon": {"type": "number"},
    "tle_path": {"type": "string", "description": "File of orbital elements for pass prediction."},
    "fetch_orbits": {"type": "boolean", "description": "Fetch current elements from CelesTrak."},
    "imagery": {"type": "boolean", "description": "Search open Sentinel-2 / Landsat catalogs."},
    "plan": {"type": "string", "enum": sorted(PLANS)},
    "max_spend_usd": {"type": "number", "description": "Research spend cap in USD (default 5)."},
}

TOOLS: list[dict[str, Any]] = [
    {
        "name": "investigate",
        "description": "Run the evidence loop (intent, plan, sense, research, verify, report) on an objective. "
                       "Returns a cited report with verified, contested and missing findings.",
        "inputSchema": {"type": "object", "required": ["objective"],
                        "properties": {"objective": {"type": "string"}, **_SOURCE_PROPS,
                                       "approve": {"type": "boolean"}}},
    },
    {
        "name": "verify_claim",
        "description": "Check one factual claim against the supplied evidence; returns supporting and "
                       "contradicting findings with calibrated confidence.",
        "inputSchema": {"type": "object", "required": ["claim"],
                        "properties": {"claim": {"type": "string"}, **_SOURCE_PROPS}},
    },
    {
        "name": "compile_intent",
        "description": "Turn an objective into an Outcome Contract: questions, constraints, evidence standard, "
                       "spend cap and actions that need approval.",
        "inputSchema": {"type": "object", "required": ["objective"],
                        "properties": {"objective": {"type": "string"}, "max_spend_usd": {"type": "number"},
                                       "lat": {"type": "number"}, "lon": {"type": "number"}}},
    },
    {
        "name": "estimate_cost",
        "description": "Estimate the job class and price of an investigation before running it.",
        "inputSchema": {"type": "object", "required": ["objective"],
                        "properties": {"objective": {"type": "string"}, **_SOURCE_PROPS}},
    },
    {
        "name": "satellite_passes",
        "description": "Predict when open-data imaging satellites (Sentinel, Landsat, Terra, Aqua, VIIRS) pass "
                       "over a location, from public orbital elements.",
        "inputSchema": {"type": "object", "required": ["lat", "lon"],
                        "properties": {"lat": {"type": "number"}, "lon": {"type": "number"},
                                       "hours": {"type": "number"}, "min_elevation_deg": {"type": "number"},
                                       "tle_path": {"type": "string"}, "tle_text": {"type": "string"},
                                       "fetch": {"type": "boolean"}}},
    },
    {
        "name": "trace_claim",
        "description": "Full provenance of one finding from a previous run: claim -> evidence -> source.",
        "inputSchema": {"type": "object", "required": ["run_id", "claim_id"],
                        "properties": {"run_id": {"type": "string"}, "claim_id": {"type": "string"}}},
    },
    {
        "name": "pricing",
        "description": "Plans, and the monthly bill for a given usage on each plan.",
        "inputSchema": {"type": "object",
                        "properties": {"standard_units": {"type": "number"}, "heavy_jobs": {"type": "integer"}}},
    },
]


class ToolError(Exception):
    pass


class Server:
    def __init__(self) -> None:
        self.runs: dict[str, RunResult] = {}
        self.handlers: dict[str, Callable[[dict], dict]] = {
            "investigate": self.t_investigate,
            "verify_claim": self.t_verify_claim,
            "compile_intent": self.t_compile_intent,
            "estimate_cost": self.t_estimate_cost,
            "satellite_passes": self.t_satellite_passes,
            "trace_claim": self.t_trace_claim,
            "pricing": self.t_pricing,
        }

    # -- tools ---------------------------------------------------------------
    @staticmethod
    def _location(a: dict) -> dict | None:
        if a.get("lat") is not None and a.get("lon") is not None:
            return {"lat": float(a["lat"]), "lon": float(a["lon"]), "name": None}
        return None

    @staticmethod
    def _registry(a: dict):
        return build_registry(files=a.get("files"), texts=a.get("texts"), urls=a.get("urls"),
                              tle_path=a.get("tle_path"), fetch_orbits=bool(a.get("fetch_orbits")),
                              imagery=bool(a.get("imagery")))

    def _run(self, objective: str, a: dict) -> dict:
        contract = compile_intent(objective, max_spend_usd=float(a.get("max_spend_usd", 5.0)),
                                  location=self._location(a))
        result = run_investigation(contract, self._registry(a), default_provider(), a.get("plan", "payg"),
                                   approved=bool(a.get("approve")))
        run_id = contract.id
        self.runs[run_id] = result
        summary = {
            "run_id": run_id,
            "completed": result.completed,
            "stopped_reason": result.stopped_reason,
            "claims": [{"id": c.id, "status": c.status.value, "confidence": c.confidence,
                        "statement": c.statement} for c in
                       sorted(result.graph.claims.values(), key=lambda c: -c.confidence)],
            "contradictions": len(result.graph.contradictions),
            "gaps": result.gaps,
            "charge_usd": result.charge.total_usd if result.charge else 0.0,
        }
        return {"text": render_markdown(result), "structured": summary}

    def t_investigate(self, a: dict) -> dict:
        return self._run(a["objective"], a)

    def t_verify_claim(self, a: dict) -> dict:
        return self._run(f"Verify the claim: {a['claim']}", a)

    def t_compile_intent(self, a: dict) -> dict:
        c = compile_intent(a["objective"], max_spend_usd=float(a.get("max_spend_usd", 5.0)),
                           location=self._location(a))
        d = to_dict(c)
        return {"text": json.dumps(d, indent=2), "structured": d}

    def t_estimate_cost(self, a: dict) -> dict:
        c = compile_intent(a["objective"], max_spend_usd=float(a.get("max_spend_usd", 5.0)),
                           location=self._location(a))
        plan = plan_research(c, self._registry(a))
        est = estimate_run(plan, a.get("plan", "payg"))
        d = {**est.as_dict(), "gather_tasks": len(plan.tasks),
             "gaps": [f"{g.question}: {g.reason}" for g in plan.gaps], "spend_cap_usd": c.max_spend_usd}
        return {"text": json.dumps(d, indent=2), "structured": d}

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
        d = {"propagator": PROPAGATOR, "from": start.isoformat(), "passes": [p.to_dict() for p in passes]}
        return {"text": json.dumps(d, indent=2), "structured": d}

    def t_trace_claim(self, a: dict) -> dict:
        run = self.runs.get(a["run_id"])
        if run is None:
            raise ToolError(f"unknown run_id {a['run_id']}; runs live for this server session only")
        if a["claim_id"] not in run.graph.claims:
            raise ToolError(f"unknown claim_id {a['claim_id']}")
        d = run.graph.trace(a["claim_id"])
        return {"text": json.dumps(d, indent=2), "structured": d}

    def t_pricing(self, a: dict) -> dict:
        d: dict[str, Any] = {"plans": {k: p.__dict__ for k, p in PLANS.items()}}
        if a.get("standard_units") is not None:
            su, hj = float(a["standard_units"]), int(a.get("heavy_jobs", 0))
            d["bills"] = {k: monthly_bill(k, su, hj) for k in PLANS if k != "free"}
            d["cheapest"] = cheapest_plan(su, hj)
        d["example_estimate"] = estimate("payg", 40).as_dict()
        return {"text": json.dumps(d, indent=2, default=list), "structured": json.loads(json.dumps(d, default=list))}

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
