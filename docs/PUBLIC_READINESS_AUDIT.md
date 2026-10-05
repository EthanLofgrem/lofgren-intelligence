# Public-readiness audit

Branch `build/public-readiness-audit`, started from `build/v3-complete` @ `05342571`
(certified `V3ReadyForV4 = TRUE`, CI run 37267642341). Audit date 2026-10-04.

Goal audited: a hosted MCP plugin that users add to their own AI clients, with
durable tenant-scoped storage, authentication, strict weekly usage limits,
accounts 1-1000 free and account 1001 onward charged through Stripe (test mode
first), privacy/export/deletion, rate limits and abuse protection, secure hosted
endpoints and onboarding documentation.

**Verdict: not public-ready.** The code on this branch is the strongest hosted
line, but `PublicMCPReady` is FALSE (0/29: no release-evidence manifest exists
anywhere in the repository), no deployment, database, OAuth-interop or Stripe
sandbox evidence exists, the hosted service does not expose V3, and the branch
that does expose V3-V6 hosted (`release/public-v1-v6`, outside this audit's
list) still contains every defect fixed here.

## 1. Per-branch results

Method: fresh clone per branch, detached at the remote head, a new virtualenv,
`pip install ".[hosted]"`, then `python -m unittest discover -s tests -t . -v`
with `PYTHONUTF8=0` and `PYTHONUTF8=1`, then every `scripts/*_gate.py` with
`--sha <full HEAD>`. `public_mcp_gate.py` takes `--evidence <manifest>` instead of
`--sha`; it was run with an empty manifest because none exists.

Environment caveat: the only interpreter available to the audit was CPython
3.14 on Windows. CI runs 3.10/3.11/3.12 on Ubuntu. Results below are therefore
local 3.14 results; CI conclusions were read separately from the GitHub Actions
API for the exact SHA.

`ops/first-1000-readiness` is package 0.2.0 and has no `hosted` extra (pip warns
and installs the base package). Skipped tests: 0 on every branch in both modes (counted from `... skipped`
lines; the gates also fail on any skip).

| Branch | Head | Tests normal | Tests UTF-8 | Gates (offline, local) | CI `tests` run for exact SHA |
|---|---|---|---|---|---|
| build/v3-production | `4366963` | 539 OK | 539 OK | boundary TRUE; V2ReadyForV3 TRUE; **V3ReadyForV4 FALSE** (GitHubCIPassing, PackageGatePassing: no `--ci-run-id`); all 11 V3 code terms TRUE | success (37231614435) |
| build/v3-complete | `0534257` | 566 OK | 566 OK | boundary TRUE; V2ReadyForV3 TRUE; **V3ReadyForV4 TRUE** | success (37267642341) |
| build/v4-execution | `ffb0d9b` | 547 OK | 547 OK | boundary TRUE; V2ReadyForV3 TRUE; V3ReadyForV4 FALSE; V4ReadyForV5 FALSE; only GitHubCIPassing and PackageGatePassing false in each (no `--ci-run-id`); all code terms TRUE | success (37232412578) |
| build/v5-outcomes | `b6646c0` | 554 OK | 554 OK | as v4, plus V5ReadyForV6 FALSE on the same two CI terms only | success (37266226828) |
| build/public-mcp-release | `198272f` | 447 OK | 447 OK | boundary TRUE (only gate besides public_mcp) | success (37229972156) |
| build/public-v2-integration | `a9a52f5` | 531 OK | 531 OK | boundary TRUE; V2ReadyForV3 TRUE | success (37232961539) |
| build/user-journey | `68868b8` | 539 OK | 539 OK | boundary TRUE; V2ReadyForV3 TRUE | success (37233118082) |
| ops/first-1000-readiness | `e3768bc` | 91, **FAILED (errors=1)**: `tests.test_pipeline_and_interfaces.CLITests.test_investigate_writes_report` (`UnicodeEncodeError` cp1252 writing the report on Windows; V1-era 0.2.0 code, fixed on later lines; Ubuntu CI passes) | 91 OK | no `scripts/` directory: no gate scripts | success (36808772027; operational-quality-gate 36808772095) |
| build/public-readiness-audit (code head) | `076d085` | 599 OK | 599 OK | boundary TRUE; V2ReadyForV3 TRUE; **V3ReadyForV4 TRUE** (CI evidence read from the API) | success (37275046085) |

