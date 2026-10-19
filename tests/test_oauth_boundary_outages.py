"""Database faults are service failures, not bad consent or client credentials."""
import json
import unittest
from unittest.mock import Mock, patch
from starlette.requests import Request
from lofgren_intelligence.hosted import web_app
from lofgren_intelligence.hosted.store import StoreError
from lofgren_intelligence.hosted.auth import AuthError

class OAuthBoundaryOutages(unittest.IsolatedAsyncioTestCase):
    async def invoke(self, handler, query=b''):
        body=json.dumps({'redirect_uris':['https://client.example/cb'], 'redirect_uri':'https://client.example/cb'}).encode()
        async def receive():
            return {'type':'http.request','body':body,'more_body':False}
        return await handler(Request({'type':'http','method':'POST','path':'/',
            'query_string':query,'headers':[(b'authorization',b'Bearer test-session')]},receive))

    async def test_store_faults_at_construction_and_operation_are_sanitized(self):
        secret='postgres://secret-password@private-host SUPABASE_SERVICE_ROLE_KEY'
        for handler, method in ((web_app.oauth_register,'register_client'),
            (web_app.oauth_complete,'authorize_from_supabase_session'),
            (web_app.oauth_authorize,'get_oauth_client')):
            for construction in (True,False):
                with self.subTest(handler=handler.__name__,construction=construction):
                    service=Mock(); getattr(service,method).side_effect=StoreError(secret)
                    store=Mock(); store.get_oauth_client.side_effect=StoreError(secret)
                    with patch.object(web_app,'SupabaseStore', side_effect=StoreError(secret) if construction else None,
                                      return_value=store), patch.object(web_app,'OAuthService',return_value=service), \
                         patch.object(web_app,'public_base',return_value='https://li.example'):
                        response=await self.invoke(handler,b'response_type=code&code_challenge_method=S256')
                    self.assertEqual(response.status_code,503)
                    self.assertEqual(json.loads(response.body)['error'],'temporarily_unavailable')
                    self.assertNotIn('secret-password',response.body.decode())
                    self.assertNotIn('SUPABASE',response.body.decode())
                    self.assertEqual(response.headers['retry-after'],'30')
                    self.assertEqual(response.headers['cache-control'],'no-store')
                    self.assertEqual(response.headers['pragma'],'no-cache')

    async def test_invalid_client_and_consent_remain_client_errors(self):
        for handler, method, expected in ((web_app.oauth_register,'register_client','invalid_client_metadata'),
            (web_app.oauth_complete,'authorize_from_supabase_session','invalid_request')):
            service=Mock();getattr(service,method).side_effect=AuthError('invalid request')
            with patch.object(web_app,'SupabaseStore'),patch.object(web_app,'OAuthService',return_value=service):
                response=await self.invoke(handler)
            self.assertEqual(response.status_code,400)
            self.assertEqual(json.loads(response.body)['error'],expected)
