"""Run database contract unit tests with a fail-closed network boundary.

No database credentials or integration endpoints are required. This runner
does not run Postgres/staging/live-source tests. Audit hooks remain active
through imports, test execution and teardown, and cannot be removed by tests.
"""
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
attempts = []


def deny_network(event, args):
    if event in {"socket.connect", "socket.getaddrinfo", "socket.sendto"}:
        attempts.append(event)  # Do not retain payloads, destinations or keys.
        raise RuntimeError("offline contract tests prohibit network access")


def main():
    sys.addaudithook(deny_network)
    names = [
        "tests.test_offline_store_contracts",
        "tests.test_mcp_routing",
        "tests.test_usage_settlement.StoreSettlementContractTests",
        "tests.test_durable_jobs.SettingsTests",
        "tests.test_account_deletion.DeletionStoreContractTests",
        "tests.test_public_hardening.StorePaginationTests",
        "tests.test_public_hardening.StoreErrorTranslationTests",
        "tests.test_public_ops_hardening.KeyedRateLimitStoreTests",
    ]
    suite = unittest.defaultTestLoader.loadTestsFromNames(names)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    print(f"Network attempts: {len(attempts)}; permitted connections: 0")
    return 0 if result.wasSuccessful() and not attempts else 1


if __name__ == "__main__":
    raise SystemExit(main())
