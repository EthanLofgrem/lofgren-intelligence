"""The real-PostgreSQL proof's own contract (owner review, Q26).

tests/pg/test_migrations_pg.py runs only in the postgres-proof CI job. These static checks run
everywhere and keep that proof honest:

1. every privilege denial is checked on a restricted API-role session (anon, authenticated or
   service_role through authenticator), never on the superuser admin connection;
2. concurrent workers use independent connections: nothing passed to run_concurrently shares the
   test's own store, service-role or admin connection;
3. every test reads the final table state on its own (admin) connection, not just RPC returns;
4. the job refuses skips, and uploads the test log and the server log as artifacts, always.
"""

from __future__ import annotations

import ast
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PG_TESTS = ROOT / "tests" / "pg" / "test_migrations_pg.py"
WORKFLOW = ROOT / ".github" / "workflows" / "postgres-proof.yml"

# Reads of table state on the admin connection (directly or through the PgCase helpers built on it).
STATE_READS = ("self.one(", "self.admin.execute(", "admin.execute(", "conn.execute(\"select count",
               "self.job(", "self.reservation(", "self.usage_events(", "self.user_rows(", "li_acl_holes(",
               "self.entitlement(", "self.li_functions(", "has_table_privilege", "has_sequence_privilege",
               "with admin_connect(")
# Tests that prove a session property rather than table state.
NO_TABLE_STATE = {"test_auth_uid_stub_reads_the_session_claim"}


def _tests():
    tree = ast.parse(PG_TESTS.read_text(encoding="utf-8"))
    for cls in (n for n in tree.body if isinstance(n, ast.ClassDef)):
        for fn in (n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name.startswith("test_")):
            yield cls.name, fn


def _restricted_names(fn: ast.FunctionDef) -> set[str]:
    """Local names bound to role_connect(...) sessions, or to stores built on them."""
    names = set()
    for node in ast.walk(fn):
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Call):
            func = node.value.func
            called = func.id if isinstance(func, ast.Name) else getattr(func, "attr", "")
            if called in ("role_connect", "new_store", "PgRpcStore"):
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        names.add(target.id)
    return names


class PostgresProofContractTests(unittest.TestCase):
    def test_every_privilege_denial_runs_on_a_restricted_role_session(self):
        checked = 0
        for cls, fn in _tests():
            restricted = _restricted_names(fn)
            for node in ast.walk(fn):
                if not isinstance(node, ast.With):
                    continue
                for item in node.items:
                    call = item.context_expr
                    if not (isinstance(call, ast.Call) and getattr(call.func, "attr", "") == "assertRaises"
                            and call.args and ast.unparse(call.args[0]) == "pg_errors.InsufficientPrivilege"):
                        continue
                    for inner in ast.walk(node):
                        if isinstance(inner, ast.Call) and getattr(inner.func, "attr", "") == "execute":
                            receiver = ast.unparse(inner.func.value)
                            with self.subTest(test=f"{cls}.{fn.name}", receiver=receiver):
                                self.assertTrue(receiver in restricted or receiver == "self.svc",
                                                f"denial checked on {receiver}, not a restricted session")
                            checked += 1
        self.assertGreaterEqual(checked, 12)

    def test_concurrent_workers_never_share_the_tests_own_connections(self):
        calls = 0
        for cls, fn in _tests():
            for node in ast.walk(fn):
                if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "run_concurrently":
                    calls += 1
                    text = ast.unparse(node)
                    with self.subTest(test=f"{cls}.{fn.name}"):
                        for shared in ("self.store", "self.svc", "self.admin"):
                            self.assertNotIn(shared, text)
        self.assertGreaterEqual(calls, 8)

    def test_every_test_reads_the_final_table_state(self):
        for cls, fn in _tests():
            if fn.name in NO_TABLE_STATE:
                continue
            body = ast.unparse(fn)
            with self.subTest(test=f"{cls}.{fn.name}"):
                self.assertTrue(any(read in body for read in STATE_READS), "no final-state read")

    def test_the_job_refuses_skips_and_always_uploads_both_logs(self):
        text = WORKFLOW.read_text(encoding="utf-8")
        self.assertRegex(text, r'push:\n    branches:\n      - "build/\*\*"\n      - "release/\*\*"')
        self.assertIn('LI_PG_REQUIRED: "1"', text)
        self.assertIn("python -m unittest -v tests.pg.test_migrations_pg 2>&1 | tee pg-proof.log", text)
        self.assertIn('grep -nE "skipped|expected failure|unexpected success" pg-proof.log', text)
        capture = text[text.index("- name: Capture the PostgreSQL server log"):]
        self.assertRegex(capture, r"^- name: Capture the PostgreSQL server log\n        if: always\(\)\n"
                                  r'        run: docker logs "\$\{\{ job\.services\.postgres\.id \}\}" > pg-server\.log')
        upload = text[text.index("- name: Upload the test and server logs"):]
        self.assertIn("if: always()", upload.split("\n")[1])
        self.assertIn("uses: actions/upload-artifact@v4", upload)
        self.assertRegex(upload, r"path: \|\n\s+pg-proof\.log\n\s+pg-server\.log")
        self.assertIn("if-no-files-found: error", upload)
        # The pg module itself fails, never skips, when CI requires it.
        source = PG_TESTS.read_text(encoding="utf-8")
        self.assertIn('raise RuntimeError("LI_PG_REQUIRED=1 but LI_PG_DSN is not set")', source)
        classes = re.findall(r"^class (\w+)\((?:PgCase|unittest\.TestCase)\):$", source, re.M)
        self.assertEqual(len(re.findall(r"^@pg_test\nclass ", source, re.M)),
                         len([c for c in classes if c != "PgCase"]))  # PgCase is the base: no tests


if __name__ == "__main__":
    unittest.main()
