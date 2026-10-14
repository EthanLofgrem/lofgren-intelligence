# Client and platform acceptance boundaries

Public no-account use, optional GitHub permissions, and protected LI OAuth are
three independent acceptance tracks. No LI signup or GitHub connection should
be required for the bounded anonymous capability. Connecting a GitHub identity
does not itself grant repository access or install LI in an AI client.

## Ordinary MCP client evidence

Run only after exact-source hosted runtime proof and controlled Preview activation.
Use a separately authorized client session. Record client name/version, protocol,
deployment URL, exact 40-character source SHA, date, initialize outcome, tool names,
and the sanitized result of one safe bounded operation. Do not retain credentials,
private prompt text, account exports or other users' records.

For anonymous use, confirm that no LI account, bearer token or checkout is requested.
For optional protected use, exercise authorized consent, PKCE exchange, access and
refresh, then revoke and confirm the former token and refresh grant are refused.
Use an explicitly approved synthetic foreign case/job identifier for cross-user
negative tests; never probe arbitrary customer data. Record actual HTTP/tool error
and confirm no state mutation. Repeat separately for each intended AI host.

An SDK test is implementation evidence, not proof of ChatGPT, Claude, Cursor or
VS Code configuration. An unavailable account/client session remains UNKNOWN.
No actual user consent can be fabricated by the server or by test mocks.

## Google 502 diagnosis

There is no retained reproducible Google failure URL, timestamp or request ID at
this checkpoint. LI's inspected Python source contains no Google-specific OAuth
provider implementation. This does not establish that Google or an upstream
connector is healthy; it limits what can be diagnosed from this source.

On recurrence, retain the origin and path without query secrets, UTC timestamp,
HTTP status, sanitized response/request correlation ID, which operation was being
performed, and whether the request reached LI. Do not retain auth codes, cookies,
passwords, access tokens or credential-bearing redirect URLs. Distinguish a Google
provider response, connector/proxy response, Vercel gateway response and LI error.
Reproduce with one authorized test request, inspect corresponding sanitized logs,
and change only the evidenced failing layer. A Vercel protection bypass cannot
repair an OAuth redirect or environment-API permission failure.

Do not substitute another identity, bypass credentials, or claim the 502 fixed
because an unrelated health request succeeds. Until evidence exists, its cause
and resolution remain UNKNOWN.
