# Dedicated MCP deployment surface

Set `LI_DEPLOYMENT_SURFACE=mcp` on a dedicated MCP deployment. The default
`full` retains the existing website and customer workspace. Unknown values
fail application startup.

The MCP profile exposes transport, health/readiness, OAuth discovery and the
public PKCE registration/consent/token flow, case/action approval pages and
their existing authorized endpoints, and the pinned browser authentication
library. Its root describes the service. Customer workspace, account management,
billing, Stripe webhook, demo pages, public marketing pages and general static
assets are excluded by an explicit route allowlist.

This is an HTTP surface restriction, not an alternate intelligence engine or
authorization model. The same MCP tools, token/resource/scope validation,
case/action ownership and approval protections remain active. Public reachability
must still be configured separately at the host. Do not distribute a Vercel
bypass secret to ordinary clients.

Use the dedicated origin as `LI_PUBLIC_BASE_URL`; OAuth discovery, token resource
and approval links are origin-bound. A token issued for another deployment's
resource does not become valid here. Register the resulting authentication
redirects in the relevant provider settings. Both deployments and the hosted
worker must align to the reviewed release and staging schema. Excluding account
pages does not remove account deletion capability from the product; retain the
protected customer deployment and customer support path.

## OAuth issuer identity

The authorization server issuer is the HTTPS origin with its root slash, for
example `https://lofgren-intelligence-mcp.vercel.app/`. Both protected-resource
metadata routes advertise that exact identity and the authorization-server
metadata returns it unchanged. This matches the MCP SDK's canonical HTTP URL
serialization. The MCP resource remains the exact origin plus `/mcp`; OAuth
endpoint paths are unchanged. Preflight compares these identities exactly
rather than treating distinct issuer strings as interchangeable.
