"""One capability truth for the hosted MCP service.

`manifest()` reads the tool registry the hosted service actually serves
(`build_mcp`) and joins it with the declared V-level of each tool, whether it
is metered, and what is *not* proven. Docs and tests compare against this
manifest instead of restating the surface by hand:

    python -m lofgren_intelligence.hosted.capabilities > docs/CAPABILITIES.json

A tool registered in `build_mcp` without a level here, or a level entry for a
tool that is not registered, makes `manifest()` raise, so the two cannot drift.
"""

from __future__ import annotations

import asyncio
import json
import sys
from typing import Any

MANIFEST_SCHEMA = "lofgren.hosted-capabilities/1"

LEVELS = ("V1", "V2", "V3", "V4", "V5", "V6", "account")

LEVEL_TITLES = {
    "V1": "Evidence intelligence (research and verification)",
    "V2": "Discovery intelligence",
    "V3": "Production (verified artifacts)",
    "V4": "Authorized execution (browser-approved actions)",
    "V5": "Outcome measurement",
    "V6": "Reviewed improvement evaluation",
    "account": "Account, usage and billing",
}

# Every hosted tool and the level it belongs to.
TOOL_LEVELS: dict[str, str] = {
    "clarify_objective": "V1",
    "case_status": "V1",
    "compile_objective": "V1",
    "plan_research": "V1",
    "investigate": "V1",
    "verify_claim": "V1",
    "get_finding": "V1",
    "find_contradictions": "V1",
    "find_gaps": "V1",
    "trace_claim": "V1",
    "get_receipt": "V1",
    "export_state": "V1",
    "export_knowledge_map2": "V1",
    "render_report": "V1",
    "satellite_passes": "V1",
    "discover": "V2",
    "find_prior_art": "V2",
    "find_discovery_gaps": "V2",
    "find_connections": "V2",
    "generate_hypotheses": "V2",
    "generate_candidates": "V2",
    "simulate_candidate": "V2",
    "analyze_sensitivity": "V2",
    "optimize_solution": "V2",
    "verify_discovery": "V2",
    "get_discovery_receipt": "V2",
    "create_v3_handoff": "V2",
    "render_discovery_report": "V2",
    "build_artifact": "V3",
    "get_artifact": "V3",
    "propose_action": "V4",
    "action_status": "V4",
    "execute_action": "V4",
    "measure_outcome": "V5",
    "get_outcome": "V5",
    "evaluate_improvement": "V6",
    "get_improvement": "V6",
    "pricing": "account",
    "account_status": "account",
    "usage_status": "account",
    "create_checkout": "account",
    "billing_portal": "account",
}

# Tools registered on the hosted endpoint but switched off unless the operator
# enables them. They are listed (the registry serves them) with the gate named.
GATED: dict[str, str] = {
    "create_checkout": "LI_BILLING_ENABLED and the P95 economic certification gate",
}

# Tools that reserve usage before running (units decided per call).
RESERVED_METERING = ("investigate", "verify_claim", "discover", "build_artifact", "measure_outcome",
                     "evaluate_improvement")

# Claims that no test or certification in this repository proves. The
# manifest states them so docs cannot claim otherwise.
NOT_PROVEN = (
    "deployment: no hosted deployment is verified by this repository",
    "real clients: no real third-party MCP client session is verified",
    "backup/restore: database backup and restore are not exercised",
    "PublicMCPReady: the public release gate (scripts/public_mcp_gate.py) has not passed",
    "Supabase owner settings: email confirmation and CAPTCHA are not verified",
)


def registered_tools(base_url: str = "https://capabilities.invalid") -> list[str]:
    """Names of the tools the hosted MCP server registers (no network, no store access)."""
    from .mcp_sdk import build_mcp

    server = build_mcp(base_url)
    tools = asyncio.run(server.list_tools())
    return sorted(t.name for t in tools)


def manifest(tool_names: list[str] | None = None) -> dict[str, Any]:
    from .service import ADHOC_UNIT_COSTS

    names = sorted(tool_names if tool_names is not None else registered_tools())
    unclassified = sorted(set(names) - set(TOOL_LEVELS))
    stale = sorted(set(TOOL_LEVELS) - set(names))
    if unclassified or stale:
        raise ValueError(f"capability manifest drift: unclassified={unclassified} not_registered={stale}")

    tools = []
    for name in names:
        if name in ADHOC_UNIT_COSTS:
            metering: dict[str, Any] = {"kind": "flat", "units_per_call": ADHOC_UNIT_COSTS[name]}
        elif name in RESERVED_METERING:
            metering = {"kind": "reserved"}
        else:
            metering = {"kind": "none"}
        tools.append({
            "name": name,
            "level": TOOL_LEVELS[name],
            "public": True,
            "gated_by": GATED.get(name),
            "metering": metering,
        })

    levels = {}
    for level in LEVELS:
        members = [t["name"] for t in tools if t["level"] == level]
        levels[level] = {
            "title": LEVEL_TITLES[level],
            "public": any(t["public"] for t in tools if t["level"] == level),
            "tools": members,
        }

    return {
        "schema": MANIFEST_SCHEMA,
        "service": "lofgren-intelligence hosted MCP (/mcp)",
        "public_means": (
            "registered on the authenticated hosted /mcp endpoint for any activated account; "
            "it does not mean deployed, publicly launched or PublicMCPReady"
        ),
        "levels": levels,
        "tools": tools,
        "not_proven": list(NOT_PROVEN),
    }


def render(data: dict[str, Any] | None = None) -> str:
    return json.dumps(data if data is not None else manifest(), indent=2, sort_keys=False) + "\n"


def main() -> int:
    sys.stdout.write(render())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
