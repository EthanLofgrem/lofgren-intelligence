"""Behavioral tests for the offline change-review helper."""
import copy
import importlib.util
from pathlib import Path
import unittest

spec = importlib.util.spec_from_file_location('change_check', Path(__file__).parents[1] / 'scripts/evidence_change_check.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def receipt(digest='a' * 64):
    return {'schema': 'lofgren.research-receipt/2', 'contract_hash': 'scope1',
            'sources': [{'id': 's1', 'uri': 'https://example.org/rules'}],
            'evidence': [{'id': 'e1', 'source_id': 's1', 'content_hash': digest}],
            'claims': [{'id': 'c1', 'supporting': ['e1'], 'contradicting': []}]}


class ChangeCheckTests(unittest.TestCase):
    def test_changed_evidence_marks_upstream_claim_for_review(self):
        result = module.compare(receipt(), receipt('b' * 64))
        self.assertEqual(result['prior_claim_ids_for_review'], ['c1'])
        self.assertEqual(result['sources'][0]['status'], 'recorded_evidence_changed')

    def test_unchanged_and_reordered_records_do_not_trigger_review(self):
        before = receipt()
        after = copy.deepcopy(before)
        after['sources'][0]['id'] = 'new-source'
        after['evidence'][0]['source_id'] = 'new-source'
        after['evidence'].append(copy.deepcopy(after['evidence'][0]) | {'id': 'e2'})
        self.assertEqual(module.compare(before, after)['prior_claim_ids_for_review'], [])

    def test_absent_source_is_not_reported_deleted(self):
        after = receipt()
        after.update(sources=[], evidence=[], claims=[])
        result = module.compare(receipt(), after)
        self.assertEqual(result['sources'][0]['status'], 'source_absent_in_new_receipt')
        self.assertEqual(result['prior_claim_ids_for_review'], ['c1'])

    def test_empty_evidence_requires_review_and_contract_change_recorded(self):
        after = receipt()
        after.update(evidence=[], claims=[], contract_hash='scope2')
        result = module.compare(receipt(), after)
        self.assertTrue(result['contract_changed'])
        self.assertEqual(result['sources'][0]['status'], 'insufficient_evidence_for_comparison')

    def test_scope_change_reviews_all_prior_claims_even_with_identical_evidence(self):
        before = receipt()
        before['claims'].append({'id': 'unresolved', 'supporting': [], 'contradicting': []})
        after = copy.deepcopy(before)
        after['contract_hash'] = 'scope2'
        result = module.compare(before, after)
        self.assertTrue(result['contract_changed'])
        self.assertEqual(result['sources'][0]['status'], 'recorded_evidence_unchanged')
        self.assertEqual(result['prior_claim_ids_for_review'], ['c1', 'unresolved'])

    def test_contradicting_evidence_change_triggers_review(self):
        before = receipt()
        before['claims'][0].update(supporting=[], contradicting=['e1'])
        after = copy.deepcopy(before)
        after['evidence'][0]['content_hash'] = 'b' * 64
        self.assertEqual(module.compare(before, after)['prior_claim_ids_for_review'], ['c1'])

    def test_dangling_reference_and_duplicate_ids_fail_closed(self):
        for change in ('dangling', 'duplicate'):
            after = receipt()
            if change == 'dangling':
                after['claims'][0]['supporting'] = ['missing']
            else:
                after['evidence'].append(copy.deepcopy(after['evidence'][0]))
            with self.assertRaises(ValueError):
                module.compare(receipt(), after)

    def test_deterministic_and_inputs_not_mutated(self):
        before, after = receipt(), receipt('b' * 64)
        snapshot = copy.deepcopy((before, after))
        self.assertEqual(module.compare(before, after), module.compare(before, after))
        self.assertEqual((before, after), snapshot)