`PublicMCPReady` (scripts/public_mcp_gate.py, 29 terms) is FALSE 0/29 on every
branch that has it: no `lofgren.public-release-evidence/1` manifest is committed
anywhere, and it needs deployment evidence (database migration, OAuth interop,
Stripe sandbox journey, backup/restore, rollback, deployed SHA) that only the
owner's environment can produce.

Gate-script note (open defect G1 below): on v3-production, v4-execution and
v5-outcomes the V3/V4/V5 gates set `GitHubCIPassing` and `PackageGatePassing`
TRUE for *any* integer passed as `--ci-run-id`, without reading the run. The
audit therefore did not pass a run id: those terms are reported FALSE offline,
and the CI conclusion was verified independently through the API (last column).
`build/v3-complete` replaced this with an API-verified exact-SHA check.

## 2. How the branches relate

```
main 0e3c675
 ├─ ops/first-1000-readiness e3768bc        (+2: OPERATIONS_1000_USERS.md, operational-quality workflow)
 └─ build/public-v2-integration a9a52f5     (hosted V1+V2 MCP, Supabase store, OAuth, Stripe, quotas)
     ├─ build/user-journey 68868b8          (+3: journey.py landing/consent/checkout-return pages)
     └─ build/v3-production 4366963         (V3 production engine; local MCP still lofgren.mcp/2)
         ├─ build/v3-complete 0534257       (+2: lofgren.mcp/3 V3 tools, exact-SHA API-verified V3 gate)
         │   └─ build/public-readiness-audit (this branch: +14)
         └─ build/v4-execution ffb0d9b      (+11: execution engine, V4 gate, FK-index migration)
             └─ build/v5-outcomes b6646c0   (+9: outcome engine, V5 gate)

build/public-mcp-release 198272f            forked at c3c5f79; 73 commits not in v3-complete
```

- `v3-production` is an ancestor of `v3-complete` (2 behind, 0 ahead).
- `v4-execution` and `v5-outcomes` fork from `v3-production`, **not** from
  `v3-complete`: they lack the `lofgren.mcp/3` V3 MCP tools and the
  API-verified V3 gate. A trial merge-tree of `v5-outcomes` into
  `v3-complete` (and into this branch) conflicts in
  `lofgren_intelligence/production/core.py`: v4 adds `expected_outcomes` and
  `acceptance_criteria` to the V4 handoff, which v3-complete's rewritten
  `build_artifact` does not emit. V4 execution consumes those fields.
- `public-mcp-release` is superseded: its tree versus v3-complete is +44/-9757
  lines (v3-complete has everything it has), and a trial merge conflicts in 9
  files. It should be retired, not merged.
- `user-journey` merges into v3-complete cleanly but conflicts with this branch
  in `hosted/web_app.py` (the consent page). Its consent intro drops the
  redirect-host disclosure and the explicit-click rule added here; the merge
  must keep both.
