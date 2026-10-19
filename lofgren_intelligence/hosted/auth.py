"""OAuth 2.1 / bearer-token helpers for the remote MCP service.

Public clients use Authorization Code + PKCE (S256). Clients are dynamically
registered as public clients (no client secret). Access and refresh tokens are
opaque; only SHA-256 hashes are persisted.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import math
import secrets
import time
import re
import os
import urllib.parse
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from .store import SupabaseStore


class AuthError(RuntimeError):
    pass


class UnknownClient(AuthError):
    """The client_id names no registered, active client (RFC 6749 5.2 invalid_client, HTTP 401)."""


class ActivationRefused(AuthError):
    """A new Founding Free/paid-required activation was refused before any slot was taken.

    `error` is the OAuth-style error code and `status` the HTTP status the web
    layer answers with; `retry_after` is set for rate limits.
    """

    def __init__(self, error: str, description: str, status: int, retry_after: int | None = None) -> None:
        super().__init__(description)
        self.error = error
        self.status = status
        self.retry_after = retry_after


def require_confirmed_email() -> bool:
    """LI_REQUIRE_CONFIRMED_EMAIL, default ON. Only an explicit 0/false/no/off disables it."""
    raw = os.environ.get("LI_REQUIRE_CONFIRMED_EMAIL", "").strip().lower()
    return raw not in {"0", "false", "no", "off"}


def email_domain(email: Any) -> str | None:
    if not isinstance(email, str) or email.count("@") != 1:
        return None
    domain = email.rsplit("@", 1)[1].strip().lower().rstrip(".")
    return domain or None


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


_LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1"}


def _valid_redirect(uri: str) -> bool:
    """https anywhere, or plain http only to an exact loopback host.

    A prefix test accepted `http://localhost.attacker.example/` and
    `http://127.0.0.1.attacker.example/` as loopback, so an authorization code
    could be sent in clear text to a remote host that the consent page then
    displayed as "localhost...". The host is parsed and compared exactly.
    """
    if not isinstance(uri, str) or any(ord(char) <= 32 or ord(char) == 127 for char in uri):
        return False
    try:
        p = urllib.parse.urlsplit(uri)
        host = (p.hostname or "").lower()
        p.port  # raises ValueError on a malformed port
    except ValueError:
        return False
    if not host or p.username is not None or p.password is not None or p.fragment:
        return False
    if p.scheme == "https":
        return True
    return p.scheme == "http" and host in _LOOPBACK_HOSTS


ACCESS_TOKEN_LIFETIME = timedelta(hours=1)
REFRESH_TOKEN_LIFETIME = timedelta(days=90)


def _token_response(access: str, refresh: str, scope: str) -> dict[str, Any]:
    return {
        "access_token": access,
        "refresh_token": refresh,
        "token_type": "Bearer",
        "expires_in": int(ACCESS_TOKEN_LIFETIME.total_seconds()),
        "scope": scope or "mcp",
    }


@dataclass(frozen=True)
class Principal:
    user_id: str
    scopes: tuple[str, ...]
    token_hash: str


def validated_access_record(row: Any) -> tuple[str, str, str, tuple[str, ...], datetime]:
    """Validate a persisted opaque-token record without granting default scopes.

    Both the SDK verifier and legacy service authenticator use this boundary.
    Storage filtering alone must not turn malformed records into valid tokens.
    """
    if not isinstance(row, dict) or row.get("revoked_at"):
        raise AuthError("invalid access token")
    for key in ("user_id", "client_id", "resource", "expires_at"):
        if not isinstance(row.get(key), str) or not row[key].strip():
            raise AuthError("invalid access token")
    if not isinstance(row.get("scope"), str):
        raise AuthError("invalid access token")
    try:
        expiry = datetime.fromisoformat(row["expires_at"].replace("Z", "+00:00"))
    except ValueError:
        raise AuthError("invalid access token") from None
    if expiry.tzinfo is None:
        raise AuthError("invalid access token")
    if expiry <= now():
        raise AuthError("access token expired")
    return row["user_id"], row["client_id"], row["resource"], tuple(row["scope"].split()), expiry


class OAuthService:
    def __init__(self, store: SupabaseStore) -> None:
        self.store = store

    def register_client(self, metadata: dict[str, Any]) -> dict[str, Any]:
        redirects = metadata.get("redirect_uris") or []
        if not isinstance(redirects, list) or not redirects or len(redirects) > 10:
            raise AuthError("redirect_uris must contain 1-10 URIs")
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
            raise UnknownClient("unknown OAuth client")
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
        client_ip: str | None = None,
    ) -> str:
        if not client_id or not redirect_uri or not code_challenge:
            raise AuthError("client_id, redirect_uri and code_challenge are required")
        if not _PKCE_CHALLENGE.fullmatch(code_challenge):
            raise AuthError("PKCE S256 code_challenge must be 43 base64url characters")
        if not isinstance(scope, str) or (scope and scope.split() != ["mcp"]):
            raise AuthError("only the mcp scope is supported")
        self._client(client_id, redirect_uri)
        expected_resource = os.environ.get("LI_PUBLIC_BASE_URL", "").rstrip("/") + "/mcp"
        if not expected_resource.startswith("http") or resource != expected_resource:
            raise AuthError("invalid OAuth resource")
        user = self.store.verify_supabase_user(supabase_access_token)
        user_id = str(user["id"])
        if not self.store.get_account(user_id):
            # First authorization: this call would take an activation number
            # (Founding Free for 1..1000). Re-authorizing an existing account
            # takes nothing and is not re-checked; li_activate_account is
            # idempotent per user, so a retry or a concurrent duplicate never
            # consumes a second slot.
            self._admit_activation(user, client_ip)
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

    def _admit_activation(self, user: dict[str, Any], client_ip: str | None) -> None:
        """Anti-abuse checks in front of a new activation.

        1. The Supabase user must have a confirmed email (`email_confirmed_at`
           on the user that verify_supabase_user fetched from /auth/v1/user)
           unless the operator sets LI_REQUIRE_CONFIRMED_EMAIL=false. This is
           meaningful only while Supabase "Confirm email" is on: with it off,
           Supabase auto-confirms every sign-up. CAPTCHA (Supabase Auth bot
           protection) and "Confirm email" are owner settings in Supabase.
        2. Global (store-backed) limits per client IP and per email domain;
           a limiter failure refuses the activation (fail closed).
        """
        from .ratelimit import RateLimitUnavailable, take

        domain = email_domain(user.get("email"))
        if require_confirmed_email() and not (domain and user.get("email_confirmed_at")):
            raise ActivationRefused(
                "email_not_confirmed",
                "confirm your email address using the link Supabase sent you, then sign in again",
                403,
            )
        checks = (("activation_ip", client_ip or "unknown"), ("activation_domain", domain or "unknown"))
        for bucket, key in checks:
            try:
                wait = take(bucket, key, store=self.store)
            except RateLimitUnavailable:
                raise ActivationRefused(
                    "temporarily_unavailable", "account activation is temporarily unavailable; retry shortly",
                    503, 30,
                ) from None
            if wait:
                raise ActivationRefused(
                    "rate_limited", "too many new account activations; retry later",
                    429, max(1, math.ceil(wait)),
                )

    def _issue_tokens(self, user_id: str, client_id: str, scope: str, resource: str, *, grant_kind: str, grant_hash: str) -> dict[str, Any]:
        """Issue an access/refresh pair from a consumed code or refresh token, atomically.

        li_issue_oauth_tokens (migration 20261013190000) re-checks the consumed parent
        under the user/client lock, records the new access token's sibling refresh hash,
        and, for a refresh grant, revokes the access token issued with the consumed
        refresh token and keeps a tombstone of the consumed hash so a later replay is
        recognised. Only SHA-256 digests reach the store.
        """
        access = new_opaque("lit_")
        refresh = new_opaque("lir_")
        access_row = {
                "token_hash": token_hash(access),
                "user_id": user_id,
                "client_id": client_id,
                "scope": scope or "mcp",
                "resource": resource,
                "expires_at": (now() + ACCESS_TOKEN_LIFETIME).isoformat(),
            }
        refresh_row = {
                "token_hash": token_hash(refresh),
                "user_id": user_id,
                "client_id": client_id,
                "scope": scope or "mcp",
                "resource": resource,
                "expires_at": (now() + REFRESH_TOKEN_LIFETIME).isoformat(),
            }
        if not self.store.issue_oauth_tokens(grant_kind, grant_hash, access_row, refresh_row):
            raise AuthError("OAuth grant was revoked or already issued")
        return _token_response(access, refresh, scope)

    @staticmethod
    def _grant_identity(row: dict[str, Any]) -> tuple[str, str, str]:
        """Never manufacture permissions from incomplete persisted grants."""
        if any(not isinstance(row.get(key), str) or not row[key].strip()
               for key in ("user_id", "scope", "resource")):
            raise AuthError("invalid persisted OAuth grant")
        if row["scope"].split() != ["mcp"]:
            raise AuthError("unsupported persisted OAuth scope")
        return row["user_id"], row["scope"], row["resource"]

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
        user_id, scope, resource = self._grant_identity(row)
        return self._issue_tokens(user_id, client_id, scope, resource,
                                  grant_kind="authorization_code", grant_hash=token_hash(code))

    def refresh(self, *, refresh_token: str, client_id: str) -> dict[str, Any]:
        """Rotate a refresh token (OAuth 2.1 section 4.3.1, public clients).

        li_consume_refresh_token consumes the presented token atomically (one winner under
        concurrency); li_issue_oauth_tokens then issues the new pair and revokes the access
        token issued with the consumed refresh token: the client has just shown it holds a
        newer credential, and leaving the old access token valid for up to an hour would only
        widen the window for a leaked copy. Presenting a refresh token that was already used
        (or is being used by a concurrent request) is replay: li_oauth_refresh_replay revokes
        the user's whole connection of that client (the same scope as RFC 7009 revocation and
        disconnect), including tokens just issued to the legitimate client, and the client
        must authorize again.
        """
        self._client(client_id)
        if not refresh_token:
            raise AuthError("refresh token is invalid, expired or already used")
        digest = token_hash(refresh_token)
        row = self.store.consume_refresh_token(digest)
        if not row:
            if self.store.oauth_refresh_replay(digest, client_id):
                raise AuthError("refresh token was already used; the authorization has been revoked")
            raise AuthError("refresh token is invalid, expired or already used")
        if row.get("client_id") != client_id:
            # A public client never legitimately holds another client's refresh token. The
            # presented token is already consumed; treat it as theft and revoke the owner's
            # authorization for that client (li_oauth_refresh_replay), then refuse.
            self.store.oauth_refresh_replay(digest, str(row.get("client_id") or ""))
            raise AuthError("refresh token client mismatch")
        user_id, scope, resource = self._grant_identity(row)
        return self._issue_tokens(user_id, client_id, scope, resource,
                                  grant_kind="refresh_token", grant_hash=digest)

    def revoke(self, *, token: str, client_id: str, token_type_hint: str = "") -> None:
        """RFC 7009 revocation for a public client.

        `client_id` must name a registered client (else UnknownClient -> 401 invalid_client).
        A currently valid token issued to that client revokes the user's whole connection
        of that client (li_revoke_oauth_token -> li_disconnect_oauth_client: every access
        token revoked, every refresh token and pending code deleted). Unknown, expired,
        already-revoked and other clients' tokens change nothing and are not an error
        (RFC 7009 section 2.2), so the answer cannot be used to probe which token strings
        exist. `token_type_hint` is accepted and ignored: one store call searches both
        token kinds (RFC 7009 section 2.1 lets the server extend the search past the hint).
        """
        self._client(client_id)
        if not token:
            return
        self.store.revoke_oauth_token(token_hash(token), client_id)

    def authenticate(self, bearer: str) -> Principal:
        if not bearer:
            raise AuthError("missing bearer token")
        digest = token_hash(bearer)
        row = self.store.get_access_token(digest)
        user_id, _client_id, _resource, scopes, _expiry = validated_access_record(row)
        return Principal(user_id, scopes, digest)

