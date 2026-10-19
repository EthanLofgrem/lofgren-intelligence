import json
import logging
import os
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
from lofgren_intelligence.hosted.targeted_worker import safe_environment, supervise_once, validate_target, run_target, main

class TargetedWorkerTests(unittest.TestCase):
    def setUp(self):
        # main() silences all logging for its own CLI process (logging.disable); restore it so
        # later tests in this process still see their log records.
        self.addCleanup(logging.disable, logging.NOTSET)
    def target(self):
        return {'application_sha':'b'*40,'job_id':'00000000-0000-4000-8000-000000000001','user_id':'00000000-0000-4000-8000-000000000002',
          'case_id':'00000000-0000-4000-8000-000000000003','content_hash':'a'*64,
          'expected_input':{'schema':'lofgren.research-job/1','execution_inputs':'approved_charter',
          'case':{'case_id':'00000000-0000-4000-8000-000000000003','content_hash':'a'*64},'args':{'texts':{'synthetic':'synthetic evidence'}}}}
    def test_only_bounded_inline_target_is_accepted(self):
        validate_target(self.target())
        for key in ('urls','search','sources_manifest','europepmc','trials'):
            t=self.target();t['expected_input']['args'][key]='forbidden'
            with self.subTest(key=key), self.assertRaises(ValueError): validate_target(t)
        t=self.target();t['expected_input']['case']['case_id']=t['user_id']
        with self.assertRaises(ValueError): validate_target(t)
    def test_environment_removes_paid_keys_and_forces_containment(self):
        env=safe_environment({'SUPABASE_URL':'https://db.example','SUPABASE_SERVICE_ROLE_KEY':'private',
          'LI_RELEASE_SHA':'b'*40,'ANTHROPIC_API_KEY':'paid','LOFGREN_PROVIDER':'anthropic','LI_EXTERNAL_EXECUTION_ENABLED':'true'})
        self.assertNotIn('ANTHROPIC_API_KEY',env)
        self.assertEqual(env['LOFGREN_PROVIDER'],'heuristic')
        self.assertEqual(env['LI_EXTERNAL_EXECUTION_ENABLED'],'false')
    def test_missing_settings_fail_not_skip(self):
        with patch.dict(os.environ,{},clear=True), patch('builtins.print') as output:
            self.assertNotEqual(main([]),0)
            output.assert_called_once_with('BOUNDED_WORKER_FAIL')
    def test_hung_child_is_killed_and_reaped(self):
        started=time.monotonic()
        result=supervise_once([sys.executable,'-c','import signal,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); time.sleep(60)'],b'',dict(os.environ),.1,.1)
        self.assertEqual(result,124)
        self.assertLess(time.monotonic()-started,3)
    def test_nonzero_child_is_not_success_and_output_is_hidden(self):
        self.assertEqual(supervise_once([sys.executable,'-c','import sys; print("PRIVATE"); sys.exit(3)'],b'',dict(os.environ),1,.1),1)
    def test_no_claim_never_runs_or_housekeeps(self):
        class Store:
            def claim_targeted_research_job(self,*args):return None
            def __getattr__(self,key):raise AssertionError('unexpected '+key)
        self.assertFalse(run_target(self.target(),Store()))
    def test_parent_launches_real_module_with_verified_identity(self):
        target=self.target()
        env={'SUPABASE_URL':'https://db.example','SUPABASE_SERVICE_ROLE_KEY':'private','LI_RELEASE_SHA':'b'*40}
        with tempfile.TemporaryDirectory() as directory:
            from pathlib import Path
            path=Path(directory)/'private.json';path.write_text(json.dumps(target))
            with patch.dict(os.environ,env,clear=True), patch('subprocess.check_output',return_value=('b'*40+'\n').encode()), \
                 patch('lofgren_intelligence.hosted.targeted_worker.supervise_once',return_value=0) as supervise,patch('builtins.print'):
                self.assertEqual(main(['--target',str(path)]),0)
                self.assertEqual(supervise.call_args.args[0][2],'lofgren_intelligence.hosted.targeted_worker')

    def proof_fixture(self):
        from lofgren_intelligence import investigate
        from lofgren_intelligence.hosted.snapshots import durable_snapshot
        from tests.helpers import TEXTS
        target=self.target()
        objective='Is industrial construction in the Phoenix metro increasing?'
        target['expected_input'].update(objective=objective,args={'texts':dict(TEXTS)})
        target['expected_input']['args']['max_spend_usd']=5
        target['expected_input']['case'].update(approval_id='00000000-0000-4000-8000-000000000004',charter_version=1)
        with patch.dict(os.environ,{'LOFGREN_PROVIDER':'heuristic'}):
            snap=durable_snapshot(investigate(objective,texts=TEXTS,approved=True))
        rid=snap['run_id']; user=target['user_id']; case=target['case_id']
        from lofgren_intelligence.hosted.cases import content_hash
        charter_body={'objective':objective,'sources':{'texts':dict(TEXTS)},'budget':{'max_spend_usd':5}}
        h=content_hash(case,charter_body,{})
        target['content_hash']=h;target['expected_input']['case']['content_hash']=h
        job={'id':target['job_id'],'user_id':user,'case_id':case,'input':target['expected_input'],
             'reservation_id':'reservation','result_run_id':rid,'status':'succeeded'}
        run={'run_id':rid,'user_id':user,'objective':objective,'status':'complete','receipt':snap['receipt'],
             'snapshot':snap,'usage_units':snap['usage_units']}
        proof={'reservation':{'id':'reservation','user_id':user,'job_id':target['job_id'],'status':'settled','actual_units':snap['usage_units']},
               'event':{'id':'reservation','user_id':user,'run_id':rid,'units':snap['usage_units'],'known_cost_usd':0}}
        charter={'case_id':case,'user_id':user,'content_hash':h,'version':1,'charter':charter_body,'answers':{}}
        approval={'id':target['expected_input']['case']['approval_id'],'user_id':user,'case_id':case,
                  'content_hash':h,'charter_version':1,'run_id':rid,'run_status':'complete'}
        return (target,job,dict(job),run,proof,charter,approval)

    def test_independently_generated_native_receipt_and_persisted_binding(self):
        from lofgren_intelligence.hosted.targeted_worker import verify_persisted
        self.assertTrue(verify_persisted(*self.proof_fixture()))

    def test_corrupt_receipt_wrong_case_input_and_accounting_are_refused(self):
        from lofgren_intelligence.hosted.targeted_worker import verify_persisted
        import copy
        original=self.proof_fixture()
        mutations=(
            lambda v:v[3]['receipt'].__setitem__('objective','corrupt'),
            lambda v:v[3].__setitem__('status','stopped'),
            lambda v:v[4]['event'].__setitem__('known_cost_usd',1),
            lambda v:v[4]['event'].__setitem__('units',999),
            lambda v:v[1].__setitem__('case_id',v[0]['user_id']),
            lambda v:v[1]['input']['args']['texts'].__setitem__('injected','different evidence'),
            lambda v:v[2].__setitem__('reservation_id','other'),
            lambda v:v[4]['reservation'].__setitem__('status','unsettled'),
            lambda v:v[4]['event'].__setitem__('run_id','different'),
            lambda v:v[5].__setitem__('content_hash','f'*64),
            lambda v:v[6].__setitem__('run_status','started'),
            lambda v:v[3]['snapshot']['knowledge_map2'].__setitem__('objective','tampered'),
        )
        for i,mutate in enumerate(mutations):
            values=[copy.deepcopy(v) for v in original]
            mutate(values)
            with self.subTest(mutation=i): self.assertFalse(verify_persisted(*values))

    def test_wrong_claim_return_never_executes(self):
        target=self.target()
        class Store:
            def claim_targeted_research_job(self,*a):return {'id':'wrong'}
            def __getattr__(self,key):raise AssertionError('unexpected '+key)
        self.assertFalse(run_target(target,Store()))
