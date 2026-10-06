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
from ..discovery.errors import DiscoveryError
from ..evidence.types import to_dict
from ..intent.compiler import compile_intent
from ..kernel.pipeline import RunResult, estimate_run, run_investigation
from ..kernel.receipt import verify_receipt
from ..kernel.knowledge_map import export_knowledge_map
from ..kernel.state import export_state
from ..models.provider import default_provider
from ..orbital.catalog import IMAGING_SATELLITES, fetch_tles, load_tles
from ..orbital.propagate import PROPAGATOR, find_passes
from ..orbital.tle import parse_tle_text
from ..report.markdown import render_markdown
from ..research.planner import gap_unknowns, plan_research
from ..verification.engine import Verifier

SUPPORTED_PROTOCOLS = ("2025-06-18", "2025-03-26", "2024-11-05")
CONTRACT = "lofgren.mcp/3"  # V1 and V2 tools unchanged; V3 production tools added
INSTRUCTIONS = (
    "Lofgren Intelligence is an evidence layer. Typical flow: `compile_objective` -> `plan_research` "
    "(shows price) -> `investigate` (returns a run_id and typed findings) -> `get_finding`, "
    "`find_contradictions`, `find_gaps`, `trace_claim`, `get_receipt`, `export_state`. `verify_claim` tests one "
    "statement. `render_report` gives a human-readable view. Confidence is provisional until calibrated; treat "
    "'contested' and 'supported' (single-source) findings as unsettled, and never present a hypothesis as a finding. "
    "Discovery (V2): `discover` takes a run_id plus an optional structured design space and returns a discovery_id, "
    "an outcome and typed candidates; `generate_hypotheses`, `find_connections`, `find_discovery_gaps`, "
    "`generate_candidates`, `verify_discovery`, `get_discovery_receipt`, `create_v3_handoff` and "
    "`render_discovery_report` read a discovery; `find_prior_art`, `simulate_candidate`, `analyze_sensitivity` and "
    "`optimize_solution` run ad hoc. Simulated values are predictions, hypotheses are not established, and a "
    "prior-art miss never means novelty. Production (V3): `build_artifact` turns a discovery's V3 handoff into a "
    "tested, independently verified artifact (an artifact_id); `verify_artifact`, `get_artifact_file`, "
    "`get_production_receipt` and `create_v4_handoff` read it. V3 grants no authority to act. Contract "
    + CONTRACT + "."
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
_DISC = {"discovery_id": {"type": "string", "description": "The discovery_id returned by discover."}}
_DESIGN = {"type": "object", "description": "Structured design space: model, value_metric, success, distributions, "
                                            "assumptions, constraints, candidates, optimization, decision_rule, "
                                            "simulation (see docs/DISCOVERY.md)."}
_PRIOR_ART = {"type": "object", "description": "Prior-art fixture: subject, queries, records, coverage "
                                               "{sources, domains, time_range, limitations}."}


def _out_schema(required: list[str], props: dict | None = None) -> dict:
    return {"type": "object", "required": required, "properties": props or {}}


_KINDED = {"kind": {"type": "string"}}
DISCOVERY_TOOLS: list[dict[str, Any]] = [
    {"name": "export_knowledge_map",
     "description": "The run's knowledge-map/2 (lofgren.knowledge-map/2): claims with question associations and "
                    "assessments, evidence and source provenance, lineage, findings with derivation, fingerprints and "
                    "the research receipt it is bound to.",
     "inputSchema": _obj(["run_id"], _RUN),
     "outputSchema": _out_schema(["schema", "research_id", "fingerprint", "receipt"])},
    {"name": "discover",
     "description": "Run V2 Discovery on a finished V1 run: frame, prior art, gaps, hypotheses and counter-hypotheses, "
                    "connections, candidates with simulation, sensitivity and optimization, the discovery verifier, an "
                    "explicit decision, a receipt and (when a candidate is selected) a V3 handoff.",
     "inputSchema": _obj(["run_id", "objective"], {**_RUN, "objective": {"type": "string"}, "design": _DESIGN,
                                                    "prior_art": _PRIOR_ART}),
     "outputSchema": _out_schema(["discovery_id", "outcome", "candidates", "hypotheses", "findings", "kind"],
                                 {"outcome": {"type": "string"}, "kind": {"const": "discovery"}})},
    {"name": "find_prior_art",
     "description": "Search a prior-art corpus and conclude only what its coverage supports: match found, no match "
                    "within coverage (never novelty), or incomplete.",
     "inputSchema": _obj(["run_id", "subject", "queries", "records", "coverage"],
                         {**_RUN, "subject": {"type": "string"}, "queries": {"type": "array"},
                          "records": {"type": "array"}, "coverage": {"type": "object"},
                          "domains": {"type": "array"}, "time_range": {"type": "array"}}),
     "outputSchema": _out_schema(["kind", "conclusion", "statement"])},
    {"name": "find_discovery_gaps",
     "description": "The gaps of a discovery, typed, with basis (unknown, contradiction, search absence) and capped "
                    "confidence for search absences.",
     "inputSchema": _obj(["discovery_id"], _DISC), "outputSchema": _out_schema(["kind", "gaps"])},
    {"name": "find_connections",
     "description": "Connections of a discovery: observed (same evidence), derived (same place/period, order, shared "
                    "variable) or speculative (feeds hypotheses only).",
     "inputSchema": _obj(["discovery_id"], _DISC), "outputSchema": _out_schema(["kind", "connections"])},
    {"name": "generate_hypotheses",
     "description": "Hypotheses and counter-hypotheses of a discovery with their evidence requirements. Never facts.",
     "inputSchema": _obj(["discovery_id"], _DISC),
     "outputSchema": _out_schema(["kind", "confidence_kind", "hypotheses", "requirements"])},
    {"name": "generate_candidates",
     "description": "Candidates of a discovery with separate measures (technical, economic, expected value, "
                    "robustness), statuses and the decision rule. Order is not ranking.",
     "inputSchema": _obj(["discovery_id"], _DISC), "outputSchema": _out_schema(["kind", "candidates", "decision"])},
    {"name": "simulate_candidate",
     "description": "Simulate one parameter set with a structured model (deterministic, or Monte Carlo with a seed). "
                    "Results are predictions with kind 'simulated'.",
     "inputSchema": _obj(["run_id", "model", "parameters"],
                         {**_RUN, "model": {"type": "object"}, "parameters": {"type": "object"},
                          "distributions": {"type": "object"}, "success": {"type": "object"},
                          "seed": {"type": "integer"}, "iterations": {"type": "integer"}}),
     "outputSchema": _out_schema(["kind", "confidence_kind", "simulation"])},
    {"name": "analyze_sensitivity",
     "description": "One-at-a-time sensitivity of a model around a parameter set: elasticities, break-even points, "
                    "failure thresholds, robust ranges and robustness.",
     "inputSchema": _obj(["run_id", "model", "parameters"],
                         {**_RUN, "model": {"type": "object"}, "parameters": {"type": "object"},
                          "success": {"type": "object"}}),
     "outputSchema": _out_schema(["kind", "sensitivity"])},
    {"name": "optimize_solution",
     "description": "Solve a structured optimization problem (exhaustive, exact simplex or grid) and re-check the "
                    "answer independently. 'optimal' only with a proof; grid search is never optimal.",
     "inputSchema": _obj(["run_id", "problem"], {**_RUN, "problem": {"type": "object"}}),
     "outputSchema": _out_schema(["kind", "status", "proof", "verified"])},
    {"name": "verify_discovery",
     "description": "Re-check a discovery: verifier issues, receipt integrity, receipt-object agreement and handoff "
                    "validation.",
     "inputSchema": _obj(["discovery_id"], _DISC),
     "outputSchema": _out_schema(["kind", "receipt_intact", "issues", "handoff_problems"])},
    {"name": "get_discovery_receipt",
     "description": "The discovery receipt (lofgren.discovery-receipt/1) and whether it is intact.",
     "inputSchema": _obj(["discovery_id"], _DISC), "outputSchema": _out_schema(["kind", "intact", "receipt"])},
    {"name": "create_v3_handoff",
     "description": "The validated V3 handoff (lofgren.v3-handoff/1) for the selected candidate, or why there is none.",
     "inputSchema": _obj(["discovery_id"], _DISC), "outputSchema": _out_schema(["kind", "ready"])},
    {"name": "render_discovery_report",
     "description": "Human-readable Markdown view of a discovery.",
     "inputSchema": _obj(["discovery_id"], _DISC)},
]
_ART = {"artifact_id": {"type": "string", "description": "The artifact_id returned by build_artifact."}}
PRODUCTION_TOOLS: list[dict[str, Any]] = [
    {"name": "build_artifact",
     "description": "V3: compile a discovery's validated V3 handoff into numbered requirements, plan and generate the "
                    "artifact (manifest, plan, checks, README, Python module, acceptance tests), run its generated "
                    "tests in an isolated interpreter, verify it independently and return a tamper-evident receipt. "
                    "Grants no authority to act.",
     "inputSchema": _obj(["discovery_id"], {**_DISC, "kind": {
         "type": "string", "enum": ["structured_bundle", "markdown", "python_module"],
         "description": "Artifact form (default structured_bundle)."}}),
     "outputSchema": _out_schema(["kind", "artifact_id", "files", "checks", "verification", "receipt_hash"],
                                 {"kind": {"const": "artifact"}})},
    {"name": "verify_artifact",
     "description": "Re-run the independent verifier on a built artifact: regeneration, fingerprint, provenance, "
                    "traceability, re-evaluated checks, the generated tests and the receipt binding.",
     "inputSchema": _obj(["artifact_id"], _ART),
     "outputSchema": _out_schema(["kind", "artifact_id", "passed", "problems"])},
    {"name": "get_artifact_file",
     "description": "The content of one file of a built artifact, with its SHA-256.",
     "inputSchema": _obj(["artifact_id", "path"], {**_ART, "path": {"type": "string"}}),
     "outputSchema": _out_schema(["kind", "path", "sha256", "content"])},
    {"name": "get_production_receipt",
     "description": "The production receipt (lofgren.production-receipt/1) and whether it is intact and bound to the "
                    "artifact.",
     "inputSchema": _obj(["artifact_id"], _ART),
     "outputSchema": _out_schema(["kind", "intact", "receipt"])},
    {"name": "create_v4_handoff",
     "description": "The validated V3 -> V4 handoff (lofgren.v4-handoff/1): the verified artifact for V4 to act on "
                    "only under a capability grant and authorization. It requests and grants nothing.",
     "inputSchema": _obj(["artifact_id"], _ART),
     "outputSchema": _out_schema(["kind", "ready", "handoff", "problems"])},
]
TOOLS = TOOLS + DISCOVERY_TOOLS + PRODUCTION_TOOLS
# Older tool names kept working for existing clients.
ALIASES = {"compile_intent": "compile_objective", "estimate_cost": "plan_research"}


class ToolError(Exception):
    pass


def _out(data: Any) -> dict:
    structured = json.loads(json.dumps(data, default=str))
    return {"text": json.dumps(structured, indent=2), "structured": structured}


class Server:
    def __init__(self, now: datetime | None = None) -> None:
        # `now` pins the session clock: evidence is verified, and passes predicted, as of that instant.
        # None (the default, and what `lofgren mcp` uses unless --as-of is given) reads the real clock.
        if now is not None and now.tzinfo is None:
            raise ValueError("Server(now=...) needs a timezone-aware datetime")
        self.now = now
        self.runs: dict[str, RunResult] = {}
        self.discoveries: dict[str, Any] = {}
        self.artifacts: dict[str, Any] = {}
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
            "export_knowledge_map": self.t_export_knowledge_map,
            "discover": self.t_discover,
            "find_prior_art": self.t_find_prior_art,
            "find_discovery_gaps": self.t_find_discovery_gaps,
            "find_connections": self.t_find_connections,
            "generate_hypotheses": self.t_generate_hypotheses,
            "generate_candidates": self.t_generate_candidates,
            "simulate_candidate": self.t_simulate_candidate,
            "analyze_sensitivity": self.t_analyze_sensitivity,
            "optimize_solution": self.t_optimize_solution,
            "verify_discovery": self.t_verify_discovery,
            "get_discovery_receipt": self.t_get_discovery_receipt,
            "create_v3_handoff": self.t_create_v3_handoff,
            "render_discovery_report": self.t_render_discovery_report,
            "build_artifact": self.t_build_artifact,
            "verify_artifact": self.t_verify_artifact,
            "get_artifact_file": self.t_get_artifact_file,
            "get_production_receipt": self.t_get_production_receipt,
            "create_v4_handoff": self.t_create_v4_handoff,
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

    def _clock(self) -> datetime:
        return self.now or datetime.now(timezone.utc)

    def _verifier(self) -> Verifier | None:
        return Verifier(now=self.now) if self.now is not None else None

    def _get_run(self, a: dict) -> RunResult:
        run = self.runs.get(a.get("run_id", ""))
        if run is None:
            raise ToolError(f"unknown run_id {a.get('run_id')}; runs live for this server session only")
        return run

    def _run(self, objective: str, a: dict) -> dict:
        result = run_investigation(self._contract(objective, a), self._registry(a), default_provider(),
                                   a.get("plan", "payg"), approved=bool(a.get("approve")),
                                   verifier=self._verifier())
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
        start = self._clock()
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

    # -- discovery (V2) --------------------------------------------------------
    def _context(self, a: dict):
        """A fresh, validated knowledge-map/2 context for a session run (one per call: discoveries never share
        a registry)."""
        from ..discovery.context import DiscoveryContext

        run = self._get_run(a)
        return DiscoveryContext(export_knowledge_map(run), run.receipt)

    def _get_discovery(self, a: dict):
        d = self.discoveries.get(a.get("discovery_id", ""))
        if d is None:
            raise ToolError(f"unknown discovery_id {a.get('discovery_id')}; discoveries live for this server session only")
        return d

    def t_export_knowledge_map(self, a: dict) -> dict:
        return _out(export_knowledge_map(self._get_run(a)))

    def t_discover(self, a: dict) -> dict:
        from ..discovery.pipeline import run_discovery
        from ..discovery.report import discovery_summary

        result = run_discovery(self._context(a), a["objective"], design=a.get("design"), prior_art=a.get("prior_art"))
        did = result.receipt["discovery_id"]
        self.discoveries[did] = result
        return _out({"kind": "discovery", "contract": CONTRACT, **discovery_summary(result)})

    def t_find_prior_art(self, a: dict) -> dict:
        from ..discovery.pipeline import _prior_art_provider
        from ..discovery.prior_art import assess_prior_art

        ctx = self._context(a)
        provider, spec = _prior_art_provider({k: a[k] for k in ("records", "coverage") if k in a})
        assessment, meta = assess_prior_art(ctx, a["subject"], list(a["queries"]), provider,
                                            domains=a.get("domains", ()), time_range=tuple(a.get("time_range", (None, None))))
        return _out({"kind": "prior_art_assessment", "id": assessment.id, "conclusion": assessment.conclusion.value,
                     "statement": assessment.statement, "matches": assessment.matches,
                     "limitations": assessment.limitations, "unsearched_areas": assessment.unsearched_areas})

    def t_find_discovery_gaps(self, a: dict) -> dict:
        d = self._get_discovery(a)
        gaps = d.gaps.gaps if d.gaps else ()
        return _out({"kind": "gaps", "discovery_id": a["discovery_id"], "gaps": [g.to_dict() for g in gaps]})

    def t_find_connections(self, a: dict) -> dict:
        d = self._get_discovery(a)
        conns = d.connections.connections if d.connections else ()
        return _out({"kind": "connections", "discovery_id": a["discovery_id"],
                     "connections": [{**c.to_dict(), "confidence_kind": "hypothesis" if c.strength.value == "speculative"
                                      else "evidence"} for c in conns]})

    def t_generate_hypotheses(self, a: dict) -> dict:
        d = self._get_discovery(a)
        return _out({"kind": "hypotheses", "confidence_kind": "hypothesis", "discovery_id": a["discovery_id"],
                     "hypotheses": [h.to_dict() for h in d.hypotheses.all],
                     "requirements": [q.to_dict() for q in d.requirements]})

    def t_generate_candidates(self, a: dict) -> dict:
        d = self._get_discovery(a)
        return _out({"kind": "candidates", "confidence_kind": "candidate_robustness", "discovery_id": a["discovery_id"],
                     "candidates": [c.to_dict() for c in d.candidates.candidates],
                     "decision": d.decision.to_dict() if d.decision else None, "outcome": d.outcome.value})

    def _adhoc(self, a: dict):
        from ..discovery import DiscoveryObjective
        from ..discovery.candidates import DesignSpace, evaluate_candidates
        from ..discovery.frame import frame_problem

        ctx = self._context(a)
        framed = frame_problem(ctx, ctx.ensure(DiscoveryObjective("Ad hoc analysis", ctx.research_id)))
        if framed.frame is None:
            raise ToolError("the run established nothing to frame (insufficient evidence)")
        space = DesignSpace.from_json({"model": a["model"], "success": a.get("success"),
                                       "distributions": a.get("distributions", {}),
                                       "simulation": {k: a[k] for k in ("seed", "iterations") if k in a},
                                       "candidates": [{"description": "Ad hoc parameter set",
                                                       "parameters": a["parameters"]}]})
        return evaluate_candidates(ctx, framed, space)

    def t_simulate_candidate(self, a: dict) -> dict:
        res = self._adhoc(a)
        if not res.simulations:
            raise ToolError("the parameters do not give every model input a value: " + "; ".join(
                c.uncertainty for c in res.candidates))
        return _out({"kind": "simulated", "confidence_kind": "simulation_uncertainty",
                     "simulation": res.simulations[0].to_dict()})

    def t_analyze_sensitivity(self, a: dict) -> dict:
        res = self._adhoc(a)
        if not res.sensitivities:
            raise ToolError("the parameters do not give every model input a value")
        s = res.sensitivities[0]
        return _out({"kind": "sensitivity", "confidence_kind": "candidate_robustness", "sensitivity": s.to_dict(),
                     "swings": res.sensitivity_tables.get(s.candidate_id, {})})

    def t_optimize_solution(self, a: dict) -> dict:
        from ..discovery.optimize import optimize, problem_from_json

        ctx = self._context(a)
        problem = ctx.ensure(problem_from_json(a["problem"]))
        r = optimize(ctx, problem)
        return _out({"kind": "optimization_result", "confidence_kind": "candidate_robustness", **r.to_dict()})

    def t_verify_discovery(self, a: dict) -> dict:
        from ..discovery.handoff import validate_handoff
        from ..discovery.receipt import objects_match_receipt, verify_discovery_receipt

        d = self._get_discovery(a)
        return _out({"kind": "discovery_verification", "receipt_intact": verify_discovery_receipt(d.receipt),
                     "receipt_object_problems": objects_match_receipt(d.receipt, d.context.objects()),
                     "issues": [i.__dict__ for i in d.verifier.issues],
                     "handoff_problems": validate_handoff(d.handoff, d.receipt, d.context) if d.handoff else []})

    def t_get_discovery_receipt(self, a: dict) -> dict:
        from ..discovery.receipt import verify_discovery_receipt

        d = self._get_discovery(a)
        return _out({"kind": "discovery_receipt", "intact": verify_discovery_receipt(d.receipt), "receipt": d.receipt})

    def t_create_v3_handoff(self, a: dict) -> dict:
        d = self._get_discovery(a)
        if d.handoff is None:
            return _out({"kind": "v3_handoff", "ready": False, "outcome": d.outcome.value,
                         "reason": f"no candidate was selected (outcome {d.outcome.value})",
                         "requirements": [q.description for q in d.requirements]})
        return _out({"kind": "v3_handoff", "ready": True, "handoff": d.handoff})

    def t_render_discovery_report(self, a: dict) -> dict:
        from ..discovery.report import render_discovery_markdown

        md = render_discovery_markdown(self._get_discovery(a))
        return {"text": md, "structured": {"discovery_id": a["discovery_id"], "format": "markdown", "report": md}}

    def _get_artifact(self, a: dict):
        r = self.artifacts.get(a.get("artifact_id", ""))
        if r is None:
            raise ToolError(f"unknown artifact_id {a.get('artifact_id')}; artifacts live for this server session only")
        return r

    def t_build_artifact(self, a: dict) -> dict:
        from ..production import build_artifact

        d = self._get_discovery(a)
        if d.handoff is None:
            raise ToolError(f"discovery {a['discovery_id']} selected no candidate (outcome {d.outcome.value}); "
                            "there is nothing to produce")
        r = build_artifact(d.handoff, discovery_receipt=d.receipt, context=d.context,
                           kind=a.get("kind") or "structured_bundle")
        self.artifacts[r.artifact_id] = r
        return _out({"kind": "artifact", "contract": CONTRACT, "artifact_id": r.artifact_id,
                     "artifact_kind": r.artifact["kind"], "fingerprint": r.artifact["fingerprint"],
                     "files": [{k: f[k] for k in ("path", "media_type", "sha256")} for f in r.artifact["files"]],
                     "requirements": r.spec.as_list() if r.spec else [],
                     "checks": [x.as_dict() for x in r.acceptance], "verification": r.verification.as_dict(),
                     "receipt_hash": r.receipt["receipt_hash"], "authority_granted": False})

    def t_verify_artifact(self, a: dict) -> dict:
        from ..production import check_production_receipt, verify_artifact

        r = self._get_artifact(a)
        v = verify_artifact(r.artifact)
        problems = list(v.problems) + check_production_receipt(r.receipt, r.artifact)
        return _out({"kind": "artifact_verification", "artifact_id": r.artifact_id, "passed": not problems,
                     "problems": problems, "tests": v.tests})

    def t_get_artifact_file(self, a: dict) -> dict:
        r = self._get_artifact(a)
        f = next((f for f in r.artifact["files"] if f["path"] == a.get("path")), None)
        if f is None:
            raise ToolError(f"artifact {r.artifact_id} has no file {a.get('path')!r}; files: "
                            f"{[x['path'] for x in r.artifact['files']]}")
        return _out({"kind": "artifact_file", "artifact_id": r.artifact_id, **f})

    def t_get_production_receipt(self, a: dict) -> dict:
        from ..production import check_production_receipt, verify_production_receipt

        r = self._get_artifact(a)
        return _out({"kind": "production_receipt", "intact": verify_production_receipt(r.receipt),
                     "binding_problems": check_production_receipt(r.receipt, r.artifact), "receipt": r.receipt})

    def t_create_v4_handoff(self, a: dict) -> dict:
        from ..production import validate_v4_handoff

        r = self._get_artifact(a)
        problems = validate_v4_handoff(r.v4_handoff, r.receipt, r.artifact)
        return _out({"kind": "v4_handoff", "ready": not problems, "handoff": r.v4_handoff, "problems": problems})

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
                except (ToolError, KeyError, ValueError, FileNotFoundError, DiscoveryError) as exc:
                    result = {"content": [{"type": "text", "text": f"error: {exc}"}], "isError": True}
            else:
                return _error(mid, -32601, f"method not found: {method}")
            return {"jsonrpc": "2.0", "id": mid, "result": result}
        except Exception as exc:  # never crash the server on one bad call
            traceback.print_exc(file=sys.stderr)
            return _error(mid, -32603, f"internal error: {exc}")


def _error(mid: Any, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": mid, "error": {"code": code, "message": message}}


def serve(stdin: TextIO = sys.stdin, stdout: TextIO = sys.stdout, now: datetime | None = None) -> None:
    server = Server(now=now)
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
