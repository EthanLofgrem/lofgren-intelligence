"""OAuth 2.1 / bearer-token helpers for the remote MCP service.

Public clients use Authorization Code + PKCE (S256). Clients are dynamically
registered as public clients (no client secret). Access and refresh tokens are
opaque; only SHA-256 hashes are persisted.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import time
import re
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from .store import SupabaseStore


class AuthError(RuntimeError):
    pass


_PKCE_VERIFIER = re.compile(r"^[A-Za-z0-9._~-]{43,128}$")
_PKCE_CHALLENGE = re.compile(r"^[A-Za-z0-9_-]{43}$")


def token_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def code_challenge_s256(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def new_opaque(prefix: str, nbytes: int = 32) -> str:
    return prefix + secrets.token_urlsafe(nbytes)


def now() -> datetime:
    return datetime.now(timezone.utc)


def _valid_redirect(uri: str) -> bool:
    return uri.startswith("https://") or uri.startswith("http://localhost") or uri.startswith("http://127.0.0.1")


@dataclass(frozen=True)
class Principal:
    user_id: str
    scopes: tuple[str, ...]
    token_hash: str


class OAuthService:
    def __init__(self, store: SupabaseStore) -> None:
        self.store = store

    def register_client(self, metadata: dict[str, Any]) -> dict[str, Any]:
        redirects = metadata.get("redirect_uris") or []
        if not isinstance(redirects, list) or not redirects or len(redirects) > 10:
            raise AuthError("redirect_uris must contain 1-10 URIs")
        redirects = [str(x) for x in redirects]
        if any(not _valid_redirect(uri) for uri in redirects):
            raise AuthError("redirect URIs must use https (loopback http is allowed)")
        auth_method = metadata.get("token_endpoint_auth_method", "none")
        if auth_method != "none":
            raise AuthError("only public PKCE clients are supported")
        client_id = new_opaque("licl_", 18)
        row = {
            "client_id": client_id,
            "client_name": str(metadata.get("client_name") or "MCP client")[:200],
            "redirect_uris": redirects,
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "token_endpoint_auth_method": "none",
            "active": True,
        }
        self.store.put_oauth_client(row)
        return {
            **row,
            "client_id_issued_at": int(time.time()),
        }

    def _client(self, client_id: str, redirect_uri: str | None = None) -> dict[str, Any]:
        row = self.store.get_oauth_client(client_id)
        if not row:
            raise AuthError("unknown OAuth client")
        if redirect_uri is not None and redirect_uri not in (row.get("redirect_uris") or []):
            raise AuthError("redirect_uri is not registered for this client")
        return row

    def authorize_from_supabase_session(
        self,
        *,
        supabase_access_token: str,
        client_id: str,
        redirect_uri: str,
        code_challenge: str,
        scope: str,
        resource: str,
    ) -> str:
        if not client_id or not redirect_uri or not code_challenge:
            raise AuthError("client_id, redirect_uri and code_challenge are required")
        if not _PKCE_CHALLENGE.fullmatch(code_challenge):
            raise AuthError("PKCE S256 code_challenge must be 43 base64url characters")
        self._client(client_id, redirect_uri)
        expected_resource = os.environ.get("LI_PUBLIC_BASE_URL", "").rstrip("/") + "/mcp"
        if not expected_resource.startswith("http") or resource != expected_resource:
            raise AuthError("invalid OAuth resource")
        user = self.store.verify_supabase_user(supabase_access_token)
        user_id = str(user["id"])
        self.store.activate_account(user_id, user.get("email"))
        raw = new_opaque("lic_")
        self.store.put_oauth_code(
            {
                "code_hash": token_hash(raw),
                "user_id": user_id,
                "client_id": client_id,
                "redirect_uri": redirect_uri,
                "code_challenge": code_challenge,
                "scope": scope or "mcp",
                "resource": resource,
                "expires_at": (now() + timedelta(minutes=10)).isoformat(),
            }
        )
        return raw

    def _issue_tokens(self, user_id: str, client_id: str, scope: str, resource: str) -> dict[str, Any]:
        access = new_opaque("lit_")
        refresh = new_opaque("lir_")
        access_lifetime = timedelta(hours=1)
        refresh_lifetime = timedelta(days=90)
        self.store.put_access_token(
            {
                "token_hash": token_hash(access),
                "user_id": user_id,
                "client_id": client_id,
                "scope": scope or "mcp",
                "resource": resource,
                "expires_at": (now() + access_lifetime).isoformat(),
            }
        )
        self.store.put_refresh_token(
            {
                "token_hash": token_hash(refresh),
                "user_id": user_id,
                "client_id": client_id,
                "scope": scope or "mcp",
                "resource": resource,
                "expires_at": (now() + refresh_lifetime).isoformat(),
            }
        )
        return {
            "access_token": access,
            "refresh_token": refresh,
            "token_type": "Bearer",
            "expires_in": int(access_lifetime.total_seconds()),
            "scope": scope or "mcp",
        }

    def exchange_code(
        self,
        *,
        code: str,
        code_verifier: str,
        client_id: str,
        redirect_uri: str,
    ) -> dict[str, Any]:
        self._client(client_id, redirect_uri)
        if not _PKCE_VERIFIER.fullmatch(code_verifier):
            raise AuthError("PKCE code_verifier must be 43-128 unreserved characters")
        row = self.store.consume_oauth_code(token_hash(code))
        if not row:
            raise AuthError("authorization code is invalid, expired or already used")
        if row.get("client_id") != client_id or row.get("redirect_uri") != redirect_uri:
            raise AuthError("authorization code client or redirect mismatch")
        expected = str(row.get("code_challenge") or "")
        if not hmac.compare_digest(code_challenge_s256(code_verifier), expected):
            raise AuthError("PKCE verification failed")
        return self._issue_tokens(str(row["user_id"]), client_id, str(row.get("scope") or "mcp"), str(row.get("resource") or ""))

    def refresh(self, *, refresh_token: str, client_id: str) -> dict[str, Any]:
        self._client(client_id)
        row = self.store.consume_refresh_token(token_hash(refresh_token))
        if not row:
            raise AuthError("refresh token is invalid, expired or already used")
        if row.get("client_id") != client_id:
            raise AuthError("refresh token client mismatch")
        return self._issue_tokens(str(row["user_id"]), client_id, str(row.get("scope") or "mcp"), str(row.get("resource") or ""))

    def authenticate(self, bearer: str) -> Principal:
        if not bearer:
            raise AuthError("missing bearer token")
        digest = token_hash(bearer)
        row = self.store.get_access_token(digest)
        if not row:
            raise AuthError("invalid access token")
        expiry = row.get("expires_at")
        if expiry:
            dt = datetime.fromisoformat(str(expiry).replace("Z", "+00:00"))
            if dt <= now():
                raise AuthError("access token expired")
        scopes = tuple(str(row.get("scope") or "mcp").split())
        return Principal(str(row["user_id"]), scopes, digest)
