> Historical checkpoint identities below are retained for provenance. For the current candidate, verify PR #45 head SHA and exact-source CI; do not reuse an older checkpoint as current.

# Remaining acceptance register

Checkpoint: remote `0074472d896fab72af029fa6c2cf449ca0c2ae77`; source review against equivalent local `410f16c1f6aab707bf9136d9c69e913d1c6402a4`. Owners below are work lanes, not assertions of completed work. All future fixes require their own exact-source evidence.

| ID | Severity | State | Assigned lane | Concrete closure evidence |
| --- | --- | --- | --- | --- |
| LI-01 disconnect migration | High delivery | Staging applied; hosted acceptance pending | Database/auth | Installed migration byte identity and restricted grants retained; hosted revocation/replay and separate-user isolation on final source |
| LI-02 hosted worker | High delivery | NOT RUN | Worker | Exactly one approved synthetic target, pinned source, bounded execution, zero known cost, persisted verified receipt and unchanged unrelated jobs |
| LI-03 ordinary AI client | High delivery | NOT RUN | Client acceptance | Independent authorized client initialize, list and safe use; retain version, protocol, steps/time and receipt |
| LI-04 private cache headers | Medium | Source/CI fixed | Auth | Current source tests and final hosted private-response no-store/no-cache checks |
| LI-05 skipped candidate gates | Medium process | Fixed on 0074472 | CI | Eight test jobs include five executed dedicated gates; rerun all applicable jobs on next candidate rather than count skipped checks |
| LI-06 GitHub onboarding | Medium | NOT AVAILABLE | Onboarding | Optional identity/repository boundary, owner app configuration and separate test user; first use/isolation/revocation demonstrated before advertising |
| LI-07 hard/distributed trial | High delivery | Local watchdog; hosted proof pending | Runtime/trial | Non-network exact-Preview launch/timeout/kill/reap; then controlled anonymous first use, concurrency and shared budget proof |
| LI-08 public schema bounds | Low | Source/CI fixed | Client/schema | Runtime/schema parity regressions retained; actual tools/list on enabled final Preview |
| LI-09 stale current docs | Low | Source correction prepared; final release reconciliation pending | Documentation | Current report no longer says installed migrations absent or gates skipped; date historical artifacts; final release notes match source/deployment |
| LI-10 drift/history/client evidence | Medium process | PARTIAL/UNKNOWN | Database/security/client | Normalized full catalog comparison; safe full-history secret scan with scope/limitations; versioned real-client matrix |
| LI-11 Vercel env API 403 | Medium operations | Dashboard workaround verified; API cause UNKNOWN | Hosting access | Service-specific credential/project/team scope diagnosis and successful authorized API metadata/config access; never expose secrets |
| LI-12 Google 502 | Unknown | Root cause UNKNOWN | Hosted authentication | Sanitized request/response/timing/provider evidence; distinguish provider navigation denial from reproduced HTTP 502; verified recovery |
| LI-13 signed public promotion | High release | NOT SATISFIED | Release | All applicable exact-source gates, owner-trusted release evidence, approved scope and verified promoted deployment identity |

## Ten inspection areas still requiring evidence

1. Backbone: exact deployed source is known; hosted job consumer and recovery remain unobserved.
2. MCP: local SDK/stdio checks exist; ordinary clients and enabled anonymous remote use remain unobserved.
3. CI: candidate dedicated gates execute; next source must rerun applicable gates and retain step outcomes.
4. SQL/RPC: installed byte/grant proof exists; full normalized semantic drift is still incomplete.
5. APIs: source auth/cache/bounds regressions exist; final hosted positive/negative private flows remain pending.
6. OAuth/secrets: local race/expiry proof exists; hosted consent/isolation and complete safe history scan remain incomplete.
7. Deployment: exact Preview health verified; runtime process behavior and API 403 cause remain unknown.
8. Docs/DX: no-account intent explicit; no remote Connect promise until working destination and separate-user acceptance.
9. Issue register: separate source fixes, staging changes, hosted acceptance and unknowns rather than mark all complete.
10. Roadmap: future payments/site remain design-only; no payment code or paid services activated. Multi-user private resources must maintain tenant isolation independently of the public stateless path.

## Prioritized next fixes

1. Prove bounded, credential-free, non-network subprocess behavior on exact Vercel Preview.
2. Fix any hosted timeout/kill/reap failure without weakening fail-closed flags.
3. Review controlled Preview activation only after runtime proof; retain global budget/concurrency evidence.
4. Complete one independently authorized anonymous client first use.
5. Complete exactly one approved synthetic hosted worker and persist independently verified result.
6. Re-run hosted OAuth revocation, expiry, replay and user isolation.
7. Finish normalized SQL/RPC drift comparison and full-history scan with explicit coverage.
8. Implement optional GitHub onboarding with separate-user acceptance; public use stays no-login.
9. Resolve API 403 through supported access diagnosis; reproduce Google 502 before claiming its root cause.
10. Reconcile final source, CI, migration, hosted evidence and documentation before merge/promotion.
