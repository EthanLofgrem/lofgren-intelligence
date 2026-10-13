"""ASGI application for the public Lofgren Intelligence MCP service."""

from __future__ import annotations

import html
import json
import os
import urllib.parse
import logging
import time
import uuid
import secrets
from typing import Any

from mcp.server.transport_security import TransportSecuritySettings
from starlette.middleware.cors import CORSMiddleware
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response
from starlette.routing import Route

from .. import __version__
from .auth import ActivationRefused, AuthError, OAuthService, UnknownClient
from .mcp_sdk import STORE_FAULT, build_mcp
from . import console, site, trial
from .journey import CONSENT_CSS, checkout_return_html, consent_intro_html, landing_html
from .ratelimit import UNAVAILABLE_RETRY_AFTER_S, client_ip, rate_limit_key_configured, rate_limited
from .service import AccountDeletionPending, PublicService, PublicServiceError
from .security import MAX_MCP_BODY_BYTES
from .store import StoreError, StoreNotConfigured, StoreUnavailable, SupabaseStore
from .stripe import StripeError, StripeNotConfigured, StripeUnavailable, apply_webhook, verify_webhook


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
    return JSONResponse(body, status_code=status,
                        headers={"Cache-Control": "no-store", "Pragma": "no-cache"})


# Server-side faults: missing deployment configuration or an unreachable/failing
# dependency. They are never the caller's fault, so they never become a 4xx, and
# their messages (which can name configuration variables) stay in server logs.
SERVER_FAULTS = (StoreUnavailable, StripeNotConfigured, StripeUnavailable)
UNAVAILABLE_DESCRIPTION = "the service is temporarily unavailable; retry later"


def _unavailable(where: str, detail: str, *, misconfigured: bool) -> JSONResponse:
    """503 per RFC 6749 4.1.2.1: server_error for misconfiguration, temporarily_unavailable for outages."""
    _LOG.error("public dependency fault at %s (%s): %s", where,
               "misconfigured" if misconfigured else "unavailable", detail)
    return JSONResponse(
        {"error": "server_error" if misconfigured else "temporarily_unavailable",
         "error_description": UNAVAILABLE_DESCRIPTION},
        status_code=503,
        headers={"Retry-After": str(UNAVAILABLE_RETRY_AFTER_S), "Cache-Control": "no-store"},
    )


def _server_fault(where: str, exc: BaseException) -> JSONResponse:
    misconfigured = isinstance(exc, (StoreNotConfigured, StripeNotConfigured))
    return _unavailable(where, f"{type(exc).__name__}: {exc}", misconfigured=misconfigured)


async def healthz(request: Request) -> Response:
    return JSONResponse({"service": "lofgren-intelligence", "version": __version__, "status": "ok"})


async def readyz(request: Request) -> Response:
    # Public readiness names generic components only; the exact missing
    # configuration variables go to the server log.
    components = (
        ("database", ("SUPABASE_URL", "SUPABASE_SERVICE_ROLE_KEY")),
        ("authorization_provider", ("SUPABASE_URL", "SUPABASE_PUBLISHABLE_KEY")),
        ("public_base_url", ("LI_PUBLIC_BASE_URL",)),
    )
    missing_vars = sorted({name for _, names in components for name in names if not os.environ.get(name)})
    missing = [component for component, names in components if any(not os.environ.get(n) for n in names)]
    release_sha = (
        os.environ.get("LI_RELEASE_SHA")
        or os.environ.get("VERCEL_GIT_COMMIT_SHA")
        or os.environ.get("GITHUB_SHA")
    )
    if not release_sha:
        missing.append("release_sha")
        missing_vars.append("LI_RELEASE_SHA")
    # Without the limiter HMAC secret, registration, consent completion and Founding
    # Free activation refuse (fail closed, hosted/ratelimit.py); say so here first.
    if not rate_limit_key_configured():
        missing.append("rate_limit_key")
        missing_vars.append("LI_RATE_LIMIT_HMAC_KEY (missing or shorter than 32 characters)")
    if missing:
        _LOG.error("readiness: missing configuration %s", ", ".join(missing_vars))
        return JSONResponse({"ready": False, "missing": missing}, status_code=503)
    try:
        store = SupabaseStore()
        zero = "00000000-0000-0000-0000-000000000000"
        store.get_oauth_client("__readiness__")
        store.get_run(zero, "__readiness__")
        store.get_discovery(zero, "__readiness__")
        store.get_artifact(zero, "__readiness__")
        store.get_action(zero, "__readiness__")
        store.get_outcome(zero, "__readiness__")
        store.get_improvement(zero, "__readiness__")
        store.cost_samples(1)
    except Exception as exc:
        _LOG.error("readiness: database probe failed: %s: %s", type(exc).__name__, exc)
        return JSONResponse({"ready": False, "database": "unavailable", "release_sha": release_sha}, status_code=503)
    return JSONResponse({"ready": True, "release_sha": release_sha, "versions": ["V1","V2","V3","V4","V5","V6"]})


async def oauth_resource_root(request: Request) -> Response:
    base = public_base()
    return JSONResponse({
        "resource": base + "/mcp",
        "authorization_servers": [base + "/"],
        "scopes_supported": ["mcp"],
        "bearer_methods_supported": ["header"],
    })


async def oauth_server_metadata(request: Request) -> Response:
    base = public_base()
    return JSONResponse({
        "issuer": base + "/",
        "authorization_endpoint": base + "/oauth/authorize",
        "token_endpoint": base + "/oauth/token",
        "registration_endpoint": base + "/oauth/register",
        "revocation_endpoint": base + "/oauth/revoke",
        "revocation_endpoint_auth_methods_supported": ["none"],
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code", "refresh_token"],
        "code_challenge_methods_supported": ["S256"],
        "token_endpoint_auth_methods_supported": ["none"],
        "scopes_supported": ["mcp"],
    })


async def oauth_register(request: Request) -> Response:
    try:
        body = await _json_body(request, 100_000)
    except ValueError:  # Never echo parser internals to the caller.
        return _error(400, "invalid_client_metadata", "client metadata must be a bounded JSON object")
    try:
        return JSONResponse(OAuthService(SupabaseStore()).register_client(body), status_code=201)
    except SERVER_FAULTS as exc:
        return _server_fault("oauth_register", exc)
    except (AuthError, StoreError) as exc:
        return _error(400, "invalid_client_metadata", str(exc))


async def oauth_complete(request: Request) -> Response:
    try:
        body = await _json_body(request, 100_000)
    except ValueError:  # Never echo parser internals to the caller.
        return _error(400, "invalid_request", "a bounded JSON object is required")
    try:
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
            client_ip=client_ip(request),
        )
        redirect = str(body["redirect_uri"])
        query: dict[str, str] = {"code": code}
        if body.get("state") is not None:
            query["state"] = str(body["state"])
        separator = "&" if urllib.parse.urlsplit(redirect).query else "?"
        return JSONResponse({"redirect_url": redirect + separator + urllib.parse.urlencode(query)})
    except ActivationRefused as exc:
        headers = {"Cache-Control": "no-store"}
        if exc.retry_after:
            headers["Retry-After"] = str(exc.retry_after)
        return JSONResponse({"error": exc.error, "error_description": str(exc)}, status_code=exc.status,
                            headers=headers)
    except SERVER_FAULTS as exc:
        return _server_fault("oauth_complete", exc)
    except (ValueError, KeyError, AuthError, StoreError) as exc:
        return _error(400, "invalid_request", str(exc))


