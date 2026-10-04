"""ASGI application for the public Lofgren Intelligence MCP service."""

from __future__ import annotations

import html
import json
import os
import urllib.parse
import logging
import time
import uuid
from typing import Any

from mcp.server.transport_security import TransportSecuritySettings
from starlette.middleware.cors import CORSMiddleware
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response
from starlette.routing import Route

from .. import __version__
from .auth import AuthError, OAuthService
from .mcp_sdk import build_mcp
from .security import MAX_MCP_BODY_BYTES
from .store import StoreError, SupabaseStore
from .stripe import StripeError, apply_webhook, verify_webhook


_LOG = logging.getLogger("lofgren_intelligence.public")


def public_base() -> str:
    value = os.environ.get("LI_PUBLIC_BASE_URL", "").rstrip("/")
    if not value.startswith("https://") and not value.startswith("http://localhost"):
        raise RuntimeError("LI_PUBLIC_BASE_URL must be configured as https (localhost allowed for development)")
    return value


async def _json_body(request: Request, limit: int = MAX_MCP_BODY_BYTES) -> dict[str, Any]:
    raw = await request.body()
    if len(raw) > limit:
        raise ValueError("request body too large")
    value = json.loads(raw.decode("utf-8") or "{}")
    if not isinstance(value, dict):
        raise ValueError("JSON body must be an object")
    return value


def _error(status: int, code: str, description: str | None = None) -> JSONResponse:
    body: dict[str, Any] = {"error": code}
    if description:
        body["error_description"] = description
    return JSONResponse(body, status_code=status)


async def healthz(request: Request) -> Response:
    return JSONResponse({"service": "lofgren-intelligence", "version": __version__, "status": "ok"})


async def readyz(request: Request) -> Response:
    missing = [
        name
        for name in ("SUPABASE_URL", "SUPABASE_SERVICE_ROLE_KEY", "SUPABASE_PUBLISHABLE_KEY", "LI_PUBLIC_BASE_URL")
        if not os.environ.get(name)
    ]
    if missing:
        return JSONResponse({"ready": False, "missing": missing}, status_code=503)
    try:
        SupabaseStore().get_oauth_client("__readiness__")
    except Exception as exc:
        return JSONResponse({"ready": False, "database": str(exc)}, status_code=503)
    return JSONResponse({"ready": True})


async def oauth_resource_root(request: Request) -> Response:
    base = public_base()
    return JSONResponse({
        "resource": base + "/mcp",
        "authorization_servers": [base],
        "scopes_supported": ["mcp"],
        "bearer_methods_supported": ["header"],
    })


async def oauth_server_metadata(request: Request) -> Response:
    base = public_base()
    return JSONResponse({
        "issuer": base,
        "authorization_endpoint": base + "/oauth/authorize",
        "token_endpoint": base + "/oauth/token",
        "registration_endpoint": base + "/oauth/register",
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code", "refresh_token"],
        "code_challenge_methods_supported": ["S256"],
        "token_endpoint_auth_methods_supported": ["none"],
        "scopes_supported": ["mcp"],
    })


async def oauth_register(request: Request) -> Response:
    try:
        body = await _json_body(request, 100_000)
        return JSONResponse(OAuthService(SupabaseStore()).register_client(body), status_code=201)
    except (ValueError, AuthError, StoreError) as exc:
        return _error(400, "invalid_client_metadata", str(exc))


async def oauth_complete(request: Request) -> Response:
    try:
        body = await _json_body(request, 100_000)
        auth = request.headers.get("authorization", "")
        if not auth.lower().startswith("bearer "):
            raise AuthError("Supabase user session is required")
        code = OAuthService(SupabaseStore()).authorize_from_supabase_session(
            supabase_access_token=auth.split(" ", 1)[1].strip(),
            client_id=str(body.get("client_id") or ""),
            redirect_uri=str(body.get("redirect_uri") or ""),
            code_challenge=str(body.get("code_challenge") or ""),
            scope=str(body.get("scope") or "mcp"),
            resource=str(body.get("resource") or ""),
        )
        redirect = str(body["redirect_uri"])
        query: dict[str, str] = {"code": code}
        if body.get("state") is not None:
            query["state"] = str(body["state"])
        separator = "&" if urllib.parse.urlsplit(redirect).query else "?"
        return JSONResponse({"redirect_url": redirect + separator + urllib.parse.urlencode(query)})
    except (ValueError, KeyError, AuthError, StoreError) as exc:
        return _error(400, "invalid_request", str(exc))


