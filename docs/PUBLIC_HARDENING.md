# Public hardening on the release line

Branch `build/public-hardening`, cut from `release/public-v1-v6` (PR #15).
Base used: `945dfdd` (the release head after the other agent's audit ports;
work started at `6d8a66c` and was rebased onto `945dfdd` unchanged).
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
| 1 | OAuth: no silent approval with an existing session | Base `a1d4583` (explicit click). Added: consent intro shows the redirect host (escaped) | `46ecc47` | `AuthorizeConsentTests` (3) |
| 2 | Redirect URIs: exact-host loopback | Base `2647f54` | `dd9102b` (tests only) | `RedirectURIValidationTests` (2) |
| 3 | Deletion stuck after subscription ended | Base `6d8a66c` | `c68f68e` (tests only) | `StripeCancellationTests` (4) |
| 4 | Weekly sum / export truncated at 1,000 rows; quota 0 ignored | Base `886e75f` (paging), `3168569` (zero quota). **Fixed here:** `_select_all` stopped on a short page, so a server max-rows below 1000 truncated again (1234 rows read as 100) | `d8f8632` | `StorePaginationTests` (3), `QuotaPolicyTests` (1) |
| 5 | SSRF: CGNAT and IPv4-in-IPv6 | Base `f6d620a` | `c54ae5e` (tests only) | `SSRFAddressTests` (3) |
| 6 | Unbounded `satellite_passes`; needs entitlement + quota | Base `203a7f9`, `945dfdd` | `b78d95c` (tests only) | `SatellitePassBoundsTests` (3), `SatellitePassEntitlementTests` (2) |
| 7 | Ad hoc compute after quota/entitlement lapsed | Base `203a7f9` (open-quota gate) | `b78d95c` (tests only) | `AdHocComputeQuotaTests` (2) |
| 8 | DB errors as 500s leaking schema names | Base `886e75f` | `d8f8632` (tests) | `StoreErrorTranslationTests` (2) |
| 9 | Checkout without `payment_status` granted access | Base `6d8a66c` | `c68f68e` (tests only) | `WebhookPaymentStatusTests` (3) |
| 10 | Webhooks apply Stripe's current status | Base `6d8a66c` | `c68f68e` (tests; also injects the status in two existing `test_public_hosted.py` webhook tests that `6d8a66c` broke, exactly as the audit branch did) | `WebhookOrderingTests` (5) |
| 11 | Landing/consent truthfulness, `docs/PUBLIC_MCP.md` rules | Landing on this line already describes the hosted V1-V6 stack truthfully. **Fixed here:** `PUBLIC_MCP.md` still said "V2 is partial" and that no external actions run | `3a71930` | `PublicDocsTruthfulnessTests` (3) |
| B | first-1000 docs + operational gate | Missing on base. Ported `docs/OPERATIONS_1000_USERS.md` and `.github/workflows/operational-quality.yml` (adds `release/**`, `build/public-hardening` triggers and `.[hosted]` install) | `c12c400` | workflow runs in CI |
| B | user-journey onboarding | Base (`hosted/journey.py`, `tests/test_user_journey.py`); redirect-host notice and approve click kept | — | `test_user_journey` |
| O4 | Rate limits on unauthenticated endpoints | **Added:** in-process per-IP sliding window on `/oauth/register`, `/oauth/authorize`, `/oauth/authorize/complete`, `/oauth/token`, `/account/export`, `/account/delete`, `/actions/{id}/details`, `/actions/{id}/approve`; 429 + `Retry-After`; per instance, documented | `95ba03f` | `UnauthenticatedRateLimitTests` (5) |

Design note on fix 7: the audit asked for metering through the reservation
path. The release line chose a check-only gate (`_require_open_quota`) for the
ad hoc tools and `satellite_passes`; they are refused once the quota or
entitlement lapses but are not charged units. This branch keeps that design
(the release line's commits are canonical) and lists metering as open item H2.

## 2. Test and certification results (local, CPython 3.14, Windows)

| Run | Base `945dfdd` | This branch |
|---|---|---|
| `unittest discover`, normal | 615 run, 5 errors, 0 skipped | 656 run, 3 errors, 0 skipped |
| `unittest discover`, `PYTHONUTF8=1` | 615 run, 5 errors, 0 skipped | 656 run, 3 errors, 0 skipped |
| `certify` / `--v2` .. `--v6` / `certify-boundary` | all TRUE | all TRUE |

The 3 remaining errors are on the base unchanged and are not caused here
(see H3/H4). The 2 base errors fixed are the webhook tests broken by `6d8a66c`.

## 3. Still open

| # | Item |
|---|---|
| O5 | Founding Free sign-up squatting: needs owner auth settings (email confirmation, CAPTCHA). |
| O6 | `@supabase/supabase-js@2` loaded from jsDelivr without SRI: no copy of the bundle is in the repository to hash, and it was not downloaded. Needs an owner-pinned version and hash or self-hosting. |
| O4 (rest) | The limiter is per instance; a global limit needs the hosting edge or a database bucket. |
| H2 | Ad hoc discovery tools and `satellite_passes` are gated but unmetered (no reservation/settlement). |
| H3 | Pre-existing on base: `HostedLifecycleTests.test_v3_artifact_is_durable_and_tenant_scoped` and `test_v4_action_requires_browser_approval_before_execution`, plus `OfficialMCPTests.test_authenticated_remote_v2_is_durable_across_tool_calls`, fail: hosted `build_artifact` raises `ProductionError: V3 handoff does not match its discovery` (the discovery's context objects are not in the context rebuilt from the run). The base CI `tests` runs fail too. Belongs to the V3 reconciliation on the release line. |
| H4 | Pre-existing on base: `build_app()` calls `streamable_http_app(custom_starlette_routes=...)`, which `mcp` 2.3.0 (allowed by `mcp>=2,<3`) does not accept, so the hosted ASGI app cannot be built with that version. No test builds the app. |
| O1-O3, O7-O14 | As in the audit; O1 and O3 are addressed on this line by the hosted V3-V6 tools and `li_reserve_usage`. O2 (release-evidence manifest) remains FALSE. |

## 4. Mapping to PR #15

All commits sit on top of `release/public-v1-v6` @ `945dfdd` and touch only
`hosted/journey.py`, `hosted/web_app.py` (one argument and route wrapping),
`hosted/store.py` (`_select_all` stop condition), the new `hosted/ratelimit.py`,
docs, one workflow and tests. They can be fast-forwarded or cherry-picked onto
PR #15 in order; none rewrites a release-line commit. Nothing was merged,
deployed or sent to an external service.
