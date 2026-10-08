# Connected-assistant selection and verification

This implementation extends the protected candidate based on
`e4df8fb2a0a99f9e4a2aa2fa578ad036016763e1`. It is not a public release,
registry publication, ChatGPT app approval or proof of real-client compatibility.
Existing deployment protection still prevents ordinary external MCP discovery.

## Selection contract

Connected, authorized assistants can discover tools with `tools/list` and choose
LI for source comparisons, claim verification, persistent investigations,
artifact production and governed work. The server instructions explain when
LI adds value and when it should be skipped. The host controls tool availability,
selection, data-sharing approval and per-turn budgets. LI cannot install itself
in another application or change that application's model-routing policy.

`li_assess_request` is a bounded deterministic recommendation. It has no LLM,
source fetch, case creation, job submission, usage reservation or action effect.
Authentication and the existing operational request-rate limit still apply;
the rate-limit RPC writes operational metadata. It does not activate an account
or consume paid research units. Service operation has costs. Its read-only
annotation refers to domain state, not an absence of operational logging.

The host must omit LI before sending externally prohibited content. A server
cannot undo disclosure by rejecting a summary after receiving it. Send a
minimal summary, not private documents or credentials. Do not assess obvious
definitions, arithmetic or casual chat. Recommendations confer no authority.

The rules are heuristic, not calibrated model judgment. Ambiguous summaries
request clarification. Monitoring is rejected because this tool does not
schedule checks or deliver alerts. Production and action proposals require
requirements/target clarification; execution keeps its separate approval.
Study support means evidence inspection, not an implemented tutoring service.

## Exact tool definition

`docs/li_assess_request.schema.json` is exported from the registered SDK tool,
not an independently invented manifest. Regenerate without calling any tool:

```bash
python scripts/mcp_tool_schema.py --tool li_assess_request
```

Required inputs are `request_summary` (string, 1–3000 characters, nonblank)
and `desired_output` (`answer`, `evidence_brief`, `comparison`, `study_support`,
`artifact`, `monitoring`, `action_proposal`). Structured output is a validated
AssessmentResult with routing decision, workflow, next step, questions and
limitations. `case_created` and `research_started` must both be false. The SDK
publishes its generated input schema; the output model rejects extra fields.
The tool never echoes the request summary in its response.

## Actual OAuth metadata

Set `LI_PUBLIC_BASE_URL` to the one canonical HTTPS origin shared by the
authorization server and MCP resource. Current staging uses
`https://lofgren-intelligence-staging.vercel.app`. Do not point a client at a
random deployment hostname while issuing tokens for a different resource.

Protected-resource metadata at `/.well-known/oauth-protected-resource`:

```json
{
  "resource": "https://lofgren-intelligence-staging.vercel.app/mcp",
  "authorization_servers": ["https://lofgren-intelligence-staging.vercel.app"],
  "scopes_supported": ["mcp"],
  "bearer_methods_supported": ["header"]
}
```

The authorization metadata is served at `/.well-known/oauth-authorization-server`.
Its issuer is the canonical base, with `/oauth/authorize`, `/oauth/token` and
`/oauth/register`. It advertises authorization_code/refresh_token, public clients
(`token_endpoint_auth_methods_supported: ["none"]`), code responses and S256
PKCE. Redirects are registered and matched. LI tokens are opaque and validated
against trusted persisted records, resource, expiry and revocation. Do not add
`jwks_uri` or pretend a Supabase website-session token is an LI MCP token.
Do not advertise `li:assess` or other granular scopes until issuance and
enforcement implement them. Current transport scope is `mcp`; ownership and
charter/action permissions are independently enforced by the case service.

## Client-side settings (OpenAI developer applications)

After the host has obtained the user's LI OAuth token and permission to share:

```python
tools = []
if external_processing_allowed and li_access_token:
    tools = [{
        "type": "mcp",
        "server_label": "lofgren_intelligence",
        "server_url": LI_MCP_URL,
        "authorization": li_access_token,
        "allowed_tools": ["li_assess_request"],
        "require_approval": "always"
    }]

response = client.responses.create(
    model=OPENAI_MODEL,
    input=user_request,
    instructions=("Use LI when evidence or persistence materially helps. "
                  "Skip simple definitions, arithmetic and externally prohibited work. "
                  "Assess only a minimized summary. Assessment is not execution approval."),
    tools=tools,
    tool_choice="auto" if tools else "none"
)
```

This is an integration fragment; the host supplies the client, model, request,
endpoint, token storage and authenticated user consent. It is not a ChatGPT UI
setting. `auto` permits calls, rather than guaranteeing correct selection.
Handle `mcp_approval_request` by presenting the exact recipient/arguments and
returning the authenticated user's `mcp_approval_response`. Resupply the token
on subsequent Responses requests. Do not log it. Keep other unrelated tools
available according to host policy. A once-per-turn assessment budget requires
host enforcement, not just this prompt. Claude/Codex use their native host
configuration; OpenAI settings are not portable MCP server settings.

