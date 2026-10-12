#!/usr/bin/env python3
"""Offline structural comparison of two LI receipts; no retrieval or truth verification."""
from __future__ import annotations
import argparse
import hashlib
import json
from pathlib import Path

MAX_BYTES = 8_000_000


def _index(receipt: dict) -> tuple[dict, dict]:
    if not isinstance(receipt, dict) or receipt.get('schema') not in {
        'lofgren.research-receipt/1', 'lofgren.research-receipt/2'
    }:
        raise ValueError('unsupported research receipt schema')
    sources, evidence, claims = (receipt.get(k) for k in ('sources', 'evidence', 'claims'))
    if not all(isinstance(x, list) for x in (sources, evidence, claims)):
        raise ValueError('sources, evidence and claims must be lists')
    ids, by_uri, evidence_uri = {}, {}, {}
    for s in sources:
        if not isinstance(s, dict) or not isinstance(s.get('id'), str) or not isinstance(s.get('uri'), str):
            raise ValueError('invalid source')
        if s['id'] in ids:
            raise ValueError('duplicate source id')
        ids[s['id']] = s['uri']
        by_uri.setdefault(s['uri'], set())
    for e in evidence:
        if not isinstance(e, dict) or e.get('source_id') not in ids:
            raise ValueError('evidence references an unknown source')
        eid, digest = e.get('id'), e.get('content_hash')
        if not isinstance(eid, str) or eid in evidence_uri:
            raise ValueError('invalid or duplicate evidence id')
        if not isinstance(digest, str) or len(digest) != 64 or any(c not in '0123456789abcdef' for c in digest):
            raise ValueError('invalid evidence hash')
        uri = ids[e['source_id']]
        by_uri[uri].add(digest)
        evidence_uri[eid] = uri
    dependent = {uri: set() for uri in by_uri}
    claim_ids = set()
    for c in claims:
        if not isinstance(c, dict) or not isinstance(c.get('id'), str) or c['id'] in claim_ids:
            raise ValueError('invalid or duplicate claim id')
        claim_ids.add(c['id'])
        for field in ('supporting', 'contradicting'):
            refs = c.get(field, [])
            if not isinstance(refs, list) or any(not isinstance(r, str) or r not in evidence_uri for r in refs):
                raise ValueError('invalid claim evidence references')
            for ref in refs:
                dependent[evidence_uri[ref]].add(c['id'])
    return by_uri, dependent


def compare(before: dict, after: dict) -> dict:
    old, dependencies = _index(before)
    new, _ = _index(after)
    rows = []
    contract_changed = before.get('contract_hash') != after.get('contract_hash')
    # Scope changes can invalidate claims even when their recorded evidence is identical.
    affected = {c['id'] for c in before['claims']} if contract_changed else set()
    for uri in sorted(set(old) | set(new)):
        if uri not in old:
            status = 'source_added'
        elif uri not in new:
            status = 'source_absent_in_new_receipt'
        elif not old[uri] or not new[uri]:
            status = 'insufficient_evidence_for_comparison'
        elif old[uri] == new[uri]:
            status = 'recorded_evidence_unchanged'
        else:
            status = 'recorded_evidence_changed'
        review = status != 'recorded_evidence_unchanged'
        claims = sorted(dependencies.get(uri, set())) if review else []
        affected.update(claims)
        rows.append({'uri': uri, 'status': status, 'prior_claim_ids_for_review': claims})
    return {'schema': 'lofgren.evidence-change-report/1', 'sources': rows,
            'prior_claim_ids_for_review': sorted(affected),
            'contract_changed': contract_changed,
            'limitations': ['Structural comparison only; receipt integrity and issuer identity are not verified.',
                            'Evidence changes can result from extraction or scope changes, not source revisions.',
                            'Unchanged evidence does not establish freshness, truth or unchanged law.',
                            'Absence in a receipt does not establish source removal or unavailability.']}


def load(path: Path) -> tuple[dict, str]:
    with path.open('rb') as handle:
        raw = handle.read(MAX_BYTES + 1)
    if len(raw) > MAX_BYTES:
        raise ValueError('receipt exceeds 8 MB limit')
    return json.loads(raw), hashlib.sha256(raw).hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('before', type=Path)
    parser.add_argument('after', type=Path)
    args = parser.parse_args()
    try:
        before, old_hash = load(args.before)
        after, new_hash = load(args.after)
        result = compare(before, after)
    except (OSError, ValueError, TypeError) as exc:
        parser.exit(2, f'Comparison refused: {exc}\n')
    result['input_file_sha256'] = {'before': old_hash, 'after': new_hash}
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == '__main__':
    main()