- `ops/first-1000-readiness` merges cleanly (docs + workflow only).
- Outside the assigned list, `release/public-v1-v6` (`c88c397`, PR #15) and
  `release/v3-lineage-repair` (`e0b9b5b`, PR #16) exist. `release/public-v1-v6`
  contains public-v2-integration, v4, v5, `build/v6-improve`, hosted V3-V6 tools
  (40 hosted tools including `build_artifact`, `execute_action`,
  `measure_outcome`) and a `li_usage_reservations` migration. It does **not**
  contain v3-complete's last 2 commits, `user-journey`, `ops/first-1000-readiness`
  or any fix from this audit; file-level inspection shows it still has the
  prefix redirect check, the silent auto-authorize on an existing session, the
  `payment_status` default of "paid", unbounded `satellite_passes` and the
  CGNAT SSRF gap. Its migration filenames (`20261004201650_public_mcp.sql`,
  `20261004202354_public_mcp_indexes.sql`) also differ from this line's
  (`20261004190000_public_mcp.sql`) and v4's (`20261004202500_public_mcp_indexes.sql`).
  These were not tested by this audit.

What each line adds to the public goals:

| Capability | Where it lives |
|---|---|
| Hosted MCP (Streamable HTTP, official SDK) | public-v2-integration → v3-complete → this branch; earlier V1-only form in public-mcp-release |
| OAuth 2.1 + PKCE, Supabase identity | same |
| Durable tenant-scoped runs/discoveries (PK `(user_id, run_id)`, RLS, no anon grants) | same |
| Founding Free 1-1000 / paid_required 1001+ (atomic `li_activate_account`) | same |
| Weekly quota (rolling 7 days), per-user rate limit (`li_take_rate_limit`) | same; atomic reservations only on release/public-v1-v6 |
| Stripe Checkout, portal, signed idempotent webhooks | same |
| Privacy export and deletion (`/account`) | same |
| Onboarding pages | user-journey (landing, consent intro, checkout return) |
| Operations runbook for 1,000 users | ops/first-1000-readiness |
| V3 production tools | local only on v3-complete (`lofgren.mcp/3`); hosted only on release/public-v1-v6 |
| V4 execution / V5 outcomes | v4-execution, v5-outcomes (CLI/library; no hosted tools on those branches) |

## 3. Hosted surface versus the certified local contract

The local stdio server is `lofgren.mcp/3` (V1, V2 and the V3 tools
`build_artifact`, `verify_artifact`, `get_artifact_file`,
`get_production_receipt`, `create_v4_handoff`). The hosted service on
v3-complete and this branch registers 31 tools: all V1 and V2 tools plus
pricing/account/usage/checkout/portal, and none of the five V3 tools;
`discover` reports `contract: "lofgren.mcp/2"` (`hosted/service.py`). This is
stated truthfully in `docs/PUBLIC_MCP.md`, `INSTRUCTIONS` and (after this audit)
the landing and consent pages, but it means the hosted plugin does not offer the
certified V3 capability. Exposing it needs durable artifact storage (artifacts
are held in process memory locally), a quota price for artifact builds, and a
review of running V3's generated-test sandbox (`production/sandbox.py`,
subprocess with a 120 s timeout) inside a 60 s serverless function. Open item
O1.

## 4. Defects

Severity: High = breaks a public goal or security boundary; Medium = exploitable
or wrong under realistic conditions; Low = hardening or wording.

### Fixed on this branch

| # | Severity | Defect | File (current line) | Commit |
|---|---|---|---|---|
| F1 | High | Authorization page completed OAuth silently when a Supabase browser session already existed (no consent click) | `hosted/web_app.py:231` | 9c5a445 |
| F2 | Medium | Account deletion failed forever with a 500 once the Stripe subscription had already ended | `hosted/stripe.py:102` | 60a7cdf |
| F3 | High | Weekly usage sum and privacy export silently truncated at PostgREST max-rows (1000); explicit quota 0 fell back to the plan default | `hosted/store.py:81`, `hosted/entitlements.py:51` | 73953e6 |
| F4 | High | SSRF: shared/CGNAT 100.64.0.0/10 and IPv4-in-IPv6 special forms were treated as public | `adapters/net.py:46` | dfd7f6e |
| F5 | High | `satellite_passes` accepted unbounded hours / element sets (minutes of CPU per call) | `hosted/service.py:460` | aafa68e |
| F6 | Medium | Simulation, sensitivity, optimization and prior-art compute stayed callable after the weekly quota or entitlement lapsed | `hosted/service.py:100` | a8940ae |
| F7 | Medium | Database errors escaped handlers as 500s and leaked PostgREST table/constraint names to clients | `hosted/store.py:23` | b0fb2e2 |
| F8 | High | Checkout webhook without `payment_status` defaulted to "paid" and granted a paid entitlement | `hosted/stripe.py:267` | 7071e5f |
| F9 | Medium | OAuth redirect URIs validated by string prefix: `http://localhost.attacker.example/` and `http://127.0.0.1.attacker.example/` registered as loopback, so codes could go in clear text to a remote host shown as "localhost..." | `hosted/auth.py:53` | 06cf1c5 |
| F10 | Medium | `satellite_passes` (and its outbound CelesTrak fetch) usable by unpaid #1001+ accounts and after the weekly quota was spent | `hosted/service.py:460`, `hosted/mcp_sdk.py:253` | f052b56 |
| F11 | Medium | Stripe events are delivered out of order; the event's own status was applied, so a late `customer.subscription.created` (incomplete) revoked a paying account and a stale "active" update could re-open a cancelled one. Each mutating event now applies Stripe's current subscription status; an older lapsed subscription no longer revokes a newer one | `hosted/stripe.py:217`, `:228` | 9c886ce |
| F12 | Low | Landing and consent pages described the hosted service as V1-only | `hosted/web_app.py` | 375f619 |
| F13 | Low | Public docs did not describe the rules above | `docs/PUBLIC_MCP.md` | 84ec61b, 076d085 |

Each fix has regression tests in `tests/test_public_readiness_audit.py`
(verified to fail before the fix) and the full suite passes in both modes.
Behaviour change to note for F11: the webhook handler now calls
`GET /v1/subscriptions/{id}` and therefore needs `STRIPE_SECRET_KEY` at webhook
time; tests inject the status function and make no Stripe call.

### Open (not fixed here)

| # | Severity | Defect / gap | File:line | Why not fixed here |
|---|---|---|---|---|
| O1 | High | Hosted service does not expose the certified V3 tools (`lofgren.mcp/2` hosted vs `/3` local) | `hosted/mcp_sdk.py` (31 tools), `hosted/service.py:316` | Needs a durable artifact table (new migration), sandbox review on the host, pricing; implemented differently on release/public-v1-v6, which must be reconciled rather than duplicated |
| O2 | High | No release-evidence manifest; `PublicMCPReady` 0/29 | `scripts/public_mcp_gate.py` | Requires deployment, database, OAuth-interop, Stripe sandbox, backup/restore and rollback evidence from the owner's environment |
| O3 | Medium | Quota check-then-record race: concurrent calls each pass `access_for_run` before usage is recorded, so the weekly limit can be exceeded by (concurrency x run size) | `hosted/entitlements.py:57`, `hosted/service.py:150`, `:189` | Needs an atomic reservation RPC + migration; release/public-v1-v6 has `li_reserve_usage` — adopt that rather than a third design |
| O4 | Medium | No rate limit or abuse control on unauthenticated endpoints (`/oauth/register` writes a row per call, `/oauth/token`, `/oauth/authorize/complete`, `/account/*`) | `hosted/web_app.py:101`, `:133`, `:266` | Per-IP limiting belongs at the hosting edge (provider firewall/WAF) or needs a new table; owner chooses |
| O5 | Medium | Founding Free squatting: one person can script many sign-ups to take free slots 1-1000 | Supabase Auth settings | Owner must enable email confirmation and CAPTCHA (or similar) in the identity provider |
| O6 | Medium | Sign-in and account pages load `@supabase/supabase-js@2` (floating major) from jsDelivr without Subresource Integrity | `hosted/web_app.py:202`, `:296` | Needs an owner-approved pinned version and its SRI hash (or self-hosting the bundle) |
| O7 | Medium | V3/V4/V5 gates on v3-production, v4-execution, v5-outcomes accept any `--ci-run-id` integer as CI and package evidence | `scripts/v3_gate.py:89` (v3-production), `scripts/v4_gate.py:79`, `scripts/v5_gate.py` | Other agents' branches; port v3-complete's API-verified check when rebasing them |
| O8 | Medium | v4/v5 are based on v3-production, not v3-complete; V4 handoff fields (`expected_outcomes`, `acceptance_criteria`) conflict with v3-complete's `production/core.py` | `production/core.py` | Requires rebasing other agents' branches and re-certifying V3-V5 |
| O9 | Low | `discover` refuses after the fact when actual units exceed the reserve, without recording the work it already did | `hosted/service.py:291` | Product decision on charging over-ceiling work |
| O10 | Low | Account deletion cancels the Stripe subscription but leaves the Stripe customer record (email) in Stripe | `hosted/service.py:574` | Retention for tax/accounting is an owner/legal decision; document in PUBLIC_PRIVACY.md |
| O11 | Low | No end-user "connect your client" guide naming supported clients; the landing content on user-journey is the closest | docs | Needs the production hostname and the list of clients the owner will support |
| O12 | Low | `public_base()` accepts `http://localhost...` by prefix (operator configuration only) | `hosted/web_app.py:35` | Operator-controlled value; harmless once the owner sets the https URL |
| O13 | Low | No token revocation endpoint and no refresh-token reuse detection (a replayed rotated refresh token is refused but does not revoke the family) | `hosted/auth.py:209` | Hardening; not needed for first release |
| O14 | Info | Migration lineage diverges between lines (see section 2); v4's index migration note says the first migration was already applied somewhere | `supabase/migrations/` | Owner must say which database exists and which filenames are applied before any line ships migrations |

Checked and found sound: tenant scoping (every run/discovery read filters by
`user_id`; primary keys include `user_id`), PostgREST query construction
(`urlencode`, `eq.` literals only), RLS with no anon/authenticated grants,
token hashing (only SHA-256 hashes stored), token expiry and resource binding
(the MCP SDK middleware checks `expires_at` and `resource`), PKCE S256 only,
webhook signature and timestamp tolerance, server-local file paths refused,
artifact paths (`production/store.py`) refuse traversal, no secrets in any
branch's tree or in history (pattern scan for Stripe, Supabase, JWT, AWS and
GitHub token formats).

