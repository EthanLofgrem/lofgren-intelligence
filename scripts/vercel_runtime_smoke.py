"""Check the hosted entrypoint in Vercel's uv runtime, without keys or network."""
import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ['LI_PUBLIC_BASE_URL'] = 'https://li.example'
for name in ('SUPABASE_URL', 'SUPABASE_SERVICE_ROLE_KEY', 'SUPABASE_PUBLISHABLE_KEY'):
    os.environ.pop(name, None)

from httpx2 import ASGITransport, AsyncClient
from api.index import app


async def main():
    async with AsyncClient(transport=ASGITransport(app=app), base_url='https://li.example') as client:
        health = await client.get('/healthz')
        assert health.status_code == 200, health.text
        assert health.json()['service'] == 'lofgren-intelligence'
        ready = await client.get('/readyz')
        assert ready.status_code == 503, ready.text
        assert not ready.json()['ready']
    print('Vercel uv runtime smoke PASS: hosted imports, health, missing-key refusal')


if __name__ == '__main__':
    asyncio.run(main())