Phase allowlists: assessment first; authorized case reads when inspecting;
`clarify_objective` for scope preparation; `start_research`/`get_job_status`/
`cancel_research` for approved durable work; only the relevant separately approved
action tools for execution. Hiding tools is not backend permission enforcement.

## Unauthorized requests

The SDK rejects missing/invalid/expired/revoked/wrong-resource credentials with
401 before tool execution. A valid token without `mcp` receives 403 with an
insufficient_scope challenge. WWW-Authenticate advertises resource_metadata.
Do not mistake Vercel's SSO 401 for LI's OAuth challenge. Authenticated domain
denials remain sanitized tool errors; do not leak another owner's object or
trigger an OAuth loop for a stale charter. Never weaken approval to fix a test.

## Reproducible test sequence

1. `python scripts/offline_store_contracts.py`: import and run the affected
   database contract tests under a process audit hook denying DNS, connects and
   datagram sends. No credentials, database or permitted connections. It fails
   on an attempted connection even if a test catches the exception.
2. `python -m unittest tests.test_mcp_routing tests.test_hosted_mcp_sdk -q`:
   in-process SDK negotiation, schemas, assessment, identity/rate denial and
   ASGI OAuth metadata/challenges. No actual provider or customer session.
3. Exact-SHA matrix, clean installed wheel, boundary, dedicated V2–V6 gates,
   operational certification and PostgreSQL proof in CI.
4. On approved staging, run `scripts/li_mcp_preflight.py` with canonical base and
   full expected SHA. Protected staging currently blocks ordinary discovery.
5. With an actual authorized test account/client: discovery → registration →
   S256 consent → callback → exchange → initialize → tools/list → assessment.
   Compare owned case/job/reservation state before/after; assessment creates none.
6. Complete approved research with a running worker. Disconnect, retrieve the
   result, cancel a second job, restart/reclaim, inspect receipt and settlement.
   Exercise V2–V6 as applicable with a safe contained separately approved action.
7. Test refresh, expiry, revocation, altered resource, stale approval and foreign
   case IDs with the real client. Repeat separately for ChatGPT, Claude and Codex.
   A generic SDK test establishes neither those sessions nor universal support.

Unit tests use fake stores and transports; Postgres integration uses its
isolated CI service; live sources are opt-in; staging is explicit and production
smoke requires separate approval. The hosted HTTP transport rejects RFC
documentation domains before sending credentials, while mocked fixtures can
retain them as inert configuration. No production endpoint is substituted.

## Measures and evidence

| Measure | Definition / evidence required |
|---|---|
| Appropriate routing | Labeled synthetic and consented evaluation: useful selections, unnecessary calls and missed useful calls. Separate host selection from deterministic assessment. |
| Activation | Eligible real users completing a useful first case, not registrations or synthetic calls. |
| Repeat value | Cohort D7/D30 return and second useful case; separate people, agents and test accounts. |
| Performance | p50/p95 assessment, enqueue/status latency and job completion by workflow. Baseline before targets. |
| Research quality | Independently inspected source validity, unsupported claims, contradictions and material omissions. |
| Reliability | Completion/failure/cancellation rates, stuck jobs, recovery time and duplicate effects. |
| Economics | Provider/retrieval/infra/support cost per successful task including failures and retries. |
| Accounting | Unsettled reservations and duplicate settlement incidents. |
| Distribution | Tested third-party integrations completing a real task; registry views/stars are discovery signals. |

These metrics are definitions, not measured customer results. No new customer
analytics or private-request collection is enabled by this increment.

## Requirement-to-evidence map

| Requirement | Implementation | Proof | State |
|---|---|---|---|
| Placeholder cannot become live DB | hosted/http.py | test_offline_store_contracts + offline runner | Local PASS; no network attempts |
| Bounded recommendation | hosted/routing.py | test_mcp_routing.RoutingTests | Local PASS; heuristic limits explicit |
| Schema + annotations | hosted/mcp_sdk.py | in-process SDK routing tests / exported schema | Local PASS |
| No assessment domain writes | assessment handler + existing caller limiter | fake-store before/after equality | Local PASS; hosted NOT RUN |
| OAuth metadata / 401 / 403 | web_app metadata + SDK middleware | in-process ASGI negative cases | Local PASS; real-client lifecycle NOT RUN |
| Durable execution/ownership | shared PublicService/worker | existing case/job/accounting tests + PostgreSQL CI | Component proof; hosted worker journey NOT RUN |
| Client selection / uptake | phase allowlists and native host configuration | real application evaluation | NOT RUN |
| Public release | exact-SHA deployment + recovery/signing evidence | owner-approved full gate | BLOCKED; protected staging only |

Primary references: OpenAI's remote MCP guide
https://developers.openai.com/api/docs/guides/tools-connectors-mcp and MCP
authorization https://modelcontextprotocol.io/specification/2025-11-25/basic/authorization.