async def oauth_token(request: Request) -> Response:
    raw = await request.body()
    if len(raw) > 100_000:
        return _error(413, "invalid_request", "request body too large")
    try:
        form = urllib.parse.parse_qs(raw.decode("utf-8"), keep_blank_values=True, max_num_fields=32)
    except (UnicodeDecodeError, ValueError):
        return _error(400, "invalid_request", "malformed token request")
    if any(len(values) != 1 for values in form.values()):
        return _error(400, "invalid_request", "duplicate token parameters")
    get = lambda key, default="": (form.get(key) or [default])[0]
    grant = get("grant_type")
    if grant not in ("authorization_code", "refresh_token"):
        return _error(400, "unsupported_grant_type")
    try:
        oauth = OAuthService(SupabaseStore())
        if grant == "authorization_code":
            result = oauth.exchange_code(
                code=get("code"),
                code_verifier=get("code_verifier"),
                client_id=get("client_id"),
                redirect_uri=get("redirect_uri"),
            )
        else:
            result = oauth.refresh(refresh_token=get("refresh_token"), client_id=get("client_id"))
        return JSONResponse(result, headers={"Cache-Control": "no-store", "Pragma": "no-cache"})
    except SERVER_FAULTS as exc:
        return _server_fault("oauth_token", exc)
    except UnknownClient:
        # RFC 6749 5.2: an unknown (or missing) client_id is a client authentication
        # failure, invalid_client with 401, not a grant problem.
        return _error(401, "invalid_client", "unknown OAuth client")
    except (AuthError, StoreError) as exc:
        return _error(400, "invalid_grant", str(exc))


async def oauth_revoke(request: Request) -> Response:
    """RFC 7009 token revocation for public clients.

    Form parameters: token (required), client_id (required: clients are public,
    so the client identifies itself without a secret), token_type_hint
    (optional, access_token or refresh_token). A registered client always gets
    200 with an empty body, whether the token was revoked now, was already
    revoked, is unknown or belongs to another client (RFC 7009 section 2.2).
    """
    raw = await request.body()
    if len(raw) > 100_000:
        return _error(413, "invalid_request", "request body too large")
    try:
        form = urllib.parse.parse_qs(raw.decode("utf-8"), keep_blank_values=True, max_num_fields=32)
    except (UnicodeDecodeError, ValueError):
        return _error(400, "invalid_request", "malformed revocation request")
    if any(len(values) != 1 for values in form.values()):
        return _error(400, "invalid_request", "duplicate revocation parameters")
    get = lambda key: (form.get(key) or [""])[0]
    client_id = get("client_id")
    if not client_id:
        return _error(401, "invalid_client", "client_id is required")
    token = get("token")
    if not token:
        return _error(400, "invalid_request", "token is required")
    try:
        OAuthService(SupabaseStore()).revoke(token=token, client_id=client_id,
                                             token_type_hint=get("token_type_hint"))
    except SERVER_FAULTS as exc:
        return _server_fault("oauth_revoke", exc)
    except UnknownClient:
        return _error(401, "invalid_client", "unknown OAuth client")
    except StoreError as exc:
        return _server_fault("oauth_revoke", StoreUnavailable(str(exc)))
    return Response(status_code=200, headers={"Cache-Control": "no-store", "Pragma": "no-cache"})


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
        return _server_fault("oauth_authorize", exc)
    if not client or params["redirect_uri"] not in (client.get("redirect_uris") or []):
        return _error(400, "invalid_client")
    if params["resource"] != public_base() + "/mcp":
        return _error(400, "invalid_target")

    supabase_url = os.environ.get("SUPABASE_URL", "")
    public_key = os.environ.get("SUPABASE_PUBLISHABLE_KEY", "")
    if not supabase_url or not public_key:
        return _unavailable("oauth_authorize", "SUPABASE_URL or SUPABASE_PUBLISHABLE_KEY is not configured",
                            misconfigured=True)

    safe_params = json.dumps(params).replace("<", "\\u003c")
    safe_url = json.dumps(supabase_url).replace("<", "\\u003c")
    safe_key = json.dumps(public_key).replace("<", "\\u003c")
    nonce = secrets.token_urlsafe(18)
    supabase_origin = urllib.parse.urlunsplit((
        urllib.parse.urlsplit(supabase_url).scheme,
        urllib.parse.urlsplit(supabase_url).netloc,
        "", "", "",
    ))
    client_name = str(client.get("client_name") or client.get("client_id") or "Unknown MCP client")
    consent_intro = consent_intro_html(client_name, params["scope"], params["redirect_uri"])
    page = f"""<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Connect Lofgren Intelligence</title>
{site.supabase_script_tag(nonce)}
<style nonce="{nonce}">
body{{font-family:system-ui;background:#0b0d10;color:#eef2f7;margin:0;display:grid;place-items:center;min-height:100vh}}
main{{width:min(92vw,480px);background:#151922;border:1px solid #2a3240;border-radius:16px;padding:28px}}
h1{{font-size:24px;margin:0 0 6px}}p{{color:#aeb8c7}}input,button{{width:100%;box-sizing:border-box;padding:12px;margin:8px 0;border-radius:9px}}
input{{background:#0e1218;color:white;border:1px solid #344054}}button{{background:#2563eb;color:white;border:0;font-weight:700;cursor:pointer}}
small{{color:#8692a6}}#status{{min-height:24px;color:#f0b94d}}
{CONSENT_CSS}
</style></head>
<body><main>{consent_intro}
<button id="continue" hidden>Approve and continue</button>
<label for="email">Email</label>
<input id="email" type="email" autocomplete="email" placeholder="you@example.com" aria-describedby="status">
<label for="password">Password</label>
<input id="password" type="password" autocomplete="current-password" placeholder="Password" aria-describedby="status">
<button id="signin">Sign in &amp; authorize this client</button>
<button id="signup">Create account</button>
<div id="status" role="status" aria-live="polite"></div>
<small>Activated accounts 1–1000 receive quota-limited Founding Free access. Account 1001 onward requires a paid entitlement.</small>
<script nonce="{nonce}">\nconst cfg={safe_params};
const sb=supabase.createClient({safe_url},{safe_key});
const status=document.querySelector('#status');
const email=document.querySelector('#email');
const password=document.querySelector('#password');
const cont=document.querySelector('#continue');
let pendingSession=null;
async function complete(session){{
  const r=await fetch('/oauth/authorize/complete',{{method:'POST',headers:{{'content-type':'application/json','authorization':'Bearer '+session.access_token}},body:JSON.stringify(cfg)}});
  const d=await r.json(); if(!r.ok) throw new Error(d.error_description||d.error||'authorization failed');
  location.href=d.redirect_url;
}}
function arm(session){{
  pendingSession=session;
  cont.textContent='Approve and continue as '+(((session.user||{{}}).email)||'the signed-in user');
  cont.hidden=false;
  status.textContent='Review the client request, then approve explicitly.';
}}
cont.onclick=async()=>{{try{{if(!pendingSession)throw new Error('Sign in first.');cont.disabled=true;status.textContent='Authorizing…';await complete(pendingSession)}}catch(e){{status.textContent=e.message;cont.disabled=false}}}};
async function existing(){{const x=await sb.auth.getSession();if(x.data.session)arm(x.data.session);}}
document.querySelector('#signin').onclick=async()=>{{try{{status.textContent='Signing in…';const x=await sb.auth.signInWithPassword({{email:email.value,password:password.value}});if(x.error)throw x.error;arm(x.data.session)}}catch(e){{status.textContent=e.message}}}};
document.querySelector('#signup').onclick=async()=>{{try{{status.textContent='Creating account…';const x=await sb.auth.signUp({{email:email.value,password:password.value,options:{{emailRedirectTo:location.href}}}});if(x.error)throw x.error;if(x.data.session)arm(x.data.session);else status.textContent='Check your email to confirm the account, then return here.'}}catch(e){{status.textContent=e.message}}}};
existing().catch(e=>status.textContent=e.message);
</script></main></body></html>"""
    csp = (
        "default-src 'none'; "
        f"script-src 'self' 'nonce-{nonce}'; "
        f"style-src 'nonce-{nonce}'; "
        f"connect-src 'self' {supabase_origin}; "
        "img-src 'self' data:; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
    )
    return HTMLResponse(page, headers={
        "Content-Security-Policy": csp,
        "Cache-Control": "no-store",
    })


