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
from .auth import ActivationRefused, AuthError, OAuthService
from .mcp_sdk import build_mcp
from . import console, site
from .journey import checkout_return_html, consent_intro_html, landing_html
from .ratelimit import client_ip, rate_limited
from .service import AccountDeletionPending, PublicService, PublicServiceError
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
    release_sha = (
        os.environ.get("LI_RELEASE_SHA")
        or os.environ.get("VERCEL_GIT_COMMIT_SHA")
        or os.environ.get("GITHUB_SHA")
    )
    if not release_sha:
        missing.append("release_sha")
    if missing:
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
        return JSONResponse({"ready": False, "database": str(exc), "release_sha": release_sha}, status_code=503)
    return JSONResponse({"ready": True, "release_sha": release_sha, "versions": ["V1","V2","V3","V4","V5","V6"]})


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


async def account_export(request: Request) -> Response:
    try:
        store = SupabaseStore()
        user = store.verify_supabase_user(_supabase_session_token(request))
        data = PublicService(store).export_account_data(str(user["id"]))
        return JSONResponse(data, headers={"Content-Disposition": "attachment; filename=lofgren-intelligence-data.json"})
    except (StoreError, PublicServiceError) as exc:
        return _error(401, "unauthorized", str(exc))


async def account_delete(request: Request) -> Response:
    try:
        body = await _json_body(request, 10_000)
        store = SupabaseStore()
        user = store.verify_supabase_user(_supabase_session_token(request))
        result = PublicService(store).delete_account(
            str(user["id"]),
            str(body.get("confirmation") or ""),
        )
        return JSONResponse(result)
    except AccountDeletionPending as exc:
        return _error(409, "account_deletion_pending", str(exc))
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
        store = SupabaseStore()
        user = store.verify_supabase_user(_supabase_session_token(request))
        result = PublicService(store).action_status(str(user["id"]), {"action_id": request.path_params["action_id"]})
        return JSONResponse(result, headers={"Cache-Control": "no-store"})
    except (StoreError, PublicServiceError) as exc:
        return _error(400, "action_unavailable", str(exc))


async def action_approve(request: Request) -> Response:
    try:
        store = SupabaseStore()
        user = store.verify_supabase_user(_supabase_session_token(request))
        result = PublicService(store).approve_action(str(user["id"]), request.path_params["action_id"])
        return JSONResponse(result, headers={"Cache-Control": "no-store"})
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
    if isinstance(exc, PublicServiceError):
        status = 409 if exc.code in {"CASE_VERSION_CONFLICT", "CASE_NOT_READY"} else 404 if exc.code == "CASE_NOT_FOUND" else 400
        return _error(status, exc.code.lower(), str(exc))
    return _error(401, "unauthorized", "a signed-in account session is required")


async def case_details(request: Request) -> Response:
    try:
        store = SupabaseStore()
        user = store.verify_supabase_user(_supabase_session_token(request))
        result = PublicService(store).case_charter_details(
            str(user["id"]), str(request.path_params["case_id"]),
            os.environ.get("LI_PUBLIC_BASE_URL", "").rstrip("/"),
        )
        return JSONResponse(result, headers={"Cache-Control": "no-store"})
    except (StoreError, PublicServiceError) as exc:
        return _case_error(exc)


async def workspace_cases(request: Request) -> Response:
    try:
        store = SupabaseStore()
        user = store.verify_supabase_user(_supabase_session_token(request))
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
        store = SupabaseStore()
        user = store.verify_supabase_user(_supabase_session_token(request))
        # The service loads the approved charter. Request-body replacements
        # cannot alter objective, budget, sources, owner or execution inputs.
        result = PublicService(store).start_research(
            str(user["id"]), {"case_id": str(request.path_params["case_id"])}, public_base())
        return JSONResponse(result, headers={"Cache-Control": "no-store"})
    except (StoreError, PublicServiceError) as exc:
        return _case_error(exc)


async def workspace_job(request: Request) -> Response:
    try:
        store = SupabaseStore()
        user = store.verify_supabase_user(_supabase_session_token(request))
        service = PublicService(store)
        args = {"job_id": str(request.path_params["job_id"])}
        result = (service.cancel_research(str(user["id"]), args) if request.method == "POST"
                  else service.get_job_status(str(user["id"]), args))
        return JSONResponse(result, headers={"Cache-Control": "no-store"})
    except (StoreError, PublicServiceError) as exc:
        return _case_error(exc)


async def workspace_job_result(request: Request) -> Response:
    try:
        store = SupabaseStore()
        user = store.verify_supabase_user(_supabase_session_token(request))
        result = PublicService(store).get_job_result(
            str(user["id"]), {"job_id": str(request.path_params["job_id"])})
        return JSONResponse(result, headers={"Cache-Control": "no-store"})
    except (StoreError, PublicServiceError) as exc:
        return _case_error(exc)


async def workspace_jobs(request: Request) -> Response:
    try:
        store = SupabaseStore()
        user = store.verify_supabase_user(_supabase_session_token(request))
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
        store = SupabaseStore()
        user = store.verify_supabase_user(_supabase_session_token(request))
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
                if scope.get("path", "").rstrip("/") == "/mcp":
                    headers = [(key, value) for key, value in headers if key.lower() != b"cache-control"]
                    headers.append((b"cache-control", b"no-store"))
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
        Route("/oauth/register", rate_limited("oauth_register", oauth_register), methods=["POST"]),
        Route("/oauth/authorize", rate_limited("oauth_authorize", oauth_authorize), methods=["GET"]),
        Route("/oauth/authorize/complete", rate_limited("oauth_complete", oauth_complete), methods=["POST"]),
        Route("/oauth/token", rate_limited("oauth_token", oauth_token), methods=["POST"]),
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

    mcp = build_mcp(base)
    # Register the routes through MCPServer.custom_route, the public API in every supported mcp 2.x release.
    # streamable_http_app(custom_starlette_routes=...) existed only in some releases (2.3.0 rejects it), and
    # routes registered this way are served without MCP bearer auth, exactly as before.
    for route in routes:
        mcp.custom_route(route.path, methods=sorted(route.methods - {"HEAD"}), name=route.name)(route.endpoint)
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
