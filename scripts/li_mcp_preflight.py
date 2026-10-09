"""Read-only LI deployment preflight; not PublicMCPReady certification."""
import argparse
import json
import re
import urllib.error
import urllib.parse
import urllib.request


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def validate_target(base, sha):
    p = urllib.parse.urlsplit(base)
    if (p.scheme != 'https' or not p.hostname or p.username or p.password
            or p.query or p.fragment or p.path not in ('', '/')):
        raise ValueError('Supply an HTTPS origin without credentials, path, query or fragment')
    if not re.fullmatch('[0-9a-f]{40}', sha):
        raise ValueError('Expected SHA must be 40 lowercase hex characters')
    return base.rstrip('/')


def fetch(url, method='GET', body=None):
    req = urllib.request.Request(url, data=body, method=method,
                                 headers={'Accept': 'application/json, text/event-stream',
                                          'Content-Type': 'application/json'})
    opener = urllib.request.build_opener(NoRedirect())
    try:
        response = opener.open(req, timeout=15)
    except urllib.error.HTTPError as exc:
        response = exc
    with response:
        raw = response.read(1048577)
        if len(raw) > 1048576:
            raise ValueError('Response exceeds 1 MiB')
        try:
            data = json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            data = None
        return response.code, dict(response.headers.items()), data


def check(base, sha, request=fetch):
    base = validate_target(base, sha)
    checks = []
    def record(name, ok):
        checks.append({'check': name, 'passed': bool(ok)})
    status, _, health = request(base + '/healthz')
    record('health', status == 200 and isinstance(health, dict)
           and health.get('service') == 'lofgren-intelligence' and health.get('status') == 'ok')
    status, _, ready = request(base + '/readyz')
    record('exact_sha_readiness', status == 200 and isinstance(ready, dict)
           and ready.get('ready') is True and ready.get('release_sha') == sha)
    status, _, resource = request(base + '/.well-known/oauth-protected-resource')
    record('resource_discovery', status == 200 and isinstance(resource, dict)
           and resource.get('resource') == base + '/mcp'
           and base in resource.get('authorization_servers', [])
           and 'mcp' in resource.get('scopes_supported', [])
           and 'header' in resource.get('bearer_methods_supported', []))
    status, _, auth = request(base + '/.well-known/oauth-authorization-server')
    record('oauth_discovery_pkce', status == 200 and isinstance(auth, dict)
           and auth.get('issuer') == base
           and auth.get('authorization_endpoint') == base + '/oauth/authorize'
           and auth.get('token_endpoint') == base + '/oauth/token'
           and auth.get('registration_endpoint') == base + '/oauth/register'
           and 'S256' in auth.get('code_challenge_methods_supported', [])
           and 'authorization_code' in auth.get('grant_types_supported', [])
           and 'refresh_token' in auth.get('grant_types_supported', []))
    body = json.dumps({'jsonrpc': '2.0', 'id': 1, 'method': 'initialize',
                       'params': {'protocolVersion': '2025-03-26', 'capabilities': {},
                                  'clientInfo': {'name': 'li-readonly-preflight', 'version': '1'}}}).encode()
    status, headers, _ = request(base + '/mcp', 'POST', body)
    headers = {k.lower(): v for k, v in headers.items()}
    record('unauthenticated_mcp_rejected', status == 401)
    challenge = headers.get('www-authenticate', '')
    # Only fetch exact, same-origin LI metadata routes. A substring match can
    # accept a hostile suffix or a URL hidden in another challenge parameter.
    values = re.findall(r'(?:^|[,\s])resource_metadata\s*=\s*"([^"]+)"', challenge, re.I)
    allowed = {base + '/.well-known/oauth-protected-resource',
               base + '/.well-known/oauth-protected-resource/mcp'}
    valid_challenge = (bool(re.match(r'^Bearer\s', challenge, re.I))
                       and len(values) == 1 and values[0] in allowed)
    record('oauth_challenge_discovery', valid_challenge)
    advertised_ok = False
    if valid_challenge:
        metadata_status, _, metadata = request(values[0])
        advertised_ok = (metadata_status == 200 and isinstance(metadata, dict)
                         and metadata.get('resource') == base + '/mcp'
                         and isinstance(metadata.get('authorization_servers'), list)
                         and base in metadata['authorization_servers'])
    record('advertised_resource_metadata', advertised_ok)
    record('mcp_response_not_cached', headers.get('cache-control', '').lower() == 'no-store')
    return {'schema': 'li.mcp-preflight/1', 'expected_sha': sha, 'origin': base,
            'passed': all(c['passed'] for c in checks), 'checks': checks,
            'not_proven': ['Authenticated MCP negotiation and tools', 'Real AI client compatibility',
                           'PKCE exchange, refresh, expiry and revocation', 'Worker operation',
                           'Tenant isolation and restart persistence', 'PublicMCPReady and signed evidence']}


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--base-url', required=True)
    p.add_argument('--expected-sha', required=True)
    a = p.parse_args()
    try:
        result = check(a.base_url, a.expected_sha)
    except (ValueError, OSError) as exc:
        # Do not print arbitrary response data or exception URLs/credentials.
        result = {'passed': False, 'error_type': type(exc).__name__, 'error': 'Preflight could not complete'}
    print(json.dumps(result, indent=2))
    raise SystemExit(0 if result['passed'] else 1)