def _supabase_session_token(request: Request) -> str:
    auth = request.headers.get("authorization", "")
    if not auth.lower().startswith("bearer "):
        raise StoreError("Supabase user session is required")
    return auth.split(" ", 1)[1].strip()


def _session_user(request: Request) -> tuple[SupabaseStore, dict[str, Any]]:
    """Refuse a request without a session token before the store is touched.

    A missing token is the caller's fault (401) whatever the deployment state;
    only a request that carries one can surface a server fault.
    """
    token = _supabase_session_token(request)
    store = SupabaseStore()
    return store, store.verify_supabase_user(token)


async def account_export(request: Request) -> Response:
    try:
        store, user = _session_user(request)
        data = PublicService(store).export_account_data(str(user["id"]))
        return JSONResponse(data, headers={"Content-Disposition": "attachment; filename=lofgren-intelligence-data.json"})
    except SERVER_FAULTS as exc:
        return _server_fault("account_export", exc)
    except (StoreError, PublicServiceError) as exc:
        return _error(401, "unauthorized", str(exc))


async def account_delete(request: Request) -> Response:
    try:
        body = await _json_body(request, 10_000)
        store, user = _session_user(request)
        result = PublicService(store).delete_account(
            str(user["id"]),
            str(body.get("confirmation") or ""),
        )
        return JSONResponse(result)
    except AccountDeletionPending as exc:
        return _error(409, "account_deletion_pending", str(exc))
    except SERVER_FAULTS as exc:
        return _server_fault("account_delete", exc)
    except (StoreError, PublicServiceError, StripeError, ValueError) as exc:
        return _error(400, "account_deletion_refused", str(exc))


async def account_page(request: Request) -> Response:
    supabase_url = os.environ.get("SUPABASE_URL", "")
    public_key = os.environ.get("SUPABASE_PUBLISHABLE_KEY", "")
    if not supabase_url or not public_key:
        return _error(503, "account_management_not_configured")
    nonce = secrets.token_urlsafe(18)
    safe_url = json.dumps(supabase_url).replace("<", "\\u003c")
    safe_key = json.dumps(public_key).replace("<", "\\u003c")
    origin = urllib.parse.urlunsplit((
        urllib.parse.urlsplit(supabase_url).scheme,
        urllib.parse.urlsplit(supabase_url).netloc,
        "", "", "",
    ))
    page = f"""<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Lofgren Intelligence account</title>
{site.supabase_script_tag(nonce)}
<style nonce="{nonce}">
body{{font-family:system-ui;background:#0b0d10;color:#eef2f7;margin:0;display:grid;place-items:center;min-height:100vh}}
main{{width:min(92vw,600px);background:#151922;border:1px solid #2a3240;border-radius:16px;padding:28px}}
input,button{{width:100%;box-sizing:border-box;padding:12px;margin:8px 0;border-radius:9px}}
input{{background:#0e1218;color:white;border:1px solid #344054}}button{{background:#2563eb;color:white;border:0;font-weight:700}}
.danger{{background:#b42318}}small,#status{{color:#aeb8c7}}
</style></head><body><main>
<h1>Account &amp; privacy</h1>
<p>Sign in to export your Lofgren Intelligence account/research data or permanently delete your account.</p>
<input id="email" type="email" autocomplete="email" placeholder="Email">
<input id="password" type="password" autocomplete="current-password" placeholder="Password">
<button id="signin">Sign in</button>
<button id="export" disabled>Export my data</button>
<input id="confirm" placeholder="Type DELETE MY LOFGREN INTELLIGENCE ACCOUNT">
<button id="delete" class="danger" disabled>Permanently delete account</button>
<div id="status"></div>
<small>Deletion first closes the account to new work and stops running research; if research or usage is still settling, nothing is deleted yet and you are asked to retry shortly. It then cancels an attached paid subscription before identity and LI data are removed. If cancellation fails, deletion stops.</small>
<script nonce="{nonce}">
const sb=supabase.createClient({safe_url},{safe_key});
const status=document.querySelector('#status');
const email=document.querySelector('#email');
const password=document.querySelector('#password');
const exp=document.querySelector('#export');
const del=document.querySelector('#delete');
const confirmInput=document.querySelector('#confirm');
let session=null;
function ready(s){{session=s;exp.disabled=!s;del.disabled=!s;status.textContent=s?'Signed in.':'';}}
document.querySelector('#signin').onclick=async()=>{{try{{const x=await sb.auth.signInWithPassword({{email:email.value,password:password.value}});if(x.error)throw x.error;ready(x.data.session)}}catch(e){{status.textContent=e.message}}}};
exp.onclick=async()=>{{try{{const r=await fetch('/account/export',{{headers:{{authorization:'Bearer '+session.access_token}}}});if(!r.ok)throw new Error((await r.json()).error_description||'export failed');const blob=await r.blob();const a=document.createElement('a');a.href=URL.createObjectURL(blob);a.download='lofgren-intelligence-data.json';a.click();URL.revokeObjectURL(a.href)}}catch(e){{status.textContent=e.message}}}};
del.onclick=async()=>{{try{{if(confirmInput.value!=='DELETE MY LOFGREN INTELLIGENCE ACCOUNT')throw new Error('Confirmation phrase does not match.');const r=await fetch('/account/delete',{{method:'POST',headers:{{'content-type':'application/json',authorization:'Bearer '+session.access_token}},body:JSON.stringify({{confirmation:confirmInput.value}})}});const d=await r.json();if(!r.ok)throw new Error(d.error_description||d.error||'deletion failed');await sb.auth.signOut();ready(null);status.textContent='Account deleted.'}}catch(e){{status.textContent=e.message}}}};
sb.auth.getSession().then(x=>ready(x.data.session));
</script></main></body></html>"""
    csp = (
        "default-src 'none'; "
        f"script-src 'self' 'nonce-{nonce}'; "
        f"style-src 'nonce-{nonce}'; "
        f"connect-src 'self' {origin}; "
        "img-src 'self' data:; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
    )
    return HTMLResponse(page, headers={"Content-Security-Policy": csp, "Cache-Control": "no-store"})


async def action_details(request: Request) -> Response:
    try:
        store, user = _session_user(request)
        result = PublicService(store).action_status(str(user["id"]), {"action_id": request.path_params["action_id"]})
        return JSONResponse(result, headers={"Cache-Control": "no-store"})
    except SERVER_FAULTS as exc:
        return _server_fault("action_details", exc)
    except (StoreError, PublicServiceError) as exc:
        return _error(400, "action_unavailable", str(exc))


