"""Gated optional GitHub linking. Public MCP does not depend on this service.

Provider must exchange codes using PKCE and fetch /user; it must intersect
/user/installations/{id}/repositories with the live installation selection.
No provider token is returned to callers or persisted by this module.
"""
import base64
import hashlib
import secrets
from dataclasses import dataclass
from urllib.parse import urlencode, urlsplit


class GitHubConnectionError(ValueError):
    pass


@dataclass(frozen=True)
class GitHubIdentity:
    user_id: int


class GitHubOnboarding:
    def __init__(self, *, store, provider, client_id: str, callback_url: str,
                 enabled: bool = False):
        parsed = urlsplit(callback_url)
        if parsed.scheme != 'https' or not parsed.hostname or parsed.query or parsed.fragment or parsed.username:
            raise GitHubConnectionError('Exact HTTPS callback required')
        if not client_id:
            raise GitHubConnectionError('GitHub client configuration required')
        self.store, self.provider = store, provider
        self.client_id, self.callback_url, self.enabled = client_id, callback_url, enabled

    def _subject(self, subject):
        if not self.enabled:
            raise GitHubConnectionError('GitHub connection unavailable')
        if not isinstance(subject, str) or not subject:
            raise GitHubConnectionError('Authenticated optional LI subject required')

    def begin(self, subject: str) -> str:
        self._subject(subject)
        state = secrets.token_urlsafe(32)
        verifier = secrets.token_urlsafe(48)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b'=').decode()
        saved = self.store.rpc('li_github_begin_link', {'p_subject': subject,
            'p_state_hash': hashlib.sha256(state.encode()).hexdigest(),
            'p_verifier': verifier, 'p_callback': self.callback_url})
        if saved is not True:
            raise GitHubConnectionError('Connection state could not be saved')
        return 'https://github.com/login/oauth/authorize?' + urlencode({
            'client_id': self.client_id, 'redirect_uri': self.callback_url,
            'state': state, 'code_challenge': challenge, 'code_challenge_method': 'S256'})

    def callback(self, subject: str, *, state: str, code: str | None,
                 denied: bool = False) -> dict:
        self._subject(subject)
        if not isinstance(state, str) or not 32 <= len(state) <= 128:
            raise GitHubConnectionError('Invalid callback state')
        row = self.store.rpc('li_github_consume_link', {'p_subject': subject,
            'p_state_hash': hashlib.sha256(state.encode()).hexdigest(),
            'p_callback': self.callback_url})
        if not isinstance(row, dict) or not row.get('verifier'):
            raise GitHubConnectionError('Expired or consumed callback')
        if denied:
            return {'connected': False, 'reason': 'authorization_denied'}
        if not isinstance(code, str) or not code or len(code) > 2048:
            raise GitHubConnectionError('Invalid authorization code')
        try:
            identity = self.provider.exchange_identity(code=code,
                verifier=row['verifier'], callback_url=self.callback_url)
        except Exception:
            raise GitHubConnectionError('GitHub authorization failed; restart connection') from None
        if not isinstance(identity, GitHubIdentity) or type(identity.user_id) is not int or identity.user_id <= 0:
            raise GitHubConnectionError('GitHub identity verification failed')
        linked = self.store.rpc('li_github_link_identity', {'p_subject': subject,
            'p_github_user_id': identity.user_id,
            'p_state_hash': hashlib.sha256(state.encode()).hexdigest()})
        if linked is not True:
            raise GitHubConnectionError('Identity already linked; disconnect before changing accounts')
        return {'connected': True, 'repository_access': False, 'ai_client_connected': False}

    def select(self, subject: str, *, installation_id: int, repository_ids: list[int]):
        self._subject(subject)
        if type(installation_id) is not int or installation_id <= 0 or not isinstance(repository_ids, list) or not 1 <= len(repository_ids) <= 100 or any(type(i) is not int or i <= 0 for i in repository_ids) or len(set(repository_ids)) != len(repository_ids):
            raise GitHubConnectionError('Invalid explicit repository selection')
        status = self.status(subject)
        github_id = status.get('github_user_id')
        if type(github_id) is not int:
            raise GitHubConnectionError('GitHub identity required')
        try:
            allowed = self.provider.current_access(github_user_id=github_id, installation_id=installation_id)
        except Exception:
            raise GitHubConnectionError('GitHub access verification unavailable') from None
        if (not isinstance(allowed, (set, frozenset))
            or any(type(i) is not int or i <= 0 for i in allowed)
            or not set(repository_ids) <= allowed):
            raise GitHubConnectionError('Repository selection is not currently authorized')
        saved = self.store.rpc('li_github_select_repositories', {'p_subject': subject,
            'p_github_user_id': github_id, 'p_installation_id': installation_id,
            'p_repository_ids': repository_ids})
        if saved is not True:
            raise GitHubConnectionError('Connection changed; restart selection')
        return True

    def status(self, subject: str):
        self._subject(subject)
        result = self.store.rpc('li_github_connection_status', {'p_subject': subject})
        if not isinstance(result, dict):
            raise GitHubConnectionError('Connection status unavailable')
        return result

    def disconnect(self, subject: str):
        self._subject(subject)
        removed = self.store.rpc('li_github_disconnect', {'p_subject': subject})
        if removed is not True:
            raise GitHubConnectionError('Disconnect failed; retry')
        return {'connected': False, 'local_access_revoked': True,
                'github_installation_removed': False}
