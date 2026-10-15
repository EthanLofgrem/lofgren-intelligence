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
* Admission is store-backed and fails closed: per client IP and global request
  limits and a per-IP tool-call allowance (`li_take_keyed_rate_limit`, per-IP
  keys are an HMAC under `LI_TRIAL_IP_HMAC_KEY`), a global concurrency slot
  (`li_acquire_trial_slot`) and an operator-wide ceiling denominated in LI
  platform-price estimate dollars. If the store is not configured, errors or
  answers malformed, every trial request is refused. There is no in-memory
  fallback.
* Each call is hard-bounded at runtime: one upstream HTTP request (no retries,
  no redirects, bounded body), and a wall-clock deadline that the call's
  response never outlives (`_run_bounded`) and that closes the upstream socket
  (`trial_fetch.TrialFetcher`).

See docs/MCP_TRIAL.md.
"""

from __future__ import annotations

import asyncio
import contextvars
import hashlib
import hmac
import ipaddress
import json
import math
import os
import threading
import time
import uuid
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
from .trial_fetch import TrialFetcher

TRIAL_PATH = "/mcp-trial"
TRIAL_TOOLS = ("trial_research", "trial_verify_claim", "trial_compare")
NOAUTH: list[dict[str, str]] = [{"type": "noauth"}]
TRIAL_SOURCES = ("europepmc", "clinicaltrials", "operator")


@dataclass(frozen=True)
class TrialBounds:
    """Hard per-call bounds. Constants, not operator-tunable upward."""

    max_sources: int = 1                 # one public source per call
    max_records: int = 5                 # records retrieved from that source
    max_pages: int = 1                   # one page of results
    max_upstream_requests: int = 1       # HTTP requests per call, enforced by TrialFetcher (retries and redirects included)
    max_retries: int = 0                 # no retry of a failed upstream request
    max_redirects: int = 0               # an upstream redirect is refused, not followed
    max_body_bytes: int = 1_000_000      # upstream response body
    max_manifest_urls: int = 1           # operator manifest entries; exactly one page is fetched per call
    max_request_bytes: int = 16_384      # inbound /mcp-trial request body, bounded before JSON parsing
    body_timeout_s: float = 5.0          # total time reading the inbound request body
    max_concurrent_calls: int = 2        # isolated child processes per instance (global admission is the slot)
    max_tasks: int = 8                   # research plan tasks
    http_timeout_s: float = 10.0         # connect and every socket operation, capped by the time left
    max_seconds: float = 40.0            # hard wall-clock bound of a tool call, admission included
    max_output_bytes: int = 256_000      # serialized tool result
    max_estimated_usd: float = 2.0       # LI platform-price estimate of one call (worst case reserved per call)
    max_question_chars: int = 500
    max_query_chars: int = 200
    max_option_chars: int = 120
    max_options: int = 4


BOUNDS = TrialBounds()
# Set only by the fixed internal subprocess entrypoint (trial_child), never by request or environment.
ISOLATION_CHILD_PROCESS = False
# Per-process resource guards. They are not admission: admission is the global database slot
# (_Slot, li_acquire_trial_slot, LI_TRIAL_MAX_CONCURRENT, default 2 across all instances).
# _call_slots bounds the isolated child processes one instance spawns (before the child's admission runs).
# _worker_slots bounds the in-process call workers, sized to the highest valid global setting so it never
# refuses a call the global slot would admit; it stays held until the worker has actually exited, even after
# a timed-out response or an expired slot lease.
_call_slots = threading.BoundedSemaphore(BOUNDS.max_concurrent_calls)
_worker_slots = threading.BoundedSemaphore(10)  # == MAX_CONCURRENT_CEILING (asserted in tests)

# Store calls made by the trial use this per-request timeout (SupabaseStore default: 30 s).
STORE_TIMEOUT_S = 5.0
# HTTP-level admission makes at most this many store calls before the MCP request is handled.
HTTP_ADMISSION_STORE_CALLS = 2
# The deployment's function limit (vercel.json maxDuration for api/index.py).
HOST_LIMIT_S = 60.0
# Concurrency slot lease: longer than the hard per-call bound, so a live call never loses its slot.
SLOT_LEASE_S = int(math.ceil(BOUNDS.max_seconds)) + 20
# Whole-request bound of an isolated child (body read, admission and the bounded call), under HOST_LIMIT_S.
REQUEST_DEADLINE_S = 50.0

# Rate buckets (all store-backed, fail closed). Defaults; LI_TRIAL_* env may change them.
TRIAL_IP_REQUESTS = ("trial_ip", "LI_TRIAL_IP_REQUESTS_PER_MINUTE", 30, 60)
TRIAL_GLOBAL_REQUESTS = ("trial_global", "LI_TRIAL_GLOBAL_REQUESTS_PER_MINUTE", 600, 60)
TRIAL_IP_CALLS = ("trial_call_ip", "LI_TRIAL_IP_CALLS_PER_HOUR", 10, 3600)
DEFAULT_SPEND_CEILING_USD = 10.0
DEFAULT_SPEND_WINDOW_S = 86_400
DEFAULT_MAX_CONCURRENT = 2
MAX_CONCURRENT_CEILING = 10
GLOBAL_KEY = "operator"
PER_IP_BUCKETS = frozenset({"trial_ip", "trial_call_ip"})
IP_HMAC_ENV = "LI_TRIAL_IP_HMAC_KEY"
MIN_IP_HMAC_KEY_CHARS = 32
TRUSTED_PROXY_ENV = "LI_TRIAL_TRUSTED_PROXY"
# Vercel documents both as set by its edge (x-forwarded-for is overwritten, external values are not forwarded);
# x-vercel-forwarded-for is preferred because a proxy in front of Vercel could overwrite x-forwarded-for.
VERCEL_CLIENT_IP_HEADERS = ("x-vercel-forwarded-for", "x-forwarded-for")

LIMITATIONS = (
    "Trial: anonymous, bounded, synchronous. Nothing is saved; there is no Case, durable run, job or history.",
    "One public source per call (Europe PMC, ClinicalTrials.gov, or one page from the operator's manifest when "
    "enabled), one upstream request, at most 5 records. Absence of evidence here is not evidence of absence.",
    "The trial uses LI's deterministic heuristic reasoning provider; the authenticated service may use a configured "
    "model provider and broader sources.",
    "Findings are provisional and scoped to the retrieved records; inspect the receipt and provenance.",
    "Cost figures are estimates, never measurements: the platform-price estimate is what LI's price list would "
    "charge (the trial charges nothing) and the infrastructure estimate uses operator-configured rates.",
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
    """(research calls allowed per window, window seconds) under the operator-wide ceiling.

    The ceiling is denominated in LI platform-price estimate dollars, not in infrastructure cost: every research
    call reserves the worst-case platform-price allocation (`BOUNDS.max_estimated_usd`) before any work, so the
    ceiling is a global call-count containment of floor(ceiling / allocation) calls per window.
    """
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


def research_calls_per_day() -> int:
    """Exact research calls the current ceiling configuration admits per rolling 86,400 s, globally."""
    calls, window = spend_ceiling()
    # A sliding window of `window` s admitting `calls` admits at most calls * ceil(86,400 / window) in any day.
    return calls * math.ceil(86_400 / window)


def max_concurrent() -> int:
    value = _env_int("LI_TRIAL_MAX_CONCURRENT", DEFAULT_MAX_CONCURRENT)
    if not 1 <= value <= MAX_CONCURRENT_CEILING:
        raise TrialUnavailable("trial is misconfigured (LI_TRIAL_MAX_CONCURRENT); the trial fails closed")
    return value


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


# ---- client address and keyed hashing ------------------------------------------------

def trusted_proxy() -> str:
    """'vercel' or 'none'. Forwarding headers are trusted only on Vercel, which sets and overwrites them."""
    raw = os.environ.get(TRUSTED_PROXY_ENV, "").strip().lower()
    on_vercel = os.environ.get("VERCEL", "").strip() == "1"
    if raw == "":
        return "vercel" if on_vercel else "none"
    if raw == "none":
        return "none"
    if raw == "vercel":
        if not on_vercel:
            # Off Vercel these headers are whatever the client sent: never trust them.
            raise TrialUnavailable(f"trial is misconfigured ({TRUSTED_PROXY_ENV}=vercel outside Vercel); the trial "
                                   "fails closed")
        return "vercel"
    raise TrialUnavailable(f"trial is misconfigured ({TRUSTED_PROXY_ENV}); the trial fails closed")


def _normalise_ip(value: str) -> str | None:
    value = value.strip()
    if "/" in value:
        # An IPv6 /64 already keyed by the isolated parent and handed to the child as its socket peer.
        try:
            net = ipaddress.ip_network(value, strict=True)
        except ValueError:
            return None
        return str(net) if isinstance(net, ipaddress.IPv6Network) and net.prefixlen == 64 else None
    try:
        ip = ipaddress.ip_address(value.strip("[]").split("%", 1)[0])
    except ValueError:
        return None
    if isinstance(ip, ipaddress.IPv6Address):
        if ip.ipv4_mapped is not None:
            return str(ip.ipv4_mapped)
        # One subscriber usually holds a whole /64: key IPv6 clients by their /64 so rotation does not reset limits.
        return str(ipaddress.IPv6Network(f"{ip}/64", strict=False))
    return str(ip)


def trial_client_ip(request: Request) -> str:
    """The client address used for per-IP limits.

    On Vercel: the first address in `x-vercel-forwarded-for`, else `x-forwarded-for`, both set by Vercel's edge.
    Anywhere else: the socket peer only; client-supplied forwarding headers (and LI_CLIENT_IP_HEADER) are ignored.
    No usable address refuses (fail closed). An address is not a user: shared egress (ChatGPT, NAT, VPN) puts many
    people behind one address.
    """
    if trusted_proxy() == "vercel":
        for header in VERCEL_CLIENT_IP_HEADERS:
            value = request.headers.get(header, "")
            if value.strip():
                ip = _normalise_ip(value.split(",", 1)[0])
                if ip is None:
                    break
                return ip
        raise TrialUnavailable("the client address could not be determined; the trial fails closed")
    peer = request.client.host if request.client else ""
    ip = _normalise_ip(peer) if peer else None
    if ip is None:
        raise TrialUnavailable("the client address could not be determined; the trial fails closed")
    return ip


def _ip_hmac_key() -> bytes:
    key = os.environ.get(IP_HMAC_ENV, "")
    if len(key.strip()) < MIN_IP_HMAC_KEY_CHARS:
        # Without the secret, per-IP keys would be reversible digests: refuse every trial request instead.
        raise TrialUnavailable(f"trial is misconfigured ({IP_HMAC_ENV} is missing or shorter than "
                               f"{MIN_IP_HMAC_KEY_CHARS} characters); the trial fails closed")
    return key.encode("utf-8")


def trial_key_hash(bucket: str, key: str) -> str:
    """The digest stored for (bucket, key). Per-IP keys: HMAC-SHA256 under the env secret. Global keys: SHA-256 of a
    constant (no client data). The raw IP never leaves the process."""
    if bucket in PER_IP_BUCKETS:
        return hmac.new(_ip_hmac_key(), f"{bucket}:{key}".encode("utf-8"), hashlib.sha256).hexdigest()
    if key != GLOBAL_KEY:
        raise ValueError("global trial buckets take only the operator key")
    return ratelimit.rate_key(bucket, key)


# ---- admission: store-backed only ----------------------------------------------------

def _store() -> Any:
    try:
        return SupabaseStore(timeout=STORE_TIMEOUT_S)
    except Exception:
        raise TrialUnavailable() from None


def _wait_seconds(value: Any) -> float:
    """A limiter answer: a non-negative finite number. Anything else is malformed and refuses."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TrialUnavailable()
    value = float(value)
    if not math.isfinite(value) or value < 0:
        raise TrialUnavailable()
    return value


