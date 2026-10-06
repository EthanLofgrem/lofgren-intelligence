"""User-facing onboarding and journey pages for the hosted MCP service.

These helpers are intentionally presentation-only. They do not grant access,
assert payment, or change entitlement state.
"""

from __future__ import annotations

import html
import urllib.parse


def landing_html(base: str, nonce: str = "") -> str:
    """The public home page (``/``). See :mod:`.site` for the page system.

    ``nonce`` must match the Content-Security-Policy sent with the page
    (:func:`.site.csp`); one is generated when omitted.
    """
    from .site import home_page, new_nonce

    return home_page(base, nonce or new_nonce())


def consent_intro_html(client_name: str, scope: str = "mcp", redirect_uri: str | None = None) -> str:
    client = html.escape(client_name or "Unknown MCP client")
    safe_scope = html.escape(scope or "mcp")
    out = (
        '<div style="font-size:13px;color:#8fb5ee;margin-bottom:10px">OAuth authorization</div>'
        '<h1>Connect Lofgren Intelligence</h1>'
        f'<p><strong>{client}</strong> is requesting permission to use Lofgren Intelligence '
        f'through the <code>{safe_scope}</code> scope.</p>'
        '<p style="font-size:14px;color:#aeb8c7">This grants the client access to your authorized '
        'LI MCP tools and LI run state. It does not reveal your password or a server service-role key.</p>'
    )
    if redirect_uri is not None:
        # Dynamic client registration is open, so the client name is
        # self-declared. The redirect host is where the code is actually sent.
        host = urllib.parse.urlsplit(redirect_uri).netloc or redirect_uri
        out += (
            '<p id="consent">After you approve, the authorization code is sent to '
            f'<strong>{html.escape(host)}</strong>. Client names are self-declared, so check this host. '
            'Only continue if you started this connection from your own AI client.</p>'
        )
    return out


def checkout_return_html() -> str:
    return """<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Checkout return · Lofgren Intelligence</title>
<style>body{font-family:system-ui;background:#0b0d10;color:#eef2f7;margin:0;display:grid;place-items:center;min-height:100vh;padding:24px}main{max-width:620px;background:#151922;border:1px solid #2a3240;border-radius:18px;padding:32px}.muted{color:#aeb8c7}.note{background:#111722;border:1px solid #344054;border-radius:12px;padding:16px}a{color:#8bbcff}</style>
</head><body><main><h1>Checkout returned</h1><p class="muted">Returning here does not prove payment succeeded or that access has been granted.</p>
<div class="note"><strong>What happens next</strong><p>Lofgren Intelligence waits for Stripe's signed webhook to update your entitlement. Return to your AI client and call <code>account_status</code>. Continue only after the account reports active access.</p></div>
<p><a href="/">Back to Lofgren Intelligence</a></p></main></body></html>"""
