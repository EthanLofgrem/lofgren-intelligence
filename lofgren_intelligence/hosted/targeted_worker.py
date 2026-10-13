"""One explicitly selected synthetic acceptance job; no queue housekeeping.

Not a production worker or an activated workflow. Output is fixed status only.
"""
from __future__ import annotations
import argparse
import json
import logging
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import uuid


def validate_target(target: dict) -> None:
    if not re.fullmatch(r'[0-9a-f]{40}', target['application_sha']):
        raise ValueError('invalid application identity')
    for key in ('job_id', 'user_id', 'case_id'):
        uuid.UUID(target[key])
    if not re.fullmatch(r'[0-9a-f]{64}', target['content_hash']):
        raise ValueError('invalid hash')
    inp = target['expected_input']
    args = inp['args']
    texts = args.get('texts')
    if (not {'texts'} <= set(args) <= {'texts', 'max_spend_usd'} or not isinstance(texts, dict) or not 1 <= len(texts) <= 4
            or any(not isinstance(k, str) or not 1 <= len(k) <= 200 or not isinstance(t, str) or not t.strip() for k,t in texts.items())
            or len(json.dumps(texts).encode()) > 16000
            or inp.get('schema') != 'lofgren.research-job/1'
            or inp.get('execution_inputs') != 'approved_charter'
            or inp['case']['case_id'] != target['case_id']
            or inp['case']['content_hash'] != target['content_hash']):
        raise ValueError('not an approved bounded inline job')


def safe_environment(env: dict[str, str]) -> dict[str, str]:
    for key in ('SUPABASE_URL', 'SUPABASE_SERVICE_ROLE_KEY', 'LI_RELEASE_SHA'):
        if not env.get(key):
            raise ValueError('missing settings')
    if not re.fullmatch(r'[0-9a-f]{40}', env['LI_RELEASE_SHA']):
        raise ValueError('invalid release identity')
    out = {key: env[key] for key in ('PATH', 'SYSTEMROOT', 'SUPABASE_URL',
           'SUPABASE_SERVICE_ROLE_KEY', 'LI_RELEASE_SHA') if key in env}
    out.update(LOFGREN_PROVIDER='heuristic', LI_EXTERNAL_EXECUTION_ENABLED='false',
               LI_INFRA_USD_PER_RUN='0', LI_RETRIEVAL_USD_PER_CALL='0',
               LI_WORKER_CONCURRENCY='1', PYTHONUNBUFFERED='1')
    return out


def supervise_once(command: list[str], data: bytes, env: dict[str, str],
                   timeout: float = 90, grace: float = 5) -> int:
    """Hard wall-time limit. Never stream child logs into public CI output."""
    proc = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL, env=env, start_new_session=True)
    def stop_group(sig):
        try:
            os.killpg(proc.pid, sig)
        except ProcessLookupError:
            pass
    try:
        proc.communicate(data, timeout=timeout)
        return 0 if proc.returncode == 0 else 1
    except subprocess.TimeoutExpired:
        stop_group(signal.SIGTERM)
        try:
            proc.communicate(timeout=grace)
        except subprocess.TimeoutExpired:
            stop_group(signal.SIGKILL)
            proc.communicate()
        return 124
    finally:
        # Also stop descendants if their parent exited during termination.
        stop_group(signal.SIGKILL)
        if proc.poll() is None:
            proc.communicate()


def run_target(target: dict, store) -> bool:
    from .worker import Worker
    from .jobs import JobSettings
    validate_target(target)
    # Claim and validation are atomic in SQL, before any state mutation.
    job = store.claim_targeted_research_job(target['job_id'], target['user_id'],
        target['case_id'], target['content_hash'], target['expected_input'], 'bounded-acceptance')
    if job is None or not matches_target(target, job):
        return False
    worker = Worker(store, JobSettings(concurrency=1, max_attempts=1, hold_grace_seconds=30),
                    worker_id='bounded-acceptance')
    signal.signal(signal.SIGTERM, worker.request_stop)
    result = worker.run_job(job)
    if result.get('status') != 'succeeded':
        return False
    reopened = store.get_research_job(target['user_id'], target['job_id'])
    if not reopened or reopened.get('status') != 'succeeded' or not reopened.get('result_run_id'):
        return False
    rid = reopened['result_run_id']
    run = store.get_run(target['user_id'], rid)
    proof = store.get_targeted_usage_proof(target['user_id'], job['reservation_id'])
    reservation, event = proof['reservation'], proof['event']
    charter = store.get_case_charter(target['user_id'], target['case_id'])
    approval = store.get_targeted_case_approval(target['user_id'], target['case_id'],
                                               target['expected_input']['case']['approval_id'])
    return verify_persisted(target, job, reopened, run, proof, charter, approval)


