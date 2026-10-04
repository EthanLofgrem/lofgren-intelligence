"""Stateless remote MCP dispatcher backed by the hosted service."""

from __future__ import annotations

import json
from typing import Any, Callable

from .. import __version__
from .auth import Principal
from .service import PaymentRequired, PublicService, PublicServiceError, QuotaExceeded
from .security import PublicInputError

SUPPORTED_PROTOCOLS = ("2025-06-18", "2025-03-26", "2024-11-05")
INSTRUCTIONS = (
    "Lofgren Intelligence is a governed evidence layer. Use compile_objective and plan_research before expensive "
    "work. investigate and verify_claim produce durable run IDs. Inspect findings, contradictions, gaps, provenance "
    "and receipts before presenting a conclusion. V1 is certified; later V2-V6 capabilities must not be implied "
    "until their own certification gates pass."
)


def _obj(required: list[str], props: dict[str, Any]) -> dict[str, Any]:
    return {"type": "object", "required": required, "properties": props, "additionalProperties": True}


SOURCE_PROPS = {
    "texts": {"type": "object", "additionalProperties": {"type": "string"}},
    "urls": {"type": "array", "items": {"type": "string"}},
    "search": {"type": "string", "enum": ["brave"]},
    "lat": {"type": "number"},
    "lon": {"type": "number"},
    "fetch_orbits": {"type": "boolean"},
    "imagery": {"type": "boolean"},
    "plan": {"type": "string", "enum": ["free", "payg", "researcher", "good_idea"]},
    "max_spend_usd": {"type": "number"},
}
RUN = {"run_id": {"type": "string"}}

TOOLS: list[dict[str, Any]] = [
    {"name": "compile_objective", "description": "Compile an objective into the V1 Outcome Contract.",
     "inputSchema": _obj(["objective"], {"objective": {"type": "string"}, **SOURCE_PROPS})},
    {"name": "plan_research", "description": "Plan research, gaps and estimated work without executing it.",
     "inputSchema": _obj(["objective"], {"objective": {"type": "string"}, **SOURCE_PROPS})},
    {"name": "investigate", "description": "Run the certified V1 evidence loop and persist a durable research run.",
     "inputSchema": _obj(["objective"], {"objective": {"type": "string"}, **SOURCE_PROPS})},
    {"name": "verify_claim", "description": "Verify one factual claim against supplied/discovered evidence.",
     "inputSchema": _obj(["claim"], {"claim": {"type": "string"}, **SOURCE_PROPS})},
    {"name": "get_finding", "description": "Read one durable finding.",
     "inputSchema": _obj(["run_id"], {**RUN, "finding_id": {"type": "string"}, "question_index": {"type": "integer"}})},
    {"name": "find_contradictions", "description": "Read contradictions for a durable run.",
     "inputSchema": _obj(["run_id"], RUN)},
    {"name": "find_gaps", "description": "Read unresolved evidence gaps for a durable run.",
     "inputSchema": _obj(["run_id"], RUN)},
    {"name": "trace_claim", "description": "Trace claim -> evidence -> source provenance.",
     "inputSchema": _obj(["run_id", "claim_id"], {**RUN, "claim_id": {"type": "string"}})},
    {"name": "get_receipt", "description": "Read and verify the research receipt.",
     "inputSchema": _obj(["run_id"], RUN)},
    {"name": "export_state", "description": "Export knowledge-map/1 compatible state.",
     "inputSchema": _obj(["run_id"], RUN)},
    {"name": "export_knowledge_map2", "description": "Export receipt-bound knowledge-map/2 state.",
     "inputSchema": _obj(["run_id"], RUN)},
    {"name": "render_report", "description": "Render the human-readable report view.",
     "inputSchema": _obj(["run_id"], RUN)},
    {"name": "satellite_passes", "description": "Predict open-data imaging satellite passes over a point.",
     "inputSchema": _obj(["lat", "lon"], {
         "lat": {"type": "number"}, "lon": {"type": "number"}, "hours": {"type": "number"},
         "min_elevation_deg": {"type": "number"}, "tle_text": {"type": "string"}, "fetch": {"type": "boolean"},
     })},
    {"name": "pricing", "description": "Inspect provisional plan formulas and usage estimates.",
     "inputSchema": _obj([], {"standard_units": {"type": "number"}, "heavy_jobs": {"type": "integer"}})},
    {"name": "account_status", "description": "Show Founding Free / paid entitlement status.",
     "inputSchema": _obj([], {})},
    {"name": "usage_status", "description": "Show rolling seven-day intelligence-unit usage and quota.",
     "inputSchema": _obj([], {})},
    {"name": "create_checkout", "description": "Create Stripe-hosted Checkout for a paid-required account.",
     "inputSchema": _obj([], {"base_url": {"type": "string"}})},
]


