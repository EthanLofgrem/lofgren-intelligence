# Client compatibility and evidence

Checkpoint: remote `0074472d896fab72af029fa6c2cf449ca0c2ae77`, equivalent local `410f16c1f6aab707bf9136d9c69e913d1c6402a4`. MCP compatibility is negotiated by client, protocol revision, transport and authentication capability. Supporting MCP does not make every model or assistant product interchangeable.

The repository uses the official Python SDK, locked to 2.3.0, for hosted stateless Streamable HTTP. The local core provides stdio independently. Retained trial tests cover legacy initialize revision `2025-06-18` and current `2026-07-28` metadata plus protocol mismatch rejection. These are harness results, not a product support matrix.

| Surface or client | Actual evidence | Remaining evidence |
| --- | --- | --- |
| Local core stdio | Subprocess initialize, 32 tools, safe `compile_objective` with maximum spend zero | Product-specific configuration and ordinary user session |
| Hosted SDK/ASGI | Schema, anonymous allowlist, protocol, host/origin and auth-negative regressions | Real deployment and user session on each supported client |
| ChatGPT | No completed installed connector/app session | Independently authorized supported product/account, client version and safe call |
| Claude Desktop/Code | No observed product session | Authorized installed client, exact transport/config and safe call |
| Cursor | No observed product session | Exact current client version, accepted configuration and safe call |
| VS Code Copilot | No observed product session | Exact current client/extension versions and accepted configuration |
| Other MCP clients/models | UNKNOWN | Explicit tested transport/auth/protocol and observed result |

## Working local configuration shape

Install the package following the README. This is the `mcpServers` configuration shape used by compatible clients; it is not a universally accepted file format:

```json
{
  "mcpServers": {
    "lofgren": {
      "command": "/absolute/path/to/.venv/bin/lofgren",
      "args": ["mcp"]
    }
  }
}
```

Replace the executable path with the actual installed executable, including a Windows executable path when applicable. Configure the chosen client using its current authoritative documentation. Do not paste tokens or owner credentials into issue reports.

## Remote use

No verified public no-account destination is available at this checkpoint. Do not publish a fabricated working URL or paste administrative Vercel bypass parameters into a connector.

When verified and enabled, the anonymous surface is `/mcp-trial`, exposing only `trial_research`, `trial_verify_claim` and `trial_compare`. It provides bounded stateless results and inline receipts, not private cases, durable jobs, repository access or account history. The client must support the deployed Streamable HTTP revision and a no-auth connection. Some client products require their own account or paid plan; LI cannot remove the client's requirements.

The authenticated `/mcp` surface is separate. GitHub sign-in is identity; GitHub App installation or repository authorization controls resources; an AI client's LI connection controls invoked capabilities. None automatically proves the other two.

## Acceptance record

For every claimed supported client, retain: exact source/deployment SHA, client and extension versions, transport/protocol negotiated, configuration with secrets removed, setup steps/time, initialize response, actual tool list, one safe first use and verified receipt. Record refusal, timeout and revocation behavior separately. Keep unavailable evidence UNKNOWN. Test account/repository isolation with separate authorized users before advertising GitHub onboarding.
