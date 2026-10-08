"""Official MCP SDK surface for the hosted Lofgren Intelligence service.

The local stdio server remains dependency-free. Public HTTP uses the official
MCP Python SDK so protocol lifecycle, current/legacy Streamable HTTP behavior,
structured output and bearer-resource semantics are not hand-rolled.
"""

from __future__ import annotations

import functools
import os
from datetime import datetime
from typing import Any, Callable

from pydantic import AnyHttpUrl

from mcp.server import MCPServer
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.provider import AccessToken, TokenVerifier
from mcp.server.auth.settings import AuthSettings
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from .auth import token_hash
from .service import PublicService, PublicServiceError
from .store import SupabaseStore
from .routing import AssessmentResult, DesiredOutput, RequestSummary, assess_request


INSTRUCTIONS = (
    "Use LI when source verification, comparisons, persistent research or governed work adds material value. "
    "Do not use it for casual conversation, basic definitions or arithmetic. If suitability is unclear, "
    "li_assess_request can assess a minimized summary without research or case creation. The host must omit LI "
    "when external sharing is prohibited. A routing recommendation grants no execution authority. "
    "Lofgren Intelligence is a governed V1-V6 outcome-intelligence stack. "
    "V1 researches and verifies evidence; V2 discovers supported possibilities; V3 builds verified artifacts; "
    "V4 proposes external actions that require explicit browser approval before execution; V5 measures outcomes; "
    "V6 evaluates reviewed improvements on held-out data without silently changing the running system. "
    "Inspect provenance and receipts at every stage. Hypotheses are not facts, simulations are predictions, "
    "an artifact is not authority, and an executed action is not proof of success."
)


class OpaqueTokenVerifier(TokenVerifier):
    def __init__(self, resource_url: str) -> None:
        self.resource_url = resource_url

    async def verify_token(self, token: str) -> AccessToken | None:
        store = SupabaseStore()
        row = store.get_access_token(token_hash(token))
        if not row:
            return None
        resource = str(row.get("resource") or "")
        if resource != self.resource_url:
            return None
        expiry = row.get("expires_at")
        expires_at: int | None = None
        if expiry:
            try:
                dt = datetime.fromisoformat(str(expiry).replace("Z", "+00:00"))
                expires_at = int(dt.timestamp())
            except ValueError:
                return None
        return AccessToken(
            token=token,
            client_id=str(row.get("client_id") or ""),
            scopes=str(row.get("scope") or "mcp").split(),
            expires_at=expires_at,
            resource=resource,
            subject=str(row.get("user_id") or ""),
            claims={"iss": self.resource_url.rsplit("/mcp", 1)[0]},
        )


def _caller() -> tuple[str, PublicService]:
    token = get_access_token()
    if token is None or not token.subject:
        raise PermissionError("authenticated subject is required")
    store = SupabaseStore()
    limit = int(os.environ.get("LI_MCP_REQUESTS_PER_MINUTE", "60"))
    if not store.take_rate_limit(str(token.subject), "mcp", limit, 60):
        raise PermissionError("MCP request rate limit reached")
    return str(token.subject), PublicService(store)


def _source_args(
    *,
    texts: dict[str, str] | None = None,
    urls: list[str] | None = None,
    search: str | None = None,
        europepmc: str | None = None,
        trials: str | None = None,
        max_records: int | None = None,
        sources_manifest: dict[str, Any] | None = None,
    lat: float | None = None,
    lon: float | None = None,
    fetch_orbits: bool = False,
    imagery: bool = False,
    max_spend_usd: float = 5.0,
) -> dict[str, Any]:
    out: dict[str, Any] = {"max_spend_usd": max_spend_usd}
    if texts is not None:
        out["texts"] = texts
    if urls is not None:
        out["urls"] = urls
    if search is not None:
        out["search"] = search
    for key, value in (("europepmc", europepmc), ("trials", trials), ("max_records", max_records), ("sources_manifest", sources_manifest)):
        if value is not None:
            out[key] = value
    if lat is not None:
        out["lat"] = lat
    if lon is not None:
        out["lon"] = lon
    if fetch_orbits:
        out["fetch_orbits"] = True
    if imagery:
        out["imagery"] = True
    return out