def _error(mid: Any, code: int, message: str, data: Any = None) -> dict[str, Any]:
    body: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        body["data"] = data
    return {"jsonrpc": "2.0", "id": mid, "error": body}


class RemoteMCP:
    def __init__(self, service: PublicService, principal: Principal, base_url: str) -> None:
        self.service = service
        self.principal = principal
        self.base_url = base_url.rstrip("/")
        uid = principal.user_id
        self.handlers: dict[str, Callable[[dict[str, Any]], Any]] = {
            "compile_objective": service.compile_objective,
            "plan_research": service.plan_research,
            "investigate": lambda a: service.investigate(uid, a),
            "verify_claim": lambda a: service.verify_claim(uid, a),
            "get_finding": lambda a: service.get_finding(uid, a),
            "find_contradictions": lambda a: service.find_contradictions(uid, a),
            "find_gaps": lambda a: service.find_gaps(uid, a),
            "trace_claim": lambda a: service.trace_claim(uid, a),
            "get_receipt": lambda a: service.get_receipt(uid, a),
            "export_state": lambda a: service.export_state(uid, a),
            "export_knowledge_map2": lambda a: service.export_knowledge_map2(uid, a),
            "render_report": lambda a: service.render_report(uid, a),
            "satellite_passes": service.satellite_passes,
            "pricing": service.pricing,
            "account_status": lambda a: service.account_status(uid),
            "usage_status": lambda a: service.usage_status(uid),
            "create_checkout": lambda a: service.checkout(uid, str(a.get("base_url") or self.base_url)),
        }

    def handle(self, msg: dict[str, Any]) -> dict[str, Any] | None:
        method = msg.get("method")
        mid = msg.get("id")
        if mid is None:
            return None
        if method == "initialize":
            requested = (msg.get("params") or {}).get("protocolVersion")
            version = requested if requested in SUPPORTED_PROTOCOLS else SUPPORTED_PROTOCOLS[0]
            return {
                "jsonrpc": "2.0",
                "id": mid,
                "result": {
                    "protocolVersion": version,
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": {"name": "lofgren-intelligence", "version": __version__},
                    "instructions": INSTRUCTIONS,
                },
            }
        if method == "ping":
            return {"jsonrpc": "2.0", "id": mid, "result": {}}
        if method == "tools/list":
            return {"jsonrpc": "2.0", "id": mid, "result": {"tools": TOOLS}}
        if method != "tools/call":
            return _error(mid, -32601, f"method not found: {method}")

        params = msg.get("params") or {}
        name = params.get("name")
        args = params.get("arguments") or {}
        handler = self.handlers.get(str(name))
        if handler is None:
            return _error(mid, -32602, f"unknown tool: {name}")
        try:
            data = handler(args)
            structured = json.loads(json.dumps(data, default=str))
            text = json.dumps(structured, indent=2, default=str)
            return {
                "jsonrpc": "2.0",
                "id": mid,
                "result": {
                    "content": [{"type": "text", "text": text}],
                    "structuredContent": structured,
                    "isError": False,
                },
            }
        except PaymentRequired as exc:
            return _error(mid, -32042, str(exc), {"kind": "payment_required", "tool": "create_checkout"})
        except QuotaExceeded as exc:
            return _error(mid, -32043, str(exc), {"kind": "quota_exceeded"})
        except (PublicServiceError, PublicInputError, KeyError, ValueError) as exc:
            return _error(mid, -32602, str(exc))
