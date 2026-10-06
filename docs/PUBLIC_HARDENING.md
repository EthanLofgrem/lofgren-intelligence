# Public hardening on the release line

Branch `build/public-hardening`, cut from `release/public-v1-v6` (PR #15).
Base used: `ad88ab1` (the release head after the release line's own H3 fix,
`ba733f5`..`ad88ab1`; work started at `6d8a66c`, was rebased onto `945dfdd`
and then onto `ad88ab1`, keeping every release-line commit unchanged).
Source of fixes: `build/public-readiness-audit` @ `6194ed7`
(`docs/PUBLIC_READINESS_AUDIT.md`, `tests/test_public_readiness_audit.py`).

The release line's own commits are kept as they are. This branch adds what was
still missing: the regression tests for every audit fix, one remaining code
defect in each of two fixes, the docs, the first-1000 operations contract and
audit item O4. All new tests are in `tests/test_public_hardening.py`; no test
calls Stripe, Supabase or the network.

## 1. Fix, commit and test

"Base" = already on `release/public-v1-v6`. Each test listed was run against the
code before the fix (file restored from the commit before the base port) and
failed, then passed on this branch.

| # | Audit fix | Code | Commit on this branch | Tests (`tests/test_public_hardening.py`) |
|---|---|---|---|---|
| 1 | OAuth: no silent approval with an existing session | Base `a1d4583` (explicit click). Added: consent intro shows the redirect host (escaped) | `9930376` | `AuthorizeConsentTests` (3) |
| 2 | Redirect URIs: exact-host loopback | Base `2647f54` | `089f55f` (tests only) | `RedirectURIValidationTests` (2) |
| 3 | Deletion stuck after subscription ended | Base `6d8a66c` | `19b4441` (tests only) | `StripeCancellationTests` (4) |
| 4 | Weekly sum / export truncated at 1,000 rows; quota 0 ignored | Base `886e75f` (paging), `3168569` (zero quota). **Fixed here:** `_select_all` stopped on a short page, so a server max-rows below 1000 truncated again (1234 rows read as 100) | `547b0c9` | `StorePaginationTests` (3), `QuotaPolicyTests` (1) |
| 5 | SSRF: CGNAT and IPv4-in-IPv6 | Base `f6d620a` | `95783a1` (tests only) | `SSRFAddressTests` (3) |
| 6 | Unbounded `satellite_passes`; needs entitlement + quota | Base `203a7f9`, `945dfdd` | `c1e6734` (tests only) | `SatellitePassBoundsTests` (3), `SatellitePassEntitlementTests` (2) |
| 7 | Ad hoc compute after quota/entitlement lapsed | Base `203a7f9` (open-quota gate) | `c1e6734` (tests only) | `AdHocComputeQuotaTests` (2) |
| 8 | DB errors as 500s leaking schema names | Base `886e75f` | `547b0c9` (tests) | `StoreErrorTranslationTests` (2) |
| 9 | Checkout without `payment_status` granted access | Base `6d8a66c` | `19b4441` (tests only) | `WebhookPaymentStatusTests` (3) |
| 10 | Webhooks apply Stripe's current status | Base `6d8a66c` | `19b4441` (tests; also injects the status in two existing `test_public_hosted.py` webhook tests that `6d8a66c` broke, exactly as the audit branch did; the release line made the same injection in `192be04`, and the rebase kept its version) | `WebhookOrderingTests` (5) |
| 11 | Landing/consent truthfulness, `docs/PUBLIC_MCP.md` rules | Landing on this line already describes the hosted V1-V6 stack truthfully. **Fixed here:** `PUBLIC_MCP.md` still said "V2 is partial" and that no external actions run | `3d40fe1` | `PublicDocsTruthfulnessTests` (3) |
| B | first-1000 docs + operational gate | Missing on base. Ported `docs/OPERATIONS_1000_USERS.md` and `.github/workflows/operational-quality.yml` (adds `release/**`, `build/public-hardening` triggers and `.[hosted]` install) | `2297dcd` | workflow runs in CI |
| B | user-journey onboarding | Base (`hosted/journey.py`, `tests/test_user_journey.py`); redirect-host notice and approve click kept | — | `test_user_journey` |
| O4 | Rate limits on unauthenticated endpoints | **Added:** in-process per-IP sliding window on `/oauth/register`, `/oauth/authorize`, `/oauth/authorize/complete`, `/oauth/token`, `/account/export`, `/account/delete`, `/actions/{id}/details`, `/actions/{id}/approve`; 429 + `Retry-After`; per instance, documented | `f0f409c` | `UnauthenticatedRateLimitTests` (5) |

Design note on fix 7: the audit asked for metering through the reservation
path. The release line chose a check-only gate (`_require_open_quota`) for the
ad hoc tools and `satellite_passes`; they are refused once the quota or
entitlement lapses but are not charged units. This branch keeps that design
(the release line's commits are canonical) and lists metering as open item H2.

## 2. Test and certification results (local, CPython 3.14, Windows)

| Run | Base `945dfdd` | This branch (on `ad88ab1`) |
|---|---|---|
| `unittest discover`, normal | 615 run, 5 errors, 0 skipped | 659 run, 0 errors, 0 failures, 0 skipped |
| `unittest discover`, `PYTHONUTF8=1` | 615 run, 5 errors, 0 skipped | 659 run, 0 errors, 0 failures, 0 skipped |
| `certify` / `--v2` .. `--v6` / `certify-boundary` | all TRUE | all TRUE |

The 2 base errors fixed first are the webhook tests broken by `6d8a66c`; the
other 3 base errors are fixed by H3 below, and H4 adds a test that builds the
deployed app. Results are with `mcp` 2.3.0 installed.

## 3. Still open

| # | Item |
|---|---|
| O5 | Founding Free sign-up squatting: needs owner auth settings (email confirmation, CAPTCHA). |
| O6 | `@supabase/supabase-js@2` loaded from jsDelivr without SRI: no copy of the bundle is in the repository to hash, and it was not downloaded. Needs an owner-pinned version and hash or self-hosting. |
| O4 (rest) | The limiter is per instance; a global limit needs the hosting edge or a database bucket. |
| H2 | Ad hoc discovery tools and `satellite_passes` are gated but unmetered (no reservation/settlement). |
| O1-O3, O7-O14 | As in the audit; O1 and O3 are addressed on this line by the hosted V3-V6 tools and `li_reserve_usage`. O2 (release-evidence manifest) remains FALSE. |

## 3a. Resolved on this branch

| # | Root cause and fix |
|---|---|
| H3 (resolved) | Three base tests failed (`HostedLifecycleTests.test_v3_artifact_is_durable_and_tenant_scoped`, `test_v4_action_requires_browser_approval_before_execution`, `OfficialMCPTests.test_authenticated_remote_v2_is_durable_across_tool_calls`) because hosted `build_artifact` raised `V3 handoff does not match its discovery`. The problem that fired was the first V3 upstream check, `discovery context: ASM-…/CAND-… is recorded but missing`: the hosted service rebuilt the `DiscoveryContext` from the stored V1 run only, so none of the V2 objects the discovery had registered (objective, assumptions, candidates, scenarios, simulations, decision) were present, while the receipt records each one's digest; the durable discovery snapshot did not keep those objects. Fixed on the release line (`ba733f5`..`ad88ab1`): the snapshot stores the typed objects (`context_objects`) and `build_artifact` re-registers them into a fresh context over the same V1 map and checks them against the receipt. `production/upstream.py` and the V3 check are unchanged. This branch's own fix of the same root cause was dropped in the rebase in favour of the release line's; it adds the regression tests (`test_stored_then_reloaded_discovery_still_builds_an_artifact`, `test_tampered_or_incomplete_stored_discovery_is_refused` in `tests/test_public_hosted.py`): an artifact is built from a JSON round-tripped discovery in a fresh service instance, and a tampered object, a dropped simulation or a missing `context_objects` is refused with no artifact saved. |
| H4 (resolved) | `build_app()` passed `custom_starlette_routes=` to `MCPServer.streamable_http_app()`. No `mcp` 2.x release accepts that keyword on `MCPServer` (checked 2.0.0 to 2.3.0; only the low-level `Server` takes it), so `api/index.py` failed with `TypeError` on every supported version, and no test built the app. Fix: register the routes with `MCPServer.custom_route`, present in every 2.x release; they are served beside `/mcp` without MCP bearer auth, as before. The `mcp>=2,<3` pin is unchanged. A new test builds the app with the installed `mcp`, serves `/healthz` and the OAuth metadata, and gets 401 from an unauthenticated `/mcp`; it passes on 2.0.0, 2.0.1, 2.1.0, 2.1.1, 2.2.0 and 2.3.0. |

## 4. Mapping to PR #15

All commits sit on top of `release/public-v1-v6` @ `ad88ab1` and touch only
`hosted/journey.py`, `hosted/web_app.py` (one argument, route wrapping and route
registration),
`hosted/store.py` (`_select_all` stop condition), the new `hosted/ratelimit.py`,
docs, one workflow and tests. They can be fast-forwarded or cherry-picked onto
PR #15 in order; none rewrites a release-line commit. Nothing was merged,
deployed or sent to an external service.

## 5. Round 2: operations hardening (`build/public-ops-hardening`)

Branch `build/public-ops-hardening`, cut from `build/public-hardening-rebased`
@ `1b5ba88` (draft PR #17 into `release/public-v1-v6`, PR #15). Each item is
one commit with regression tests in `tests/test_public_ops_hardening.py`
(46 tests). Every new test was run against the code before its commit and
failed (missing behaviour, missing type or wrong answer), then passed. No test
calls Stripe, Supabase or the network; stores are the in-memory `FakeStore`.

| # | Item | Commit | Tests |
|---|---|---|---|
| R2-1 | Corrupt stored discovery state. Restoring `context_objects` raised bare `ValueError`/`TypeError`/`KeyError` with object ids and digests; through the MCP SDK that was an unexpected crash with a server traceback, and the `build_artifact` reservation stayed held. Now any restore failure, and any snapshot missing a durable key, is `DiscoveryStateInvalid` (code `DISCOVERY_STATE_INVALID`) with a fixed message and no chained cause; no artifact is built and the reservation is released. `PublicServiceError` carries a stable `code`, and the hosted MCP tools re-raise it as `ToolError("<CODE>: message")`. The V3 upstream binding is unchanged (a tampered handoff specification is still refused). | `268c481` | `CorruptDiscoveryStateServiceTests` (5), `CorruptDiscoveryStateMCPTests` (2); `test_tampered_or_incomplete_stored_discovery_is_refused` now expects the typed error |
| R2-2 | Ad hoc tools charged (resolves H2). `find_prior_art`, `simulate_candidate`, `analyze_sensitivity`, `optimize_solution` and `satellite_passes` run through `_metered`: open-quota check, `li_reserve_usage`, the work, `li_finalize_usage`; a failure releases through `li_release_usage`. Flat documented costs (2/5/5/5/1 units, `ADHOC_UNIT_COSTS`, override `LI_UNITS_<TOOL>`). | `8b5ba7b` | `AdHocChargingTests` (9), including five concurrent callers against a 2-unit quota |
| R2-3 | Global rate limiting (resolves O4 rest). `li_rate_events` is keyed by `auth.users(id)`, so a new migration `20261005191819_keyed_rate_limits.sql` adds `li_rate_limit_events` (RLS on, no anon/authenticated grants) and the atomic, advisory-locked `li_take_keyed_rate_limit`. The limiter is store-backed when `SUPABASE_URL` and the service role key are configured, sends SHA-256 digests only, and falls back to memory otherwise. On a store error `/oauth/register` and `/oauth/authorize/complete` fail closed (503 + `Retry-After`); other endpoints fall back to the per-instance limiter. Applied migrations are hash-pinned by a test. | `bb5d30d` | `GlobalRateLimitTests` (7), `KeyedRateLimitStoreTests` (2), `RateLimitMigrationTests` (2) |
| R2-4 | Founding Free anti-abuse (addresses O5 in code). A new activation needs `email_confirmed_at` on the Supabase user (`LI_REQUIRE_CONFIRMED_EMAIL`, default on) and passes per-IP (5/h) and per-email-domain (30/h) global limits, failing closed. Existing accounts are not re-checked; `li_activate_account` stays idempotent, so retries and concurrent duplicates take one slot. | `a78a039` | `FoundingFreeActivationTests` (9) |
| R2-5 | One capability truth. `hosted/capabilities.py` builds the manifest from the registry `build_mcp` serves; `docs/CAPABILITIES.json` is generated from it; README, ARCHITECTURE and PUBLIC_MCP list exactly its tools per level and state what is not proven. | `a4932d7` | `CapabilityManifestTests` (7) |
| R2-6 | CI triggers. `operational-quality.yml` runs on `ops/**`, `build/**`, `release/**` pushes and every PR; the V3-V6 exact-SHA gate jobs in `tests.yml` also run for `build/public-ops-hardening` and `build/public-hardening-rebased` pushes. No other condition changed. | `610e536` | `CITriggerTests` (3) |

Local results (CPython 3.14, Windows, `mcp` 2.3.0): `unittest discover` 705
run, 0 failures, 0 errors, 0 skipped, in both normal and `PYTHONUTF8=1` mode;
`certify`, `certify --v2` .. `--v6` and `certify-boundary` all TRUE.

### Still open after round 2

| # | Item |
|---|---|
| O5 (owner) | Supabase "Confirm email" and CAPTCHA (Auth bot protection) are owner settings in the Supabase dashboard. The server check on `email_confirmed_at` only bites while "Confirm email" is on. Not verified here. |
| R2-ops | The keyed rate-limit migration must be applied before sign-ups open; until it is, activation and client registration fail closed (503). Not applied or verified against a real database here. |
| O6 | `@supabase/supabase-js@2` from jsDelivr without SRI (unchanged). |
| H5 (observation) | The V3 upstream binding checks a handoff's `specifications`, `expected_outcomes`, `constraints` and `acceptance_criteria` against the discovery; its free-text `objective` and the informational `assumptions` list are not bound. Not changed here (no check was weakened or added to V3). |
| Not proven | Deployment, real MCP clients, backup/restore, Stripe sandbox journey and PublicMCPReady (`scripts/public_mcp_gate.py` needs deployment evidence). |