async def oauth_token(request: Request) -> Response:
    raw = await request.body()
    if len(raw) > 100_000:
        return _error(413, "invalid_request", "request body too large")
    form = urllib.parse.parse_qs(raw.decode("utf-8"), keep_blank_values=True)
    get = lambda key, default="": (form.get(key) or [default])[-1]
    try:
        oauth = OAuthService(SupabaseStore())
        grant = get("grant_type")
        if grant == "authorization_code":
            result = oauth.exchange_code(
                code=get("code"),
                code_verifier=get("code_verifier"),
                client_id=get("client_id"),
                redirect_uri=get("redirect_uri"),
            )
        elif grant == "refresh_token":
            result = oauth.refresh(refresh_token=get("refresh_token"), client_id=get("client_id"))
        else:
            return _error(400, "unsupported_grant_type")
        return JSONResponse(result, headers={"Cache-Control": "no-store", "Pragma": "no-cache"})
    except (AuthError, StoreError) as exc:
        return _error(400, "invalid_grant", str(exc))


async def oauth_authorize(request: Request) -> Response:
    q = request.query_params
    params = {
        "client_id": q.get("client_id", ""),
        "redirect_uri": q.get("redirect_uri", ""),
        "code_challenge": q.get("code_challenge", ""),
        "code_challenge_method": q.get("code_challenge_method", "S256"),
        "scope": q.get("scope", "mcp"),
        "state": q.get("state", ""),
        "resource": q.get("resource", public_base() + "/mcp"),
    }
    if q.get("response_type") != "code" or params["code_challenge_method"] != "S256":
        return _error(400, "invalid_request", "authorization code + PKCE S256 required")
    try:
        client = SupabaseStore().get_oauth_client(params["client_id"])
    except StoreError as exc:
        return _error(503, "temporarily_unavailable", str(exc))
    if not client or params["redirect_uri"] not in (client.get("redirect_uris") or []):
        return _error(400, "invalid_client")
    if params["resource"] != public_base() + "/mcp":
        return _error(400, "invalid_target")

    supabase_url = os.environ.get("SUPABASE_URL", "")
    public_key = os.environ.get("SUPABASE_PUBLISHABLE_KEY", "")
    if not supabase_url or not public_key:
        return _error(503, "temporarily_unavailable", "authorization provider is not configured")

    safe_params = json.dumps(params).replace("<", "\\u003c")
    safe_url = json.dumps(supabase_url).replace("<", "\\u003c")
    safe_key = json.dumps(public_key).replace("<", "\\u003c")
    page = f"""<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Connect Lofgren Intelligence</title>
<script src="https://cdn.jsdelivr.net/npm/@supabase/supabase-js@2"></script>
<style>
body{{font-family:system-ui;background:#0b0d10;color:#eef2f7;margin:0;display:grid;place-items:center;min-height:100vh}}
main{{width:min(92vw,480px);background:#151922;border:1px solid #2a3240;border-radius:16px;padding:28px}}
h1{{font-size:24px;margin:0 0 6px}}p{{color:#aeb8c7}}input,button{{width:100%;box-sizing:border-box;padding:12px;margin:8px 0;border-radius:9px}}
input{{background:#0e1218;color:white;border:1px solid #344054}}button{{background:#2563eb;color:white;border:0;font-weight:700;cursor:pointer}}
small{{color:#8692a6}}#status{{min-height:24px;color:#f0b94d}}
</style></head>
<body><main><h1>Lofgren Intelligence</h1>
<p>Connect your AI client to certified V1 Evidence Intelligence.</p>
<input id="email" type="email" autocomplete="email" placeholder="Email">
<input id="password" type="password" autocomplete="current-password" placeholder="Password">
<button id="signin">Sign in &amp; connect</button>
<button id="signup">Create account</button>
<div id="status"></div>
<small>Activated accounts 1–1000 receive quota-limited Founding Free access. Account 1001 onward requires a paid entitlement.</small>
<script>
const cfg={safe_params};
const sb=supabase.createClient({safe_url},{safe_key});
const status=document.querySelector('#status');
const email=document.querySelector('#email');
const password=document.querySelector('#password');
async function complete(session){{
  const r=await fetch('/oauth/authorize/complete',{{method:'POST',headers:{{'content-type':'application/json','authorization':'Bearer '+session.access_token}},body:JSON.stringify(cfg)}});
  const d=await r.json(); if(!r.ok) throw new Error(d.error_description||d.error||'authorization failed');
  location.href=d.redirect_url;
}}
async function existing(){{const x=await sb.auth.getSession();if(x.data.session)await complete(x.data.session);}}
document.querySelector('#signin').onclick=async()=>{{try{{status.textContent='Signing in…';const x=await sb.auth.signInWithPassword({{email:email.value,password:password.value}});if(x.error)throw x.error;await complete(x.data.session)}}catch(e){{status.textContent=e.message}}}};
document.querySelector('#signup').onclick=async()=>{{try{{status.textContent='Creating account…';const x=await sb.auth.signUp({{email:email.value,password:password.value,options:{{emailRedirectTo:location.href}}}});if(x.error)throw x.error;if(x.data.session)await complete(x.data.session);else status.textContent='Check your email to confirm the account, then return here.'}}catch(e){{status.textContent=e.message}}}};
existing().catch(e=>status.textContent=e.message);
</script></main></body></html>"""
    return HTMLResponse(page)


