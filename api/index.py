"""Vercel Python entry point for the Lofgren Intelligence public MCP service."""

from __future__ import annotations

import html
import json
import os
import urllib.parse
from http.server import BaseHTTPRequestHandler
from typing import Any

from lofgren_intelligence import __version__
from lofgren_intelligence.hosted.auth import AuthError, OAuthService
from lofgren_intelligence.hosted.remote_mcp import RemoteMCP
from lofgren_intelligence.hosted.security import MAX_MCP_BODY_BYTES
from lofgren_intelligence.hosted.service import PublicService
from lofgren_intelligence.hosted.store import StoreError, SupabaseStore
from lofgren_intelligence.hosted.stripe import StripeError, apply_webhook, verify_webhook


def _base(headers: Any) -> str:
    configured = os.environ.get("LI_PUBLIC_BASE_URL", "").rstrip("/")
    if configured:
        return configured
    proto = headers.get("x-forwarded-proto", "https")
    host = headers.get("x-forwarded-host") or headers.get("host") or "localhost"
    return f"{proto}://{host}".rstrip("/")


def _json_bytes(value: Any) -> bytes:
    return json.dumps(value, separators=(",", ":"), default=str).encode("utf-8")


class handler(BaseHTTPRequestHandler):
    server_version = "LofgrenIntelligence/" + __version__

    def _headers(self, status: int, content_type: str, extra: dict[str, str] | None = None) -> None:
        self.send_response(status)
        self.send_header("content-type", content_type)
        self.send_header("cache-control", "no-store")
        self.send_header("x-content-type-options", "nosniff")
        self.send_header("referrer-policy", "no-referrer")
        self.send_header("x-frame-options", "DENY")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()

    def _json(self, status: int, value: Any, extra: dict[str, str] | None = None) -> None:
        data = _json_bytes(value)
        self._headers(status, "application/json; charset=utf-8", {"content-length": str(len(data)), **(extra or {})})
        self.wfile.write(data)

    def _text(self, status: int, value: str, content_type: str = "text/plain; charset=utf-8") -> None:
        data = value.encode("utf-8")
        self._headers(status, content_type, {"content-length": str(len(data))})
        self.wfile.write(data)

    def _body(self, limit: int = MAX_MCP_BODY_BYTES) -> bytes:
        try:
            size = int(self.headers.get("content-length", "0"))
        except ValueError:
            raise ValueError("invalid Content-Length")
        if size < 0 or size > limit:
            raise ValueError("request body too large")
        return self.rfile.read(size)

    def _json_body(self) -> dict[str, Any]:
        raw = self._body()
        value = json.loads(raw.decode("utf-8") or "{}")
        if not isinstance(value, dict):
            raise ValueError("JSON body must be an object")
        return value

    def _form_body(self) -> dict[str, str]:
        raw = self._body().decode("utf-8")
        rows = urllib.parse.parse_qs(raw, keep_blank_values=True)
        return {k: v[-1] for k, v in rows.items()}

    def _store(self) -> SupabaseStore:
        return SupabaseStore()

    def _oauth(self) -> OAuthService:
        return OAuthService(self._store())

    def _bearer(self) -> str:
        value = self.headers.get("authorization", "")
        if not value.lower().startswith("bearer "):
            return ""
        return value.split(" ", 1)[1].strip()

    def _require_principal(self):
        try:
            return self._oauth().authenticate(self._bearer())
        except (AuthError, StoreError) as exc:
            base = _base(self.headers)
            self._json(
                401,
                {"error": "invalid_token", "error_description": str(exc)},
                {"www-authenticate": f'Bearer resource_metadata="{base}/.well-known/oauth-protected-resource"'},
            )
            return None

    def do_OPTIONS(self) -> None:
        self.send_response(204)
        self.send_header("access-control-allow-origin", self.headers.get("origin", "null"))
        self.send_header("vary", "Origin")
        self.send_header("access-control-allow-headers", "authorization,content-type,mcp-protocol-version")
        self.send_header("access-control-allow-methods", "GET,POST,OPTIONS")
        self.send_header("access-control-max-age", "600")
        self.end_headers()

    def do_GET(self) -> None:
        parsed = urllib.parse.urlsplit(self.path)
        path = parsed.path
        base = _base(self.headers)
        if path == "/healthz":
            return self._json(200, {"service": "lofgren-intelligence", "version": __version__, "status": "ok"})
        if path == "/readyz":
            missing = [
                name
                for name in ("SUPABASE_URL", "SUPABASE_SERVICE_ROLE_KEY", "SUPABASE_PUBLISHABLE_KEY", "LI_PUBLIC_BASE_URL")
                if not os.environ.get(name)
            ]
            if missing:
                return self._json(503, {"ready": False, "missing": missing})
            try:
                self._store().get_oauth_client("__readiness__")
            except Exception as exc:
                return self._json(503, {"ready": False, "database": str(exc)})
            return self._json(200, {"ready": True})
        if path == "/.well-known/oauth-protected-resource":
            return self._json(200, {
                "resource": base + "/mcp",
                "authorization_servers": [base],
                "scopes_supported": ["mcp"],
            })
        if path in ("/.well-known/oauth-authorization-server", "/.well-known/openid-configuration"):
            return self._json(200, {
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
        if path == "/oauth/authorize":
            return self._authorize_page(parsed, base)
        if path == "/billing/success":
            return self._text(200, "Payment received. Return to your AI client and retry the Lofgren Intelligence request.")
        if path == "/billing/cancelled":
            return self._text(200, "Checkout cancelled. No entitlement change was made.")
        if path == "/":
            return self._landing(base)
        self._json(404, {"error": "not_found"})

    def do_POST(self) -> None:
        path = urllib.parse.urlsplit(self.path).path
        try:
            if path == "/oauth/register":
                return self._oauth_register()
            if path == "/oauth/authorize/complete":
                return self._oauth_complete()
            if path == "/oauth/token":
                return self._oauth_token()
            if path == "/stripe/webhook":
                return self._stripe_webhook()
            if path == "/mcp":
                return self._mcp()
            self._json(404, {"error": "not_found"})
        except (ValueError, json.JSONDecodeError) as exc:
            self._json(400, {"error": "invalid_request", "error_description": str(exc)})
        except AuthError as exc:
            self._json(400, {"error": "invalid_grant", "error_description": str(exc)})
        except StripeError as exc:
            self._json(400, {"error": "stripe_error", "error_description": str(exc)})
        except StoreError as exc:
            self._json(503, {"error": "store_unavailable", "error_description": str(exc)})
        except Exception:
            # Keep internal details out of public responses. Runtime logs retain the traceback.
            import traceback
            traceback.print_exc()
            self._json(500, {"error": "internal_error"})

    def _oauth_register(self) -> None:
        result = self._oauth().register_client(self._json_body())
        self._json(201, result)

    def _oauth_complete(self) -> None:
        body = self._json_body()
        bearer = self._bearer()
        if not bearer:
            raise AuthError("Supabase user session is required")
        code = self._oauth().authorize_from_supabase_session(
            supabase_access_token=bearer,
            client_id=str(body.get("client_id") or ""),
            redirect_uri=str(body.get("redirect_uri") or ""),
            code_challenge=str(body.get("code_challenge") or ""),
            scope=str(body.get("scope") or "mcp"),
        )
        redirect = str(body["redirect_uri"])
        query = {"code": code}
        if body.get("state") is not None:
            query["state"] = str(body["state"])
        separator = "&" if urllib.parse.urlsplit(redirect).query else "?"
        self._json(200, {"redirect_url": redirect + separator + urllib.parse.urlencode(query)})

    def _oauth_token(self) -> None:
        form = self._form_body()
        grant = form.get("grant_type")
        oauth = self._oauth()
        if grant == "authorization_code":
            result = oauth.exchange_code(
                code=form.get("code", ""),
                code_verifier=form.get("code_verifier", ""),
                client_id=form.get("client_id", ""),
                redirect_uri=form.get("redirect_uri", ""),
            )
        elif grant == "refresh_token":
            result = oauth.refresh(
                refresh_token=form.get("refresh_token", ""),
                client_id=form.get("client_id", ""),
            )
        else:
            return self._json(400, {"error": "unsupported_grant_type"})
        self._json(200, result)

    def _stripe_webhook(self) -> None:
        raw = self._body(limit=2_000_000)
        event = verify_webhook(raw, self.headers.get("stripe-signature", ""))
        result = apply_webhook(self._store(), event)
        self._json(200, {"received": True, "result": result})

    def _mcp(self) -> None:
        principal = self._require_principal()
        if principal is None:
            return
        msg = self._json_body()
        service = PublicService(self._store())
        reply = RemoteMCP(service, principal, _base(self.headers)).handle(msg)
        if reply is None:
            self._headers(202, "application/json; charset=utf-8", {"content-length": "0"})
            return
        self._json(200, reply, {"mcp-protocol-version": "2025-06-18"})

    def _authorize_page(self, parsed: urllib.parse.SplitResult, base: str) -> None:
        q = urllib.parse.parse_qs(parsed.query)
        get = lambda name, default="": (q.get(name) or [default])[-1]
        params = {
            "client_id": get("client_id"),
            "redirect_uri": get("redirect_uri"),
            "code_challenge": get("code_challenge"),
            "code_challenge_method": get("code_challenge_method", "S256"),
            "scope": get("scope", "mcp"),
            "state": get("state"),
        }
        if get("response_type") != "code" or params["code_challenge_method"] != "S256":
            return self._json(400, {"error": "invalid_request", "error_description": "code + PKCE S256 required"})
        client = self._store().get_oauth_client(params["client_id"])
        if not client or params["redirect_uri"] not in (client.get("redirect_uris") or []):
            return self._json(400, {"error": "invalid_client"})
        supabase_url = os.environ.get("SUPABASE_URL", "")
        public_key = os.environ.get("SUPABASE_PUBLISHABLE_KEY", "")
        if not supabase_url or not public_key:
            return self._json(503, {"error": "authorization_not_configured"})
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
<p>Connect your AI client to the certified V1 evidence and verification service.</p>
<input id="email" type="email" autocomplete="email" placeholder="Email">
<input id="password" type="password" autocomplete="current-password" placeholder="Password">
<button id="signin">Sign in & connect</button>
<button id="signup">Create account</button>
<div id="status"></div>
<small>The first 1,000 activated accounts receive Founding Free access subject to usage limits. Later accounts require a paid entitlement.</small>
<script>
const cfg={safe_params};
const sb=supabase.createClient({safe_url},{safe_key});
const status=document.querySelector('#status');
async function complete(session){{
  const r=await fetch('/oauth/authorize/complete',{{method:'POST',headers:{{'content-type':'application/json','authorization':'Bearer '+session.access_token}},body:JSON.stringify(cfg)}});
  const d=await r.json(); if(!r.ok) throw new Error(d.error_description||d.error||'authorization failed');
  location.href=d.redirect_url;
}}
async function existing(){{
  const x=await sb.auth.getSession(); if(x.data.session) await complete(x.data.session);
}}
document.querySelector('#signin').onclick=async()=>{{try{{status.textContent='Signing in…';const x=await sb.auth.signInWithPassword({{email:email.value,password:password.value}});if(x.error)throw x.error;await complete(x.data.session)}}catch(e){{status.textContent=e.message}}}};
document.querySelector('#signup').onclick=async()=>{{try{{status.textContent='Creating account…';const x=await sb.auth.signUp({{email:email.value,password:password.value,options:{{emailRedirectTo:location.href}}}});if(x.error)throw x.error;if(x.data.session)await complete(x.data.session);else status.textContent='Check your email to confirm the account, then return to this page.'}}catch(e){{status.textContent=e.message}}}};
existing().catch(e=>status.textContent=e.message);
</script></main></body></html>"""
        self._text(200, page, "text/html; charset=utf-8")

    def _landing(self, base: str) -> None:
        page = f"""<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Lofgren Intelligence</title><style>body{{font-family:system-ui;background:#0b0d10;color:#eef2f7;max-width:760px;margin:60px auto;padding:20px}}code{{background:#171c25;padding:3px 6px;border-radius:5px}}a{{color:#7db2ff}}</style></head>
<body><h1>Lofgren Intelligence</h1><p>Evidence-driven research and verification through MCP.</p>
<p><strong>Current public capability:</strong> certified V1 Evidence Intelligence. V2-V6 are not represented as completed until separately certified.</p>
<p>MCP endpoint: <code>{html.escape(base)}/mcp</code></p>
<p>Health: <a href="/healthz">/healthz</a> · OAuth metadata: <a href="/.well-known/oauth-authorization-server">authorization server</a></p>
</body></html>"""
        self._text(200, page, "text/html; charset=utf-8")
