"""Exercise the installed release artifact outside the source checkout.

This smoke test is intentionally provider-free.  It builds the hosted ASGI/MCP
application with synthetic configuration, serves public/static routes in
process, and proves that unauthenticated MCP access remains closed.  It never
contacts Supabase, Stripe, a model provider, or a search provider.
"""

from __future__ import annotations

import argparse
import json
import os
from importlib import resources
from pathlib import Path
from unittest.mock import patch


PUBLIC_ROUTES = (
    "/",
    "/product",
    "/pricing",
    "/mcp-clients",
    "/docs",
    "/about",
    "/app",
    "/app/research/new",
)

STATIC_ASSETS = {
    "site.css": "text/css",
    "site.js": "text/javascript",
    "mark.svg": "image/svg+xml",
}


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise SystemExit(message)


def _outside_checkout(package_file: Path, checkout: Path) -> None:
    package_file = package_file.resolve()
    checkout = checkout.resolve()
    try:
        package_file.relative_to(checkout)
    except ValueError:
        return
    raise SystemExit(f"package imported from source checkout: {package_file}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--expected-sha", required=True)
    args = parser.parse_args()

    checkout = Path(__file__).resolve().parents[1]
    _require(Path.cwd().resolve() != checkout, "smoke test must run outside the source checkout")

    import lofgren_intelligence

    package_file = Path(lofgren_intelligence.__file__ or "")
    _outside_checkout(package_file, checkout)

    safe_environment = {
        "LI_PUBLIC_BASE_URL": "https://release-smoke.invalid",
        "LI_RELEASE_SHA": args.expected_sha,
        "LI_RATE_LIMIT_BACKEND": "memory",
        "LI_BILLING_ENABLED": "false",
        "SUPABASE_URL": "https://release-smoke.supabase.invalid",
        "SUPABASE_SERVICE_ROLE_KEY": "synthetic-release-smoke-service-role",
        "SUPABASE_PUBLISHABLE_KEY": "synthetic-release-smoke-publishable",
    }

    with patch.dict(os.environ, safe_environment, clear=True):
        from starlette.testclient import TestClient

        from lofgren_intelligence.hosted.web_app import build_app

        app = build_app()
        with TestClient(app, base_url=safe_environment["LI_PUBLIC_BASE_URL"]) as client:
            health = client.get("/healthz")
            _require(health.status_code == 200, f"healthz returned {health.status_code}")
            _require(
                health.json()
                == {
                    "service": "lofgren-intelligence",
                    "version": lofgren_intelligence.__version__,
                    "status": "ok",
                },
                "healthz identity/version mismatch",
            )
            for header in ("x-content-type-options", "referrer-policy", "x-frame-options", "permissions-policy"):
                _require(header in health.headers, f"healthz missing security header: {header}")

            for route in PUBLIC_ROUTES:
                response = client.get(route)
                _require(response.status_code == 200, f"{route} returned {response.status_code}")
                _require(response.headers.get("content-type", "").startswith("text/html"),
                         f"{route} is not HTML")

            metadata = client.get("/.well-known/oauth-authorization-server")
            _require(metadata.status_code == 200, "OAuth metadata is not served")
            _require(metadata.json().get("issuer") == safe_environment["LI_PUBLIC_BASE_URL"] + "/",
                     "OAuth issuer does not match the configured public base")

            static_root = resources.files("lofgren_intelligence.hosted").joinpath("static")
            asset_sizes: dict[str, int] = {}
            for name, media_type in STATIC_ASSETS.items():
                packaged = static_root.joinpath(name).read_bytes()
                _require(bool(packaged), f"installed static asset is empty: {name}")
                response = client.get(f"/static/{name}")
                _require(response.status_code == 200, f"static asset returned {response.status_code}: {name}")
                _require(response.content == packaged, f"served static asset differs from installed wheel: {name}")
                _require(response.headers.get("content-type", "").startswith(media_type),
                         f"static asset has the wrong content type: {name}")
                asset_sizes[name] = len(packaged)

            unauthenticated = client.post(
                "/mcp",
                json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                headers={"Accept": "application/json, text/event-stream"},
            )
            _require(unauthenticated.status_code == 401,
                     f"unauthenticated MCP request returned {unauthenticated.status_code}, expected 401")

    print(json.dumps({
        "artifact": "installed-wheel",
        "package_file": str(package_file.resolve()),
        "release_sha": args.expected_sha,
        "version": lofgren_intelligence.__version__,
        "public_routes": len(PUBLIC_ROUTES),
        "static_assets": asset_sizes,
        "mcp_unauthenticated_status": 401,
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
