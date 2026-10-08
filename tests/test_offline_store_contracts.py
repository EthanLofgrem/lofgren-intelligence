import unittest
from unittest.mock import Mock, patch

from lofgren_intelligence.hosted.http import json_request
from lofgren_intelligence.hosted.store import StoreError, SupabaseStore


class OfflineStoreTests(unittest.TestCase):
    def test_constructor_is_inert_even_with_documentation_configuration(self):
        with patch("lofgren_intelligence.hosted.store.json_request") as transport:
            store = SupabaseStore("https://db.example", "synthetic-service", "synthetic-public")
            self.assertEqual(store.url, "https://db.example")
            transport.assert_not_called()

    def test_documentation_hosts_are_refused_before_the_opener_receives_credentials(self):
        for host in ("db.example", "DB.EXAMPLE.", "example.com", "db.example.org", "db.invalid"):
            opener = Mock()
            with self.subTest(host=host), self.assertRaises(ValueError):
                json_request("https://" + host + "/rest/v1/li_runs", headers={"apikey": "synthetic"}, opener=opener)
            opener.assert_not_called()

    def test_unmocked_store_fixture_fails_before_network(self):
        store = SupabaseStore("https://db.example", "synthetic-service", "synthetic-public")
        with self.assertRaises(StoreError):
            store.get_account("synthetic-user")
