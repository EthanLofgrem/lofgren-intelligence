# LI execution and review coverage — 2026-10-13

Baseline inspected: local `4554e131363058d17d8c0ed1041a2a480b3ba5e6`, tree `4f01cadd7cb5d197b137c529b36f5ff620b2f219`; equivalent reported remote `e5f26cd34f652df52a2ebf941f72826d5340c0f6`. This is source/document inspection, not a new hosted acceptance receipt. Subsequent commits require fresh integration verification.

## Seven fixer roles

| Role | Assigned outcome | Completion evidence |
|---|---|---|
| F1 Runtime configuration | Preview-only probe credential and pinned rebuild | Scoped settings and deployment SHA; credential never in report |
| F2 Runtime acceptance | Fixed non-network probe launch, timeout, kill, reap | Hosted sanitized JSON at exact SHA; all flags true |
| F3 Clients and authorization | Ordinary client first use and hosted revocation/isolation | Separate authorized account/client evidence; UNKNOWN if unavailable |
| F4 Synthetic worker | Exactly one approved target, no unrelated mutation | Pinned runner, DB atomic claim, receipt/result and before/after digest |
| F5 GitHub onboarding | Optional identity, selected repositories, independent AI authorization | Callback/grants/webhooks/revocation plus separate-user acceptance |
| F6 Vercel connector | Establish missing API privilege and remedy | Verified permission/configuration evidence; dashboard success is separate |
| F7 Google failure | Reproduce and diagnose the reported 502 | URL, UTC timestamp, request ID and responsible component; otherwise UNKNOWN |

## Five independent reviewer roles

| Role | Review responsibility |
|---|---|
| R1 Isolation reviewer | Probe gates, child environment, process cleanup, accurate sandbox claims |
| R2 OAuth reviewer | PKCE/resource binding, expiry, replay, consent, revocation and cross-user denial |
| R3 SQL/worker reviewer | RPC grants, RLS, migration/catalog drift, targeted claim and reservation ownership |
| R4 Onboarding reviewer | Identity/repository/client distinctions, callback replay and minimum permissions |
| R5 Evidence reviewer | Exact SHA/tree, skipped gates, logs, quotas and unsupported completeness claims |

## Eight verification and management roles

| Role | User-requested responsibility | Required output |
|---|---|---|
| V1 Testing | Behavioral positive/negative tests | Reproducible command, exact source, exit status |
| V2 Validation | Independently verify acceptance assertions | Passed/failed/NOT RUN/UNKNOWN matrix |
| V3 Source review | Check finished corrections | Reviewed diff and remaining defects |
| V4 Deployments | Verify actual deployed source/settings | Health/readiness, SHA and enabled capability flags |
| V5 Branches | Reconcile working and broken branches | Ref/PR ancestry and CI evidence, no accidental stale promotion |
| V6 Claude coordination | Inspect observable Claude work | Observable PR/run/task facts; no inferred session access |
| V7 MCP capabilities | OAuth, API, CI and tool boundaries | Transport/client/tool matrix; no universal-client claim |
| V8 Project manager | Integrate whole stack and gate releases | Priorities, owners, dependencies and evidence ledger |

These are 20 responsibilities, not 20 concurrent sessions. With root plus six child slots, execute in waves: first six fixer roles; then F7 plus outstanding fixers; then five independent reviewer assignments; then verification roles. A completed agent may be reused for a distinct responsibility; independent review must not be the author's self-certification. If a platform/account fact is unavailable, more agents cannot manufacture it.

## Ten-area health checkpoint

| Inspection area | Baseline source observation | Remaining acceptance |
|---|---|---|
| Backbone | Python core; Starlette web entry; hosted MCP SDK; Supabase store/RPC; Vercel rewrite | Runtime and worker execution on actual host |
| MCP | SDK-based tools; authorized private tools; separate bounded anonymous trial | Actual named client compatibility, version/config receipts |
| CI | Matrix, lifecycle, PostgreSQL and operational workflows | Fresh exact-source gates after integration; skipped PR copies are not passes |
| SQL/RPC | Fifteen migration files; atomic targeted claim and OAuth expiry corrections prepared | Deployed catalog and persisted-state evidence for latest source |
| APIs | Explicit web routes and capability gates | Hosted negative/positive authorization coverage |
| OAuth/secrets | Dedicated probe bearer credential distinct from OAuth tokens | Owner/platform credential policy and real consent flow evidence |
| Deployment | Sixty-second Vercel function bound; Preview-specific configuration | Actual process launch/kill/reap, retained proof |
| Docs/DX | GitHub onboarding accurately unavailable | Verified destination and separate-user journey before advertising |
| Issues | Seven residual blocks retained explicitly | One-by-one closure with evidence, not agent assignment alone |
| Roadmap | Anonymous use separate from optional GitHub/private capabilities | No payments or paid providers in this mission |

## Token Machine scope and assumptions

LLM context tokens measure model input/output units; OAuth bearer tokens authorize operations; the Preview probe secret gates diagnostics. Never share storage, counting logic or claims between these types. LI only sees tool inputs supplied by a host. It cannot silently analyze every host conversation, intercept all prompts, or force a host to expose all tool definitions in context. Host discovery/loading behavior varies; tool-definition size and token counts need client/model/tokenizer-specific measurement. A standard-library estimate is explicitly an estimate, never provider billing evidence.

A zero-cost local request-analysis component can classify submitted summaries, enforce input/context/output budgets, minimize unnecessary content, select supported capabilities and return actionable blockers. It cannot repair platform privileges, generate owner consent, unlock unavailable client sessions, or autonomously authorize external writes. Existing `li_assess_request` already provides read-only suitability routing for minimized summaries; extend rather than duplicate its contract.

Probe limitations: the fixed child denies Python socket events via an audit hook. This proves trusted probe code does not use Python sockets, not an OS sandbox for arbitrary native code. `_USED` limits attempts per warm process, not globally across Vercel instances. Hosted proof does not itself activate research or certify worker tenancy.

## Priority

1. Respect actual credential-entry capability/policy; prepare all independent source work meanwhile.
2. Hosted probe at pinned SHA; keep anonymous trial disabled and verification false until observed proof.
3. Integrate and review Token Machine/onboarding source, rerun exact-source gates.
4. Obtain actual client/identity facts and synthetic worker target before acceptance execution.
5. Merge/promote only after applicable gates; preserve unavailable evidence explicitly.
