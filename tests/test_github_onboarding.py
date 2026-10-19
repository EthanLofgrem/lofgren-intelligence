import unittest
from urllib.parse import urlsplit, parse_qs
from lofgren_intelligence.hosted.github_onboarding import GitHubOnboarding, GitHubIdentity, GitHubConnectionError

class Store:
    def __init__(self): self.rows={}; self.calls=[]; self.link=None
    def rpc(self,name,body):
        self.calls.append((name,body))
        if name.endswith('begin_link'): self.rows[(body['p_subject'],body['p_state_hash'])]={'verifier':body['p_verifier']}; return True
        if name.endswith('consume_link'): return self.rows.pop((body['p_subject'],body['p_state_hash']),None)
        if name.endswith('link_identity'): self.link=body['p_github_user_id']; return True
        if name.endswith('connection_status'): return {'github_user_id':self.link}
        return True
class Provider:
    def exchange_identity(self,**kw): return GitHubIdentity(123)
    def current_access(self,**kw): return {4}
class Tests(unittest.TestCase):
    def setUp(self):
        self.store=Store(); self.service=GitHubOnboarding(store=self.store,provider=Provider(),client_id='test',callback_url='https://example.com/github/callback',enabled=True)
    def begin(self): return parse_qs(urlsplit(self.service.begin('a')).query)['state'][0]
    def test_gate(self):
        self.service.enabled=False
        with self.assertRaises(GitHubConnectionError): self.service.begin('a')
        self.assertEqual(self.store.calls,[])
    def test_pkce_and_no_identity_repo_inference(self):
        url=self.service.begin('a'); q=parse_qs(urlsplit(url).query)
        self.assertEqual(q['code_challenge_method'],['S256']); self.assertNotIn('scope',q)
        result=self.service.callback('a',state=q['state'][0],code='code')
        self.assertFalse(result['repository_access']); self.assertFalse(result['ai_client_connected'])
    def test_wrong_subject_and_replay(self):
        state=self.begin()
        with self.assertRaises(GitHubConnectionError): self.service.callback('b',state=state,code='code')
        self.service.callback('a',state=state,code='code')
        with self.assertRaises(GitHubConnectionError): self.service.callback('a',state=state,code='code')
    def test_denied_consumes_without_linking(self):
        state=self.begin(); self.service.callback('a',state=state,code=None,denied=True)
        self.assertIsNone(self.store.link)
        with self.assertRaises(GitHubConnectionError): self.service.callback('a',state=state,code='code')
    def test_selection_requires_live_access(self):
        self.store.link=123
        with self.assertRaises(GitHubConnectionError): self.service.select('a',installation_id=10,repository_ids=[5])
        self.assertTrue(self.service.select('a',installation_id=10,repository_ids=[4]))
    def test_invalid_repository_ids(self):
        for ids in ([True],[4,4],[],[0]):
            with self.assertRaises(GitHubConnectionError): self.service.select('a',installation_id=10,repository_ids=ids)
    def test_no_link_selection(self):
        with self.assertRaises(GitHubConnectionError): self.service.select('a',installation_id=10,repository_ids=[4])
    def test_disconnect_distinguishes_installation(self):
        self.assertFalse(self.service.disconnect('a')['github_installation_removed'])
    def test_provider_failure_redacted(self):
        class Bad:
            def exchange_identity(self,**kw): raise RuntimeError('secret-token')
        self.service.provider=Bad()
        with self.assertRaisesRegex(GitHubConnectionError,'restart connection') as error: self.service.callback('a',state=self.begin(),code='secret-code')
        self.assertNotIn('secret',str(error.exception))

    def test_malformed_live_access_fails_closed(self):
        self.store.link=123
        for value in ({True}, [4], None, {"4"}):
            class BadAccess:
                def current_access(self,**kw): return value
            self.service.provider=BadAccess()
            with self.assertRaises(GitHubConnectionError): self.service.select('a',installation_id=10,repository_ids=[4])
    def test_selection_provider_error_redacted(self):
        self.store.link=123
        class BadAccess:
            def current_access(self,**kw): raise RuntimeError('secret')
        self.service.provider=BadAccess()
        with self.assertRaisesRegex(GitHubConnectionError,'unavailable') as e: self.service.select('a',installation_id=10,repository_ids=[4])
        self.assertNotIn('secret',str(e.exception))
    def test_relink_refusal(self):
        class Refusing(Store):
            def rpc(self,name,body):
                if name.endswith('link_identity'): return False
                return super().rpc(name,body)
        self.service.store=Refusing()
        state=parse_qs(urlsplit(self.service.begin('a')).query)['state'][0]
        with self.assertRaisesRegex(GitHubConnectionError,'already linked'): self.service.callback('a',state=state,code='code')

    def test_mutation_failure_never_claims_success(self):
        class Refusing(Store):
            def rpc(self,name,body): return False
        self.service.store=Refusing()
        with self.assertRaises(GitHubConnectionError): self.service.begin('a')
        with self.assertRaises(GitHubConnectionError): self.service.disconnect('a')
        with self.assertRaises(GitHubConnectionError): self.service.status('a')