def _take(bucket: str, key: str, limit: int, window: int) -> float:
    """One store-backed take. Any store problem refuses (never an in-memory fallback, never allow)."""
    if bucket not in ratelimit.DEFAULT_LIMITS:
        raise ValueError(f"unknown rate-limit bucket {bucket!r}")
    key_hash = trial_key_hash(bucket, key)
    if limit <= 0:
        return float(window)
    store = _store()
    try:
        wait = store.take_keyed_rate_limit(bucket, key_hash, int(limit), int(window))
    except Exception:
        raise TrialUnavailable() from None
    return _wait_seconds(wait)


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
        raise TrialRefused("TRIAL_CEILING_REACHED", "the operator-wide trial ceiling for the current window is "
                           "reached; nothing ran. Retry later or connect an account.")


class _Slot:
    """One global concurrency slot (li_acquire_trial_slot), released when the call's worker ends."""

    def __init__(self) -> None:
        self.slot_id = str(uuid.uuid4())
        self.held = False

    def acquire(self) -> None:
        limit = max_concurrent()
        store = _store()
        try:
            answer = store.acquire_trial_slot(self.slot_id, limit, SLOT_LEASE_S)
        except Exception:
            raise TrialUnavailable() from None
        if isinstance(answer, bool) or not isinstance(answer, int) or answer < 0:
            raise TrialUnavailable()
        if answer:
            raise TrialRefused("TRIAL_BUSY", f"the trial is running its maximum of {limit} concurrent call(s); "
                               f"nothing ran. Retry in about {answer} s.")
        self.held = True

    def release(self) -> None:
        if not self.held:
            return
        self.held = False
        try:
            _store().release_trial_slot(self.slot_id)
        except Exception:
            pass  # the lease (SLOT_LEASE_S) reclaims it; release failure never extends capacity


