import os
import unittest
from unittest.mock import patch
from lofgren_intelligence.hosted.security import PublicInputError, validate_remote_args
from lofgren_intelligence.hosted.service import PublicService, QuotaExceeded
from tests.test_public_hosted import FakeStore, OBJECTIVE
from tests.helpers import TEXTS

class CostCeilingTests(unittest.TestCase):
    def test_invalid_client_caps_fail_closed(self):
        for cap in (float('nan'), float('inf'), float('-inf'), 'NaN', 'Infinity', object()):
            with self.subTest(cap=repr(cap)), self.assertRaises(PublicInputError):
                validate_remote_args({'objective': 'x', 'max_spend_usd': cap})
        self.assertEqual(validate_remote_args({'objective':'x','max_spend_usd':'4.25'})['max_spend_usd'],4.25)

    def test_invalid_operator_cap_releases_before_work(self):
        store=FakeStore()
        for cap in ('NaN','Infinity','-1','not-a-number'):
            with self.subTest(cap=cap), patch.dict(os.environ, {'LI_PUBLIC_MAX_ESTIMATED_USD_PER_RUN':cap,
                'LOFGREN_PROVIDER':'heuristic','LI_INFRA_USD_PER_RUN':'0','LI_RETRIEVAL_USD_PER_CALL':'0'}), self.assertRaises(QuotaExceeded):
                PublicService(store).investigate('u1', {'objective':OBJECTIVE,'texts':TEXTS})
        self.assertEqual(store.runs,{})
        self.assertEqual(store.usage,[])
        self.assertTrue(store.reservations)
        self.assertEqual({r['status'] for r in store.reservations.values()},{'released'})
