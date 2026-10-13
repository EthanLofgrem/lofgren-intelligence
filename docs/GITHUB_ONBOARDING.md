# Optional GitHub connection — not available yet

Public LI MCP use must require no LI login, signup, GitHub account, or payment.
GitHub connection is an optional future capability for researching repositories
that a user explicitly chooses. There is currently no verified onboarding
URL; do not publish a Connect button or claim a successful separate-user flow.

## Decision

Use a GitHub App for the optional repository feature. GitHub Apps offer selected
repository installation, fine-grained permissions and short-lived tokens.
A broad OAuth App `repo` scope is unnecessary for this read-only use case.
Do not register a second OAuth App merely to create a mandatory LI identity.
An authenticated optional LI subject can link GitHub's stable numeric identity;
this must never depend on matching email addresses or mutable GitHub usernames.

Three separate grants remain explicit:

| Concern | Boundary |
| --- | --- |
| GitHub identity | Verified GitHub user authorization callback, bound to an existing authenticated optional LI subject |
| Repository access | GitHub App installation on selected repositories, verified server-side on every request |
| AI-client access | Public anonymous MCP connection, or separate LI OAuth consent for private tools |

GitHub identity authorization does not grant repository installation access.
An installation does not automatically link an AI client. Installation owner,
requesting user's current repository access, saved selection and requested
operation must all intersect. Never use Ethan's credentials for other users.

## Owner registration values to prepare

Account: EthanLofgrem, as App owner. Do not publish or activate until callback,
persistent grants, webhook validation and separate-user acceptance are complete.

- App name: a unique LI name selected by owner; uniqueness is not yet verified.
- Homepage: https://github.com/EthanLofgrem/lofgren-intelligence
- Repository permissions: Metadata read (required), Contents read.
- Organization permissions: none. Account permissions: none beyond GitHub's
  identity authorization needs. Do not request email, write or administration.
- Installation scope: selected repositories on personal or organization accounts.
- User authorization: required for linking; retain expiring user tokens.
- Callback path planned: `/github/callback`; webhook path planned:
  `/github/webhook`. These endpoints are NOT implemented or registered. Exact
  full URLs must use a reachable deployment proven at the release SHA before
  registration; the current protected MCP-only surface excludes these routes.
- Server-only configuration names planned: `LI_GITHUB_APP_ID`,
  `LI_GITHUB_CLIENT_ID`, `LI_GITHUB_CLIENT_SECRET`,
  `LI_GITHUB_APP_PRIVATE_KEY`, `LI_GITHUB_WEBHOOK_SECRET`.
  No values have been generated, fetched or committed by this implementation.

App registration does not require introducing a paid LI service. GitHub API
quotas and existing Vercel/Supabase quotas remain real limits; free-tier capacity
is not guaranteed. No subscription/payment code is added.

## Safe implementation order

1. Persist single-use random callback state bound to authenticated LI subject,
   action (link vs login), exact redirect and short expiry. Consume atomically;
   repeated/expired/denied callbacks must not change links.
2. Exchange code server-side, fetch GitHub's authoritative numeric user identity,
   enforce a unique identity-to-LI-subject mapping. Never automatically link an
   existing account by email. Never trust callback installation ID alone.
3. Verify user's authorized installation and current selected repositories via
   GitHub. Persist immutable numeric IDs, explicit allowed read permissions,
   ownership, expiry/revocation and encrypted credentials. RLS plus service RPCs
   must scope every record to the authenticated subject.
4. Validate signed webhooks using constant-time MAC comparison; invalidate local
   grants on installation suspension/deletion and repository removal. Recheck
   remote access on every protected operation even if no webhook was received.
5. Mint repository-scoped short-lived tokens for approved operations; redact
   tokens, callback codes and private repository metadata from logs. No shared
   owner identity, provider credentials or client-supplied installation authority.
6. Status UI exposes only the subject's connection. Disconnect invalidates local
   grant/tokens immediately; offer separate GitHub deauthorization and installation
   removal, explaining organization-owned installation consequences.
7. First use: read selected repository metadata only; refuse writes and other
   repositories. AI-client connection is a separate independently tested step.

`hosted/github_boundary.py` implements a conservative read-only authorization
primitive and unavailable status. It is not wired to a live provider, OAuth
callback, persistence layer or MCP tool. Its tests prove only that primitive.
Do not treat it as end-to-end onboarding or a production security guarantee.

## Separate-user release acceptance

Use a separately authorized test user; owner-only testing is insufficient.
Measure actual steps and elapsed time. Verify chosen repo success, unchosen repo
and other-user denial, revoked/expired permissions, repeated and interrupted
callbacks, rejected account relinking, org restrictions and immediate disconnect.
Run AI-client authorization independently. Preserve UNKNOWN for unavailable
accounts/clients and NOT RUN for unexecuted flows.

If GitHub onboarding is advertised, a separate authorized test user must have
completed the full connection flow and successfully used LI, with
repository-access boundaries and revocation verified.

## Authoritative documentation inspected

- https://docs.github.com/en/apps/oauth-apps/building-oauth-apps/differences-between-github-apps-and-oauth-apps
- https://docs.github.com/en/apps/using-github-apps/authorizing-github-apps
- https://docs.github.com/en/apps/creating-github-apps/authenticating-with-a-github-app/authenticating-with-a-github-app-on-behalf-of-a-user
- https://docs.github.com/en/apps/creating-github-apps/about-creating-github-apps/best-practices-for-creating-a-github-app

Inspected 2026-10-13 UTC. Live provider configuration and separate-user evidence
remain unavailable. No working connection destination is advertised.

## Prepared callback service (2026-10-13)

`hosted/github_onboarding.py` now provides a disabled-by-default optional service
for PKCE start, one-shot callback, explicit live-verified repository selection,
subject-specific status and local disconnect. The proposed new migration
`20261013193000_github_optional_link.sql` supplies backend-only atomic RPCs and
RLS-denied tables. It has NOT been applied or executed against PostgreSQL.
Unit tests use fake store/provider contracts; they do not establish SQL execution,
real GitHub authorization or end-to-end usability.

Callback completion retains a short-lived consumed state generation through code
exchange; disconnect invalidates this generation, so delayed callbacks cannot
recreate a disconnected link. Numeric GitHub identity is globally unique and
cannot silently replace a subject's different existing link. PKCE verifier is
short-lived backend-only state; provider credentials are never stored here.

Integration requirements still outstanding: verified authenticated subject from
LI auth (never request parameters), actual GitHub provider adapter with bounded
HTTPS exchange and authoritative identity/current repository checks, protected
route wiring, signed webhook invalidation, UI and PostgreSQL concurrency proof.
Stored selections are intent, never standalone operation authorization; every
repository operation must recheck live user/installation/permission intersection.
Public anonymous LI MCP continues to require no GitHub connection.
