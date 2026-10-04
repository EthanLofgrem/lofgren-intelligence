"""OAuth 2.1 / bearer-token helpers for the remote MCP service."""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from .store import StoreError, SupabaseStore


class AuthError(RuntimeError):
    pass


def token_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def code_challenge_s256(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def new_opaque(prefix: str, nbytes: int = 32) -> str:
    return prefix + secrets.token_urlsafe(nbytes)


def now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class Principal:
    user_id: str
    scopes: tuple[str, ...]
    token_hash: str


class OAuthService:
    def __init__(self, store: SupabaseStore) -> None:
        self.store = store

    def authorize_from_supabase_session(
        self,
        *,
        supabase_access_token: str,
        client_id: str,
        redirect_uri: str,
        code_challenge: str,
        scope: str,
    ) -> str:
        if not client_id or not redirect_uri or not code_challenge:
            raise AuthError("client_id, redirect_uri and code_challenge are required")
        if not redirect_uri.startswith(("https://", "http://localhost", "http://127.0.0.1")):
            raise AuthError("redirect_uri must use https (localhost is allowed for development)")
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
                "expires_at": (now() + timedelta(minutes=10)).isoformat(),
            }
        )
        return raw

    def exchange_code(
        self,
        *,
        code: str,
        code_verifier: str,
        client_id: str,
        redirect_uri: str,
    ) -> dict[str, Any]:
        row = self.store.consume_oauth_code(token_hash(code))
        if not row:
            raise AuthError("authorization code is invalid, expired or already used")
        if row.get("client_id") != client_id or row.get("redirect_uri") != redirect_uri:
            raise AuthError("authorization code client or redirect mismatch")
        expected = str(row.get("code_challenge") or "")
        if not hmac.compare_digest(code_challenge_s256(code_verifier), expected):
            raise AuthError("PKCE verification failed")
        raw = new_opaque("lit_")
        expires = now() + timedelta(days=30)
        self.store.put_access_token(
            {
                "token_hash": token_hash(raw),
                "user_id": row["user_id"],
                "scope": row.get("scope") or "mcp",
                "expires_at": expires.isoformat(),
            }
        )
        return {
            "access_token": raw,
            "token_type": "Bearer",
            "expires_in": int(timedelta(days=30).total_seconds()),
            "scope": row.get("scope") or "mcp",
        }

    def authenticate(self, bearer: str) -> Principal:
        if not bearer:
            raise AuthError("missing bearer token")
        row = self.store.get_access_token(token_hash(bearer))
        if not row:
            raise AuthError("invalid access token")
        expiry = row.get("expires_at")
        if expiry:
            dt = datetime.fromisoformat(str(expiry).replace("Z", "+00:00"))
            if dt <= now():
                raise AuthError("access token expired")
        scopes = tuple(str(row.get("scope") or "mcp").split())
        return Principal(str(row["user_id"]), scopes, token_hash(bearer))
