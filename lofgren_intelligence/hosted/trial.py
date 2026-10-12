"""No-login trial MCP endpoint (`/mcp-trial`) on the dedicated MCP surface.

The trial is a second, separate MCP server. It never shares the authenticated
`/mcp` server's tools, token verifier or user identity:

* Only the tools in `TRIAL_TOOLS` are registered. Each declares the OpenAI Apps
  SDK per-tool security scheme `[{"type": "noauth"}]` (top-level
  `securitySchemes` on the tool descriptor, mirrored in `_meta`).
* Any other tool name (every protected `/mcp` tool included) is refused with
  ACCOUNT_REQUIRED and a pointer to the authenticated resource.
* Trial tools run the real LI kernel (`run_investigation`, the verifier, the
  native research receipt and, for comparisons, `run_discovery`) over bounded
  public-source retrieval, synchronously, and return artifacts and receipts
  inline. Nothing is persisted: no run, Case, job, usage reservation or
  account row is created or read.
* Identity is always "anonymous". An MCP session id, a bearer token or any
  argument the model supplies is never treated as identity.
* Abuse controls use the existing keyed rate limiter in the database
  (`li_take_keyed_rate_limit`), always store-backed and fail closed: per client
  IP and global request limits, a per-IP tool-call allowance, and an
  operator-wide spending ceiling. If the store is not configured or errors,
  every trial request is refused. There is no in-memory fallback.

See docs/MCP_TRIAL.md.
"""

from __future__ import annotations

import asyncio
import contextvars
import json
import math
import os
import time
import threading
from dataclasses import asdict, dataclass
from typing import Any, Callable

from mcp.server import MCPServer
from mcp.server.auth.middleware.auth_context import auth_context_var
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import CallToolResult, TextContent, ToolAnnotations
from starlette.requests import Request
from starlette.responses import JSONResponse

from . import ratelimit
from .store import SupabaseStore

TRIAL_PATH = "/mcp-trial"
TRIAL_TOOLS = ("trial_research", "trial_verify_claim", "trial_compare")
NOAUTH: list[dict[str, str]] = [{"type": "noauth"}]
TRIAL_SOURCES = ("europepmc", "clinicaltrials", "operator")


@dataclass(frozen=True)
class TrialBounds:
    """Hard per-call bounds. Constants, not operator-tunable upward."""

    max_sources: int = 1                 # one public source per call
    max_records: int = 5                 # records retrieved from that source
    max_pages: int = 1                   # one page / logical fetch; protected redirects may add HTTP requests
    max_manifest_urls: int = 1           # operator manifest entries, when the operator enables it
    max_request_bytes: int = 16_384      # before JSON parsing
    body_timeout_s: float = 5.0          # total time reading request bytes
    max_concurrent_calls: int = 2        # per process; shared spend controls remain global
    max_tasks: int = 8                   # research plan tasks
    http_timeout_s: float = 10.0         # per upstream request, no retries
    max_seconds: float = 40.0            # wall-clock deadline, checked at every pipeline stage
    max_output_bytes: int = 256_000      # serialized tool result
    max_estimated_usd: float = 2.0       # LI platform-price estimate of one call
    max_question_chars: int = 500
    max_query_chars: int = 200
    max_option_chars: int = 120
    max_options: int = 4


BOUNDS = TrialBounds()
_call_slots = threading.BoundedSemaphore(BOUNDS.max_concurrent_calls)

# Rate buckets (all store-backed, fail closed). Defaults; LI_TRIAL_* env may change them.
TRIAL_IP_REQUESTS = ("trial_ip", "LI_TRIAL_IP_REQUESTS_PER_MINUTE", 30, 60)
TRIAL_GLOBAL_REQUESTS = ("trial_global", "LI_TRIAL_GLOBAL_REQUESTS_PER_MINUTE", 600, 60)
TRIAL_IP_CALLS = ("trial_call_ip", "LI_TRIAL_IP_CALLS_PER_HOUR", 10, 3600)
DEFAULT_SPEND_CEILING_USD = 10.0
DEFAULT_SPEND_WINDOW_S = 86_400
GLOBAL_KEY = "operator"