def _public_errors(fn: Callable[..., Any]) -> Callable[..., Any]:
    """Report a PublicServiceError to the client as an anticipated tool error.

    The SDK treats any other exception as a crash: the client sees only
    "Error executing tool <name>" and the server logs a traceback. A
    PublicServiceError is a deliberate, user-facing refusal, so it is re-raised
    as ToolError carrying its stable code and message and nothing else.
    """

    @functools.wraps(fn)
    def wrapped(*args: Any, **kwargs: Any) -> Any:
        try:
            return fn(*args, **kwargs)
        except PublicServiceError as exc:
            raise ToolError(f"{exc.code}: {exc}") from None

    return wrapped


def build_mcp(base_url: str) -> MCPServer:
    base = base_url.rstrip("/")
    resource = base + "/mcp"
    mcp = MCPServer(
        "Lofgren Intelligence",
        instructions=INSTRUCTIONS,
        token_verifier=OpaqueTokenVerifier(resource),
        auth=AuthSettings(
            issuer_url=AnyHttpUrl(base),
            resource_server_url=AnyHttpUrl(resource),
            required_scopes=["mcp"],
            validate_token_resource=True,
        ),
    )

    def tool() -> Callable[[Callable[..., Any]], Any]:
        """Register a hosted tool whose PublicServiceError reaches the client as a typed refusal."""
        return lambda fn: mcp.tool()(_public_errors(fn))

    @mcp.tool(title="Assess suitability for Lofgren Intelligence",
              annotations=ToolAnnotations(read_only_hint=True, destructive_hint=False,
                                          idempotent_hint=True, open_world_hint=False))
    def li_assess_request(request_summary: RequestSummary, desired_output: DesiredOutput) -> AssessmentResult:
        """Assess a minimized summary for evidence-backed persistent work; returns routing only.

        Skip casual chat, definitions and arithmetic. The host must omit LI if external sharing is prohibited.
        No case, source retrieval, research job, paid research reservation or action is created. Requires an
        authorized MCP connection and uses its operational request-rate limit. Does not schedule monitoring.
        Send no secrets or unnecessary personal identifiers. Recommendations never grant execution approval.
        """
        try:
            _caller()  # Existing authorization and operational rate limit, without account activation.
        except PermissionError:
            raise ToolError("REQUEST_DENIED: authenticated identity and request allowance are required") from None
        try:
            return assess_request(request_summary, desired_output)
        except ValueError:
            raise ToolError("INVALID_REQUEST: provide a nonempty bounded summary and supported output") from None

    @tool()
    def clarify_objective(
        objective: str | None = None,
        answers: dict[str, str] | None = None,
        case_id: str | None = None,
        expected_version: int | None = None,
        max_spend_usd: float | None = None,
        max_units: float | None = None,
        texts: dict[str, str] | None = None,
        urls: list[str] | None = None,
        search: str | None = None,
        europepmc: str | None = None,
        trials: str | None = None,
        max_records: int | None = None,
        sources_manifest: dict[str, Any] | None = None,
        lat: float | None = None,
        lon: float | None = None,
    ) -> dict[str, Any]:
        """Open or revise an Intelligence Case before serious research begins.

        Without case_id: opens a case for the objective and returns 3-7 high-information
        questions (or a ready Case Charter). With case_id and expected_version: records the
        answers (an answer, 'unknown', 'skip', 'ask me later' or 'use a reasonable default')
        as a new charter version; a stale expected_version is refused. When the charter is
        ready, approval_url is where the account owner approves it in the browser -- approval
        is never an MCP call, and investigate runs only with that approval.
        """
        user_id, service = _caller()
        args: dict[str, Any] = {"answers": answers or {}}
        if objective is not None:
            args["objective"] = objective
        if case_id is not None:
            args["case_id"] = case_id
        if expected_version is not None:
            args["expected_version"] = expected_version
        budget = {k: v for k, v in (("max_spend_usd", max_spend_usd), ("max_units", max_units)) if v is not None}
        if budget:
            args["budget"] = budget
        for key, value in (("texts", texts), ("urls", urls), ("search", search), ("europepmc", europepmc), ("trials", trials), ("max_records", max_records), ("sources_manifest", sources_manifest), ("lat", lat), ("lon", lon)):
            if value is not None:
                args[key] = value
        return service.clarify_objective(user_id, args, base)

    @tool()
    def case_status(case_id: str) -> dict[str, Any]:
        """Read an Intelligence Case: latest charter version, open questions and approval state."""
        user_id, service = _caller()
        return service.case_status(user_id, {"case_id": case_id}, base)

    @tool()
    def compile_objective(
        objective: str,
        max_spend_usd: float = 5.0,
        lat: float | None = None,
        lon: float | None = None,
    ) -> dict[str, Any]:
        """Compile an objective into the certified V1 Outcome Contract."""
        _, service = _caller()
        return service.compile_objective(_source_args(
            max_spend_usd=max_spend_usd, lat=lat, lon=lon, **{"texts": None}
        ) | {"objective": objective})

    @tool()
    def plan_research(
        objective: str,
        answers: dict[str, str] | None = None,
        case_id: str | None = None,
        texts: dict[str, str] | None = None,
        urls: list[str] | None = None,
        search: str | None = None,
        europepmc: str | None = None,
        trials: str | None = None,
        max_records: int | None = None,
        sources_manifest: dict[str, Any] | None = None,
        lat: float | None = None,
        lon: float | None = None,
        fetch_orbits: bool = False,
        imagery: bool = False,
        max_spend_usd: float = 5.0,
    ) -> dict[str, Any]:
        """Plan research, evidence gaps and estimated work without running it.

        A serious objective returns clarification questions until it is planned through an
        approved Intelligence Case (case_id); the approved charter then supplies the inputs.
        """
        user_id, service = _caller()
        args = _source_args(
            texts=texts, urls=urls, search=search, europepmc=europepmc, trials=trials, max_records=max_records, sources_manifest=sources_manifest, lat=lat, lon=lon,
            fetch_orbits=fetch_orbits, imagery=imagery, max_spend_usd=max_spend_usd,
        ) | {"objective": objective, "answers": answers or {}}
        if case_id is not None:
            args["case_id"] = case_id
        return service.plan_research(args, user_id, base)

    @tool()
    def investigate(
        objective: str,
        answers: dict[str, str] | None = None,
        case_id: str | None = None,
        idempotency_key: str | None = None,
        texts: dict[str, str] | None = None,
        urls: list[str] | None = None,
        search: str | None = None,
        europepmc: str | None = None,
        trials: str | None = None,
        max_records: int | None = None,
        sources_manifest: dict[str, Any] | None = None,
        lat: float | None = None,
        lon: float | None = None,
        fetch_orbits: bool = False,
        imagery: bool = False,
        max_spend_usd: float = 5.0,
    ) -> dict[str, Any]:
        """Run certified V1 research/verification and persist a durable run.

        A serious objective runs only through an Intelligence Case whose latest charter the
        account owner approved in the browser: pass case_id. That approval is used once; a
        retry with the same idempotency_key returns the same run. The approved charter's
        objective, sources and budget drive the run; client-supplied values are ignored.
        """
        user_id, service = _caller()
        args = _source_args(
            texts=texts, urls=urls, search=search, europepmc=europepmc, trials=trials, max_records=max_records, sources_manifest=sources_manifest, lat=lat, lon=lon,
            fetch_orbits=fetch_orbits, imagery=imagery, max_spend_usd=max_spend_usd,
        ) | {"objective": objective, "answers": answers or {}}
        if case_id is not None:
            args["case_id"] = case_id
        if idempotency_key is not None:
            args["idempotency_key"] = idempotency_key
        return service.investigate(user_id, args, base)

    @tool()
    def start_research(
        objective: str,
        answers: dict[str, str] | None = None,
        case_id: str | None = None,
        idempotency_key: str | None = None,
        max_units: float | None = None,
        texts: dict[str, str] | None = None,
        urls: list[str] | None = None,
        search: str | None = None,
        europepmc: str | None = None,
        trials: str | None = None,
        max_records: int | None = None,
        sources_manifest: dict[str, Any] | None = None,
        lat: float | None = None,
        lon: float | None = None,
        fetch_orbits: bool = False,
        imagery: bool = False,
        max_spend_usd: float = 5.0,
    ) -> dict[str, Any]:
        """Queue V1 research as a durable job and return its job_id at once (no 60 s request limit).

        A background worker runs the job; poll get_job_status, stop it with cancel_research.
        Usage is reserved now and held for the job's life. The same idempotency_key returns the
        same job and never runs twice. With case_id the approved charter is consumed once and its
        frozen objective, sources and budget are the only inputs the worker uses.
        """
        user_id, service = _caller()
        args = _source_args(
            texts=texts, urls=urls, search=search, europepmc=europepmc, trials=trials, max_records=max_records, sources_manifest=sources_manifest, lat=lat, lon=lon,
            fetch_orbits=fetch_orbits, imagery=imagery, max_spend_usd=max_spend_usd,
        ) | {"objective": objective, "answers": answers or {}}
        if case_id is not None:
            args["case_id"] = case_id
        if idempotency_key is not None:
            args["idempotency_key"] = idempotency_key
        if max_units is not None:
            args["max_units"] = max_units
        return service.start_research(user_id, args, base)

    @tool()
    def get_job_status(job_id: str) -> dict[str, Any]:
        """Read one of your research jobs: status, attempts, units so far and, once it succeeded, the result."""
        user_id, service = _caller()
        return service.get_job_status(user_id, {"job_id": job_id})

    @tool()
    def cancel_research(job_id: str) -> dict[str, Any]:
        """Cancel one of your research jobs: a queued job ends now; a running job stops at its next stage
        and is charged only for the work done; a finished job is unchanged."""
        user_id, service = _caller()
        return service.cancel_research(user_id, {"job_id": job_id})

    @tool()
    def verify_claim(
        claim: str,
        texts: dict[str, str] | None = None,
        urls: list[str] | None = None,
        search: str | None = None,
        europepmc: str | None = None,
        trials: str | None = None,
        max_records: int | None = None,
        sources_manifest: dict[str, Any] | None = None,
        max_spend_usd: float = 5.0,
    ) -> dict[str, Any]:
        """Verify one factual claim against supplied or discovered evidence."""
        user_id, service = _caller()
        return service.verify_claim(user_id, _source_args(
            texts=texts, urls=urls, search=search, europepmc=europepmc, trials=trials, max_records=max_records, sources_manifest=sources_manifest, max_spend_usd=max_spend_usd,
        ) | {"claim": claim})

    @tool()
    def get_finding(
        run_id: str,
        finding_id: str | None = None,
        question_index: int | None = None,
    ) -> dict[str, Any]:
        """Read one durable finding from a run."""
        user_id, service = _caller()
        return service.get_finding(user_id, {
            "run_id": run_id, "finding_id": finding_id, "question_index": question_index,
        })

    @tool()
    def find_contradictions(run_id: str) -> dict[str, Any]:
        """Read the contradiction graph for a durable run."""
        user_id, service = _caller()
        return service.find_contradictions(user_id, {"run_id": run_id})

    @tool()
    def find_gaps(run_id: str) -> dict[str, Any]:
        """Read unresolved evidence gaps and acquisition plans."""
        user_id, service = _caller()
        return service.find_gaps(user_id, {"run_id": run_id})

    @tool()
    def trace_claim(run_id: str, claim_id: str) -> dict[str, Any]:
        """Trace claim -> evidence -> source provenance."""
        user_id, service = _caller()
        return service.trace_claim(user_id, {"run_id": run_id, "claim_id": claim_id})

    @tool()
    def get_receipt(run_id: str) -> dict[str, Any]:
        """Read the research receipt and its integrity result."""
        user_id, service = _caller()
        return service.get_receipt(user_id, {"run_id": run_id})

    @tool()
    def export_state(run_id: str) -> dict[str, Any]:
        """Export knowledge-map/1-compatible state for a durable run."""
        user_id, service = _caller()
        return service.export_state(user_id, {"run_id": run_id})

    @tool()
    def export_knowledge_map2(run_id: str) -> dict[str, Any]:
        """Export receipt-bound knowledge-map/2 state."""
        user_id, service = _caller()
        return service.export_knowledge_map2(user_id, {"run_id": run_id})

    @tool()
    def render_report(run_id: str) -> dict[str, Any]:
        """Render the human-readable report view of validated run state."""
        user_id, service = _caller()
        return service.render_report(user_id, {"run_id": run_id})

    @tool()
    def satellite_passes(
        lat: float,
        lon: float,
        hours: float = 24.0,
        min_elevation_deg: float = 30.0,
        tle_text: str | None = None,
        fetch: bool = False,
    ) -> dict[str, Any]:
        """Predict open-data imaging-satellite passes over a location."""
        user_id, service = _caller()
        return service.satellite_passes(user_id, {
            "lat": lat, "lon": lon, "hours": hours,
            "min_elevation_deg": min_elevation_deg,
            "tle_text": tle_text, "fetch": fetch,
        })

    @tool()
    def discover(
        run_id: str,
        objective: str,
        design: dict[str, Any] | None = None,
        prior_art: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Run certified V2 Discovery on a durable V1 research run."""
        user_id, service = _caller()
        return service.discover(user_id, {
            "run_id": run_id,
            "objective": objective,
            "design": design,
            "prior_art": prior_art,
        })

    @tool()
    def find_prior_art(
        run_id: str,
        subject: str,
        queries: list[str],
        records: list[dict[str, Any]],
        coverage: dict[str, Any],
        domains: list[str] | None = None,
        time_range: list[str | None] | None = None,
    ) -> dict[str, Any]:
        """Assess prior art within explicit coverage; a miss is never novelty."""
        user_id, service = _caller()
        return service.find_prior_art(user_id, {
            "run_id": run_id,
            "subject": subject,
            "queries": queries,
            "records": records,
            "coverage": coverage,
            "domains": domains or [],
            "time_range": time_range or [None, None],
        })

    @tool()
    def find_discovery_gaps(discovery_id: str) -> dict[str, Any]:
        """Read durable typed discovery gaps."""
        user_id, service = _caller()
        return service.find_discovery_gaps(user_id, {"discovery_id": discovery_id})

    @tool()
    def find_connections(discovery_id: str) -> dict[str, Any]:
        """Read durable observed, derived and speculative discovery connections."""
        user_id, service = _caller()
        return service.find_connections(user_id, {"discovery_id": discovery_id})

    @tool()
    def generate_hypotheses(discovery_id: str) -> dict[str, Any]:
        """Read durable hypotheses, counter-hypotheses and evidence requirements."""
        user_id, service = _caller()
        return service.generate_hypotheses(user_id, {"discovery_id": discovery_id})

    @tool()
    def generate_candidates(discovery_id: str) -> dict[str, Any]:
        """Read durable candidate evaluations and the explicit decision."""
        user_id, service = _caller()
        return service.generate_candidates(user_id, {"discovery_id": discovery_id})

    @tool()
    def simulate_candidate(
        run_id: str,
        model: dict[str, Any],
        parameters: dict[str, Any],
        distributions: dict[str, Any] | None = None,
        success: dict[str, Any] | None = None,
        seed: int | None = None,
        iterations: int | None = None,
    ) -> dict[str, Any]:
        """Run a bounded V2 simulation against a durable research context."""
        user_id, service = _caller()
        args: dict[str, Any] = {
            "run_id": run_id, "model": model, "parameters": parameters,
            "distributions": distributions or {}, "success": success,
        }
        if seed is not None:
            args["seed"] = seed
        if iterations is not None:
            args["iterations"] = iterations
        return service.simulate_candidate(user_id, args)

    @tool()
    def analyze_sensitivity(
        run_id: str,
        model: dict[str, Any],
        parameters: dict[str, Any],
        success: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Run V2 sensitivity analysis against a durable research context."""
        user_id, service = _caller()
        return service.analyze_sensitivity(user_id, {
            "run_id": run_id,
            "model": model,
            "parameters": parameters,
            "success": success,
        })

    @tool()
    def optimize_solution(run_id: str, problem: dict[str, Any]) -> dict[str, Any]:
        """Solve and independently re-check a structured V2 optimization problem."""
        user_id, service = _caller()
        return service.optimize_solution(user_id, {"run_id": run_id, "problem": problem})

    @tool()
    def verify_discovery(discovery_id: str) -> dict[str, Any]:
        """Verify durable discovery receipt/object/handoff integrity."""
        user_id, service = _caller()
        return service.verify_discovery(user_id, {"discovery_id": discovery_id})

    @tool()
    def get_discovery_receipt(discovery_id: str) -> dict[str, Any]:
        """Read a durable V2 discovery receipt."""
        user_id, service = _caller()
        return service.get_discovery_receipt(user_id, {"discovery_id": discovery_id})

    @tool()
    def create_v3_handoff(discovery_id: str) -> dict[str, Any]:
        """Read the validated V3 handoff when V2 selected a candidate."""
        user_id, service = _caller()
        return service.create_v3_handoff(user_id, {"discovery_id": discovery_id})

    @tool()
    def render_discovery_report(discovery_id: str) -> dict[str, Any]:
        """Render the durable human-readable V2 discovery report."""
        user_id, service = _caller()
        return service.render_discovery_report(user_id, {"discovery_id": discovery_id})

    @tool()
    def build_artifact(discovery_id: str, kind: str = "structured_bundle") -> dict[str, Any]:
        """Build and independently verify a V3 artifact from a durable V2 discovery."""
        user_id, service = _caller()
        return service.build_artifact(user_id, {"discovery_id": discovery_id, "kind": kind})

    @tool()
    def get_artifact(artifact_id: str) -> dict[str, Any]:
        """Read a durable V3 artifact, production receipt and verification state."""
        user_id, service = _caller()
        return service.get_artifact(user_id, {"artifact_id": artifact_id})

    @tool()
    def propose_action(
        artifact_id: str,
        target: str,
        payload: dict[str, Any],
        cost_usd: float = 0.0,
    ) -> dict[str, Any]:
        """Propose a bounded HTTPS action. Human browser approval is always required."""
        user_id, service = _caller()
        return service.propose_action(user_id, {
            "artifact_id": artifact_id,
            "kind": "https_webhook",
            "target": target,
            "payload": payload,
            "cost_usd": cost_usd,
        }, base)

    @tool()
    def action_status(action_id: str) -> dict[str, Any]:
        """Read approval/execution state for a durable V4 action proposal."""
        user_id, service = _caller()
        return service.action_status(user_id, {"action_id": action_id})

    @tool()
    def execute_action(action_id: str) -> dict[str, Any]:
        """Execute an already browser-approved V4 action and return its action receipt."""
        user_id, service = _caller()
        return service.execute_action(user_id, {"action_id": action_id})

    @tool()
    def measure_outcome(
        action_id: str,
        measurements: list[dict[str, Any]],
        causal_design: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Run V5 expected-vs-actual measurement over a committed V4 action."""
        user_id, service = _caller()
        return service.measure_outcome(user_id, {
            "action_id": action_id,
            "measurements": measurements,
            "causal_design": causal_design,
        })

    @tool()
    def get_outcome(outcome_id: str) -> dict[str, Any]:
        """Read a durable V5 outcome receipt and V6 handoff."""
        user_id, service = _caller()
        return service.get_outcome(user_id, {"outcome_id": outcome_id})

    @tool()
    def evaluate_improvement(outcome_id: str, proposal: dict[str, Any]) -> dict[str, Any]:
        """Evaluate a V6 candidate on held-out data; never applies the change automatically."""
        user_id, service = _caller()
        return service.evaluate_improvement(user_id, {
            "outcome_id": outcome_id,
            "proposal": proposal,
        })

    @tool()
    def get_improvement(improvement_id: str) -> dict[str, Any]:
        """Read a durable V6 improvement receipt and review-only next-cycle handoff."""
        user_id, service = _caller()
        return service.get_improvement(user_id, {"improvement_id": improvement_id})

    @tool()
    def pricing(standard_units: float | None = None, heavy_jobs: int = 0) -> dict[str, Any]:
        """Inspect provisional price formulas; not proof of economic certification."""
        _, service = _caller()
        return service.pricing({"standard_units": standard_units, "heavy_jobs": heavy_jobs})

    @tool()
    def account_status() -> dict[str, Any]:
        """Show Founding Free or paid entitlement status."""
        user_id, service = _caller()
        return service.account_status(user_id)

    @tool()
    def usage_status() -> dict[str, Any]:
        """Show rolling seven-day intelligence-unit usage and quota."""
        user_id, service = _caller()
        return service.usage_status(user_id)

    @tool()
    def create_checkout() -> dict[str, Any]:
        """Create Stripe-hosted Checkout for an account that requires payment."""
        user_id, service = _caller()
        return service.checkout(user_id, base)

    @tool()
    def billing_portal() -> dict[str, Any]:
        """Open Stripe-hosted subscription management for a paid account."""
        user_id, service = _caller()
        return service.billing_portal(user_id, base)

    return mcp
