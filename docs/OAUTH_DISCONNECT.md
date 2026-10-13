# OAuth disconnect candidate

This increment is not deployment-ready until its new migration passes real PostgreSQL CI and is applied to staging before the API revision is deployed.

`POST /oauth/revoke` is an RFC 7009 form endpoint accepting `token` and `client_id` for registered public clients. A matching access or refresh credential revokes the caller’s entire user/client connection: active access tokens are revoked, refresh tokens and pending authorization codes are deleted. Unknown tokens and client mismatches return the same empty success. This does not delete the account or its research.

`POST /oauth/disconnect` accepts JSON `client_id` and a verified Supabase session in the Authorization header. The user identity always comes from Supabase, never the request body. This lets a user disconnect a client after losing its LI token. No UI is supplied by this increment.

Token issuance uses `li_issue_oauth_tokens` to insert access/refresh credentials together, bound to a still-present consumed grant. It shares a user/client advisory lock with disconnect. A revoked or already-issued parent cannot mint new credentials. Revocation does not roll back requests already authorized or cancel already-approved jobs.

## Required deployment order

1. Run the complete unit suite and real PostgreSQL proof, including concurrency and privilege tests, on the exact commit.
2. Apply `20261012060000_oauth_disconnect.sql` to staging. It adds three service-role-only RPCs; total store RPC inventory becomes 27.
3. Deploy this application revision only after the RPCs exist.
4. Run signed-in authorization, refresh, revoke, old-access rejection, old-refresh rejection, and new-consent acceptance against the hosted endpoint.
5. Test disconnect through an actual MCP client. Local tests are not a hosted-client receipt.

Do not deploy the API before the migration: its token exchange now deliberately requires atomic issuance and fails closed when that RPC is absent. No credentials, infrastructure charges, or remote database writes are part of this source increment.