LIMITATIONS = (
    "Trial: anonymous, bounded, synchronous. Nothing is saved; there is no Case, durable run, job or history.",
    "One public source per call (Europe PMC, ClinicalTrials.gov, or the operator's manifest when enabled), "
    "at most 5 records from one page. Absence of evidence here is not evidence of absence.",
    "The trial uses LI's deterministic heuristic reasoning provider; the authenticated service may use a configured "
    "model provider and broader sources.",
    "Findings are provisional and scoped to the retrieved records; inspect the receipt and provenance.",
    "Consequential or underspecified objectives return clarification questions instead of research; serious work "
    "requires an approved Intelligence Case on a connected account.",
    "No private Cases, runs, jobs, billing, durable work or external execution are reachable from the trial.",
)

_client_ip: contextvars.ContextVar[str | None] = contextvars.ContextVar("li_trial_client_ip", default=None)


class TrialRefused(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code


class TrialUnavailable(TrialRefused):
    def __init__(self, message: str = "trial abuse accounting is unavailable; the trial fails closed. Retry later "
                                      "or connect an account.") -> None:
        super().__init__("TRIAL_UNAVAILABLE", message)


# ---- configuration (unknown or invalid => refuse) ----------------------------------------

def trial_enabled() -> bool:
    raw = os.environ.get("LI_TRIAL_ENABLED", "").strip().lower()
    if raw in ("", "false"):
        return False
    if raw == "true":
        return True
    raise RuntimeError("LI_TRIAL_ENABLED must be true or false")


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        raise TrialUnavailable(f"trial is misconfigured ({name}); the trial fails closed") from None
    if value < 0:
        raise TrialUnavailable(f"trial is misconfigured ({name}); the trial fails closed")
    return value


def spend_ceiling() -> tuple[int, int]:
    """(worst-case calls allowed per window, window seconds) for the operator-wide spending ceiling."""
    raw = os.environ.get("LI_TRIAL_SPEND_CEILING_USD", "").strip()
    try:
        usd = DEFAULT_SPEND_CEILING_USD if not raw else float(raw)
    except ValueError:
        raise TrialUnavailable("trial is misconfigured (LI_TRIAL_SPEND_CEILING_USD); the trial fails closed") from None
    if not math.isfinite(usd) or usd < 0:
        raise TrialUnavailable("trial is misconfigured (LI_TRIAL_SPEND_CEILING_USD); the trial fails closed")
    window = _env_int("LI_TRIAL_SPEND_WINDOW_SECONDS", DEFAULT_SPEND_WINDOW_S)
    # The limiter's opportunistic prune drops rows older than one day, so the window cannot exceed it.
    if not 60 <= window <= 86_400:
        raise TrialUnavailable("trial is misconfigured (LI_TRIAL_SPEND_WINDOW_SECONDS); the trial fails closed")
    return int(math.floor(usd / BOUNDS.max_estimated_usd + 1e-9)), window


def operator_manifest() -> dict[str, Any] | None:
    raw = os.environ.get("LI_TRIAL_SOURCES_MANIFEST", "").strip()
    if not raw:
        return None
    try:
        value = json.loads(raw)
    except ValueError:
        raise TrialUnavailable("trial is misconfigured (LI_TRIAL_SOURCES_MANIFEST); the trial fails closed") from None
    if not isinstance(value, dict):
        raise TrialUnavailable("trial is misconfigured (LI_TRIAL_SOURCES_MANIFEST); the trial fails closed")
    return value


# ---- abuse accounting: keyed limiter, store-backed only --------------------------------

def _store() -> Any:
    try:
        return SupabaseStore()
    except Exception:
        raise TrialUnavailable() from None


def _take(bucket: str, key: str, limit: int, window: int) -> float:
    """One store-backed take. Any store problem refuses (never an in-memory fallback, never allow)."""
    if limit <= 0:
        return float(window)
    store = _store()
    try:
        wait = ratelimit.take(bucket, key, store=store, limit=limit, window=window)
    except Exception:
        raise TrialUnavailable() from None
    try:
        wait = float(wait)
    except (TypeError, ValueError):
        raise TrialUnavailable() from None
    if not math.isfinite(wait) or wait < 0:
        raise TrialUnavailable()
    return wait


def take_request_allowance(ip: str) -> float:
    """HTTP-level per-IP then global request limits. Returns 0 or seconds to wait."""
    for bucket, env, default, window in (TRIAL_IP_REQUESTS, TRIAL_GLOBAL_REQUESTS):
        key = ip if bucket == "trial_ip" else GLOBAL_KEY
        wait = _take(bucket, key, _env_int(env, default), window)
        if wait:
            return wait
    return 0.0


def _take_call_allowance() -> None:
    bucket, env, default, window = TRIAL_IP_CALLS
    ip = _client_ip.get()
    if not ip:
        # No request context means no accountable caller: refuse rather than run unaccounted.
        raise TrialUnavailable()
    wait = _take(bucket, ip, _env_int(env, default), window)
    if wait:
        raise TrialRefused("TRIAL_RATE_LIMITED", f"the per-client trial allowance is used; retry in about "
                           f"{max(1, math.ceil(wait))} s, or connect an account for regular use")


def _reserve_spend() -> None:
    calls, window = spend_ceiling()
    wait = _take("trial_spend", GLOBAL_KEY, calls, window)
    if wait:
        raise TrialRefused("TRIAL_CEILING_REACHED", "the operator-wide trial spending ceiling for the current "
                           "window is reached; nothing ran. Retry later or connect an account.")


# ---- the real pipeline, bounded ------------------------------------------------------

def _text(value: Any, name: str, limit: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise TrialRefused("TRIAL_INVALID_INPUT", f"{name} must be a nonempty string")
    value = value.strip()
    if len(value) > limit:
        raise TrialRefused("TRIAL_INVALID_INPUT", f"{name} is longer than the trial bound of {limit} characters")
    return value


def _registry(source: str, query: str | None):
    from .. import build_registry
    from ..adapters.clinicaltrials import ClinicalTrialsAdapter
    from ..adapters.europepmc import EuropePMCAdapter
    from ..adapters.manifest import ManifestError, OperatorSourcesAdapter, parse_manifest
    from ..adapters.public_api import PublicHTTPClient

    if source not in TRIAL_SOURCES:
        raise TrialRefused("TRIAL_INVALID_INPUT", "source must be one of: " + ", ".join(TRIAL_SOURCES))
    registry = build_registry(texts=None, urls=None, fetch_orbits=False, imagery=False, search=None)
    if source == "operator":
        manifest_doc = operator_manifest()
        if manifest_doc is None:
            raise TrialRefused("TRIAL_INVALID_INPUT", "the operator source manifest is not enabled on this trial")
        try:
            manifest = parse_manifest(json.dumps(manifest_doc, sort_keys=True, allow_nan=False).encode("utf-8"))
        except (ManifestError, ValueError, TypeError):
            raise TrialUnavailable("the operator trial manifest is invalid; the trial fails closed") from None
        if len(manifest.sources) > BOUNDS.max_manifest_urls:
            raise TrialUnavailable("the operator trial manifest exceeds the trial bound; the trial fails closed")
        registry.register(OperatorSourcesAdapter(manifest, client=PublicHTTPClient(
            "operator_sources", timeout=BOUNDS.http_timeout_s, max_retries=0, min_interval_s=0.0)))
        return registry
    query = _text(query, "query", BOUNDS.max_query_chars)
    adapter = EuropePMCAdapter if source == "europepmc" else ClinicalTrialsAdapter
    key = "europepmc" if source == "europepmc" else "trials"
    try:
        registry.register(adapter(query, max_records=BOUNDS.max_records, max_pages=BOUNDS.max_pages,
                                  client=PublicHTTPClient(key, timeout=BOUNDS.http_timeout_s, max_retries=0,
                                                          min_interval_s=0.0)))
    except ValueError as exc:
        raise TrialRefused("TRIAL_INVALID_INPUT", str(exc)) from None
    return registry


def _clarification(objective: str) -> dict[str, Any] | None:
    from ..intent.clarification import clarify_objective, requires_clarification, to_dict

    if not requires_clarification(objective):
        return None
    out = to_dict(clarify_objective(objective, None))
    return {
        "approval_required": True,
        "researched": False,
        "message": ("This objective is consequential or underspecified for an anonymous trial. Narrow it to one "
                    "factual question, or connect an account to open an Intelligence Case, answer these questions "
                    "and approve the Case Charter in the browser."),
        "clarification": out,
    }


class _Deadline:
    def __init__(self, seconds: float, clock: Callable[[], float] = time.monotonic) -> None:
        self.clock = clock
        self.end = clock() + seconds

    def check(self, stage: str = "") -> None:
        if self.clock() > self.end:
            raise TrialRefused("TRIAL_TIME_BOUND", f"the trial time bound of {BOUNDS.max_seconds:g} s was reached"
                               f"{' before ' + stage if stage else ''}; nothing was saved")


def _research(objective: str, source: str, query: str | None, deadline: _Deadline) -> dict[str, Any]:
    from ..billing.pricing import PLANS
    from ..intent.compiler import compile_intent
    from ..kernel.ledger import CostLedger
    from ..kernel.pipeline import estimate_run, run_investigation
    from ..models.provider import HeuristicProvider
    from ..research.planner import plan_research
    from .snapshots import durable_snapshot

    registry = _registry(source, query)
    contract = compile_intent(objective, max_spend_usd=BOUNDS.max_estimated_usd)
    plan = plan_research(contract, registry)
    network_adapters = {t.adapter_id for t in plan.tasks if t.adapter_id != "documents"}
    if len(plan.tasks) > BOUNDS.max_tasks or len(network_adapters) > BOUNDS.max_sources:
        raise TrialRefused("TRIAL_BOUND_EXCEEDED", "the research plan exceeds the trial bounds; connect an account "
                           "for larger research")
    estimate = estimate_run(plan, "payg")
    if estimate.total_usd > BOUNDS.max_estimated_usd + 1e-9:
        raise TrialRefused("TRIAL_BOUND_EXCEEDED", f"estimated cost ${estimate.total_usd:.2f} exceeds the trial "
                           f"per-call bound of ${BOUNDS.max_estimated_usd:.2f}; nothing ran")
    _reserve_spend()  # worst-case per-call amount against the operator-wide ceiling, before any work
    deadline.check("research")
    result = run_investigation(contract, registry, HeuristicProvider(), "payg", approved=False,
                               ledger=CostLedger(PLANS["payg"].rate), stage_hook=deadline.check)
    deadline.check("snapshot")
    snap = durable_snapshot(result)
    snap["_estimate"] = estimate.as_dict()
    return snap


def _envelope(tool: str, kind: str, snap: dict[str, Any], base_url: str) -> dict[str, Any]:
    from .snapshots import summary

    receipt = snap["receipt"]
    return {
        "trial": True,
        "identity": "anonymous",
        "persisted": False,
        "tool": tool,
        "kind": kind,
        "result": summary(snap),
        "artifacts": {
            "report_markdown": snap["report"],
            "findings": snap["findings"],
            "contradictions": snap["contradictions"],
            "unknowns": snap["unknowns"],
        },
        "receipt": receipt,
        "receipt_intact": bool(snap["receipt_intact"]),
        "provenance": {
            "sources": receipt.get("sources", []),
            "evidence": receipt.get("evidence", []),
            "rejected_evidence": receipt.get("rejected_evidence", []),
            "provider": receipt.get("provider"),
        },
        "estimate": snap.get("_estimate"),
        "bounds": asdict(BOUNDS),
        "limitations": list(LIMITATIONS),
        "account_connection": _account_pointer(base_url),
    }


def _account_pointer(base_url: str) -> dict[str, str]:
    return {
        "resource": base_url.rstrip("/") + "/mcp",
        "authentication": "OAuth 2.1 with PKCE S256",
        "message": "Connect a Lofgren Intelligence account for Cases, durable research, history and the full tool set.",
    }


def _bounded_output(out: dict[str, Any]) -> dict[str, Any]:
    """Keep the receipt; shed the largest re-derivable artifacts first; refuse rather than truncate the receipt."""
    def size() -> int:
        return len(json.dumps(out, default=str).encode("utf-8"))

    shed: list[str] = []
    for drop in ("report_markdown", "findings", "evidence"):
        if size() <= BOUNDS.max_output_bytes:
            break
        if drop == "evidence":
            out["provenance"].pop("evidence", None)
        else:
            out["artifacts"].pop(drop, None)
            if isinstance(out.get("discovery"), dict) and drop == "report_markdown":
                out["discovery"].pop("report_markdown", None)
        shed.append(drop)
    if shed:
        out["output_shed"] = shed
    if size() > BOUNDS.max_output_bytes:
        raise TrialRefused("TRIAL_OUTPUT_BOUND", "the result exceeds the trial output bound even without the report; "
                           "narrow the question or connect an account")
    return out


def _trial_call(tool: str, objective: str, run: Callable[[_Deadline], dict[str, Any]],
                base_url: str) -> dict[str, Any]:
    _take_call_allowance()
    gated = _clarification(objective)
    if gated is not None:
        gated.update({"trial": True, "identity": "anonymous", "persisted": False, "tool": tool,
                      "limitations": list(LIMITATIONS), "account_connection": _account_pointer(base_url)})
        return gated
    if not _call_slots.acquire(blocking=False):
        raise TrialRefused("TRIAL_BUSY", "this trial process is at capacity; retry later")
    try:
        deadline = _Deadline(BOUNDS.max_seconds)
        out = run(deadline)
        deadline.check("serialization")
        out = _bounded_output(out)
        deadline.check("delivery")
        return out
    finally:
        # Hold capacity until the synchronous operation actually exits, even on errors.
        _call_slots.release()


# ---- the trial MCP server ----------------------------------------------------------

class TrialMCPServer(MCPServer):
    """Refuses every tool name outside the trial allowlist with a typed, account-pointing limitation."""

    def __init__(self, *args: Any, base_url: str, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.base_url = base_url

    async def list_tools(self) -> Any:
        """Publish the same immutable limits that the runtime enforces.

        Keep validation in the tool bodies so existing typed trial refusals and
        accounting order remain stable; clients can reject oversize requests
        before sending them without gaining any authority from the schema.
        """
        tools = await super().list_tools()
        for item in tools:
            props = item.input_schema.get("properties", {})
            for name, limit in (("question", BOUNDS.max_question_chars),
                                ("claim", BOUNDS.max_question_chars)):
                if name in props:
                    props[name].update(minLength=1, maxLength=limit)
            if "query" in props:
                for branch in props["query"].get("anyOf", []):
                    if branch.get("type") == "string":
                        branch.update(minLength=1, maxLength=BOUNDS.max_query_chars)
            if "source" in props:
                props["source"]["enum"] = list(TRIAL_SOURCES)
            if "options" in props:
                props["options"].update(minItems=2, maxItems=BOUNDS.max_options)
                props["options"]["items"].update(minLength=1, maxLength=BOUNDS.max_option_chars)
        return tools

    async def call_tool(self, name: str, arguments: dict[str, Any], context: Any = None) -> Any:
        if name not in TRIAL_TOOLS:
            pointer = _account_pointer(self.base_url)
            text = (f"ACCOUNT_REQUIRED: '{name[:64]}' is not available on the anonymous trial. "
                    f"Connect a Lofgren Intelligence account at {pointer['resource']} to use it.")
            return CallToolResult(content=[TextContent(type="text", text=text)], is_error=True,
                                  _meta={"li/account_required": pointer})
        return await super().call_tool(name, arguments, context)


TRIAL_INSTRUCTIONS = (
    "Anonymous, bounded trial of Lofgren Intelligence. Each call runs the real LI research/verification kernel "
    "over one public source with at most 5 records and returns findings, provenance, limitations and the native "
    "receipt inline. Nothing is saved. Consequential or underspecified objectives return clarification questions. "
    "Cases, durable research, history, discovery tools, artifacts and actions require connecting an account."
)

_ANNOTATIONS = ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=False,
                               open_world_hint=True)
_META = {"securitySchemes": NOAUTH, "li/trial_only": True}


def _refusals(fn: Callable[..., Any]) -> Callable[..., Any]:
    import functools

    @functools.wraps(fn)
    def wrapped(*args: Any, **kwargs: Any) -> Any:
        try:
            return fn(*args, **kwargs)
        except TrialRefused as exc:
            raise ToolError(str(exc)) from None

    return wrapped


def build_trial_mcp(base_url: str) -> TrialMCPServer:
    base = base_url.rstrip("/")
    server = TrialMCPServer("Lofgren Intelligence Trial", instructions=TRIAL_INSTRUCTIONS, base_url=base)

    def tool(title: str):
        return lambda fn: server.tool(title=title, annotations=_ANNOTATIONS, meta=dict(_META))(_refusals(fn))

    @tool("Trial: research one question against a public source")
    def trial_research(question: str, source: str = "europepmc", query: str | None = None) -> dict[str, Any]:
        """Anonymous trial. Research one narrow factual question with the real LI pipeline over ONE public source
        (source: europepmc | clinicaltrials | operator, when enabled) using `query` as the source search, at most
        5 records. Returns findings, provenance, limitations and the native receipt inline; nothing is saved."""
        question = _text(question, "question", BOUNDS.max_question_chars)

        def run(deadline: _Deadline) -> dict[str, Any]:
            snap = _research(question, source, query or question, deadline)
            return _envelope("trial_research", "research", snap, base)

        return _trial_call("trial_research", question, run, base)

    @tool("Trial: verify one claim against a public source")
    def trial_verify_claim(claim: str, source: str = "europepmc", query: str | None = None) -> dict[str, Any]:
        """Anonymous trial. Verify one factual claim with the real LI verifier over ONE public source (at most 5
        records). Returns the verification findings, provenance, limitations and native receipt; nothing is saved."""
        claim = _text(claim, "claim", BOUNDS.max_question_chars)
        objective = f"Verify the claim: {claim}"

        def run(deadline: _Deadline) -> dict[str, Any]:
            snap = _research(objective, source, query or claim, deadline)
            return _envelope("trial_verify_claim", "claim_verification", snap, base)

        return _trial_call("trial_verify_claim", objective, run, base)

    @tool("Trial: compare options with bounded discovery")
    def trial_compare(question: str, options: list[str], source: str = "europepmc",
                      query: str | None = None) -> dict[str, Any]:
        """Anonymous trial. Compare 2-4 named options for one question: bounded public-source research (ONE source,
        at most 5 records) followed by LI V2 discovery over that evidence (framing, gaps, connections, hypotheses).
        Both native receipts are returned inline; hypotheses are not facts; nothing is saved."""
        question = _text(question, "question", BOUNDS.max_question_chars)
        if not isinstance(options, list) or not 2 <= len(options) <= BOUNDS.max_options:
            raise ToolError(f"TRIAL_INVALID_INPUT: options must list 2 to {BOUNDS.max_options} items")
        try:
            names = [_text(o, "option", BOUNDS.max_option_chars) for o in options]
        except TrialRefused as exc:
            raise ToolError(str(exc)) from None
        objective = f"Compare {' versus '.join(names)}: {question}"

        def run(deadline: _Deadline) -> dict[str, Any]:
            from ..discovery.context import DiscoveryContext
            from ..discovery.pipeline import run_discovery
            from .snapshots import durable_discovery_snapshot

            snap = _research(objective, source, query or " ".join(names), deadline)
            out = _envelope("trial_compare", "discovery_comparison", snap, base)
            deadline.check("discovery")
            ctx = DiscoveryContext(snap["knowledge_map2"], snap["receipt"])
            disc = durable_discovery_snapshot(run_discovery(ctx, objective))
            deadline.check("output")
            out["options"] = names
            out["discovery"] = {
                "summary": disc["summary"],
                "gaps": disc["gaps"],
                "connections": disc["connections"],
                "hypotheses": disc["hypotheses"],
                "requirements": disc["requirements"],
                "decision": disc["decision"],
                "report_markdown": disc["report"],
                "receipt": disc["receipt"],
                "receipt_intact": bool(disc["receipt_intact"]),
            }
            return out

        return _trial_call("trial_compare", objective, run, base)

    return server


# ---- HTTP wrapper: anonymous context, request limits, securitySchemes on tools/list ----------

class TrialEndpoint:
    """ASGI endpoint for /mcp-trial around the trial server's Streamable HTTP app."""

    def __init__(self, inner: Any) -> None:
        self.inner = inner

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        from starlette.authentication import AuthCredentials, UnauthenticatedUser
        from starlette.concurrency import run_in_threadpool

        # Bound bytes before the MCP SDK parses JSON. A total read deadline also
        # refuses slow/chunked upload bodies; it does not cancel running research.
        chunks = []
        size = 0
        async def read_body():
            nonlocal size
            while True:
                message = await receive()
                if message["type"] == "http.disconnect":
                    return False
                if message["type"] != "http.request":
                    continue
                size += len(message.get("body", b""))
                if size > BOUNDS.max_request_bytes:
                    return False
                chunks.append(message.get("body", b""))
                if not message.get("more_body", False):
                    return True
        try:
            complete = await asyncio.wait_for(read_body(), timeout=BOUNDS.body_timeout_s)
        except asyncio.TimeoutError:
            await JSONResponse({"error": "request_timeout"}, status_code=408,
                               headers={"Cache-Control": "no-store"})(scope, receive, send)
            return
        if not complete:
            if size > BOUNDS.max_request_bytes:
                await JSONResponse({"error": "request_too_large"}, status_code=413,
                                   headers={"Cache-Control": "no-store"})(scope, receive, send)
            return
        delivered = False
        original_receive = receive
        async def replay_receive():
            nonlocal delivered
            if not delivered:
                delivered = True
                return {"type": "http.request", "body": b"".join(chunks), "more_body": False}
            return await original_receive()
        receive = replay_receive

        # Anonymous by construction: drop any credential the request carried or the auth middleware resolved.
        scope = dict(scope)
        scope["headers"] = [(k, v) for k, v in scope.get("headers", [])
                            if k.lower() not in (b"authorization", b"cookie")]
        scope["user"] = UnauthenticatedUser()
        scope["auth"] = AuthCredentials()
        auth_token = auth_context_var.set(None)
        try:
            ip = ratelimit.client_ip(Request(scope))
            try:
                wait = await run_in_threadpool(take_request_allowance, ip)
            except TrialUnavailable:
                await JSONResponse({"error": "temporarily_unavailable",
                                    "error_description": "trial abuse accounting is unavailable; the trial fails "
                                                         "closed"},
                                   status_code=503, headers={"Retry-After": "30", "Cache-Control": "no-store"}
                                   )(scope, receive, send)
                return
            if wait:
                await JSONResponse({"error": "rate_limited",
                                    "error_description": "trial request limit reached; retry later"},
                                   status_code=429, headers={"Retry-After": str(max(1, math.ceil(wait))),
                                                             "Cache-Control": "no-store"})(scope, receive, send)
                return
            ip_token = _client_ip.set(ip)
            try:
                await self.inner(scope, receive, _SecuritySchemeInjector(send))
            finally:
                _client_ip.reset(ip_token)
        finally:
            auth_context_var.reset(auth_token)


class _SecuritySchemeInjector:
    """Copy `_meta.securitySchemes` to the top-level tool field on JSON tools/list results.

    The MCP SDK's Tool model drops unknown fields, while the OpenAI Apps SDK reads `securitySchemes` as a
    top-level tool-descriptor property. JSON responses only (the trial runs with json_response=True).
    """

    def __init__(self, send: Any) -> None:
        self.send = send
        self.start: dict[str, Any] | None = None
        self.body = b""

    async def __call__(self, message: Any) -> None:
        if message["type"] == "http.response.start":
            self.start = dict(message)
            return
        if message["type"] == "http.response.body" and self.start is not None:
            self.body += message.get("body", b"")
            if message.get("more_body"):
                return
            body = _inject(self.start, self.body)
            headers = [(k, v) for k, v in self.start.get("headers", []) if k.lower() != b"content-length"]
            headers.append((b"content-length", str(len(body)).encode("ascii")))
            await self.send({**self.start, "headers": headers})
            await self.send({"type": "http.response.body", "body": body})
            self.start = None
            return
        await self.send(message)


def _inject(start: dict[str, Any], body: bytes) -> bytes:
    content_type = dict((k.lower(), v) for k, v in start.get("headers", [])).get(b"content-type", b"")
    if b"application/json" not in content_type or b'"tools"' not in body:
        return body
    try:
        doc = json.loads(body)
    except ValueError:
        return body
    tools = (doc.get("result") or {}).get("tools") if isinstance(doc, dict) else None
    if not isinstance(tools, list):
        return body
    for item in tools:
        if isinstance(item, dict) and isinstance(item.get("_meta"), dict) and "securitySchemes" in item["_meta"]:
            item["securitySchemes"] = item["_meta"]["securitySchemes"]
    return json.dumps(doc, separators=(",", ":")).encode("utf-8")


class TrialDispatch:
    """Send /mcp-trial to the trial endpoint before the authenticated app's bearer middleware runs.

    The trial never passes through the /mcp token verifier: a bearer header on a trial request is neither
    looked up nor honoured. Every other path, and the lifespan, go to the authenticated app unchanged.
    """

    def __init__(self, app: Any, endpoint: Any) -> None:
        self.app = app
        self.endpoint = endpoint

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope.get("type") == "http" and scope.get("path", "").rstrip("/") == TRIAL_PATH:
            await self.endpoint(scope, receive, send)
            return
        await self.app(scope, receive, send)


def mount_trial(app: Any, base_url: str, **http_kwargs: Any) -> TrialDispatch:
    """Wrap the dedicated MCP Starlette app with /mcp-trial and run the trial session manager in its lifespan."""
    import contextlib

    server = build_trial_mcp(base_url)
    trial_app = server.streamable_http_app(streamable_http_path=TRIAL_PATH, **http_kwargs)
    endpoint = next(r.endpoint for r in trial_app.routes if getattr(r, "path", None) == TRIAL_PATH)
    outer = app.router.lifespan_context

    @contextlib.asynccontextmanager
    async def lifespan(a: Any):
        async with outer(a) as state:
            async with server.session_manager.run():
                yield state

    app.router.lifespan_context = lifespan
    return TrialDispatch(app, TrialEndpoint(endpoint))


def trial_manifest(base_url: str = "https://capabilities.invalid") -> dict[str, Any]:
    import asyncio

    tools = asyncio.run(build_trial_mcp(base_url).list_tools())
    names = sorted(t.name for t in tools)
    if names != sorted(TRIAL_TOOLS):
        raise ValueError(f"trial manifest drift: registered={names}")
    return {
        "endpoint": TRIAL_PATH,
        "surface": "LI_DEPLOYMENT_SURFACE=mcp with LI_TRIAL_ENABLED=true (disabled by default)",
        "authentication": "none (anonymous); identity is never derived from session ids, tokens or arguments",
        "tools": [{"name": t.name, "trial_only": True, "security_schemes": (t.meta or {}).get("securitySchemes"),
                   "persists": False} for t in sorted(tools, key=lambda t: t.name)],
        "bounds": asdict(BOUNDS),
        "abuse_controls": {
            "per_ip_requests_per_minute": TRIAL_IP_REQUESTS[2],
            "global_requests_per_minute": TRIAL_GLOBAL_REQUESTS[2],
            "per_ip_tool_calls_per_hour": TRIAL_IP_CALLS[2],
            "spend_ceiling_usd_default": DEFAULT_SPEND_CEILING_USD,
            "spend_window_seconds_default": DEFAULT_SPEND_WINDOW_S,
            "backend": "li_take_keyed_rate_limit (database); fail closed, no in-memory fallback",
        },
        "protected_tools": "refused with ACCOUNT_REQUIRED; connect an account at /mcp",
        "not_proven": ["real ChatGPT client: no ChatGPT connector installation or session against /mcp-trial "
                       "has been performed"],
    }