async def stripe_webhook(request: Request) -> Response:
    raw = await request.body()
    if len(raw) > 2_000_000:
        return _error(413, "payload_too_large")
    try:
        event = verify_webhook(raw, request.headers.get("stripe-signature", ""))
        result = apply_webhook(SupabaseStore(), event)
        return JSONResponse({"received": True, "result": result})
    except (StripeError, StoreError) as exc:
        return _error(400, "invalid_webhook", str(exc))


async def billing_success(request: Request) -> Response:
    return PlainTextResponse("Payment received. Return to your AI client and retry the Lofgren Intelligence request.")


async def billing_cancelled(request: Request) -> Response:
    return PlainTextResponse("Checkout cancelled. No entitlement change was made.")


async def landing(request: Request) -> Response:
    base = public_base()
    page = f"""<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Lofgren Intelligence</title><style>body{{font-family:system-ui;background:#0b0d10;color:#eef2f7;max-width:760px;margin:60px auto;padding:20px}}code{{background:#171c25;padding:3px 6px;border-radius:5px}}a{{color:#7db2ff}}</style></head>
<body><h1>Lofgren Intelligence</h1><p>Evidence-driven research and verification through MCP.</p>
<p><strong>Certified public capability:</strong> V1 Evidence Intelligence. Later versions are not represented as complete until their own gates pass.</p>
<p>MCP endpoint: <code>{html.escape(base)}/mcp</code></p>
<p><a href="/healthz">Health</a> · <a href="/.well-known/oauth-authorization-server">OAuth metadata</a></p>
</body></html>"""
    return HTMLResponse(page)


class RequestTelemetry:
    """Emit content-free structured request telemetry with a correlation id."""

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        request_id = str(uuid.uuid4())
        started = time.perf_counter()
        status = 500

        async def wrapped(message: Any) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = int(message["status"])
                headers = list(message.get("headers", []))
                headers.append((b"x-request-id", request_id.encode("ascii")))
                message["headers"] = headers
            await send(message)

        try:
            await self.app(scope, receive, wrapped)
        finally:
            _LOG.info(
                "public_request",
                extra={
                    "request_id": request_id,
                    "method": scope.get("method"),
                    "path": scope.get("path"),
                    "status": status,
                    "duration_ms": round((time.perf_counter() - started) * 1000, 2),
                },
            )


class SecurityHeaders:
    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        async def wrapped(message: Any) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                headers.extend([
                    (b"x-content-type-options", b"nosniff"),
                    (b"referrer-policy", b"no-referrer"),
                    (b"x-frame-options", b"DENY"),
                    (b"permissions-policy", b"camera=(), microphone=(), geolocation=()"),
                ])
                message["headers"] = headers
            await send(message)
        await self.app(scope, receive, wrapped)


def build_app():
    base = public_base()
    parsed = urllib.parse.urlsplit(base)
    hostname = (parsed.hostname or "").lower()
    host_entries = [hostname, hostname + ":*"]
    origin_entries = [base]
    origin_entries.extend(
        x.strip() for x in os.environ.get("LI_ALLOWED_ORIGINS", "").split(",") if x.strip()
    )

    routes = [
        Route("/", landing, methods=["GET"]),
        Route("/healthz", healthz, methods=["GET"]),
        Route("/readyz", readyz, methods=["GET"]),
        Route("/.well-known/oauth-protected-resource", oauth_resource_root, methods=["GET"]),
        Route("/.well-known/oauth-authorization-server", oauth_server_metadata, methods=["GET"]),
        Route("/.well-known/openid-configuration", oauth_server_metadata, methods=["GET"]),
        Route("/oauth/register", oauth_register, methods=["POST"]),
        Route("/oauth/authorize", oauth_authorize, methods=["GET"]),
        Route("/oauth/authorize/complete", oauth_complete, methods=["POST"]),
        Route("/oauth/token", oauth_token, methods=["POST"]),
        Route("/stripe/webhook", stripe_webhook, methods=["POST"]),
        Route("/billing/success", billing_success, methods=["GET"]),
        Route("/billing/cancelled", billing_cancelled, methods=["GET"]),
    ]

    mcp = build_mcp(base)
    app = mcp.streamable_http_app(
        streamable_http_path="/mcp",
        json_response=True,
        stateless_http=True,
        max_request_body_size=MAX_MCP_BODY_BYTES,
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=host_entries,
            allowed_origins=origin_entries,
        ),
        custom_starlette_routes=routes,
        host=hostname,
    )
    app = CORSMiddleware(
        app,
        allow_origins=origin_entries,
        allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type", "Mcp-Protocol-Version", "Mcp-Session-Id"],
        expose_headers=["Mcp-Session-Id", "WWW-Authenticate"],
        max_age=600,
    )
    return RequestTelemetry(SecurityHeaders(app))
