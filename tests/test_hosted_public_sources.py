"""Hosted source inputs stay bounded, durable and part of the approved scope."""
import json
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

from lofgren_intelligence.hosted.cases import normalize_sources, content_hash
from lofgren_intelligence.hosted.security import validate_remote_args
from lofgren_intelligence.hosted.service import AsyncRequired, PublicService
from .test_durable_jobs import JobTestBase
from .test_public_sources import fixture


class HostedSourcePolicyTests(TestCase):
    def test_queries_and_record_caps(self):
        self.assertEqual(validate_remote_args({'europepmc': ' epilepsy ', 'max_records': 2})['europepmc'], 'epilepsy')
        for args in ({'trials': ''}, {'max_records': 21}, {'max_records': True}, {'sources_manifest': '/etc/passwd'}):
            with self.subTest(args=args), self.assertRaises(ValueError):
                validate_remote_args(args)

    def test_inline_manifest_and_source_scope(self):
        manifest = json.loads(fixture('sources_manifest.json'))
        args = validate_remote_args({'sources_manifest': manifest, 'trials': 'epilepsy', 'max_records': 2})
        self.assertEqual(normalize_sources(args), args)
        charter = {"sources": normalize_sources(args)}
        changed = {"sources": normalize_sources(args | {"trials": "different scope"})}
        self.assertNotEqual(content_hash("case", charter, {}), content_hash("case", changed, {}))
        registry = PublicService._registry(args)
        self.assertEqual(registry.get('clinicaltrials').max_pages, 2)
        self.assertEqual(registry.get('clinicaltrials').max_records, 2)
        self.assertEqual(registry.get('clinicaltrials').client.max_retries, 1)
        self.assertIsNotNone(registry.get('operator_sources'))

    def test_public_sources_cannot_run_in_request(self):
        for adapter in ('europepmc', 'clinicaltrials', 'operator_sources'):
            plan = SimpleNamespace(tasks=[SimpleNamespace(adapter_id=adapter)], estimated_work_units=1)
            with self.subTest(adapter=adapter), self.assertRaises(AsyncRequired):
                PublicService._require_sync_bounded(plan)


class HostedSourceWorkerTests(JobTestBase):
    def test_literature_input_survives_queue_and_worker_reads_fixture(self):
        with patch('lofgren_intelligence.adapters.public_api.PublicHTTPClient.get_json', return_value=json.loads(fixture('europepmc_page1.json'))) as fetch:
            out = self.start('literature', europepmc='zelvapril hypertension', max_records=2)
            fetch.assert_not_called()
            frozen = self.job(out['job_id'])['input']['args']
            self.assertEqual(frozen['europepmc'], 'zelvapril hypertension')
            self.assertEqual(frozen['max_records'], 2)
            results = self.drain()
        self.assertEqual([r['status'] for r in results], ['succeeded'])
        self.assertTrue(fetch.called)
        self.assertEqual(len(self.store.runs), 1)
        saved = next(iter(self.store.runs.values()))["snapshot"]
        self.assertTrue(saved["receipt_intact"])
        self.assertIn("europepmc", json.dumps(saved).lower())

    def test_trial_input_survives_queue_with_registry_label(self):
        with patch('lofgren_intelligence.adapters.public_api.PublicHTTPClient.get_json', return_value=json.loads(fixture('clinicaltrials_page1.json'))) as fetch:
            out = self.start('trials', trials='zelvapril hypertension', max_records=2)
            fetch.assert_not_called()
            self.assertEqual(self.job(out['job_id'])['input']['args']['trials'], 'zelvapril hypertension')
            results = self.drain()
        self.assertEqual([r['status'] for r in results], ['succeeded'])
        self.assertTrue(fetch.called)
        saved = next(iter(self.store.runs.values()))['snapshot']
        self.assertTrue(saved['receipt_intact'])
        self.assertIn('clinicaltrials', json.dumps(saved).lower())
