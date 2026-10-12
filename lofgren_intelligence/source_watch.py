"""Explicit, bounded public-source refresh; no scheduler or automatic policy updates."""
from __future__ import annotations

import argparse
import hashlib
import json
import re
from datetime import date
from pathlib import Path

from .adapters.manifest import MAX_MANIFEST_BYTES, OperatorSourcesAdapter, parse_manifest
from .adapters.public_api import OUTCOME_COMPLETE, PublicHTTPClient

SCHEMA = 'lofgren.source-watch/1'
SNAPSHOT = 'lofgren.source-watch-snapshot/1'


def bounded_load(path: Path) -> dict:
    with path.open('rb') as stream:
        raw = stream.read(MAX_MANIFEST_BYTES + 1)
    if len(raw) > MAX_MANIFEST_BYTES:
        raise ValueError('watch input exceeds 256000 bytes')
    return json.loads(raw)


def validate_watch(data: dict):
    if not isinstance(data, dict) or set(data) != {'schema', 'sources'} or data['schema'] != SCHEMA:
        raise ValueError('invalid source watch schema or fields')
    sources = data['sources']
    if not isinstance(sources, list):
        raise ValueError('sources must be a list')
    manifest_sources = []
    for source in sources:
        if not isinstance(source, dict) or set(source) - {'url', 'title', 'publisher', 'jurisdiction', 'effective_date'}:
            raise ValueError('invalid source watch entry')
        for key in ('jurisdiction', 'effective_date'):
            value = source.get(key, '')
            if not isinstance(value, str) or len(value) > 200 or re.search(r'[\x00-\x1f\x7f]', value):
                raise ValueError('invalid source metadata')
        if source.get('effective_date'):
            date.fromisoformat(source['effective_date'])
        manifest_sources.append({k: v for k, v in source.items() if k in {'url', 'title', 'publisher'}})
    manifest = parse_manifest(json.dumps({'schema': 'lofgren.sources-manifest/1', 'sources': manifest_sources}).encode())
    return manifest


def validate_previous(previous):
    if previous is None:
        return {}
    if not isinstance(previous, dict) or previous.get('schema') != SNAPSHOT or not isinstance(previous.get('sources'), list):
        raise ValueError('invalid previous snapshot')
    if len(previous['sources']) > 25:
        raise ValueError('previous snapshot exceeds 25 sources')
    rows = {}
    for row in previous['sources']:
        if not isinstance(row, dict) or not isinstance(row.get('url'), str) or row['url'] in rows:
            raise ValueError('invalid or duplicate previous source')
        digest = row.get('last_successful_sha256')
        if digest is not None and (not isinstance(digest, str) or not re.fullmatch('[0-9a-f]{64}', digest)):
            raise ValueError('invalid previous content hash')
        rows[row['url']] = row
    return rows


def refresh(watch, previous=None, *, fetch=False, client=None):
    manifest = validate_watch(watch)
    prior = validate_previous(previous)
    # Construct the client only after explicit network opt-in and full input validation.
    fetched = OperatorSourcesAdapter(manifest, client=client or PublicHTTPClient(
        'source_watch', timeout=5, max_retries=0, min_interval_s=0.5)).fetch_all() if fetch else []
    by_url = {f.entry['url']: f for f in fetched}
    rows = []
    for source in watch['sources']:
        url = source['url'].strip()
        old = prior.get(url, {})
        prior_hash = old.get('last_successful_sha256')
        result = by_url.get(url)
        new_hash = result.sha256 if result and result.outcome == OUTCOME_COMPLETE else None
        status = 'not_fetched' if not fetch else 'unavailable' if new_hash is None else 'first_observation' if prior_hash is None else 'unchanged_bytes' if new_hash == prior_hash else 'changed_bytes'
        metadata = {k: source.get(k, '') for k in ('jurisdiction', 'effective_date')}
        metadata_changed = bool(old) and any(old.get(k, '') != v for k, v in metadata.items())
        rows.append({'url': url, **metadata, 'metadata_assertion': 'operator_supplied_unverified',
                     'previous_successful_sha256': prior_hash,
                     'observed_sha256': new_hash, 'last_successful_sha256': new_hash or prior_hash,
                     'retrieved_at': result.retrieved_at if result else None,
                     'status': status, 'metadata_changed': metadata_changed,
                     'review_required': status != 'unchanged_bytes' or metadata_changed})
    return {'schema': SNAPSHOT, 'watch_sha256': hashlib.sha256(json.dumps(watch, sort_keys=True).encode()).hexdigest(),
            'sources': rows, 'removed_watch_urls': sorted(set(prior) - {r['url'] for r in rows}),
            'limitations': ['Byte changes trigger review, not factual or legal conclusions.',
                            'Jurisdiction and effective dates are unverified operator assertions.',
                            'Unchanged bytes do not establish current law or truth.',
                            'No automatic code, policy, finding or external-action updates.']}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('watchlist', type=Path)
    parser.add_argument('--previous', type=Path)
    parser.add_argument('--fetch', action='store_true', help='Opt in to at most 25 source retrievals (bounded redirects may add GETs); offline by default')
    args = parser.parse_args()
    try:
        report = refresh(bounded_load(args.watchlist), bounded_load(args.previous) if args.previous else None, fetch=args.fetch)
    except (OSError, ValueError, TypeError) as exc:
        parser.exit(2, f'Refresh refused: {exc}\n')
    print(json.dumps(report, indent=2, sort_keys=True))


if __name__ == '__main__':
    main()