def matches_target(target: dict, job: dict) -> bool:
    return (job.get('id') == target['job_id'] and job.get('user_id') == target['user_id']
            and job.get('case_id') == target['case_id'] and job.get('input') == target['expected_input'])


def verify_persisted(target: dict, claimed: dict, reopened: dict, run: dict,
                     proof: dict, charter: dict, approval: dict) -> bool:
    from .cases import content_hash
    from ..kernel.receipt import verify_receipt
    from ..kernel.knowledge_map import knowledge_map_problems
    from ..adapters.documents import split_passages
    import hashlib
    try:
        if not matches_target(target, claimed) or not matches_target(target, reopened):
            return False
        rid = reopened['result_run_id']
        ref = target['expected_input']['case']
        receipt, snap = run['receipt'], run['snapshot']
        reservation, event = proof['reservation'], proof['event']
        allowed = {hashlib.sha256(p.encode()).hexdigest()
                   for text in target['expected_input']['args']['texts'].values()
                   for p in split_passages(text)}
        hashes = {e['content_hash'] for e in receipt['evidence']}
        return bool(
            run['user_id'] == target['user_id'] and run['run_id'] == rid and run['status'] == 'complete'
            and run['objective'] == target['expected_input']['objective'] == receipt['objective']
            and verify_receipt(receipt) and receipt['research_id'] == rid
            and snap['run_id'] == rid and snap['completed'] is True and snap['receipt'] == receipt
            and not knowledge_map_problems(snap['knowledge_map2'], receipt)
            and hashes and hashes <= allowed
            and charter['case_id'] == target['case_id'] and charter['user_id'] == target['user_id']
            and charter['content_hash'] == target['content_hash'] and charter['version'] == ref['charter_version']
            and content_hash(target['case_id'], charter['charter'], charter.get('answers') or {}) == target['content_hash']
            and charter['charter']['objective'] == target['expected_input']['objective']
            and charter['charter']['sources']['texts'] == target['expected_input']['args']['texts']
            and approval['id'] == ref['approval_id'] and approval['user_id'] == target['user_id']
            and approval['case_id'] == target['case_id'] and approval['content_hash'] == target['content_hash']
            and approval['charter_version'] == ref['charter_version'] and not approval.get('revoked_at')
            and approval['run_id'] == rid and approval['run_status'] == 'complete'
            and reopened['reservation_id'] == claimed['reservation_id']
            and reservation['id'] == event['id'] == claimed['reservation_id']
            and reservation['user_id'] == event['user_id'] == target['user_id']
            and reservation['job_id'] == target['job_id'] and reservation['status'] == 'settled'
            and event['run_id'] == rid and float(event['known_cost_usd']) == 0
            and float(event['units']) == float(reservation['actual_units']) == float(run['usage_units'])
        )
    except (KeyError, TypeError, ValueError, OverflowError):
        return False



def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--target', type=Path)
    parser.add_argument('--child', action='store_true', help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    logging.disable(logging.CRITICAL)
    try:
        env = safe_environment(dict(os.environ))
        raw = sys.stdin.buffer.read(32769) if args.child else args.target.read_bytes()
        if len(raw) > 32768:
            raise ValueError('oversized target')
        target = json.loads(raw)
        validate_target(target)
        if target['application_sha'] != env['LI_RELEASE_SHA']:
            raise ValueError('release mismatch')
        actual = subprocess.check_output(['git', '-C', str(Path(__file__).resolve().parents[2]),
                                          'rev-parse', 'HEAD'], stderr=subprocess.DEVNULL, timeout=5).decode().strip()
        if actual != target['application_sha']:
            raise ValueError('checkout mismatch')
        if args.child:
            from .store import SupabaseStore
            return 0 if run_target(target, SupabaseStore()) else 1
        code = supervise_once([sys.executable, '-m', 'lofgren_intelligence.hosted.targeted_worker', '--child'], raw, env)
    except Exception:
        code = 2
    if not args.child:
        print('BOUNDED_WORKER_PASS' if code == 0 else 'BOUNDED_WORKER_FAIL')
    return code


if __name__ == '__main__':
    raise SystemExit(main())