# ---- the real pipeline, bounded ------------------------------------------------------

def _text(value: Any, name: str, limit: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise TrialRefused("TRIAL_INVALID_INPUT", f"{name} must be a nonempty string")
    value = value.strip()
    if len(value) > limit:
        raise TrialRefused("TRIAL_INVALID_INPUT", f"{name} is longer than the trial bound of {limit} characters")
    return value


class _Deadline:
    def __init__(self, seconds: float, clock: Callable[[], float] = time.monotonic) -> None:
        self.clock = clock
        self.end = clock() + seconds
        self.cancelled = False

    def remaining(self) -> float:
        return self.end - self.clock()

    def cancel(self) -> None:
        self.cancelled = True

    def check(self, stage: str = "") -> None:
        if self.cancelled or self.clock() > self.end:
            raise TrialRefused("TRIAL_TIME_BOUND", f"the trial time bound of {BOUNDS.max_seconds:g} s was reached"
                               f"{' before ' + stage if stage else ''}; nothing was saved")


def _pick_manifest_entry(sources: list[dict[str, str]], objective: str) -> dict[str, str]:
    """The one manifest page a call fetches: the best token overlap with the objective; ties go to the first."""
    from ..evidence.types import topic_tokens

    wanted = set(topic_tokens(objective))
    best, best_score = sources[0], -1
    for entry in sources:
        text = " ".join(entry.get(k, "") for k in ("title", "publisher", "note", "url"))
        score = len(set(topic_tokens(text)) & wanted)
        if score > best_score:
            best, best_score = entry, score
    return best


def _registry(source: str, query: str | None, objective: str = "",
              fetcher: TrialFetcher | None = None) -> tuple[Any, Any, TrialFetcher]:
    """(registry, the one network adapter, the call's fetcher). The fetcher is the adapter client's only opener."""
    from .. import build_registry
    from ..adapters.clinicaltrials import ClinicalTrialsAdapter
    from ..adapters.europepmc import EuropePMCAdapter
    from ..adapters.manifest import ManifestError, OperatorSourcesAdapter, SourceManifest, parse_manifest
    from ..adapters.public_api import PublicHTTPClient

    if source not in TRIAL_SOURCES:
        raise TrialRefused("TRIAL_INVALID_INPUT", "source must be one of: " + ", ".join(TRIAL_SOURCES))
    if fetcher is None:
        fetcher = TrialFetcher(deadline_at=time.monotonic() + BOUNDS.max_seconds,
                               max_requests=BOUNDS.max_upstream_requests, max_redirects=BOUNDS.max_redirects,
                               max_body_bytes=BOUNDS.max_body_bytes, op_timeout_s=BOUNDS.http_timeout_s)
    registry = build_registry(texts=None, urls=None, fetch_orbits=False, imagery=False, search=None)

    def client(name: str) -> PublicHTTPClient:
        return PublicHTTPClient(name, timeout=BOUNDS.http_timeout_s, max_retries=BOUNDS.max_retries,
                                min_interval_s=0.0, opener=fetcher)

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
        # One upstream request per call: only the best-matching page is fetched (manifest hash kept for provenance).
        entry = _pick_manifest_entry(manifest.sources, objective or (query or ""))
        one = SourceManifest([entry], manifest.sha256, manifest.path, manifest.description)
        adapter = OperatorSourcesAdapter(one, client=client("operator_sources"))
        registry.register(adapter)
        return registry, adapter, fetcher
    query = _text(query, "query", BOUNDS.max_query_chars)
    cls = EuropePMCAdapter if source == "europepmc" else ClinicalTrialsAdapter
    key = "europepmc" if source == "europepmc" else "trials"
    try:
        adapter = cls(query, max_records=BOUNDS.max_records, max_pages=BOUNDS.max_pages, client=client(key))
    except ValueError as exc:
        raise TrialRefused("TRIAL_INVALID_INPUT", str(exc)) from None
    registry.register(adapter)
    return registry, adapter, fetcher


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


def _retrieval_report(source: str, adapter: Any, fetcher: TrialFetcher) -> dict[str, Any]:
    """How the one source retrieval ended. Read from the adapter itself, never inferred from the findings."""
    from ..adapters.public_api import OUTCOME_COMPLETE, OUTCOME_UNAVAILABLE

    if source == "operator":
        fetched = adapter.fetched
        if fetched is None:
            outcome, errors, records = "not_attempted", ["the source was not queried"], 0
        else:
            ok = [f for f in fetched if f.outcome == OUTCOME_COMPLETE]
            outcome = OUTCOME_COMPLETE if ok and len(ok) == len(fetched) else OUTCOME_UNAVAILABLE
            errors, records = [f.error for f in fetched if f.error], len(ok)
    else:
        r = adapter.retrieval
        if r is None:
            outcome, errors, records = "not_attempted", ["the source was not queried"], 0
        else:
            outcome, errors, records = r.outcome, list(r.errors), r.records
    return {
        "source": source,
        "outcome": outcome,
        "records": records,
        "errors": [str(e)[:300] for e in errors][:5],
        "upstream_requests": fetcher.attempts,
        "upstream_requests_refused": fetcher.refused,
        "upstream_bytes": fetcher.bytes_read,
    }


def cost_report(platform_estimate: dict[str, Any] | None, provider_info: dict[str, Any] | None,
                ledger: Any) -> dict[str, Any]:
    """Platform price and infrastructure cost, kept apart. Both are estimates; neither is a measured cost."""
    from .costing import actual_run_cost

    try:
        infra = actual_run_cost(provider_info or {}, ledger)
        known: float | None = infra.known_cost_usd
        unpriced = list(infra.unpriced_components)
    except ValueError:  # an invalid operator rate: say so rather than guess
        known, unpriced = None, ["invalid_operator_rate"]
    return {
        "platform_price_estimate": {
            "kind": "estimate",
            "measured": False,
            "usd": None if platform_estimate is None else platform_estimate.get("total_usd"),
            "detail": platform_estimate,
            "meaning": "What LI's pay-as-you-go price list would charge for this research plan. The trial charges "
                       "nothing. The trial ceiling is counted in these dollars.",
        },
        "infrastructure_cost_estimate": {
            "kind": "estimate",
            "measured": False,
            "known_usd": known,
            "unpriced_components": unpriced,
            "fully_priced": known is not None and not unpriced,
            "meaning": "LI's cost to serve this call, from hosted/costing.actual_run_cost with operator-configured "
                       "rates (LI_RETRIEVAL_USD_PER_CALL, LI_INFRA_USD_PER_RUN). The heuristic provider makes no "
                       "model call and the sources are free keyless APIs. Components without a configured rate are "
                       "listed as unpriced, never counted as zero. Not a metered cost.",
        },
    }


def _research(objective: str, source: str, query: str | None, deadline: _Deadline,
              slot: _Slot) -> dict[str, Any]:
    from ..billing.pricing import PLANS
    from ..intent.compiler import compile_intent
    from ..kernel.ledger import CostLedger
    from ..kernel.pipeline import estimate_run, run_investigation
    from ..models.provider import HeuristicProvider
    from ..research.planner import plan_research
    from .snapshots import durable_snapshot

    fetcher = TrialFetcher(deadline_at=deadline.end, max_requests=BOUNDS.max_upstream_requests,
                           max_redirects=BOUNDS.max_redirects, max_body_bytes=BOUNDS.max_body_bytes,
                           op_timeout_s=BOUNDS.http_timeout_s, clock=deadline.clock,
                           cancelled=lambda: deadline.cancelled)
    registry, adapter, fetcher = _registry(source, query, objective, fetcher)
    contract = compile_intent(objective, max_spend_usd=BOUNDS.max_estimated_usd)
    plan = plan_research(contract, registry)
    network_adapters = {t.adapter_id for t in plan.tasks if t.adapter_id != "documents"}
    if len(plan.tasks) > BOUNDS.max_tasks or len(network_adapters) > BOUNDS.max_sources:
        raise TrialRefused("TRIAL_BOUND_EXCEEDED", "the research plan exceeds the trial bounds; connect an account "
                           "for larger research")
    estimate = estimate_run(plan, "payg")
    if estimate.total_usd > BOUNDS.max_estimated_usd + 1e-9:
        raise TrialRefused("TRIAL_BOUND_EXCEEDED", f"platform-price estimate ${estimate.total_usd:.2f} exceeds the "
                           f"trial per-call bound of ${BOUNDS.max_estimated_usd:.2f}; nothing ran")
    deadline.check("admission")
    slot.acquire()          # global concurrency slot before any spend or upstream request
    deadline.check("admission")
    _reserve_spend()        # worst-case per-call platform-price allocation against the global ceiling
    deadline.check("research")
    ledger = CostLedger(PLANS["payg"].rate)
    result = run_investigation(contract, registry, HeuristicProvider(), "payg", approved=False,
                               ledger=ledger, stage_hook=deadline.check)
    deadline.check("output")
    retrieval = _retrieval_report(source, adapter, fetcher)
    if retrieval["outcome"] not in ("complete", "partial"):
        raise TrialRefused("TRIAL_SOURCE_FAILED", f"the {source} retrieval failed ({retrieval['outcome']}: "
                           f"{'; '.join(retrieval['errors']) or 'no data'}). No evidence was retrieved, so nothing "
                           f"was concluded. The call counted against the trial ceiling.")
    deadline.check("snapshot")
    snap = durable_snapshot(result)
    from ..kernel.receipt import verify_receipt

    if not snap.get("receipt_intact") or not verify_receipt(snap["receipt"]):
        raise TrialRefused("TRIAL_RECEIPT_INVALID", "the research receipt did not verify; no result is returned")
    snap["_retrieval"] = retrieval
    snap["_cost"] = cost_report(estimate.as_dict(), snap["receipt"].get("provider"), ledger)
    return snap


def _envelope(tool: str, kind: str, snap: dict[str, Any], base_url: str) -> dict[str, Any]:
    from .snapshots import summary

    receipt = snap["receipt"]
    retrieval = snap["_retrieval"]
    out = {
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
        "retrieval": retrieval,
        "provenance": {
            "sources": receipt.get("sources", []),
            "evidence": receipt.get("evidence", []),
            "rejected_evidence": receipt.get("rejected_evidence", []),
            "provider": receipt.get("provider"),
        },
        "cost": snap["_cost"],
        "bounds": asdict(BOUNDS),
        "limitations": list(LIMITATIONS),
        "account_connection": _account_pointer(base_url),
    }
    if retrieval["outcome"] == "partial":
        out["warnings"] = [f"partial retrieval from {retrieval['source']}: " + "; ".join(retrieval["errors"])]
    return out


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


def _run_bounded(fn: Callable[[], dict[str, Any]], deadline: _Deadline) -> dict[str, Any]:
    """Run `fn` in a worker thread and answer by the deadline, whatever the worker is doing.

    At the deadline the caller gets TRIAL_TIME_BOUND; the deadline is cancelled so the worker's next stage check
    stops it, and its upstream socket is already shut by the fetcher's watchdog. The worker releases its
    concurrency slot when it ends.
    """
    box: dict[str, Any] = {}
    ctx = contextvars.copy_context()

    def target() -> None:
        try:
            box["value"] = ctx.run(fn)
        except BaseException as exc:  # noqa: BLE001 - re-raised in the caller
            box["error"] = exc

    worker = threading.Thread(target=target, name="li-trial-call", daemon=True)
    worker.start()
    worker.join(max(0.0, deadline.remaining()))
    if worker.is_alive():
        deadline.cancel()
        raise TrialRefused("TRIAL_TIME_BOUND", f"the trial time bound of {BOUNDS.max_seconds:g} s was reached; "
                           "nothing was saved")
    if "error" in box:
        raise box["error"]
    return box["value"]


def _trial_call(tool: str, objective: str, run: Callable[[_Deadline, _Slot], dict[str, Any]],
                base_url: str) -> dict[str, Any]:
    deadline = _Deadline(BOUNDS.max_seconds)

    def body() -> dict[str, Any]:
        _take_call_allowance()
        gated = _clarification(objective)
        if gated is not None:
            gated.update({"trial": True, "identity": "anonymous", "persisted": False, "tool": tool,
                          "limitations": list(LIMITATIONS), "account_connection": _account_pointer(base_url)})
            return gated
        if not _worker_slots.acquire(blocking=False):
            raise TrialRefused("TRIAL_BUSY", "this trial process is at capacity; nothing ran. Retry later.")
        slot = _Slot()
        try:
            out = run(deadline, slot)
            deadline.check("serialization")
            out = _bounded_output(out)
            deadline.check("delivery")
            return out
        finally:
            slot.release()
            # Held until the synchronous work actually exits, even on errors or a timed-out response.
            _worker_slots.release()

    return _run_bounded(body, deadline)


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

        def run(deadline: _Deadline, slot: _Slot) -> dict[str, Any]:
            snap = _research(question, source, query or question, deadline, slot)
            return _envelope("trial_research", "research", snap, base)

        return _trial_call("trial_research", question, run, base)

    @tool("Trial: verify one claim against a public source")
    def trial_verify_claim(claim: str, source: str = "europepmc", query: str | None = None) -> dict[str, Any]:
        """Anonymous trial. Verify one factual claim with the real LI verifier over ONE public source (at most 5
        records). Returns the verification findings, provenance, limitations and native receipt; nothing is saved."""
        claim = _text(claim, "claim", BOUNDS.max_question_chars)
        objective = f"Verify the claim: {claim}"

        def run(deadline: _Deadline, slot: _Slot) -> dict[str, Any]:
            snap = _research(objective, source, query or claim, deadline, slot)
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

        def run(deadline: _Deadline, slot: _Slot) -> dict[str, Any]:
            from ..discovery.context import DiscoveryContext
            from ..discovery.pipeline import run_discovery
            from .snapshots import durable_discovery_snapshot

            snap = _research(objective, source, query or " ".join(names), deadline, slot)
            out = _envelope("trial_compare", "discovery_comparison", snap, base)
            deadline.check("discovery")
            ctx = DiscoveryContext(snap["knowledge_map2"], snap["receipt"])
            disc = durable_discovery_snapshot(run_discovery(ctx, objective))
            deadline.check("output")
            from ..discovery.receipt import verify_discovery_receipt

            if not disc.get("receipt_intact") or not verify_discovery_receipt(disc["receipt"]):
                raise TrialRefused("TRIAL_RECEIPT_INVALID", "the discovery receipt did not verify; no result is "
                                   "returned")
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

    def __init__(self, inner: Any, transport_security: Any = None) -> None:
        self.inner = inner
        from mcp.server.transport_security import TransportSecurityMiddleware
        self.transport_security = (TransportSecurityMiddleware(transport_security)
                                   if transport_security is not None else None)

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        from starlette.authentication import AuthCredentials, UnauthenticatedUser
        from starlette.concurrency import run_in_threadpool

        request_deadline = time.monotonic() + REQUEST_DEADLINE_S
        if self.transport_security is not None:
            invalid = await self.transport_security.validate_request(
                Request(scope), is_post=scope.get("method") == "POST")
            if invalid is not None:
                await invalid(scope, receive, send)
                return
        isolation = os.environ.get("LI_TRIAL_ISOLATION_MODE", "off").strip().lower()
        if isolation not in {"off", "required"}:
            await JSONResponse({"error": "temporarily_unavailable"}, status_code=503,
                               headers={"Cache-Control": "no-store"})(scope, receive, send)
            return
        isolated_parent = isolation == "required" and not ISOLATION_CHILD_PROCESS
        if isolated_parent and os.environ.get("LI_TRIAL_ISOLATION_VERIFIED", "").strip().lower() != "true":
            await JSONResponse({"error": "temporarily_unavailable",
                                "error_description": "hosted process isolation is not verified"}, status_code=503,
                               headers={"Cache-Control": "no-store"})(scope, receive, send)
            return

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
        if isolated_parent:
            from .process_watchdog import ChildRefused, run_json_child
            from starlette.responses import Response
            if not _call_slots.acquire(blocking=False):
                await JSONResponse({"error": "trial_busy"}, status_code=429,
                                   headers={"Cache-Control": "no-store"})(scope, receive, send)
                return
            async def disconnected():
                while True:
                    message = await receive()
                    if message["type"] == "http.disconnect":
                        return
            allowed_headers = {b"accept", b"content-type", b"mcp-protocol-version", b"mcp-method", b"mcp-name", b"host", b"origin"}
            headers = {k.decode("ascii").lower(): v.decode("latin-1")
                       for k, v in scope.get("headers", []) if k.lower() in allowed_headers}
            try:
                # The parent resolves the client address (trusted proxy headers only on Vercel); the child sees it
                # as its socket peer and never receives forwarding headers.
                child_ip = trial_client_ip(Request(scope))
            except TrialUnavailable:
                _call_slots.release()
                await JSONResponse({"error": "temporarily_unavailable",
                                    "error_description": "trial abuse accounting is unavailable; the trial fails "
                                                         "closed"},
                                   status_code=503, headers={"Retry-After": "30", "Cache-Control": "no-store"}
                                   )(scope, receive, send)
                return
            try:
                # Admission, reservation and research all occur inside the child
                # and are covered by this request-wide process watchdog.
                result = await run_json_child("lofgren_intelligence.hosted.trial_child", {
                    "body": b"".join(chunks).decode("utf-8"), "headers": headers,
                    "method": scope.get("method", "POST"), "ip": child_ip,
                }, deadline=request_deadline, disconnected=disconnected())
                if (set(result) != {"status", "headers", "body"} or
                    not isinstance(result["status"], int) or not 100 <= result["status"] <= 599 or
                    not isinstance(result["headers"], dict) or not isinstance(result["body"], str) or
                    len(result["body"].encode()) > 550_000):
                    raise ChildRefused("invalid child response")
                response_headers = {k: v for k, v in result["headers"].items()
                                    if k in {"content-type", "cache-control", "pragma", "retry-after"}}
                response_headers["cache-control"] = "no-store"
                await Response(result["body"], status_code=result["status"], headers=response_headers)(scope, receive, send)
            except (ChildRefused, UnicodeError, ValueError):
                await JSONResponse({"error": "trial_unavailable",
                                    "error_description": "bounded execution refused; no success result is available"},
                                   status_code=503, headers={"Cache-Control": "no-store"})(scope, receive, send)
            finally:
                # run_json_child kills and reaps before returning or raising.
                _call_slots.release()
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
            try:
                # Fail closed: no trusted client address, no IP HMAC key or no working store refuses the request.
                ip = trial_client_ip(Request(scope))
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
    return TrialDispatch(app, TrialEndpoint(endpoint, http_kwargs.get("transport_security")))


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
            "spend_ceiling_unit": "LI platform-price estimate dollars (not infrastructure cost)",
            "spend_window_seconds_default": DEFAULT_SPEND_WINDOW_S,
            "worst_case_allocation_per_research_call_usd": BOUNDS.max_estimated_usd,
            "research_calls_per_day_default": int(math.floor(DEFAULT_SPEND_CEILING_USD / BOUNDS.max_estimated_usd
                                                             + 1e-9)),
            "max_concurrent_calls_default": DEFAULT_MAX_CONCURRENT,
            "concurrency_slot_lease_seconds": SLOT_LEASE_S,
            "per_ip_key": f"HMAC-SHA256 under {IP_HMAC_ENV}; missing key refuses every trial request",
            "client_address": "Vercel: x-vercel-forwarded-for, else x-forwarded-for (set by Vercel's edge); "
                              "elsewhere: the socket peer only",
            "backend": "li_take_keyed_rate_limit and li_acquire_trial_slot (database); fail closed, no in-memory "
                       "fallback",
        },
        "worst_case_wall_seconds": (BOUNDS.body_timeout_s + BOUNDS.max_seconds
                                    + HTTP_ADMISSION_STORE_CALLS * STORE_TIMEOUT_S),
        "protected_tools": "refused with ACCOUNT_REQUIRED; connect an account at /mcp",
        "not_proven": ["real ChatGPT client: no ChatGPT connector installation or session against /mcp-trial "
                       "has been performed"],
    }
