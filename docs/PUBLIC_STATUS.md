# Public release status

Checkpoint: 2026-10-13 UTC (2026-10-12 America/Phoenix). This is a dated evidence report, not a live dashboard.

**Local package available; public no-account hosted use not yet available.** There is no committed launch date or verified donation destination. Public LI use is intended to require no LI login, signup or payment. Optional access to private GitHub resources requires separate explicit authorization.

## Code and deployment

[Draft PR #45](https://github.com/EthanLofgrem/lofgren-intelligence/pull/45) candidate **0074472d896fab72af029fa6c2cf449ca0c2ae77** has retained frozen-source proof: 1,163 full tests, 78 focused checks and 25 offline contracts; counts overlap. PostgreSQL proof passed 68 tests without skips. All 13 CI jobs passed, including dedicated lifecycle gates. The rebuilt isolated Preview reports the exact candidate and returns 200 for health/readiness. These results do not certify a later commit.

The stable authenticated MCP endpoint remains `https://lofgren-intelligence-mcp.vercel.app/mcp` on **b3204f00ba84d8133743fa1e9398a89225190dad**. It requires LI authorization; an anonymous 401 is expected. It is not a verified public no-account connection destination. The separate `/mcp-trial` endpoint is disabled. No administrative bypass secret belongs in a user's MCP configuration.

## Remaining release requirements

| Area | Status | Required evidence |
| --- | --- | --- |
| Candidate reachability and exact identity | PASS | Recheck after each deployment |
| Hosted authenticated OAuth | PARTIAL | Consent, PKCE, refresh, expiry, revocation and separate-user isolation |
| Ordinary AI-client connection | NOT RUN | Client/version, connection steps, initialize, tool list and safe result/receipt |
| Anonymous trial | DISABLED | Vercel subprocess launch/timeout/kill/reap, controlled Preview activation, distributed accounting and real first use |
| Synthetic hosted worker | NOT RUN | One explicitly approved target, pinned source, zero known cost, verified persisted receipt and unrelated jobs untouched |
| External execution | DISABLED | Applicable effect fencing and independently approved scope |
| GitHub onboarding | NOT AVAILABLE | Optional identity/repository authorization, separate test user and verified revocation |
| Full public release | NOT SATISFIED | Applicable hosted gates, trusted release evidence and approved promotion |

Staging now has 15 migrations, including tested disconnect/replay, expiry and targeted-claim changes. Catalog readback verified restricted service-role-only execution. Prior expired mutation request-state errors were resolved for these authorized requests.

Vercel API environment access still returned 403. The authorized dashboard configuration path succeeded for the candidate branch's Preview only. Required isolation is configured but unverified; anonymous execution remains disabled. No Google 502 root cause is established. No paid host, provider call, live billing or hosted worker was activated.

## Learn, support and updates

See [six-step conformance](RELEASE_CONFORMANCE_CURRENT.md), [client evidence](CLIENT_COMPATIBILITY.md), [remaining issues](REMAINING_ACCEPTANCE_REGISTER.md), [architecture](ARCHITECTURE.md) and [source watch](SOURCE_WATCH.md). Report sanitized reproducible problems through [GitHub issues](https://github.com/EthanLofgrem/lofgren-intelligence/issues).

Source refresh detects changes for review; continuous hosted monitoring remains unimplemented. Legal changes require jurisdiction, effective dates, authoritative sources and reviewed applicability. LI does not silently rewrite earlier findings, policy or production code.
