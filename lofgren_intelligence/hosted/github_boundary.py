"""Optional GitHub resource boundary; public MCP never depends on GitHub identity.

This is an authorization primitive, not an OAuth callback or token store. All
records must be loaded server-side for the authenticated LI subject. A client
supplied repository name, installation ID, or username never establishes access.
"""
from dataclasses import dataclass
from datetime import datetime, timezone


class GitHubAccessDenied(ValueError):
    pass


@dataclass(frozen=True)
class GitHubGrant:
    li_user_id: str
    github_user_id: int
    installation_id: int
    repository_ids: frozenset[int]
    permissions: frozenset[str]
    expires_at: datetime
    revoked: bool = False


def authorize_repository(*, authenticated_user_id: str, repository_id: int,
                         permission: str, grant: GitHubGrant,
                         now: datetime | None = None) -> tuple[int, int]:
    """Return installation/repository IDs only after every boundary passes.

    Caller must ALSO query GitHub's current installation/repository access before
    each protected operation; a saved grant cannot override remote revocation.
    Tokens are minted for this repository and permission only, never shared.
    """
    checked_at = now or datetime.now(timezone.utc)
    if (not isinstance(grant, GitHubGrant)
        or not isinstance(authenticated_user_id, str) or not authenticated_user_id
        or authenticated_user_id != grant.li_user_id
        or type(repository_id) is not int or repository_id <= 0
        or type(grant.github_user_id) is not int or grant.github_user_id <= 0
        or type(grant.installation_id) is not int or grant.installation_id <= 0
        or type(grant.revoked) is not bool or grant.revoked
        or not isinstance(grant.expires_at, datetime)
        or not isinstance(checked_at, datetime)
        or not isinstance(grant.repository_ids, frozenset)
        or not isinstance(grant.permissions, frozenset)
        or grant.expires_at.tzinfo is None
        or checked_at.tzinfo is None or grant.expires_at <= checked_at
        or repository_id not in grant.repository_ids
        or not isinstance(permission, str)
        or permission not in {"metadata:read", "contents:read"}
        or permission not in grant.permissions):
        raise GitHubAccessDenied("GitHub repository access is not authorized")
    return grant.installation_id, repository_id


def connection_status() -> dict[str, object]:
    """Truthful public status until the end-to-end separate-user gate is passed."""
    return {"available": False, "state": "in_development",
            "public_mcp_requires_github": False,
            "github_identity_grants_repository_access": False,
            "github_connection_connects_ai_client": False,
            "reason": "GitHub App registration, persistent linking, and separate-user acceptance are pending"}