## 5. Recommended integration order (one certified public line)

Nothing below was merged by this audit.

1. **Pick the base:** `build/v3-complete` + this branch (`build/public-readiness-audit`).
   It is the only line with an API-verified exact-SHA V3 gate that passes and the
   audit fixes. Retire `build/public-mcp-release` (superseded).
2. Merge `ops/first-1000-readiness` (docs + workflow; clean).
3. Merge `build/user-journey`, resolving `hosted/web_app.py` so the consent page
   keeps the redirect-host disclosure and the explicit approve click.
4. Rebase `build/v4-execution` then `build/v5-outcomes` onto that line: re-apply
   the V4 handoff fields in `production/core.py`, keep v4's FK-index migration as
   a new file, and replace their `--ci-run-id`-only gate terms with the
   API-verified check. Re-run V3, V4 and V5 gates at the new SHAs.
5. Reconcile with `release/public-v1-v6` / `release/v3-lineage-repair` (another
   agent's line): either port their hosted V3-V6 tools and `li_usage_reservations`
   onto this line, or cherry-pick this branch's commits (`05342571..076d085`) onto
   theirs. Do not ship the release line without these fixes. Agree one migration
   lineage first (O14).
6. Close O1 (hosted V3 tools with durable artifacts) and O3 (atomic quota) on the
   integrated line, then produce the public-release evidence manifest (O2) from a
   staging deployment and run `scripts/public_mcp_gate.py`.

Recommended next build: **step 1-3 integration plus O3 and O1 on top of this
branch**, certified with `scripts/v3_gate.py` and the hosted test suite, before
taking v4/v5.

## 6. Owner inputs that block public release

| Input | Blocks |
|---|---|
| Hosting provider and production hostname (`LI_PUBLIC_BASE_URL`; `vercel.json` assumes Vercel) and permission to deploy a staging environment | O2, O4 (edge rate limits), O11, all deployment evidence terms |
| Supabase project (URL, service-role and publishable keys set as deployment secrets), which migrations are already applied, auth settings (email confirmation, CAPTCHA) | O2, O5, O14, DatabaseMigrationCertified, BackupRestoreCertified |
| Stripe test-mode account: `STRIPE_SECRET_KEY` (test), `STRIPE_WEBHOOK_SECRET`, `LI_STRIPE_PRICE_ID`, `LI_PAID_PLAN_ID`, price and fee inputs for the economic gate (`LI_PAID_MONTHLY_USD`, `LI_PAYMENT_FEE_PERCENT`, `LI_PAYMENT_FEE_FIXED_USD`), and approval to set `LI_BILLING_ENABLED` | Paid path for account 1001+ (checkout is refused until the P95 economic gate passes, so 1001+ users currently cannot pay at all), StripeSandboxJourneyCertified, PaidPlanEconomicsCertified |
| Weekly quota numbers (Founding Free is seeded at 500 units in SQL; `LI_FOUNDER_WEEKLY_UNITS` does not change already-activated accounts) and per-run cost ceilings | Strict weekly limits as a product decision |
| Model/search provider keys and cost rates (`LI_MODEL_*`, `LI_RETRIEVAL_USD_PER_CALL`, `LI_INFRA_USD_PER_RUN`, `BRAVE_API_KEY`) | ActualCOGSMeteringCertified (unpriced components fail the gate) |
| Decision on which integration line is canonical (this line vs `release/public-v1-v6`) | Section 5 steps 4-5 |
| Supabase JS version to pin with SRI, or approval to self-host it | O6 |
| Stripe customer retention policy on deletion | O10 |
