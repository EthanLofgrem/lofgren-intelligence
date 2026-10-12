import copy
import unittest
from unittest.mock import patch
from lofgren_intelligence.source_watch import refresh
from lofgren_intelligence.adapters.public_api import ProviderUnavailable

WATCH = {'schema': 'lofgren.source-watch/1', 'sources': [{'url': 'https://example.org/rules', 'jurisdiction': 'Arizona', 'effective_date': '2026-10-12'}]}

class Client:
    def __init__(self, body=b'old', unavailable=False):
        self.body, self.unavailable, self.calls = body, unavailable, 0
    def get(self, url, **kwargs):
        self.calls += 1
        if self.unavailable:
            raise ProviderUnavailable('test failure')
        return self.body, 'text/plain'

class WatchTests(unittest.TestCase):
    def test_offline_default_never_constructs_client(self):
        with patch('lofgren_intelligence.source_watch.PublicHTTPClient', side_effect=AssertionError('network client')):
            self.assertEqual(refresh(WATCH)['sources'][0]['status'], 'not_fetched')

    def test_first_unchanged_changed_and_preserved_hash(self):
        client = Client()
        first = refresh(WATCH, fetch=True, client=client)
        self.assertEqual(first['sources'][0]['status'], 'first_observation')
        unchanged = refresh(WATCH, first, fetch=True, client=client)
        self.assertFalse(unchanged['sources'][0]['review_required'])
        changed = refresh(WATCH, unchanged, fetch=True, client=Client(b'new'))
        self.assertEqual(changed['sources'][0]['status'], 'changed_bytes')
        self.assertEqual(changed['sources'][0]['previous_successful_sha256'], first['sources'][0]['observed_sha256'])
        self.assertEqual(client.calls, 2)

    def test_failure_preserves_success_without_claiming_unchanged(self):
        first = refresh(WATCH, fetch=True, client=Client())
        failure = refresh(WATCH, first, fetch=True, client=Client(unavailable=True))
        row = failure['sources'][0]
        self.assertEqual(row['status'], 'unavailable')
        self.assertTrue(row['review_required'])
        self.assertEqual(row['last_successful_sha256'], first['sources'][0]['observed_sha256'])

    def test_metadata_change_requires_review_with_same_bytes(self):
        first = refresh(WATCH, fetch=True, client=Client())
        watch = copy.deepcopy(WATCH)
        watch['sources'][0]['jurisdiction'] = 'California'
        row = refresh(watch, first, fetch=True, client=Client())['sources'][0]
        self.assertTrue(row['metadata_changed'])
        self.assertTrue(row['review_required'])

    def test_unsafe_or_oversized_watch_refused_before_fetch(self):
        for url in ('http://127.0.0.1/', 'http://user:password@example.org'):
            watch = copy.deepcopy(WATCH)
            watch['sources'][0]['url'] = url
            client = Client()
            with self.assertRaises(ValueError):
                refresh(watch, fetch=True, client=client)
            self.assertEqual(client.calls, 0)
        with self.assertRaises(ValueError):
            refresh({'schema': WATCH['schema'], 'sources': WATCH['sources'] * 26})

    def test_invalid_previous_hash_and_metadata_refused(self):
        previous = refresh(WATCH)
        previous['sources'][0]['last_successful_sha256'] = 'forged'
        with self.assertRaises(ValueError):
            refresh(WATCH, previous)
        watch = copy.deepcopy(WATCH)
        watch['sources'][0]['effective_date'] = 'not-a-date'
        with self.assertRaises(ValueError):
            refresh(watch)