async def action_approve(request: Request) -> Response:
    try:
        store, user = _session_user(request)
        result = PublicService(store).approve_action(str(user["id"]), request.path_params["action_id"])
        return JSONResponse(result, headers={"Cache-Control": "no-store"})
    except SERVER_FAULTS as exc:
        return _server_fault("action_approve", exc)
    except (StoreError, PublicServiceError) as exc:
        return _error(400, "approval_refused", str(exc))


async def action_page(request: Request) -> Response:
    supabase_url = os.environ.get("SUPABASE_URL", "")
    public_key = os.environ.get("SUPABASE_PUBLISHABLE_KEY", "")
    if not supabase_url or not public_key:
        return _error(503, "action_approval_not_configured")
    nonce = secrets.token_urlsafe(18)
    safe_url = json.dumps(supabase_url).replace("<", "\\u003c")
    safe_key = json.dumps(public_key).replace("<", "\\u003c")
    safe_action = json.dumps(str(request.path_params["action_id"])).replace("<", "\\u003c")
    origin = urllib.parse.urlunsplit((
        urllib.parse.urlsplit(supabase_url).scheme,
        urllib.parse.urlsplit(supabase_url).netloc,
        "", "", "",
    ))
    page = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Approve action · Lofgren Intelligence</title>
{site.supabase_script_tag(nonce)}
<style nonce="{nonce}">
body{{font-family:system-ui;background:#0b0d10;color:#eef2f7;margin:0;display:grid;place-items:center;min-height:100vh;padding:24px}}
main{{width:min(94vw,720px);background:#151922;border:1px solid #2a3240;border-radius:16px;padding:28px}}
input,button{{width:100%;box-sizing:border-box;padding:12px;margin:7px 0;border-radius:9px}}
input{{background:#0e1218;color:white;border:1px solid #344054}}button{{background:#2563eb;color:white;border:0;font-weight:700;cursor:pointer}}
button[disabled]{{opacity:.45;cursor:not-allowed}}pre{{white-space:pre-wrap;word-break:break-word;background:#0d131c;border:1px solid #344054;padding:14px;border-radius:10px}}
.warn{{border-left:3px solid #e6b450;padding:12px 14px;background:#1a160d;color:#dac99b}}#status{{min-height:24px;color:#e6b450}}
</style></head><body><main>
<h1>Approve an exact external action</h1>
<p class="warn">Lofgren Intelligence will not execute this action until you sign in and explicitly approve the exact target and payload shown below. Approval expires after ten minutes and does not authorize a different action.</p>
<label for="email">Email</label><input id="email" type="email" autocomplete="email">
<label for="password">Password</label><input id="password" type="password" autocomplete="current-password">
<button id="signin">Sign in to review</button>
<h2>Action</h2><pre id="details">Sign in to load the exact action.</pre>
<button id="approve" disabled>Approve this exact action</button>
<div id="status" role="status" aria-live="polite"></div>
<script nonce="{nonce}">
const sb=supabase.createClient({safe_url},{safe_key});
const actionId={safe_action};
let session=null;
const status=document.querySelector('#status'), details=document.querySelector('#details'), approve=document.querySelector('#approve');
const email=document.querySelector('#email'), password=document.querySelector('#password');
async function load(){{
  if(!session)return;
  const r=await fetch('/actions/'+encodeURIComponent(actionId)+'/details',{{headers:{{authorization:'Bearer '+session.access_token}}}});
  const d=await r.json(); if(!r.ok)throw new Error(d.error_description||d.error||'Could not load action');
  details.textContent=JSON.stringify({{action_id:d.action_id,status:d.status,request:d.request}},null,2);
  approve.disabled=d.status!=='awaiting_approval';
}}
document.querySelector('#signin').onclick=async()=>{{try{{status.textContent='Signing in…';const x=await sb.auth.signInWithPassword({{email:email.value,password:password.value}});if(x.error)throw x.error;session=x.data.session;status.textContent='Review the exact action before approving.';await load()}}catch(e){{status.textContent=e.message}}}};
approve.onclick=async()=>{{try{{approve.disabled=true;status.textContent='Recording approval…';const r=await fetch('/actions/'+encodeURIComponent(actionId)+'/approve',{{method:'POST',headers:{{authorization:'Bearer '+session.access_token}}}});const d=await r.json();if(!r.ok)throw new Error(d.error_description||d.error||'Approval failed');status.textContent='Approved. Return to your AI client and call execute_action before '+d.expires_at;await load()}}catch(e){{status.textContent=e.message;approve.disabled=false}}}};
sb.auth.getSession().then(async x=>{{session=x.data.session;if(session){{status.textContent='Review the exact action before approving.';await load()}}}}).catch(e=>status.textContent=e.message);
</script></main></body></html>"""
    csp = (
        "default-src 'none'; "
        f"script-src 'self' 'nonce-{nonce}'; "
        f"style-src 'nonce-{nonce}'; "
        f"connect-src 'self' {origin}; "
        "img-src 'self' data:; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
    )
    return HTMLResponse(page, headers={"Content-Security-Policy": csp, "Cache-Control": "no-store"})


# ---- Intelligence Case charter approval (separate from V4 action approval) ----------------
#
# The owner signs in with the same account session as /account and /actions, reviews the
# exact latest charter version and approves it. The approval binds the signed-in user, the
# case, the charter version and content hash shown on the page, the scope and the budget,
# and expires. It is never an MCP call, and it does not approve any V4 action.

def _case_error(exc: Exception) -> JSONResponse:
    if isinstance(exc, SERVER_FAULTS):
        return _server_fault("cases", exc)
    if isinstance(exc, PublicServiceError):
        status = 409 if exc.code in {"CASE_VERSION_CONFLICT", "CASE_NOT_READY"} else 404 if exc.code == "CASE_NOT_FOUND" else 400
        return _error(status, exc.code.lower(), str(exc))
    return _error(401, "unauthorized", "a signed-in account session is required")


async def case_details(request: Request) -> Response:
    try:
        store, user = _session_user(request)
        result = PublicService(store).case_charter_details(
            str(user["id"]), str(request.path_params["case_id"]),
            os.environ.get("LI_PUBLIC_BASE_URL", "").rstrip("/"),
        )
        return JSONResponse(result, headers={"Cache-Control": "no-store"})
    except (StoreError, PublicServiceError) as exc:
        return _case_error(exc)


async def workspace_cases(request: Request) -> Response:
    try:
        store, user = _session_user(request)
        service = PublicService(store)
        if request.method == "POST":
            try:
                body = await _json_body(request, 10_000)
            except (ValueError, UnicodeDecodeError):
                return _error(400, "invalid_request", "a bounded JSON objective is required")
            result = service.clarify_objective(str(user["id"]), body, public_base())
        else:
            result = service.list_owned_cases(str(user["id"]))
        return JSONResponse(result, headers={"Cache-Control": "no-store"})
    except (StoreError, PublicServiceError) as exc:
        return _case_error(exc)


async def workspace_page(request: Request) -> Response:
    url = os.environ.get("SUPABASE_URL", "")
    key = os.environ.get("SUPABASE_PUBLISHABLE_KEY", "")
    if not url or not key:
        return _error(503, "workspace_not_configured")
    nonce = secrets.token_urlsafe(18)
    origin = urllib.parse.urlsplit(url)
    safe_url = json.dumps(url).replace("<", "\\u003c")
    safe_key = json.dumps(key).replace("<", "\\u003c")
    page = f'''<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Saved cases · Lofgren Intelligence</title>
{site.supabase_script_tag(nonce)}
<style nonce="{nonce}">body{{font-family:system-ui;max-width:780px;margin:40px auto;padding:20px}}input,button,textarea{{display:block;padding:12px;margin:8px 0;box-sizing:border-box;max-width:100%;width:100%}}pre{{white-space:pre-wrap;overflow-wrap:anywhere}}li{{margin:16px 0}}</style>
</head><body><main><h1>Your saved Intelligence Cases</h1>
<p>Real persisted cases. <a href="/app">Open the separate illustrative demo</a>.</p>
<label for="email">Email</label><input id="email" type="email" autocomplete="email">
<label for="password">Password</label><input id="password" type="password" autocomplete="current-password">
<button id="signin">Sign in</button><button id="refresh" disabled>Refresh saved cases</button>
<div id="status" role="status" aria-live="polite">Sign in to load your cases.</div><ul id="cases"></ul>
<h2>Create or clarify a case</h2><p>This saves scope only. It does not start research or authorize external actions.</p>
<label for="objective">Research objective</label><textarea id="objective" maxlength="3000"></textarea>
<button id="create" disabled>Save objective and ask clarification questions</button>
<pre id="questions"></pre><div id="answers"></div><button id="revise" disabled>Save clarification answers</button><a id="review" hidden>Review saved charter and history</a>
<h2>Approved research</h2><p>Queueing reserves usage against the approved budget. Work can remain queued while a worker is unavailable. Refresh the case after approving its charter.</p>
<button id="start" disabled>Queue approved research</button>
<label for="jobid">Saved job ID</label><input id="jobid" maxlength="100">
<button id="jobstatus" disabled>Check job status and saved result</button>
<button id="cancel" disabled>Request cancellation</button><pre id="job"></pre>
<h3>Saved jobs</h3><p id="jobliststatus">Sign in to recover your saved jobs.</p><ul id="savedjobs"></ul>
<h2>Inspect saved research</h2><p>A saved result can exist even when a job failed during settlement. Receipt integrity records provenance; it does not prove a source is truthful.</p>
<button id="inspect" disabled>Load report, evidence and receipt</button>
<div id="resultstatus" role="status"></div><pre id="report"></pre>
<details><summary>Claims, contradictions and unknowns</summary><pre id="findings"></pre></details>
<details><summary>Claim evidence and source traces</summary><pre id="traces"></pre></details>
<details><summary>Research receipt</summary><pre id="receipt"></pre></details>
</main><script nonce="{nonce}">
const sb=supabase.createClient({safe_url},{safe_key});let session=null,currentCase=null;
const status=document.querySelector('#status'),list=document.querySelector('#cases');
async function request(method,body,path='/workspace/cases'){{const r=await fetch(path,{{method,headers:{{authorization:'Bearer '+session.access_token,'content-type':'application/json'}},body:body?JSON.stringify(body):undefined}});const d=await r.json();if(!r.ok)throw new Error(d.error_description||d.error||'Request failed');return d}}
async function load(){{if(!session)return;status.textContent='Loading saved cases…';const d=await request('GET');list.replaceChildren();for(const c of d.cases){{const li=document.createElement('li'),a=document.createElement('a');a.href='/cases/'+encodeURIComponent(c.id);a.textContent=(c.objective||c.id)+' — '+c.status;li.append(a);const resume=document.createElement('button');resume.textContent='Continue clarification';resume.onclick=async()=>{{try{{show(await request('GET',null,'/cases/'+encodeURIComponent(c.id)+'/charter'))}}catch(e){{status.textContent=e.message}}}};li.append(resume);list.append(li)}}status.textContent=d.cases.length?(d.truncated?'First 100 saved cases shown.':'Saved cases loaded.'):'No saved cases yet.'}}
async function loadJobs(){{const d=await request('GET',null,'/workspace/jobs');const ul=document.querySelector('#savedjobs');ul.replaceChildren();for(const j of d.jobs){{const li=document.createElement('li'),button=document.createElement('button');button.textContent=(j.case_id?'Case '+j.case_id:'Research')+' — '+j.status+' — '+(j.created_at||'');button.onclick=()=>jobView(j);li.append(button);ul.append(li)}}document.querySelector('#jobliststatus').textContent=d.jobs.length?(d.truncated?'First 100 saved jobs shown.':'Select a saved job, then load its result.'):'No saved jobs yet.'}}
async function recover(){{const x=await sb.auth.getSession();session=x.data.session;for(const id of ['create','refresh','jobstatus','cancel','inspect'])document.querySelector('#'+id).disabled=!session;if(session){{await load();await loadJobs()}}}}
document.querySelector('#signin').onclick=async()=>{{try{{const x=await sb.auth.signInWithPassword({{email:document.querySelector('#email').value,password:document.querySelector('#password').value}});if(x.error)throw x.error;await recover()}}catch(e){{status.textContent=e.message}}}};
document.querySelector('#refresh').onclick=()=>recover().catch(e=>status.textContent=e.message);
function show(d){{currentCase=d;document.querySelector('#start').disabled=!(d.approval&&d.approval.state==='live');document.querySelector('#objective').value=d.objective;document.querySelector('#questions').textContent=JSON.stringify({{status:d.status,budget:d.budget,critical_unknowns:d.critical_unknowns,safety_notice:d.safety_notice}},null,2);const fields=document.querySelector('#answers');fields.replaceChildren();for(const q of d.questions){{const label=document.createElement('label'),input=document.createElement('textarea'),why=document.createElement('p');input.id='answer-'+q.key;input.dataset.answerKey=q.key;input.maxLength=3000;label.htmlFor=input.id;label.textContent=q.prompt;why.textContent=q.why;fields.append(label,why,input)}}document.querySelector('#revise').disabled=!d.questions.length;const a=document.querySelector('#review');a.href='/cases/'+encodeURIComponent(d.case_id);a.hidden=false}}
function clearResult(){{for(const id of ['report','findings','traces','receipt'])document.querySelector('#'+id).textContent=''}}
function jobView(d){{clearResult();document.querySelector('#resultstatus').textContent='Load the saved result to inspect its evidence.';document.querySelector('#jobid').value=d.job_id;document.querySelector('#job').textContent=JSON.stringify(d,null,2)}}
document.querySelector('#jobid').oninput=()=>{{clearResult();document.querySelector('#resultstatus').textContent=''}};
document.querySelector('#inspect').onclick=async()=>{{clearResult();const jobId=document.querySelector('#jobid').value;try{{const d=await request('GET',null,'/workspace/jobs/'+encodeURIComponent(jobId)+'/result');if(jobId!==document.querySelector('#jobid').value)return;document.querySelector('#resultstatus').textContent=d.result_available?'Saved result — job '+d.status+'. Research '+(d.completed?'completed':'stopped')+'. Receipt integrity: '+(d.receipt.intact?'intact':'FAILED'):'No saved result yet — job '+d.status;if(!d.result_available)return;document.querySelector('#report').textContent=d.report;document.querySelector('#findings').textContent=JSON.stringify({{findings:d.findings,contradictions:d.contradictions,unknowns:d.unknowns,stopped_reason:d.stopped_reason}},null,2);document.querySelector('#traces').textContent=JSON.stringify(d.traces,null,2);document.querySelector('#receipt').textContent=JSON.stringify(d.receipt,null,2)}}catch(e){{document.querySelector('#resultstatus').textContent=e.message}}}};
document.querySelector('#start').onclick=async()=>{{const button=document.querySelector('#start');try{{button.disabled=true;jobView(await request('POST',{{}},'/workspace/cases/'+encodeURIComponent(currentCase.case_id)+'/research'));await load();await loadJobs()}}catch(e){{status.textContent=e.message;button.disabled=false}}}};
document.querySelector('#jobstatus').onclick=async()=>{{try{{jobView(await request('GET',null,'/workspace/jobs/'+encodeURIComponent(document.querySelector('#jobid').value)))}}catch(e){{status.textContent=e.message}}}};
document.querySelector('#cancel').onclick=async()=>{{try{{jobView(await request('POST',{{}},'/workspace/jobs/'+encodeURIComponent(document.querySelector('#jobid').value)+'/cancel'))}}catch(e){{status.textContent=e.message}}}};
document.querySelector('#revise').onclick=async()=>{{const button=document.querySelector('#revise');try{{button.disabled=true;const answers={{}};for(const input of document.querySelectorAll('[data-answer-key]'))answers[input.dataset.answerKey]=input.value;show(await request('POST',{{case_id:currentCase.case_id,expected_version:currentCase.charter_version,objective:document.querySelector('#objective').value,answers}}));await load()}}catch(e){{status.textContent=e.message;button.disabled=false}}}};
document.querySelector('#create').onclick=async()=>{{const button=document.querySelector('#create');try{{button.disabled=true;const d=await request('POST',{{objective:document.querySelector('#objective').value}});show(d);await load()}}catch(e){{status.textContent=e.message}}finally{{button.disabled=!session}}}};
recover().catch(e=>status.textContent=e.message);
</script></body></html>'''
    csp = ("default-src 'none'; " + f"script-src 'self' 'nonce-{nonce}'; style-src 'nonce-{nonce}'; "
           + f"connect-src 'self' {origin.scheme}://{origin.netloc}; "
           + "base-uri 'none'; form-action 'none'; frame-ancestors 'none'")
    return HTMLResponse(page, headers={"Content-Security-Policy": csp, "Cache-Control": "no-store"})


async def workspace_start_research(request: Request) -> Response:
    try:
        store, user = _session_user(request)
        # The service loads the approved charter. Request-body replacements
        # cannot alter objective, budget, sources, owner or execution inputs.
        result = PublicService(store).start_research(
            str(user["id"]), {"case_id": str(request.path_params["case_id"])}, public_base())
        return JSONResponse(result, headers={"Cache-Control": "no-store"})
    except (StoreError, PublicServiceError) as exc:
        return _case_error(exc)


async def workspace_job(request: Request) -> Response:
    try:
        store, user = _session_user(request)
        service = PublicService(store)
        args = {"job_id": str(request.path_params["job_id"])}
        result = (service.cancel_research(str(user["id"]), args) if request.method == "POST"
                  else service.get_job_status(str(user["id"]), args))
        return JSONResponse(result, headers={"Cache-Control": "no-store"})
    except (StoreError, PublicServiceError) as exc:
        return _case_error(exc)


async def workspace_job_result(request: Request) -> Response:
    try:
        store, user = _session_user(request)
        result = PublicService(store).get_job_result(
            str(user["id"]), {"job_id": str(request.path_params["job_id"])})
        return JSONResponse(result, headers={"Cache-Control": "no-store"})
    except (StoreError, PublicServiceError) as exc:
        return _case_error(exc)


async def workspace_jobs(request: Request) -> Response:
    try:
        store, user = _session_user(request)
        result = PublicService(store).list_owned_jobs(str(user["id"]))
        return JSONResponse(result, headers={"Cache-Control": "no-store"})
    except (StoreError, PublicServiceError) as exc:
        return _case_error(exc)


async def case_approve(request: Request) -> Response:
    try:
        body = await _json_body(request, 10_000)
    except (ValueError, UnicodeDecodeError):
        return _error(400, "invalid_request", "a JSON body with charter_version and content_hash is required")
    try:
        store, user = _session_user(request)
        result = PublicService(store).approve_case_charter(
            str(user["id"]), str(request.path_params["case_id"]),
            body.get("charter_version"), body.get("content_hash"),
        )
        return JSONResponse(result, headers={"Cache-Control": "no-store"})
    except (StoreError, PublicServiceError) as exc:
        return _case_error(exc)


async def case_page(request: Request) -> Response:
    supabase_url = os.environ.get("SUPABASE_URL", "")
    public_key = os.environ.get("SUPABASE_PUBLISHABLE_KEY", "")
    if not supabase_url or not public_key:
        return _error(503, "case_approval_not_configured")
    nonce = secrets.token_urlsafe(18)
    safe_url = json.dumps(supabase_url).replace("<", "\\u003c")
    safe_key = json.dumps(public_key).replace("<", "\\u003c")
    safe_case = json.dumps(str(request.path_params["case_id"])).replace("<", "\\u003c")
    origin = urllib.parse.urlunsplit((
        urllib.parse.urlsplit(supabase_url).scheme,
        urllib.parse.urlsplit(supabase_url).netloc,
        "", "", "",
    ))
    page = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Approve Case Charter · Lofgren Intelligence</title>
{site.supabase_script_tag(nonce)}
<style nonce="{nonce}">
body{{font-family:system-ui;background:#0b0d10;color:#eef2f7;margin:0;display:grid;place-items:center;min-height:100vh;padding:24px}}
main{{width:min(94vw,760px);background:#151922;border:1px solid #2a3240;border-radius:16px;padding:28px}}
input,button{{width:100%;box-sizing:border-box;padding:12px;margin:7px 0;border-radius:9px}}
input{{background:#0e1218;color:white;border:1px solid #344054}}button{{background:#2563eb;color:white;border:0;font-weight:700;cursor:pointer}}
button[disabled]{{opacity:.45;cursor:not-allowed}}pre{{white-space:pre-wrap;word-break:break-word;background:#0d131c;border:1px solid #344054;padding:14px;border-radius:10px}}
.warn{{border-left:3px solid #e6b450;padding:12px 14px;background:#1a160d;color:#dac99b}}#status{{min-height:24px;color:#e6b450}}
</style></head><body><main>
<h1>Approve a research Case Charter</h1>
<p class="warn">Research on this case starts only after you sign in and approve the exact charter version shown below:
its objective, scope, explicit unknowns, sources and budget. The approval can start research once, expires,
and is void as soon as the charter is edited. It does not approve any external action.</p>
<label for="email">Email</label><input id="email" type="email" autocomplete="email">
<label for="password">Password</label><input id="password" type="password" autocomplete="current-password">
<button id="signin">Sign in to review</button>
<h2>Charter</h2><pre id="details">Sign in to load the exact charter.</pre>
<h2>Persisted case history</h2><pre id="history">Sign in to inspect saved events. A saved event is not proof that every stage completed.</pre>
<button id="approve" disabled>Approve this exact charter version</button>
<div id="status" role="status" aria-live="polite"></div>
<script nonce="{nonce}">
const sb=supabase.createClient({safe_url},{safe_key});
const caseId={safe_case};
let session=null, shown=null;
const status=document.querySelector('#status'), details=document.querySelector('#details'), approve=document.querySelector('#approve');
const email=document.querySelector('#email'), password=document.querySelector('#password');
async function load(){{
  if(!session)return;
  const r=await fetch('/cases/'+encodeURIComponent(caseId)+'/charter',{{headers:{{authorization:'Bearer '+session.access_token}}}});
  const d=await r.json(); if(!r.ok)throw new Error(d.error_description||d.error||'Could not load the case');
  shown={{charter_version:d.charter_version,content_hash:d.content_hash}};
  details.textContent=JSON.stringify({{case_id:d.case_id,charter_version:d.charter_version,content_hash:d.content_hash,status:d.status,objective:d.objective,budget:d.budget,accepted_answers:d.accepted_answers,critical_unknowns:d.critical_unknowns,open_questions:d.questions,charter:d.case_charter,approval:d.approval}},null,2);
  document.querySelector('#history').textContent=JSON.stringify(d.history,null,2);
  approve.disabled=d.status!=='READY_FOR_SCOPE_APPROVAL'||(d.approval&&d.approval.state==='live');
}}
document.querySelector('#signin').onclick=async()=>{{try{{status.textContent='Signing in…';const x=await sb.auth.signInWithPassword({{email:email.value,password:password.value}});if(x.error)throw x.error;session=x.data.session;status.textContent='Review the exact charter before approving.';await load()}}catch(e){{status.textContent=e.message}}}};
approve.onclick=async()=>{{try{{approve.disabled=true;status.textContent='Recording approval…';const r=await fetch('/cases/'+encodeURIComponent(caseId)+'/approve',{{method:'POST',headers:{{'content-type':'application/json',authorization:'Bearer '+session.access_token}},body:JSON.stringify(shown)}});const d=await r.json();if(!r.ok)throw new Error(d.error_description||d.error||'Approval failed');status.textContent='Approved version '+d.charter_version+'. Return to your AI client and call investigate with this case before '+d.expires_at;await load()}}catch(e){{status.textContent=e.message;approve.disabled=false}}}};
sb.auth.getSession().then(async x=>{{session=x.data.session;if(session){{status.textContent='Review the exact charter before approving.';await load()}}}}).catch(e=>status.textContent=e.message);
</script></main></body></html>"""
    csp = (
        "default-src 'none'; "
        f"script-src 'self' 'nonce-{nonce}'; "
        f"style-src 'nonce-{nonce}'; "
        f"connect-src 'self' {origin}; "
        "img-src 'self' data:; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
    )
    return HTMLResponse(page, headers={"Content-Security-Policy": csp, "Cache-Control": "no-store"})


async def stripe_webhook(request: Request) -> Response:
    raw = await request.body()
    if len(raw) > 2_000_000:
        return _error(413, "payload_too_large")
    try:
        event = verify_webhook(raw, request.headers.get("stripe-signature", ""))
        result = apply_webhook(SupabaseStore(), event)
        return JSONResponse({"received": True, "result": result})
    except SERVER_FAULTS as exc:
        return _server_fault("stripe_webhook", exc)
    except (StripeError, StoreError) as exc:
        return _error(400, "invalid_webhook", str(exc))


async def billing_success(request: Request) -> Response:
    return HTMLResponse(checkout_return_html(), headers={"Cache-Control": "no-store"})


async def billing_cancelled(request: Request) -> Response:
    return PlainTextResponse("Checkout cancelled. No entitlement change was made.")


async def landing(request: Request) -> Response:
    nonce = site.new_nonce()
    return HTMLResponse(landing_html(public_base(), nonce), headers=site.page_headers(nonce))


def _site_page(render, needs_base: bool = False):
    """Wrap a presentation-only page renderer (no store, no network, no auth)."""

    async def endpoint(request: Request) -> Response:
        nonce = site.new_nonce()
        body = render(public_base(), nonce) if needs_base else render(nonce)
        return HTMLResponse(body, headers=site.page_headers(nonce))

    endpoint.__name__ = "site_" + getattr(render, "__name__", "page")
    return endpoint


async def static_asset(request: Request) -> Response:
    name = str(request.path_params["name"])
    data = site.static_bytes(name)
    if data is None:
        return PlainTextResponse("not found", status_code=404)
    return Response(data, media_type=site.STATIC_TYPES[name],
                    headers={"Cache-Control": "public, max-age=3600"})


async def vendor_asset(request: Request) -> Response:
    name = str(request.path_params["name"])
    data = site.vendor_bytes(name)
    if data is None:
        return PlainTextResponse("not found", status_code=404)
    return Response(data, media_type=site.VENDOR_TYPES[name],
                    headers={"Cache-Control": site.VENDOR_CACHE_CONTROL})


# Public site and research-console shell. The MCP transport owns /mcp, so the
# human-readable MCP page is served at site.MCP_PAGE.
SITE_ROUTES = (
    ("/product", site.product_page, False),
    ("/pricing", site.pricing_page, False),
    (site.MCP_PAGE, site.mcp_page, True),
    ("/docs", site.docs_page, True),
    ("/about", site.about_page, False),
) + tuple((path, render, False) for path, render in console.PAGES.items())


METADATA_CACHE_CONTROL = b"public, max-age=300"


def _public_headers(path: str, headers: list[tuple[bytes, bytes]]) -> list[tuple[bytes, bytes]]:
    """Uniform cache policy and a single Vary header on every public response.

    /mcp and every /oauth/* response is no-store (RFC 6749 5.1), and token
    responses also carry Pragma: no-cache. Both protected-resource metadata
    documents and the authorization-server metadata share one short policy.
    """
    path = path.rstrip("/") or "/"
    cache: bytes | None = None
    pragma = False
    if path in ("/mcp", "/mcp-trial") or path.startswith("/oauth/"):
        cache = b"no-store"
        pragma = path in ("/oauth/token", "/oauth/revoke")
    elif path.startswith("/.well-known/"):
        cache = METADATA_CACHE_CONTROL
    drop = {b"vary"} | ({b"cache-control"} if cache else set()) | ({b"pragma"} if pragma else set())
    vary: list[str] = []
    for key, value in headers:
        if key.lower() == b"vary":
            for token in value.decode("latin-1").split(","):
                token = token.strip()
                if token and token.lower() not in {v.lower() for v in vary}:
                    vary.append(token)
    out = [(key, value) for key, value in headers if key.lower() not in drop]
    if vary:
        out.append((b"vary", ", ".join(vary).encode("latin-1")))
    if cache:
        out.append((b"cache-control", cache))
    if pragma:
        out.append((b"pragma", b"no-cache"))
    return out


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
                headers = _public_headers(scope.get("path", ""), list(message.get("headers", [])))
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


async def mcp_service_description(request: Request) -> Response:
    body: dict[str, Any] = {"service": "Lofgren Intelligence", "interface": "MCP",
                            "resource": public_base() + "/mcp", "authentication": "OAuth 2.1 with PKCE S256"}
    if trial.trial_enabled():
        body["trial"] = {"endpoint": public_base() + trial.TRIAL_PATH, "authentication": "none",
                         "tools": list(trial.TRIAL_TOOLS), "persists": False}
    return JSONResponse(body, headers={"Cache-Control": "no-store"})


async def _unhandled_fault(request: Request, exc: Exception) -> Response:
    if isinstance(exc, SERVER_FAULTS):
        return _server_fault(request.url.path, exc)
    return PlainTextResponse("Internal Server Error", status_code=500)


# Browser-based MCP clients documented to call remote MCP servers (OAuth
# discovery, dynamic registration, token exchange and /mcp) from these origins.
# LI_MCP_CLIENT_ORIGINS replaces this default (an empty value allows none);
# LI_ALLOWED_ORIGINS adds origins. A wildcard is never accepted.
DEFAULT_MCP_CLIENT_ORIGINS = ("https://chatgpt.com", "https://chat.openai.com", "https://claude.ai")


def _origin_list(value: str) -> list[str]:
    return [x.strip().rstrip("/") for x in value.split(",") if x.strip() and x.strip() != "*"]


def allowed_origins(base: str) -> list[str]:
    configured = os.environ.get("LI_MCP_CLIENT_ORIGINS")
    clients = list(DEFAULT_MCP_CLIENT_ORIGINS) if configured is None else _origin_list(configured)
    origins: list[str] = []
    for origin in [base, *clients, *_origin_list(os.environ.get("LI_ALLOWED_ORIGINS", ""))]:
        if origin not in origins:
            origins.append(origin)
    return origins


class StoreFaultGuard:
    """Turn a 401 caused by a store fault during bearer verification into a 503.

    The SDK verifies any bearer token in middleware on every route. The verifier
    records a store fault (see mcp_sdk.STORE_FAULT) and reports no identity, so
    routes that ignore the bearer are unaffected; a route that then refuses for
    want of identity (/mcp) answers 503 instead of blaming the caller's token.
    """

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope.get("type") != "http" or str(scope.get("path", "")).rstrip("/") != "/mcp":
            # Elsewhere the SDK's verdict on the bearer is unused: each route
            # authenticates (or not) on its own, and the verifier is fail-safe.
            await self.app(scope, receive, send)
            return
        faults: list[BaseException] = []
        token = STORE_FAULT.set(faults)
        replaced = False

        async def wrapped(message: Any) -> None:
            nonlocal replaced
            if replaced:
                return
            if message["type"] == "http.response.start" and faults and int(message["status"]) == 401:
                replaced = True
                await _server_fault(str(scope.get("path", "")), faults[0])(scope, receive, send)
                return
            await send(message)

        try:
            await self.app(scope, receive, wrapped)
        finally:
            STORE_FAULT.reset(token)


def build_app():
    surface = os.environ.get("LI_DEPLOYMENT_SURFACE", "full")
    if surface not in {"full", "mcp"}:
        raise RuntimeError("LI_DEPLOYMENT_SURFACE must be full or mcp")
    # Unknown LI_TRIAL_ENABLED values fail startup; the trial is mounted only on the dedicated MCP surface.
    trial_on = trial.trial_enabled() and surface == "mcp"
    base = public_base()
    parsed = urllib.parse.urlsplit(base)
    hostname = (parsed.hostname or "").lower()
    host_entries = [hostname, hostname + ":*"]
    origin_entries = allowed_origins(base)

    routes = [
        Route("/", landing, methods=["GET"]),
        Route("/healthz", healthz, methods=["GET"]),
        Route("/readyz", readyz, methods=["GET"]),
        Route("/.well-known/oauth-protected-resource", oauth_resource_root, methods=["GET"]),
        Route("/.well-known/oauth-authorization-server", oauth_server_metadata, methods=["GET"]),
        Route("/.well-known/openid-configuration", oauth_server_metadata, methods=["GET"]),
        Route("/oauth/register", rate_limited("oauth_register", oauth_register), methods=["POST"]),
        Route("/oauth/authorize", rate_limited("oauth_authorize", oauth_authorize), methods=["GET"]),
        Route("/oauth/authorize/complete", rate_limited("oauth_complete", oauth_complete), methods=["POST"]),
        Route("/oauth/token", rate_limited("oauth_token", oauth_token), methods=["POST"]),
        Route("/oauth/revoke", rate_limited("oauth_revoke", oauth_revoke), methods=["POST"]),
        Route("/stripe/webhook", stripe_webhook, methods=["POST"]),
        Route("/actions/{action_id:str}", action_page, methods=["GET"]),
        Route("/actions/{action_id:str}/details", rate_limited("actions", action_details), methods=["GET"]),
        Route("/actions/{action_id:str}/approve", rate_limited("actions", action_approve), methods=["POST"]),
        Route("/cases/{case_id:str}", case_page, methods=["GET"]),
        Route("/workspace", workspace_page, methods=["GET"]),
        Route("/workspace/cases", rate_limited("cases", workspace_cases), methods=["GET", "POST"]),
        Route("/workspace/cases/{case_id:str}/research", rate_limited("cases", workspace_start_research), methods=["POST"]),
        Route("/workspace/jobs/{job_id:str}", rate_limited("cases", workspace_job), methods=["GET"]),
        Route("/workspace/jobs", rate_limited("cases", workspace_jobs), methods=["GET"]),
        Route("/workspace/jobs/{job_id:str}/cancel", rate_limited("cases", workspace_job), methods=["POST"]),
        Route("/workspace/jobs/{job_id:str}/result", rate_limited("cases", workspace_job_result), methods=["GET"]),
        Route("/cases/{case_id:str}/charter", rate_limited("cases", case_details), methods=["GET"]),
        Route("/cases/{case_id:str}/approve", rate_limited("cases", case_approve), methods=["POST"]),
        Route("/account", account_page, methods=["GET"]),
        Route("/account/export", rate_limited("account", account_export), methods=["GET"]),
        Route("/account/delete", rate_limited("account", account_delete), methods=["POST"]),
        Route("/billing/success", billing_success, methods=["GET"]),
        Route("/billing/cancelled", billing_cancelled, methods=["GET"]),
        Route("/static/{name:str}", static_asset, methods=["GET"]),
        Route("/static/vendor/{name:str}", vendor_asset, methods=["GET"]),
    ]
    routes.extend(
        Route(path, _site_page(render, needs_base), methods=["GET"]) for path, render, needs_base in SITE_ROUTES
    )

    if surface == "mcp":
        # Explicit allowlist: new customer/admin routes remain excluded by default.
        allowed = {
            "/healthz", "/readyz", "/.well-known/oauth-protected-resource",
            "/.well-known/oauth-authorization-server", "/.well-known/openid-configuration",
            "/oauth/register", "/oauth/authorize", "/oauth/authorize/complete", "/oauth/token",
            "/oauth/revoke",
            "/actions/{action_id:str}", "/actions/{action_id:str}/details", "/actions/{action_id:str}/approve",
            "/cases/{case_id:str}", "/cases/{case_id:str}/charter", "/cases/{case_id:str}/approve",
            "/static/vendor/{name:str}",
        }
        routes = [Route("/", mcp_service_description, methods=["GET"])] + [
            route for route in routes if route.path in allowed
        ]

    mcp = build_mcp(base)
    # Register the routes through MCPServer.custom_route, the public API in every supported mcp 2.x release.
    # streamable_http_app(custom_starlette_routes=...) existed only in some releases (2.3.0 rejects it), and
    # routes registered this way are served without MCP bearer auth, exactly as before.
    for route in routes:
        mcp.custom_route(route.path, methods=sorted(route.methods - {"HEAD"}), name=route.name)(route.endpoint)
    http_settings: dict[str, Any] = dict(
        json_response=True,
        stateless_http=True,
        max_request_body_size=MAX_MCP_BODY_BYTES,
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=host_entries,
            allowed_origins=origin_entries,
        ),
        host=hostname,
    )
    app = mcp.streamable_http_app(streamable_http_path="/mcp", **http_settings)
    # The SDK's bearer verifier reaches the store in middleware, outside every
    # route handler; a server fault there must answer 503, not a bare 500.
    app.add_exception_handler(Exception, _unhandled_fault)
    if trial_on:
        # A separate anonymous MCP server with its own allowlist; /mcp and its bearer auth are unchanged.
        app = trial.mount_trial(app, base, **http_settings)
    app = StoreFaultGuard(app)
    app = CORSMiddleware(
        app,
        allow_origins=origin_entries,
        allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type", "Mcp-Protocol-Version", "Mcp-Session-Id"],
        expose_headers=["Mcp-Session-Id", "WWW-Authenticate"],
        max_age=600,
    )
    return RequestTelemetry(SecurityHeaders(app))
