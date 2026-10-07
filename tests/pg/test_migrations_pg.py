"""Real-PostgreSQL proof of supabase/migrations and their SQL functions.

These tests run only against a real PostgreSQL server named by LI_PG_DSN (a
superuser DSN; CI uses a GitHub Actions `services: postgres` container). Without
LI_PG_DSN every test here is SKIPPED, which is not a pass: nothing was proven.
With LI_PG_REQUIRED=1 (set in CI) a missing DSN or psycopg is an error, never a
skip.

Every test database is created clean, then scripts/pg/bootstrap_supabase_stub.sql
(a documented Supabase stub: roles, auth.users, auth.uid(), Supabase's default
privileges) and then every migration in filename order, each in one transaction
as the non-superuser owner li_migrator, recorded in a migrations ledger.

RPCs are driven through the real SupabaseStore methods (hosted/store.py): only
SupabaseStore.rpc is replaced, by a PostgREST-like direct call that resolves the
function by name and by the exact named arguments the store sends, and runs it as
service_role through the authenticator role (SET ROLE), one transaction per call.
A store argument name that does not match the SQL signature fails the test.

What this does not prove: Supabase's hosted auth (GoTrue/JWTs), the RLS platform
behind PostgREST, PostgREST's JWT-to-role switch and HTTP/JSON mapping.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
import unittest
import uuid
from datetime import timedelta
from pathlib import Path

DSN = os.environ.get("LI_PG_DSN", "").strip()
REQUIRED = os.environ.get("LI_PG_REQUIRED", "").strip() == "1"
SKIP_REASON = ("LI_PG_DSN is not set: the real-PostgreSQL migration tests did NOT run "
               "(skipped, not passed)")

if REQUIRED and not DSN:
    raise RuntimeError("LI_PG_REQUIRED=1 but LI_PG_DSN is not set")

try:
    import psycopg
    from psycopg import errors as pg_errors
    from psycopg import sql as pgsql
    from psycopg.conninfo import make_conninfo
except ImportError:  # pragma: no cover - depends on the environment
    psycopg = None
    if DSN or REQUIRED:
        raise RuntimeError("LI_PG_DSN is set but psycopg is not installed")

from lofgren_intelligence.hosted.store import StoreError, SupabaseStore

ROOT = Path(__file__).resolve().parents[2]
MIGRATIONS = ROOT / "supabase" / "migrations"
BOOTSTRAP = ROOT / "scripts" / "pg" / "bootstrap_supabase_stub.sql"
STORE_SOURCE = ROOT / "lofgren_intelligence" / "hosted" / "store.py"

LI_TABLES = {
    "li_activation_counter", "li_accounts", "li_entitlements", "li_oauth_clients", "li_oauth_codes",
    "li_access_tokens", "li_refresh_tokens", "li_runs", "li_usage_events", "li_rate_events",
    "li_billing_events", "li_discoveries", "li_artifacts", "li_action_proposals", "li_outcomes",
    "li_improvements", "li_usage_reservations", "li_rate_limit_events", "li_cases", "li_case_charters",
    "li_case_approvals", "li_case_events", "li_research_jobs",
}
INTERNAL_FUNCTIONS = {"li_research_job_account", "li_research_job_abandon_settlement"}

pg_test = unittest.skipUnless(DSN, SKIP_REASON)

# ---------------------------------------------------------------------------
# database provisioning
# ---------------------------------------------------------------------------

_CREATED_DATABASES: list[str] = []
_SHARED: dict[str, str] = {}


def admin_connect(dbname: str | None = None):
    dsn = make_conninfo(DSN, dbname=dbname) if dbname else DSN
    return psycopg.connect(dsn, autocommit=True)


def create_clean_database(prefix: str) -> str:
    name = f"li_pg_{prefix}_{uuid.uuid4().hex[:10]}"
    with admin_connect() as conn:
        conn.execute(pgsql.SQL("create database {}").format(pgsql.Identifier(name)))
    _CREATED_DATABASES.append(name)
    return name


def apply_bootstrap(dbname: str) -> None:
    with admin_connect(dbname) as conn:
        with conn.transaction():
            conn.execute(BOOTSTRAP.read_text(encoding="utf-8"))


def migration_files() -> list[Path]:
    return sorted(MIGRATIONS.glob("*.sql"), key=lambda p: p.name)


LEDGER_DDL = """
create schema if not exists supabase_migrations;
create table if not exists supabase_migrations.schema_migrations (
  version text primary key,
  name text not null,
  sha256 text not null,
  applied_at timestamptz not null default clock_timestamp()
);
"""


def apply_migrations(dbname: str, before: str | None = None) -> list[str]:
    """Apply every not-yet-applied migration in filename order; return the applied names.

    With ``before``, stop at (and do not apply) that migration and everything after it.
    """
    applied: list[str] = []
    with admin_connect(dbname) as conn:
        conn.execute(LEDGER_DDL)
        for path in migration_files():
            if before is not None and path.name >= before:
                break
            version, _, name = path.stem.partition("_")
            raw = path.read_bytes()
            text = raw.decode("utf-8")
            with conn.transaction():
                seen = conn.execute(
                    "select 1 from supabase_migrations.schema_migrations where version = %s", (version,)
                ).fetchone()
                if seen:
                    continue
                conn.execute("set local role li_migrator")
                try:
                    conn.execute(text)
                except psycopg.Error as exc:
                    raise AssertionError(f"migration {path.name} failed: {exc.sqlstate}: {exc}") from exc
                conn.execute("reset role")
                conn.execute(
                    "insert into supabase_migrations.schema_migrations(version, name, sha256) values (%s, %s, %s)",
                    (version, name, hashlib.sha256(raw).hexdigest()),
                )
            applied.append(path.name)
    return applied


def provision(prefix: str) -> tuple[str, list[str]]:
    dbname = create_clean_database(prefix)
    apply_bootstrap(dbname)
    return dbname, apply_migrations(dbname)


def shared_db() -> str:
    if "db" not in _SHARED:
        _SHARED["db"], _ = provision("shared")
    return _SHARED["db"]


def tearDownModule():  # noqa: N802 - unittest hook
    if not DSN or psycopg is None:
        return
    with admin_connect() as conn:
        for name in _CREATED_DATABASES:
            conn.execute(pgsql.SQL("drop database if exists {} with (force)").format(pgsql.Identifier(name)))


def role_connect(dbname: str, role: str = "service_role"):
    """A session as Supabase's PostgREST runs it: authenticator, then SET ROLE <api role>."""
    conn = admin_connect(dbname)
    conn.execute("set session authorization authenticator")
    conn.execute(pgsql.SQL("set role {}").format(pgsql.Identifier(role)))
    who = conn.execute("select current_user::text, session_user::text").fetchone()
    assert who == (role, "authenticator"), who
    return conn


# ---------------------------------------------------------------------------
# PostgREST-like RPC through the real SupabaseStore
# ---------------------------------------------------------------------------

_SIG_SQL = """
select p.proretset, p.pronargs, p.pronargdefaults,
       coalesce(p.proargnames, '{}'::text[]),
       array(select format_type(t, null) from unnest(p.proargtypes::oid[]) with ordinality u(t, i) order by i)
  from pg_proc p join pg_namespace n on n.oid = p.pronamespace
 where n.nspname = 'public' and p.proname = %s
"""


def _param(value, argtype: str):
    if value is None:
        return None
    if argtype in ("jsonb", "json"):
        return json.dumps(value)
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def pg_rpc(conn, name: str, body: dict, cache: dict | None = None):
    cache = {} if cache is None else cache
    if name not in cache:
        cache[name] = conn.execute(_SIG_SQL, (name,)).fetchall()
    candidates = []
    for retset, nargs, ndefaults, argnames, argtypes in cache[name]:
        names = list(argnames)[:nargs]
        required = set(names[: nargs - ndefaults])
        if set(body) <= set(names) and required <= set(body):
            candidates.append((retset, dict(zip(names, argtypes))))
    if len(candidates) != 1:
        raise AssertionError(
            f"rpc {name}({', '.join(sorted(body))}) matches {len(candidates)} SQL signatures; expected exactly one")
    retset, types = candidates[0]
    args = pgsql.SQL(", ").join(
        pgsql.SQL("{} => {}::{}").format(pgsql.Identifier(k), pgsql.Placeholder(), pgsql.SQL(types[k]))
        for k in body
    )
    call = pgsql.SQL("public.{}({})").format(pgsql.Identifier(name), args)
    if retset:
        query = pgsql.SQL("select coalesce(jsonb_agg(to_jsonb(r)), '[]'::jsonb) from {} r").format(call)
    else:
        query = pgsql.SQL("select to_jsonb({})").format(call)
    params = [_param(v, types[k]) for k, v in body.items()]
    try:
        return conn.execute(query, params).fetchone()[0]
    except psycopg.Error as exc:
        raise StoreError(f"rpc {name} failed: {exc.sqlstate}: {exc}") from exc


class PgRpcStore(SupabaseStore):
    """The real SupabaseStore with its HTTP RPC transport replaced by direct SQL as service_role."""

    def __init__(self, conn):
        super().__init__(url="http://direct-postgres.invalid", service_key="direct-postgres-no-http")
        self.conn = conn
        self.calls: list[str] = []
        self._sig_cache: dict = {}

    def rpc(self, name, body):
        self.calls.append(name)
        return pg_rpc(self.conn, name, body, self._sig_cache)

    def _table(self, *args, **kwargs):
        raise AssertionError("PostgREST table access is not exercised by the real-PostgreSQL proof")


# ---------------------------------------------------------------------------
# shared helpers
# ---------------------------------------------------------------------------

def sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def run_concurrently(funcs, timeout: float = 30.0):
    """Run each callable in its own thread, released together by a barrier."""
    barrier = threading.Barrier(len(funcs))
    results: list = [None] * len(funcs)
    errors: list = [None] * len(funcs)

    def runner(i, fn):
        try:
            barrier.wait(timeout)
            results[i] = fn()
        except BaseException as exc:  # noqa: BLE001 - reported below
            errors[i] = exc

    threads = [threading.Thread(target=runner, args=(i, fn)) for i, fn in enumerate(funcs)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout)
    if any(t.is_alive() for t in threads):
        raise AssertionError("concurrent workers did not finish (deadlock or lock wait?)")
    failures = [e for e in errors if e is not None]
    if failures:
        raise AssertionError(f"concurrent worker failed: {failures[0]!r}") from failures[0]
    return results


class PgCase(unittest.TestCase):
    dbname: str

    @classmethod
    def setUpClass(cls):
        cls.dbname = shared_db()

    def setUp(self):
        self.admin = admin_connect(self.dbname)
        self.svc = role_connect(self.dbname)
        self.store = PgRpcStore(self.svc)
        self.addCleanup(self.admin.close)
        self.addCleanup(self.svc.close)

    def new_store(self):
        conn = role_connect(self.dbname)
        self.addCleanup(conn.close)
        return PgRpcStore(conn)

    def new_auth_user(self) -> str:
        uid = str(uuid.uuid4())
        self.admin.execute("insert into auth.users(id, email, email_confirmed_at) values (%s, %s, now())",
                           (uid, f"{uid[:8]}@example.test"))
        return uid

    def new_user(self) -> str:
        uid = self.new_auth_user()
        account = self.store.activate_account(uid, f"{uid[:8]}@example.test")
        self.assertEqual(account["user_id"], uid)
        return uid

    def one(self, query, params=()):
        return self.admin.execute(query, params).fetchone()

    def db_now(self):
        return self.one("select now()")[0]

    def wait_db_past(self, ts, limit: float = 15.0):
        deadline = time.monotonic() + limit
        while time.monotonic() < deadline:
            if self.one("select now() > %s::timestamptz", (ts,))[0]:
                return
            time.sleep(0.1)
        self.fail(f"database clock never passed {ts}")

    def reservation(self, rid):
        return self.one(
            "select status, reserved_units, actual_units, pending_units, pending_known_cost_usd, "
            "pending_unpriced_components, run_id, held_until, job_id from public.li_usage_reservations where id = %s",
            (rid,))

    def usage_events(self, rid):
        return self.admin.execute(
            "select units, run_id, known_cost_usd from public.li_usage_events where id = %s", (rid,)).fetchall()

    def save_run(self, conn, user_id, run_id):
        # The store's save_run is a PostgREST table insert as service_role; this is the same insert in SQL.
        conn.execute(
            "insert into public.li_runs(user_id, run_id, objective, status, summary, snapshot, receipt, "
            "knowledge_state, report) values (%s, %s, 'objective', 'complete', '{}', '{}', '{}', '{}', 'report')",
            (user_id, run_id))


# ---------------------------------------------------------------------------
# migrations apply cleanly, in order, once
# ---------------------------------------------------------------------------

@pg_test
class MigrationApplyTest(unittest.TestCase):
    def test_bootstrap_then_every_migration_applies_in_order_exactly_once(self):
        files = [p.name for p in migration_files()]
        self.assertGreaterEqual(len(files), 9)
        self.assertEqual(files, sorted(files))
        dbname, applied = provision("apply")
        self.assertEqual(applied, files, "every migration must apply, in filename order")
        with admin_connect(dbname) as conn:
            ledger = conn.execute(
                "select version || '_' || name || '.sql', sha256 from supabase_migrations.schema_migrations "
                "order by applied_at, version").fetchall()
            self.assertEqual([row[0] for row in ledger], files)
            for name, digest in ledger:
                self.assertEqual(digest, hashlib.sha256((MIGRATIONS / name).read_bytes()).hexdigest())
        # A second run re-applies nothing.
        self.assertEqual(apply_migrations(dbname), [])
        with admin_connect(dbname) as conn:
            self.assertEqual(conn.execute("select count(*) from supabase_migrations.schema_migrations").fetchone()[0],
                             len(files))

    def test_objects_are_owned_by_the_non_superuser_migrator(self):
        dbname = shared_db()
        with admin_connect(dbname) as conn:
            self.assertFalse(conn.execute("select rolsuper from pg_roles where rolname = 'li_migrator'").fetchone()[0])
            owners = conn.execute(
                "select c.relname, pg_get_userbyid(c.relowner) from pg_class c "
                "join pg_namespace n on n.oid = c.relnamespace "
                "where n.nspname = 'public' and c.relkind = 'r' and c.relname like 'li\\_%'").fetchall()
            self.assertEqual({r[0] for r in owners}, LI_TABLES)
            self.assertEqual({r[1] for r in owners}, {"li_migrator"})
            fowners = conn.execute(
                "select distinct pg_get_userbyid(p.proowner) from pg_proc p "
                "join pg_namespace n on n.oid = p.pronamespace "
                "where n.nspname = 'public' and p.proname like 'li\\_%'").fetchall()
            self.assertEqual(fowners, [("li_migrator",)])


# ---------------------------------------------------------------------------
# privileges and RLS
# ---------------------------------------------------------------------------

def store_rpc_names() -> set[str]:
    return set(re.findall(r'self\.rpc\(\s*"([a-z_]+)"', STORE_SOURCE.read_text(encoding="utf-8")))


@pg_test
class PrivilegeTest(PgCase):
    def li_functions(self):
        return self.admin.execute(
            "select p.oid, p.proname, p.prosecdef, p.proconfig, "
            "array(select format_type(t, null) from unnest(p.proargtypes::oid[]) with ordinality u(t, i) order by i) "
            "from pg_proc p join pg_namespace n on n.oid = p.pronamespace "
            "where n.nspname = 'public' and p.proname like 'li\\_%' order by p.proname").fetchall()

    def null_call(self, name, argtypes):
        args = pgsql.SQL(", ").join(pgsql.SQL("null::{}").format(pgsql.SQL(t)) for t in argtypes)
        return pgsql.SQL("select public.{}({})").format(pgsql.Identifier(name), args)

    def test_rls_is_enabled_on_every_li_table_with_no_policies(self):
        rows = self.admin.execute(
            "select c.relname, c.relrowsecurity from pg_class c join pg_namespace n on n.oid = c.relnamespace "
            "where n.nspname = 'public' and c.relkind = 'r' and c.relname like 'li\\_%'").fetchall()
        self.assertEqual({r[0] for r in rows}, LI_TABLES)
        for name, rls in rows:
            with self.subTest(table=name):
                self.assertTrue(rls, f"RLS is not enabled on {name}")
        policies = self.admin.execute(
            "select tablename, policyname from pg_policies where schemaname = 'public' and tablename like 'li\\_%'"
        ).fetchall()
        self.assertEqual(policies, [], "no RLS policy may open an li_ table to API roles")

    def test_anon_and_authenticated_cannot_read_or_write_any_li_table(self):
        for role in ("anon", "authenticated"):
            conn = role_connect(self.dbname, role)
            self.addCleanup(conn.close)
            for table in sorted(LI_TABLES):
                with self.subTest(role=role, table=table):
                    for priv in ("SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE", "REFERENCES", "TRIGGER"):
                        self.assertFalse(
                            self.one("select has_table_privilege(%s, %s, %s)", (role, f"public.{table}", priv))[0],
                            f"{role} has {priv} on {table}")
                    ident = pgsql.Identifier("public", table)
                    with self.assertRaises(pg_errors.InsufficientPrivilege):
                        conn.execute(pgsql.SQL("select * from {} limit 1").format(ident))
                    with self.assertRaises(pg_errors.InsufficientPrivilege):
                        conn.execute(pgsql.SQL("insert into {} default values").format(ident))
                    with self.assertRaises(pg_errors.InsufficientPrivilege):
                        conn.execute(pgsql.SQL("delete from {}").format(ident))

    def test_anon_and_authenticated_cannot_execute_any_li_function(self):
        functions = self.li_functions()
        self.assertEqual({f[1] for f in functions}, store_rpc_names() | INTERNAL_FUNCTIONS)
        for role in ("anon", "authenticated"):
            conn = role_connect(self.dbname, role)
            self.addCleanup(conn.close)
            for oid, name, _secdef, _config, argtypes in functions:
                with self.subTest(role=role, function=name):
                    self.assertFalse(self.one("select has_function_privilege(%s, %s::oid, 'EXECUTE')", (role, oid))[0])
                    with self.assertRaises(pg_errors.InsufficientPrivilege):
                        conn.execute(self.null_call(name, argtypes))

    def test_service_role_executes_exactly_the_store_rpcs_as_security_definer(self):
        expected = store_rpc_names()
        self.assertEqual(len(expected), 23, sorted(expected))
        functions = self.li_functions()
        seen = set()
        for oid, name, secdef, config, argtypes in functions:
            can = self.one("select has_function_privilege('service_role', %s::oid, 'EXECUTE')", (oid,))[0]
            with self.subTest(function=name):
                self.assertIn("search_path=public", config or [])
                if name in INTERNAL_FUNCTIONS:
                    self.assertFalse(can, f"service_role may execute internal helper {name}")
                    self.assertFalse(secdef)
                    with self.assertRaises(pg_errors.InsufficientPrivilege):
                        self.svc.execute(self.null_call(name, argtypes))
                else:
                    self.assertTrue(can, f"service_role cannot execute {name}")
                    self.assertTrue(secdef, f"{name} is not SECURITY DEFINER")
                    seen.add(name)
        self.assertEqual(seen, expected)
        # The uncapped reclaim overload is gone; exactly one signature remains.
        self.assertEqual(self.admin.execute(
            "select pg_get_function_identity_arguments(p.oid) from pg_proc p "
            "where p.proname = 'li_reclaim_research_jobs'").fetchall(),
            [("p_limit integer, p_queue_ttl_seconds integer, p_max_finalize_reclaims integer",)])

    def test_service_role_has_table_access_for_the_store(self):
        for table in sorted(LI_TABLES):
            for priv in ("SELECT", "INSERT", "UPDATE", "DELETE"):
                with self.subTest(table=table, priv=priv):
                    self.assertTrue(self.one("select has_table_privilege('service_role', %s, %s)",
                                             (f"public.{table}", priv))[0])
        self.assertTrue(self.one("select rolbypassrls from pg_roles where rolname = 'service_role'")[0])
        self.svc.execute("select count(*) from public.li_accounts").fetchone()

    def test_auth_uid_stub_reads_the_session_claim(self):
        uid = str(uuid.uuid4())
        conn = role_connect(self.dbname, "authenticated")
        self.addCleanup(conn.close)
        self.assertIsNone(conn.execute("select auth.uid()").fetchone()[0])
        conn.execute("select set_config('request.jwt.claim.sub', %s, false)", (uid,))
        self.assertEqual(str(conn.execute("select auth.uid()").fetchone()[0]), uid)


# ---------------------------------------------------------------------------
# Q30: no default PUBLIC / anon / authenticated privilege on any li_ object
# ---------------------------------------------------------------------------

Q30_MIGRATION = "20261006090000_li_revoke_public_defaults.sql"
LI_SEQUENCES = {"li_rate_events_id_seq", "li_rate_limit_events_id_seq", "li_case_events_id_seq"}

# Every ACL entry on every li_ object in schema public. A NULL ACL means the
# built-in default (acldefault), which for functions is EXECUTE to PUBLIC and
# for types USAGE to PUBLIC, so the defaults are expanded rather than skipped.
LI_ACL_SQL = r"""
select kind, name, (acl).grantee, (acl).privilege_type from (
  select 'sequence' as kind, c.relname::text as name,
         aclexplode(coalesce(c.relacl, acldefault('s', c.relowner))) as acl
    from pg_class c join pg_namespace n on n.oid = c.relnamespace
   where n.nspname = 'public' and c.relname like 'li\_%' and c.relkind = 'S'
  union all
  select 'table', c.relname::text, aclexplode(coalesce(c.relacl, acldefault('r', c.relowner)))
    from pg_class c join pg_namespace n on n.oid = c.relnamespace
   where n.nspname = 'public' and c.relname like 'li\_%' and c.relkind in ('r', 'p', 'v', 'm', 'f')
  union all
  select 'function', p.oid::regprocedure::text, aclexplode(coalesce(p.proacl, acldefault('f', p.proowner)))
    from pg_proc p join pg_namespace n on n.oid = p.pronamespace
   where n.nspname = 'public' and p.proname like 'li\_%'
  union all
  select 'type', t.typname::text, aclexplode(coalesce(t.typacl, acldefault('T', t.typowner)))
    from pg_type t join pg_namespace n on n.oid = t.typnamespace
   where n.nspname = 'public' and t.typname like 'li\_%' and t.typelem = 0
) s
"""


def li_acl_holes(conn) -> list[tuple]:
    """(kind, object, grantee, privilege) for every privilege PUBLIC, anon or authenticated holds."""
    roles = {oid: name for oid, name in conn.execute(
        "select oid, rolname::text from pg_roles where rolname in ('anon', 'authenticated')").fetchall()}
    assert set(roles.values()) == {"anon", "authenticated"}, roles
    roles[0] = "PUBLIC"
    return sorted((kind, name, roles[grantee], priv)
                  for kind, name, grantee, priv in conn.execute(LI_ACL_SQL).fetchall() if grantee in roles)


@pg_test
class PublicDefaultPrivilegeTest(PgCase):
    def test_the_sequence_hole_existed_before_q30_and_q30_closes_it(self):
        dbname = create_clean_database("q30")
        apply_bootstrap(dbname)
        applied = apply_migrations(dbname, before=Q30_MIGRATION)
        self.assertTrue(applied)
        self.assertNotIn(Q30_MIGRATION, applied)
        with admin_connect(dbname) as conn:
            # Before Q30, Supabase's default privileges left the identity sequences open.
            for seq in sorted(LI_SEQUENCES):
                for role in ("anon", "authenticated"):
                    for priv in ("USAGE", "SELECT", "UPDATE"):
                        with self.subTest(stage="before", sequence=seq, role=role, priv=priv):
                            self.assertTrue(conn.execute("select has_sequence_privilege(%s, %s, %s)",
                                                         (role, f"public.{seq}", priv)).fetchone()[0])
            self.assertTrue(li_acl_holes(conn))
        self.assertEqual(apply_migrations(dbname)[0], Q30_MIGRATION)
        with admin_connect(dbname) as conn:
            self.assertEqual(li_acl_holes(conn), [])

    def test_no_li_object_grants_anything_to_public_anon_or_authenticated(self):
        names = {r[0] for r in self.admin.execute(
            "select c.relname::text from pg_class c join pg_namespace n on n.oid = c.relnamespace "
            "where n.nspname = 'public' and c.relkind = 'S' and c.relname like 'li\\_%'").fetchall()}
        self.assertEqual(names, LI_SEQUENCES)
        kinds = {r[0] for r in self.admin.execute(LI_ACL_SQL).fetchall()}
        self.assertEqual(kinds, {"sequence", "table", "function", "type"}, "the ACL sweep saw every object kind")
        self.assertEqual(li_acl_holes(self.admin), [])

    def test_anon_and_authenticated_cannot_read_advance_or_reset_any_li_sequence(self):
        state_sql = pgsql.SQL("select last_value, is_called from {}")
        before = {seq: self.admin.execute(state_sql.format(pgsql.Identifier("public", seq))).fetchone()
                  for seq in LI_SEQUENCES}
        for role in ("anon", "authenticated"):
            conn = role_connect(self.dbname, role)  # a restricted API role, never the admin
            self.addCleanup(conn.close)
            for seq in sorted(LI_SEQUENCES):
                qualified = f"public.{seq}"
                with self.subTest(role=role, sequence=seq):
                    for priv in ("USAGE", "SELECT", "UPDATE"):
                        self.assertFalse(self.one("select has_sequence_privilege(%s, %s, %s)",
                                                  (role, qualified, priv))[0], f"{role} has {priv} on {seq}")
                    with self.assertRaises(pg_errors.InsufficientPrivilege):
                        conn.execute("select nextval(%s::regclass)", (qualified,))
                    with self.assertRaises(pg_errors.InsufficientPrivilege):
                        conn.execute("select setval(%s::regclass, 1)", (qualified,))
                    with self.assertRaises(pg_errors.InsufficientPrivilege):
                        conn.execute(state_sql.format(pgsql.Identifier("public", seq)))
        # Final state: no sequence moved.
        after = {seq: self.admin.execute(state_sql.format(pgsql.Identifier("public", seq))).fetchone()
                 for seq in LI_SEQUENCES}
        self.assertEqual(after, before)

    def test_service_role_keeps_what_the_store_needs(self):
        for seq in sorted(LI_SEQUENCES):
            with self.subTest(sequence=seq):
                self.assertTrue(self.one("select has_sequence_privilege('service_role', %s, 'USAGE')",
                                         (f"public.{seq}",))[0])
        for typ in ("li_research_jobs", "li_case_events"):
            with self.subTest(type=typ):
                self.assertTrue(self.one("select has_type_privilege('service_role', %s, 'USAGE')",
                                         (f"public.{typ}",))[0])
                for role in ("anon", "authenticated"):
                    self.assertFalse(self.one("select has_type_privilege(%s, %s, 'USAGE')",
                                              (role, f"public.{typ}"))[0])
        # An identity-backed insert through a store RPC still works for service_role.
        key = sha(f"q30-{uuid.uuid4()}")
        self.assertEqual(self.store.take_keyed_rate_limit("activate", key, 5, 60), 0)
        self.assertEqual(self.one("select count(*) from public.li_rate_limit_events where key_hash = %s",
                                  (key,))[0], 1)


# ---------------------------------------------------------------------------
# Intelligence Case approvals
# ---------------------------------------------------------------------------

@pg_test
class CaseApprovalTest(PgCase):
    def make_case(self, user_id):
        case_id = str(uuid.uuid4())
        self.assertEqual(self.store.create_case(case_id, user_id, "Is X increasing?", "clarifying",
                                                sha(case_id + ":1"), {"objective": "x", "v": 1}, {}), 1)
        return case_id

    def approval_row(self, case_id, user_id, version, content_hash, *, seconds=600, expires_at=None):
        approval_id = str(uuid.uuid4())
        token = sha(approval_id + ":token")
        expires = expires_at or (self.db_now() + timedelta(seconds=seconds))
        return {
            "id": approval_id, "case_id": case_id, "user_id": user_id, "charter_version": version,
            "content_hash": content_hash, "scope": {"sources": ["web"]}, "budget_usd": 2.5,
            "budget_units": 100, "expires_at": expires.isoformat(), "token_hash": token,
        }

    def test_create_revise_approve_consume_once(self):
        user = self.new_user()
        case_id = self.make_case(user)
        h2 = sha(case_id + ":2")
        self.assertEqual(self.store.revise_case(case_id, user, 1, "ready_for_approval", h2, {"v": 2}, {"a": 1}), 2)
        # Version conflict: a stale expected version is refused and recorded.
        self.assertEqual(self.store.revise_case(case_id, user, 1, "ready_for_approval", sha("x"), {"v": 9}, {}), -1)
        self.assertEqual(self.one("select max(version) from public.li_case_charters where case_id = %s",
                                  (case_id,))[0], 2)
        self.assertEqual(self.one("select count(*) from public.li_case_events where case_id = %s "
                                  "and kind = 'revision_conflict'", (case_id,))[0], 1)
        # Only the exact latest version and content hash can be approved.
        self.assertFalse(self.store.approve_case_charter(self.approval_row(case_id, user, 1, sha(case_id + ":1"))))
        self.assertFalse(self.store.approve_case_charter(self.approval_row(case_id, user, 2, sha("wrong"))))
        row = self.approval_row(case_id, user, 2, h2)
        self.assertTrue(self.store.approve_case_charter(row))
        self.assertEqual(self.one("select status from public.li_cases where id = %s", (case_id,))[0], "approved")

        self.assertIsNone(self.store.consume_case_approval(row["id"], user, sha("not-the-token"), "k1"))
        first = self.store.consume_case_approval(row["id"], user, row["token_hash"], "k1")
        self.assertIsNotNone(first)
        self.assertTrue(first["consumed_now"])
        self.assertEqual(first["run_status"], "started")
        # A second consume (another key) is refused; the same key is a retry, never a second execution.
        self.assertIsNone(self.store.consume_case_approval(row["id"], user, row["token_hash"], "k2"))
        retry = self.store.consume_case_approval(row["id"], user, row["token_hash"], "k1")
        self.assertFalse(retry["consumed_now"])
        self.assertEqual(self.one("select count(*) from public.li_case_events where case_id = %s "
                                  "and kind = 'approval_consumed'", (case_id,))[0], 1)
        self.assertEqual(self.one("select status from public.li_cases where id = %s", (case_id,))[0],
                         "research_started")
        self.assertTrue(self.store.record_case_run(row["id"], user, "run-1", "complete"))
        self.assertFalse(self.store.record_case_run(row["id"], user, "run-2", "complete"))
        self.assertEqual(self.one("select status from public.li_cases where id = %s", (case_id,))[0],
                         "research_complete")

    def test_concurrent_consumes_execute_once(self):
        user = self.new_user()
        case_id = self.make_case(user)
        row = self.approval_row(case_id, user, 1, sha(case_id + ":1"))
        self.assertTrue(self.store.approve_case_charter(row))
        stores = [self.new_store() for _ in range(4)]
        results = run_concurrently([
            (lambda s=s, i=i: s.consume_case_approval(row["id"], user, row["token_hash"], f"key-{i}"))
            for i, s in enumerate(stores)])
        winners = [r for r in results if r is not None and r["consumed_now"]]
        self.assertEqual(len(winners), 1)
        self.assertEqual(sum(1 for r in results if r is None), 3)

    def test_wrong_user_is_refused(self):
        owner, other = self.new_user(), self.new_user()
        case_id = self.make_case(owner)
        self.assertEqual(self.store.revise_case(case_id, other, 1, "clarifying", sha("o"), {}, {}), 0)
        h1 = sha(case_id + ":1")
        self.assertFalse(self.store.approve_case_charter(self.approval_row(case_id, other, 1, h1)))
        row = self.approval_row(case_id, owner, 1, h1)
        self.assertTrue(self.store.approve_case_charter(row))
        self.assertIsNone(self.store.consume_case_approval(row["id"], other, row["token_hash"], "k"))
        self.assertIsNotNone(self.store.consume_case_approval(row["id"], owner, row["token_hash"], "k"))
        self.assertFalse(self.store.record_case_run(row["id"], other, "run-x", "complete"))
        self.assertEqual(self.one("select max(version) from public.li_case_charters where case_id = %s",
                                  (case_id,))[0], 1)

    def test_expired_approval_is_refused(self):
        user = self.new_user()
        case_id = self.make_case(user)
        h1 = sha(case_id + ":1")
        past = self.db_now() - timedelta(seconds=5)
        self.assertFalse(self.store.approve_case_charter(self.approval_row(case_id, user, 1, h1, expires_at=past)))
        row = self.approval_row(case_id, user, 1, h1, seconds=1.5)
        self.assertTrue(self.store.approve_case_charter(row))
        self.wait_db_past(row["expires_at"])
        self.assertIsNone(self.store.consume_case_approval(row["id"], user, row["token_hash"], "k"))
        self.assertIsNone(self.one("select consumed_at from public.li_case_approvals where id = %s", (row["id"],))[0])

    def test_edit_after_approval_invalidates_it(self):
        user = self.new_user()
        case_id = self.make_case(user)
        h1 = sha(case_id + ":1")
        first = self.approval_row(case_id, user, 1, h1)
        self.assertTrue(self.store.approve_case_charter(first))
        # Re-approval revokes the earlier live approval (one live approval per case).
        second = self.approval_row(case_id, user, 1, h1)
        self.assertTrue(self.store.approve_case_charter(second))
        self.assertIsNotNone(self.one("select revoked_at from public.li_case_approvals where id = %s",
                                      (first["id"],))[0])
        self.assertIsNone(self.store.consume_case_approval(first["id"], user, first["token_hash"], "k"))
        # An edit after approval revokes it.
        self.assertEqual(self.store.revise_case(case_id, user, 1, "ready_for_approval", sha("edit"), {"v": 2}, {}), 2)
        self.assertIsNotNone(self.one("select revoked_at from public.li_case_approvals where id = %s",
                                      (second["id"],))[0])
        self.assertIsNone(self.store.consume_case_approval(second["id"], user, second["token_hash"], "k"))
        self.assertEqual(self.one("select count(*) from public.li_case_approvals where case_id = %s "
                                  "and consumed_at is null and revoked_at is null", (case_id,))[0], 0)


# ---------------------------------------------------------------------------
# usage reservations and settlement
# ---------------------------------------------------------------------------

@pg_test
class UsageTest(PgCase):
    def rid(self):
        return str(uuid.uuid4())

    def test_reserve_within_quota_and_refuse_over_quota(self):
        user = self.new_user()
        self.assertEqual(self.one("select kind, quota_units_per_week from public.li_entitlements where user_id = %s",
                                  (user,)), ("founding_free", 500))
        r1, r2, r3 = self.rid(), self.rid(), self.rid()
        self.assertTrue(self.store.reserve_usage(r1, user, "investigate", 300))
        self.assertFalse(self.store.reserve_usage(r2, user, "investigate", 250))
        self.assertIsNone(self.reservation(r2))
        self.assertTrue(self.store.reserve_usage(r3, user, "investigate", 200))  # exactly the quota
        self.assertFalse(self.store.reserve_usage(self.rid(), user, "investigate", 0.01))

    def test_finalize_release_and_requota(self):
        user = self.new_user()
        self.save_run(self.svc, user, "run-usage-1")
        r1, r2 = self.rid(), self.rid()
        self.assertTrue(self.store.reserve_usage(r1, user, "investigate", 300))
        self.assertTrue(self.store.reserve_usage(r2, user, "investigate", 200))
        self.assertTrue(self.store.finalize_usage(r1, "run-usage-1", 280, 0.1, ["research_compute"]))
        self.assertEqual(self.reservation(r1)[0], "settled")
        self.assertEqual([(float(u), run) for u, run, _ in self.usage_events(r1)], [(280.0, "run-usage-1")])
        self.assertFalse(self.store.finalize_usage(r1, "run-usage-1", 280, 0.1, []))  # never twice
        self.assertEqual(len(self.usage_events(r1)), 1)
        self.assertTrue(self.store.release_usage(r2))
        self.assertFalse(self.store.release_usage(r2))
        self.assertEqual(self.reservation(r2)[0], "released")
        # 280 used, nothing reserved: 220 fits, 221 does not.
        self.assertFalse(self.store.reserve_usage(self.rid(), user, "investigate", 221))
        self.assertTrue(self.store.reserve_usage(self.rid(), user, "investigate", 220))

    def test_unsettled_marker_settles_exactly_once(self):
        user = self.new_user()
        rid = self.rid()
        self.assertTrue(self.store.reserve_usage(rid, user, "investigate", 100))
        # Actual usage overran the quota: finalize is refused, work is marked and then settled once.
        self.assertFalse(self.store.finalize_usage(rid, None, 600, 0.4, ["x"]))
        self.assertEqual(self.reservation(rid)[0], "reserved")
        self.assertTrue(self.store.mark_usage_unsettled(rid, None, 600, 0.4, ["x"]))
        self.assertEqual(self.reservation(rid)[0], "unsettled")
        self.assertEqual(self.store.settle_usage(rid), "settled")
        self.assertEqual(self.store.settle_usage(rid), "already_settled")
        self.assertEqual(self.store.settle_usage(rid), "already_settled")
        self.assertEqual([float(e[0]) for e in self.usage_events(rid)], [600.0])
        self.assertFalse(self.store.mark_usage_unsettled(rid, None, 5, 0, []))  # settled: nothing to mark
        self.assertEqual(self.store.settle_usage(str(uuid.uuid4())), "missing")
        other = self.rid()
        self.assertFalse(self.store.reserve_usage(other, user, "investigate", 1))  # 600 > 500 used
        user2 = self.new_user()
        held = self.rid()
        self.assertTrue(self.store.reserve_usage(held, user2, "investigate", 1))
        self.assertEqual(self.store.settle_usage(held), "not_unsettled")

    def test_pending_unsettled_usage_counts_against_the_quota(self):
        user = self.new_user()
        rid = self.rid()
        self.assertTrue(self.store.reserve_usage(rid, user, "investigate", 10))
        self.assertTrue(self.store.mark_usage_unsettled(rid, None, 450, 0, ["x"]))
        self.assertFalse(self.store.reserve_usage(self.rid(), user, "investigate", 60))
        self.assertTrue(self.store.reserve_usage(self.rid(), user, "investigate", 50))
        self.assertEqual(self.store.settle_usage(rid), "settled")
        self.assertEqual([float(e[0]) for e in self.usage_events(rid)], [450.0])

    def test_concurrent_reservations_never_exceed_the_quota(self):
        user = self.new_user()
        stores = [self.new_store() for _ in range(10)]
        results = run_concurrently([(lambda s=s: s.reserve_usage(str(uuid.uuid4()), user, "investigate", 60))
                                    for s in stores])
        self.assertEqual(results.count(True), 8)
        self.assertEqual(float(self.one("select sum(reserved_units) from public.li_usage_reservations "
                                        "where user_id = %s and status = 'reserved'", (user,))[0]), 480.0)

    def test_two_connections_at_the_quota_boundary(self):
        for _ in range(3):
            user = self.new_user()
            self.assertTrue(self.store.reserve_usage(str(uuid.uuid4()), user, "investigate", 440))
            a, b = self.new_store(), self.new_store()
            results = run_concurrently([
                lambda: a.reserve_usage(str(uuid.uuid4()), user, "investigate", 60),
                lambda: b.reserve_usage(str(uuid.uuid4()), user, "investigate", 60),
            ])
            self.assertEqual(sorted(results), [False, True])
            self.assertEqual(float(self.one("select sum(reserved_units) from public.li_usage_reservations "
                                            "where user_id = %s and status = 'reserved'", (user,))[0]), 500.0)


# ---------------------------------------------------------------------------
# durable research jobs
# ---------------------------------------------------------------------------

@pg_test
class JobsTest(PgCase):
    @classmethod
    def setUpClass(cls):
        # Claims and reclaims are global; jobs get their own clean database.
        cls.dbname, _ = provision("jobs")

    def setUp(self):
        super().setUp()
        self.admin.execute("delete from public.li_research_jobs")

    def reserve(self, user, units=10):
        rid = str(uuid.uuid4())
        self.assertTrue(self.store.reserve_usage(rid, user, "investigate", units))
        return rid

    def enqueue(self, user, rid, key=None, max_attempts=3, ttl=600, job_id=None):
        return self.store.enqueue_research_job(job_id or str(uuid.uuid4()), user, None, "investigate",
                                               key or str(uuid.uuid4()), {"objective": "x"}, rid, max_attempts, ttl)

    def job(self, job_id):
        return self.one("select status, attempts, lease_owner, lease_expires_at, error_code, result_run_id, "
                        "finalize_reclaims, cost_so_far from public.li_research_jobs where id = %s", (job_id,))

    def test_enqueue_is_idempotent_and_binds_only_a_usable_reservation(self):
        user, other = self.new_user(), self.new_user()
        r1, r2 = self.reserve(user), self.reserve(user)
        first = self.enqueue(user, r1, key="same-key")
        self.assertTrue(first["created"])
        again = self.enqueue(user, r2, key="same-key")
        self.assertFalse(again["created"])
        self.assertEqual(again["id"], first["id"])
        self.assertIsNone(self.reservation(r2)[8], "a repeated key must bind nothing")
        self.assertEqual(str(self.reservation(r1)[8]), first["id"])
        self.assertIsNone(self.enqueue(user, r1))  # already bound to a job
        self.assertIsNone(self.enqueue(other, r2))  # another user's reservation
        self.assertTrue(self.store.release_usage(r2))
        self.assertIsNone(self.enqueue(user, r2))  # not 'reserved'
        self.assertEqual(self.one("select count(*) from public.li_research_jobs")[0], 1)

    def test_concurrent_claims_claim_each_job_once(self):
        user = self.new_user()
        jobs = {self.enqueue(user, self.reserve(user))["id"] for _ in range(12)}
        workers = [self.new_store() for _ in range(2)]

        def drain(store, name):
            got = []
            while True:
                row = store.claim_research_job(name, 60, 60)
                if row is None:
                    return got
                got.append(row["id"])

        claimed = run_concurrently([lambda: drain(workers[0], "w0"), lambda: drain(workers[1], "w1")])
        flat = claimed[0] + claimed[1]
        self.assertEqual(len(flat), len(set(flat)), "a job was claimed twice")
        self.assertEqual(set(flat), jobs)
        self.assertEqual(self.one("select count(*) from public.li_research_jobs where status = 'running' "
                                  "and attempts = 1")[0], 12)

    def test_claim_skips_a_row_locked_by_another_worker(self):
        user = self.new_user()
        j1 = self.enqueue(user, self.reserve(user))["id"]
        j2 = self.enqueue(user, self.reserve(user))["id"]
        holder = role_connect(self.dbname)
        self.addCleanup(holder.close)
        other = self.new_store()
        other.conn.execute("set statement_timeout = '5s'")
        third = self.new_store()
        third.conn.execute("set statement_timeout = '5s'")
        with holder.transaction():
            held = holder.execute("select public.li_claim_research_job('holder', 60, 60)").fetchone()[0]
            # Uncommitted claim holds its row lock; the other worker skips it instead of waiting.
            got = other.claim_research_job("other", 60, 60)
            self.assertIsNotNone(got)
            self.assertNotEqual(got["id"], held["id"])
            self.assertIsNone(third.claim_research_job("third", 60, 60))
        self.assertEqual({held["id"], got["id"]}, {j1, j2})
        self.assertEqual(self.job(held["id"])[2], "holder")
        self.assertEqual(self.job(got["id"])[2], "other")

    def test_heartbeat_extends_the_lease(self):
        user = self.new_user()
        job_id = self.enqueue(user, self.reserve(user))["id"]
        claimed = self.store.claim_research_job("w1", 5, 60)
        self.assertEqual(claimed["id"], job_id)
        lease1 = self.job(job_id)[3]
        beat = self.store.heartbeat_research_job(job_id, "w1", 300, 60, {"phase": "searching"}, 3)
        self.assertIsNotNone(beat)
        status, _attempts, owner, lease2, *_rest, cost = self.job(job_id)
        self.assertEqual((status, owner, float(cost)), ("running", "w1", 3.0))
        self.assertGreater(lease2, lease1 + timedelta(seconds=200))
        self.assertIsNone(self.store.heartbeat_research_job(job_id, "intruder", 300, 60, None, None))
        # Cost never goes backwards.
        self.store.heartbeat_research_job(job_id, "w1", 300, 60, None, 1)
        self.assertEqual(float(self.job(job_id)[7]), 3.0)

    def test_reservation_hold_is_extended_by_the_heartbeat(self):
        user = self.new_user()
        rid = self.reserve(user)
        job_id = self.enqueue(user, rid)["id"]
        self.store.claim_research_job("w1", 5, 60)
        lease1, hold1 = self.job(job_id)[3], self.reservation(rid)[7]
        self.assertEqual(hold1, lease1 + timedelta(seconds=60))
        self.store.heartbeat_research_job(job_id, "w1", 600, 120, None, None)
        lease2, hold2 = self.job(job_id)[3], self.reservation(rid)[7]
        self.assertEqual(hold2, lease2 + timedelta(seconds=120))
        self.assertGreater(hold2, hold1)
        # A live hold keeps the one-hour expiry in li_reserve_usage away; an unheld reservation expires.
        loose = self.reserve(user)
        self.admin.execute("update public.li_usage_reservations set created_at = now() - interval '2 hours' "
                           "where id = any(%s::uuid[])", ([rid, loose],))
        self.reserve(user, 1)
        self.assertEqual(self.reservation(rid)[0], "reserved")
        self.assertEqual(self.reservation(loose)[0], "expired")

    def test_lease_expiry_is_reclaimed_and_the_old_worker_is_fenced(self):
        user = self.new_user()
        rid = self.reserve(user)
        job_id = self.enqueue(user, rid, max_attempts=3)["id"]
        self.store.claim_research_job("w1", 1, 0)
        self.wait_db_past(self.job(job_id)[3])
        self.assertIsNone(self.store.heartbeat_research_job(job_id, "w1", 60, 0, None, None))
        self.assertIn(job_id, self.store.reclaim_research_jobs(100, 600, 3))
        status, attempts, owner, *_ = self.job(job_id)
        self.assertEqual((status, attempts, owner), ("queued", 1, None))
        self.assertGreater(self.reservation(rid)[7], self.db_now())
        again = self.store.claim_research_job("w2", 60, 60)
        self.assertEqual((again["id"], again["attempts"]), (job_id, 2))
        self.assertIsNone(self.store.complete_research_job(job_id, "w1", "run-x", 1))
        self.assertTrue(self.store.finalize_usage(rid, None, 4, 0, []))
        done = self.store.complete_research_job(job_id, "w2", "run-x", 4)
        self.assertEqual(done["status"], "succeeded")
        self.assertIsNone(self.reservation(rid)[7])

    def test_lease_expiry_with_no_attempts_left_fails_and_accounts_the_work(self):
        user = self.new_user()
        rid = self.reserve(user, 20)
        job_id = self.enqueue(user, rid, max_attempts=1)["id"]
        self.store.claim_research_job("w1", 2, 0)
        self.store.heartbeat_research_job(job_id, "w1", 1, 0, {"phase": "searching", "known_cost_usd": 0.2}, 7)
        self.wait_db_past(self.job(job_id)[3])
        self.assertIn(job_id, self.store.reclaim_research_jobs(100, 600, 3))
        self.assertEqual(self.job(job_id)[0::4], ("failed", "LEASE_EXPIRED"))
        status, *_r, pending, cost, comps, run_id, held, _j = self.reservation(rid)
        self.assertEqual((status, float(pending), float(cost), run_id, held), ("unsettled", 7.0, 0.2, None, None))
        self.assertEqual(comps, ["partial_run", "lease_expired"])
        self.assertEqual(self.store.settle_usage(rid), "settled")
        self.assertEqual([float(e[0]) for e in self.usage_events(rid)], [7.0])

    def test_cancel_before_claim_releases_and_after_completion_is_a_no_op(self):
        user, other = self.new_user(), self.new_user()
        rid = self.reserve(user)
        job_id = self.enqueue(user, rid)["id"]
        self.assertIsNone(self.store.request_cancel_research_job(job_id, other))
        cancelled = self.store.request_cancel_research_job(job_id, user)
        self.assertEqual((cancelled["status"], cancelled["error_code"]), ("cancelled", "CANCELLED"))
        self.assertEqual(self.reservation(rid)[0], "released")
        self.assertEqual(self.store.request_cancel_research_job(job_id, user)["status"], "cancelled")
        self.assertIsNone(self.store.claim_research_job("w1", 60, 60))

        rid2 = self.reserve(user)
        job2 = self.enqueue(user, rid2)["id"]
        self.store.claim_research_job("w1", 60, 60)
        self.assertTrue(self.store.finalize_usage(rid2, None, 6, 0, []))
        self.assertEqual(self.store.complete_research_job(job2, "w1", "run-done", 6)["status"], "succeeded")
        after = self.store.request_cancel_research_job(job2, user)
        self.assertEqual((after["status"], after["result_run_id"]), ("succeeded", "run-done"))
        self.assertIsNone(self.job(job2)[4])
        self.assertEqual(self.reservation(rid2)[0], "settled")

    def test_cancel_while_running_is_requested_then_honoured(self):
        user = self.new_user()
        rid = self.reserve(user)
        job_id = self.enqueue(user, rid)["id"]
        self.store.claim_research_job("w1", 60, 60)
        self.assertEqual(self.store.request_cancel_research_job(job_id, user)["status"], "cancel_requested")
        self.assertEqual(self.store.heartbeat_research_job(job_id, "w1", 60, 60, None, None)["status"],
                         "cancel_requested")
        ended = self.store.fail_research_job(job_id, "w1", "CANCELLED", True, 0, 0, None, 600)
        self.assertEqual(ended["status"], "cancelled")
        self.assertEqual(self.reservation(rid)[0], "released")

    def test_fail_with_retry_then_exhaustion(self):
        user = self.new_user()
        rid = self.reserve(user)
        job_id = self.enqueue(user, rid, max_attempts=2)["id"]
        self.assertEqual(self.store.claim_research_job("w1", 60, 60)["attempts"], 1)
        retried = self.store.fail_research_job(job_id, "w1", "UPSTREAM_TIMEOUT", True, 0, 0, None, 600)
        self.assertEqual((retried["status"], retried["attempts"], retried["error_code"]),
                         ("queued", 1, "UPSTREAM_TIMEOUT"))
        self.assertEqual(self.reservation(rid)[0], "reserved")
        self.assertGreater(self.reservation(rid)[7], self.db_now())
        # WORKER_SHUTDOWN requeues without using up an attempt.
        self.assertEqual(self.store.claim_research_job("w1", 60, 60)["attempts"], 2)
        shut = self.store.fail_research_job(job_id, "w1", "WORKER_SHUTDOWN", True, 30, 0, None, 600)
        self.assertEqual((shut["status"], shut["attempts"]), ("queued", 1))
        self.assertIsNone(self.store.claim_research_job("w1", 60, 60))  # backoff not yet over
        self.admin.execute("update public.li_research_jobs set not_before = now() where id = %s", (job_id,))
        self.assertEqual(self.store.claim_research_job("w2", 60, 60)["attempts"], 2)
        # A non-owner cannot end it.
        self.assertIsNone(self.store.fail_research_job(job_id, "w1", "UPSTREAM_TIMEOUT", True, 0, 0, None, 600))
        final = self.store.fail_research_job(job_id, "w2", "UPSTREAM_TIMEOUT", True, 0, 0, None, 600)
        self.assertEqual((final["status"], final["attempts"]), ("failed", 2))
        self.assertEqual(self.reservation(rid)[0], "released")
        self.assertIsNone(self.store.claim_research_job("w2", 60, 60))

    def test_finalize_requeue_cap_abandons_settlement_keeping_the_result(self):
        user = self.new_user()
        self.save_run(self.svc, user, "run-cap")
        rid = self.reserve(user, 20)
        job_id = self.enqueue(user, rid, max_attempts=3)["id"]
        checkpoint = {"phase": "result_saved", "run_id": "run-cap", "units": 12, "known_cost_usd": 0.25,
                      "unpriced": ["research_compute"]}
        cap = 2
        for n in range(1, cap + 2):
            claimed = self.store.claim_research_job(f"w{n}", 5, 0)
            self.assertEqual(claimed["id"], job_id)
            self.assertIsNotNone(self.store.heartbeat_research_job(job_id, f"w{n}", 1, 0, checkpoint, 12))
            self.wait_db_past(self.job(job_id)[3])
            self.assertIn(job_id, self.store.reclaim_research_jobs(100, 600, cap))
            status, _a, _o, _l, error, result_run, reclaims, _c = self.job(job_id)
            self.assertEqual(reclaims, n)
            if n <= cap:
                self.assertEqual((status, error), ("queued", None))
                self.assertEqual(self.reservation(rid)[0], "reserved")
        self.assertEqual((status, error, result_run), ("failed", "SETTLEMENT_ABANDONED", "run-cap"))
        r_status, _ru, _au, pending, cost, comps, r_run, held, _j = self.reservation(rid)
        self.assertEqual((r_status, float(pending), float(cost), r_run, held), ("unsettled", 12.0, 0.25, "run-cap", None))
        self.assertEqual(comps, ["research_compute", "settlement_abandoned"])
        self.assertIsNone(self.store.claim_research_job("w9", 60, 60))
        self.assertEqual(self.store.settle_usage(rid), "settled")
        self.assertEqual(self.store.settle_usage(rid), "already_settled")
        self.assertEqual([(float(u), run) for u, run, _ in self.usage_events(rid)], [(12.0, "run-cap")])
        self.assertEqual(self.one("select count(*) from public.li_runs where user_id = %s and run_id = 'run-cap'",
                                  (user,))[0], 1)

    def test_queue_expiry_fails_the_job_and_releases(self):
        user = self.new_user()
        rid = self.reserve(user)
        job_id = self.enqueue(user, rid, ttl=1)["id"]
        self.wait_db_past(self.one("select queue_expires_at from public.li_research_jobs where id = %s",
                                   (job_id,))[0])
        self.assertIsNone(self.store.claim_research_job("w1", 60, 60))
        self.assertIn(job_id, self.store.reclaim_research_jobs(100, 600, 3))
        self.assertEqual(self.job(job_id)[0::4], ("failed", "QUEUE_EXPIRED"))
        self.assertEqual(self.reservation(rid)[0], "released")


# ---------------------------------------------------------------------------
# Founding Free activation boundary
# ---------------------------------------------------------------------------

@pg_test
class FoundingFreeTest(unittest.TestCase):
    def fresh(self, prefix):
        dbname, _ = provision(prefix)
        admin = admin_connect(dbname)
        self.addCleanup(admin.close)
        svc = role_connect(dbname)
        self.addCleanup(svc.close)
        return dbname, admin, PgRpcStore(svc)

    def new_users(self, admin, n):
        rows = admin.execute(
            "insert into auth.users(id, email, email_confirmed_at) "
            "select gen_random_uuid(), 'u' || g || '@example.test', now() from generate_series(1, %s) g "
            "returning id::text", (n,)).fetchall()
        return [r[0] for r in rows]

    def activate_998(self, admin, store):
        for uid in self.new_users(admin, 998):
            store.activate_account(uid, None)
        self.assertEqual(admin.execute("select next_number from public.li_activation_counter").fetchone()[0], 999)

    def entitlement(self, admin, uid):
        return admin.execute("select a.activation_number, e.kind, e.active, e.quota_units_per_week "
                             "from public.li_accounts a join public.li_entitlements e using (user_id) "
                             "where a.user_id = %s", (uid,)).fetchone()

    def test_999_and_1000_are_free_1001_is_paid_required_and_retries_take_no_slot(self):
        _db, admin, store = self.fresh("founding")
        self.activate_998(admin, store)
        u999, u1000, u1001 = self.new_users(admin, 3)
        self.assertEqual(store.activate_account(u999)["activation_number"], 999)
        self.assertEqual(store.activate_account(u1000)["activation_number"], 1000)
        self.assertEqual(store.activate_account(u1000)["activation_number"], 1000)  # retry
        self.assertEqual(admin.execute("select next_number from public.li_activation_counter").fetchone()[0], 1001)
        self.assertEqual(store.activate_account(u1001)["activation_number"], 1001)
        self.assertEqual(store.activate_account(u1001)["activation_number"], 1001)  # retry
        self.assertEqual(tuple(self.entitlement(admin, u999)[:3]), (999, "founding_free", True))
        self.assertEqual(tuple(self.entitlement(admin, u1000)[:3]), (1000, "founding_free", True))
        n, kind, active, quota = self.entitlement(admin, u1001)
        self.assertEqual((n, kind, active, float(quota)), (1001, "paid_required", False, 0.0))
        self.assertEqual(admin.execute("select count(*), max(activation_number) from public.li_accounts").fetchone(),
                         (1001, 1001))
        self.assertEqual(admin.execute("select count(*) from public.li_entitlements where kind = 'founding_free'"
                                       ).fetchone()[0], 1000)
        self.assertFalse(store.reserve_usage(str(uuid.uuid4()), u1001, "investigate", 1))

    def test_concurrent_activations_at_the_boundary_stay_exact(self):
        dbname, admin, store = self.fresh("founding_conc")
        self.activate_998(admin, store)
        a, b, c, d = self.new_users(admin, 4)
        callers = [a, b, c, d, a, b, a, c]
        stores = []
        for _ in callers:
            conn = role_connect(dbname)
            self.addCleanup(conn.close)
            stores.append(PgRpcStore(conn))
        results = run_concurrently([(lambda s=s, u=u: s.activate_account(u)) for s, u in zip(stores, callers)])
        per_user: dict[str, set] = {}
        for uid, row in zip(callers, results):
            per_user.setdefault(uid, set()).add(row["activation_number"])
        self.assertTrue(all(len(v) == 1 for v in per_user.values()), per_user)
        numbers = sorted(next(iter(v)) for v in per_user.values())
        self.assertEqual(numbers, [999, 1000, 1001, 1002])
        self.assertEqual(admin.execute("select next_number from public.li_activation_counter").fetchone()[0], 1003)
        kinds = {uid: self.entitlement(admin, uid) for uid in per_user}
        for uid, (n, kind, active, _q) in kinds.items():
            self.assertEqual(kind, "founding_free" if n <= 1000 else "paid_required")
            self.assertEqual(active, n <= 1000)
        self.assertEqual(admin.execute("select count(*) from public.li_entitlements where kind = 'founding_free'"
                                       ).fetchone()[0], 1000)


# ---------------------------------------------------------------------------
# rate limits
# ---------------------------------------------------------------------------

@pg_test
class RateLimitTest(PgCase):
    def test_keyed_rate_limit_counts_atomically_across_two_connections(self):
        key = sha(f"ip:{uuid.uuid4()}")
        a, b = self.new_store(), self.new_store()

        def burst(store):
            return [store.take_keyed_rate_limit("oauth_token", key, 5, 60) for _ in range(8)]

        out = run_concurrently([lambda: burst(a), lambda: burst(b)])
        flat = out[0] + out[1]
        self.assertEqual(flat.count(0), 5)
        self.assertTrue(all(1 <= w <= 60 for w in flat if w != 0), flat)
        self.assertEqual(self.one("select count(*) from public.li_rate_limit_events where bucket = 'oauth_token' "
                                  "and key_hash = %s", (key,))[0], 5)
        # Other keys and buckets are independent.
        self.assertEqual(self.store.take_keyed_rate_limit("oauth_token", sha("other"), 5, 60), 0)
        self.assertEqual(self.store.take_keyed_rate_limit("activate", key, 5, 60), 0)

    def test_keyed_rate_limit_fails_closed_on_invalid_arguments(self):
        self.assertGreater(self.store.take_keyed_rate_limit("b", "not-a-sha", 5, 60), 0)
        self.assertGreater(self.store.take_keyed_rate_limit("b", sha("x"), 0, 60), 0)
        self.assertGreater(self.store.take_keyed_rate_limit("", sha("x"), 5, 60), 0)
        self.assertEqual(self.one("select count(*) from public.li_rate_limit_events where bucket = 'b'")[0], 0)

    def test_user_rate_limit_counts_atomically_across_two_connections(self):
        user = self.new_user()
        a, b = self.new_store(), self.new_store()
        out = run_concurrently([
            lambda: [a.take_rate_limit(user, "mcp", 5, 60) for _ in range(6)],
            lambda: [b.take_rate_limit(user, "mcp", 5, 60) for _ in range(6)],
        ])
        self.assertEqual((out[0] + out[1]).count(True), 5)


# ---------------------------------------------------------------------------
# tenant boundaries
# ---------------------------------------------------------------------------

@pg_test
class TenantBoundaryTest(PgCase):
    def test_job_and_usage_functions_refuse_another_users_rows(self):
        owner, intruder = self.new_user(), self.new_user()
        rid = str(uuid.uuid4())
        self.assertTrue(self.store.reserve_usage(rid, owner, "investigate", 10))
        # Another user cannot bind the owner's reservation to a job.
        self.assertIsNone(self.store.enqueue_research_job(str(uuid.uuid4()), intruder, None, "investigate", "k",
                                                          {}, rid, 3, 600))
        job = self.store.enqueue_research_job(str(uuid.uuid4()), owner, None, "investigate", "k", {}, rid, 3, 600)
        self.assertTrue(job["created"])
        # The same idempotency key under another user is a different job namespace, not the owner's job.
        rid2 = str(uuid.uuid4())
        self.assertTrue(self.store.reserve_usage(rid2, intruder, "investigate", 10))
        theirs = self.store.enqueue_research_job(str(uuid.uuid4()), intruder, None, "investigate", "k", {}, rid2,
                                                 3, 600)
        self.assertTrue(theirs["created"])
        self.assertNotEqual(theirs["id"], job["id"])
        self.assertIsNone(self.store.request_cancel_research_job(job["id"], intruder))
        self.assertEqual(self.one("select status from public.li_research_jobs where id = %s", (job["id"],))[0],
                         "queued")
        # Quota is per user: the intruder's reservations never consume the owner's quota.
        self.assertTrue(self.store.reserve_usage(str(uuid.uuid4()), owner, "investigate", 480))
        self.assertTrue(self.store.reserve_usage(str(uuid.uuid4()), intruder, "investigate", 480))
        self.admin.execute("delete from public.li_research_jobs where id = any(%s::uuid[])",
                           ([job["id"], theirs["id"]],))

    def test_case_functions_refuse_another_users_case(self):
        owner, intruder = self.new_user(), self.new_user()
        case_id = str(uuid.uuid4())
        h1 = sha(case_id)
        self.assertEqual(self.store.create_case(case_id, owner, "objective", "ready_for_approval", h1, {}, {}), 1)
        self.assertEqual(self.store.revise_case(case_id, intruder, 1, "clarifying", sha("i"), {}, {}), 0)
        approval = {"id": str(uuid.uuid4()), "case_id": case_id, "user_id": intruder, "charter_version": 1,
                    "content_hash": h1, "scope": {}, "budget_usd": 1, "budget_units": 1,
                    "expires_at": (self.db_now() + timedelta(minutes=5)).isoformat(), "token_hash": sha("t")}
        self.assertFalse(self.store.approve_case_charter(approval))
        self.assertEqual(self.one("select count(*) from public.li_case_approvals where case_id = %s",
                                  (case_id,))[0], 0)


# ---------------------------------------------------------------------------
# OAuth one-time codes and Stripe event idempotency
# ---------------------------------------------------------------------------

@pg_test
class OAuthAndBillingTest(PgCase):
    def test_oauth_code_and_refresh_token_are_single_use(self):
        user = self.new_user()
        client = f"client-{uuid.uuid4().hex[:8]}"
        self.svc.execute("insert into public.li_oauth_clients(client_id, client_name, redirect_uris, grant_types, "
                         "response_types, token_endpoint_auth_method) values (%s, 'c', '[]', '[]', '[]', 'none')",
                         (client,))
        code, refresh = sha(f"code:{client}"), sha(f"refresh:{client}")
        self.svc.execute("insert into public.li_oauth_codes(code_hash, user_id, client_id, redirect_uri, "
                         "code_challenge, resource, expires_at) values (%s, %s, %s, 'https://x.test/cb', 'c', 'r', "
                         "now() + interval '5 minutes')", (code, user, client))
        self.svc.execute("insert into public.li_refresh_tokens(token_hash, user_id, client_id, resource, expires_at) "
                         "values (%s, %s, %s, 'r', now() + interval '1 day')", (refresh, user, client))
        a, b = self.new_store(), self.new_store()
        got = run_concurrently([lambda: a.consume_oauth_code(code), lambda: b.consume_oauth_code(code)])
        self.assertEqual(sum(1 for g in got if g), 1)
        self.assertIsNone(self.store.consume_oauth_code(code))
        self.assertEqual(self.store.consume_refresh_token(refresh)["user_id"], user)
        self.assertIsNone(self.store.consume_refresh_token(refresh))

    def test_stripe_event_applies_exactly_once(self):
        user = self.new_user()
        args = dict(event_id=f"evt_{uuid.uuid4().hex}", event_type="customer.subscription.updated",
                    payload_hash=sha("p"), user_id=user, customer_id="cus_test", subscription_id="sub_test",
                    active=True, plan_id="pro", quota_units_per_week=5000)
        first = self.store.apply_stripe_entitlement_event(**args)
        second = self.store.apply_stripe_entitlement_event(**args)
        self.assertEqual([bool(first), bool(second)], [True, False])
        kind, quota = self.one("select kind, quota_units_per_week from public.li_entitlements where user_id = %s",
                               (user,))
        self.assertEqual((kind, float(quota)), ("paid", 5000.0))



# ---------------------------------------------------------------------------
# Q18 owner review: account deletion racing a research worker
# ---------------------------------------------------------------------------

USER_TABLES_SQL = r"""
select c.table_name::text from information_schema.columns c
 where c.table_schema = 'public' and c.table_name like 'li\_%' and c.column_name = 'user_id'
 order by 1
"""


def wait_until_lock_waiting(admin, pid: int, limit: float = 15.0) -> None:
    """Block until backend `pid` is waiting on a heavyweight lock (deterministic interleaving)."""
    deadline = time.monotonic() + limit
    while time.monotonic() < deadline:
        row = admin.execute("select wait_event_type from pg_stat_activity where pid = %s", (pid,)).fetchone()
        if row and row[0] == "Lock":
            return
        time.sleep(0.05)
    raise AssertionError(f"backend {pid} never waited on a lock")


@pg_test
class AccountDeletionRaceTest(PgCase):
    """A worker that is mid-job when deletion starts recreates nothing after the cascade.

    The worker, the reclaimer, the deletion and the identity removal each use their own
    connection. Every refusal is checked on the worker's own restricted service_role
    session, and the proof is the final table state: no row of any li_ table keyed by
    user_id names the deleted user.
    """

    @classmethod
    def setUpClass(cls):
        # Claims and reclaims are global: this class gets its own clean database.
        cls.dbname, _ = provision("deletion")

    def user_rows(self, user):
        tables = [r[0] for r in self.admin.execute(USER_TABLES_SQL).fetchall()]
        self.assertGreaterEqual(len(tables), 17)
        for table in ("li_runs", "li_research_jobs", "li_usage_reservations", "li_usage_events", "li_artifacts",
                      "li_discoveries", "li_cases", "li_case_events"):
            self.assertIn(table, tables)
        counts = {}
        for table in tables:
            n = self.admin.execute(pgsql.SQL("select count(*) from {} where user_id = %s").format(
                pgsql.Identifier("public", table)), (user,)).fetchone()[0]
            if n:
                counts[table] = n
        return counts

    def delete_identity(self, user):
        """What Supabase Auth's admin delete does: remove auth.users (the cascade does the rest)."""
        self.admin.execute("delete from auth.users where id = %s", (user,))

    def test_a_worker_whose_job_ended_under_it_recreates_nothing_after_deletion(self):
        user = self.new_user()
        rid = str(uuid.uuid4())
        self.assertTrue(self.store.reserve_usage(rid, user, "investigate", 20))
        job_id = self.store.enqueue_research_job(str(uuid.uuid4()), user, None, "investigate", "zombie",
                                                 {"objective": "x"}, rid, 1, 600)["id"]
        worker = self.new_store()       # the worker's own connection
        reclaimer = self.new_store()    # another worker's housekeeping
        deleter = self.new_store()      # PublicService.delete_account's RPCs
        self.assertEqual(worker.claim_research_job("w1", 1, 0)["id"], job_id)
        self.assertIsNotNone(worker.heartbeat_research_job(job_id, "w1", 1, 0,
                                                           {"phase": "running", "known_cost_usd": 0.01}, 3))
        lease = self.one("select lease_expires_at from public.li_research_jobs where id = %s", (job_id,))[0]
        self.wait_db_past(lease)
        # The worker is stalled mid-research; its lease expires and the job ends without it.
        self.assertIn(job_id, reclaimer.reclaim_research_jobs(100, 600, 3))
        self.assertEqual(self.one("select status, error_code from public.li_research_jobs where id = %s",
                                  (job_id,)), ("failed", "LEASE_EXPIRED"))
        # Deletion: no open job, the marker is settled, then the identity and the cascade.
        self.assertEqual(deleter.request_cancel_research_job(job_id, user)["status"], "failed")
        self.assertEqual(deleter.settle_usage(rid), "settled")
        self.delete_identity(user)
        self.assertEqual(self.user_rows(user), {})

        # The worker wakes up and tries to finish: every write is refused, every job RPC finds nothing.
        with self.assertRaises(pg_errors.ForeignKeyViolation):
            self.save_run(worker.conn, user, "run-zombie")
        with self.assertRaises(pg_errors.ForeignKeyViolation):
            worker.conn.execute("insert into public.li_usage_events(id, user_id, run_id, operation, units) "
                                "values (%s, %s, null, 'investigate', 5)", (str(uuid.uuid4()), user))
        self.assertIsNone(worker.heartbeat_research_job(job_id, "w1", 60, 0, {"phase": "result_saved"}, 5))
        self.assertIsNone(worker.complete_research_job(job_id, "w1", "run-zombie", 5))
        self.assertIsNone(worker.fail_research_job(job_id, "w1", "RESULT_NOT_SAVED", False, 0, 5,
                                                   {"phase": "finalizing"}, 600))
        self.assertFalse(worker.finalize_usage(rid, "run-zombie", 5, 0.01, []))
        self.assertFalse(worker.mark_usage_unsettled(rid, "run-zombie", 5, 0.01, ["partial_run"]))
        self.assertEqual(worker.settle_usage(rid), "missing")
        self.assertFalse(worker.reserve_usage(str(uuid.uuid4()), user, "investigate", 1))
        try:
            again = worker.enqueue_research_job(str(uuid.uuid4()), user, None, "investigate", "zombie-2",
                                                {"objective": "x"}, rid, 1, 600)
        except StoreError:
            again = None
        self.assertIsNone(again)
        # Final state, read on an independent admin connection.
        self.assertEqual(self.user_rows(user), {})
        self.assertIsNone(self.one("select 1 from auth.users where id = %s", (user,)))

    def test_a_result_insert_that_wins_the_race_is_removed_by_the_cascade(self):
        user = self.new_user()
        worker = role_connect(self.dbname)
        self.addCleanup(worker.close)
        deleter = admin_connect(self.dbname)
        self.addCleanup(deleter.close)
        failures = []
        with worker.transaction():
            self.save_run(worker, user, "run-first")  # uncommitted: holds a key-share lock on auth.users

            def delete():
                try:
                    deleter.execute("delete from auth.users where id = %s", (user,))
                except BaseException as exc:  # noqa: BLE001 - reported below
                    failures.append(exc)

            t = threading.Thread(target=delete)
            t.start()
            wait_until_lock_waiting(self.admin, deleter.info.backend_pid)  # the delete waits for the insert
        t.join(30)
        self.assertFalse(t.is_alive())
        self.assertEqual(failures, [])
        self.assertEqual(self.user_rows(user), {})
        self.assertIsNone(self.one("select 1 from auth.users where id = %s", (user,)))

    def test_a_result_insert_that_loses_the_race_is_refused(self):
        user = self.new_user()
        worker = role_connect(self.dbname)
        self.addCleanup(worker.close)
        deleter = admin_connect(self.dbname)
        self.addCleanup(deleter.close)
        outcome = []
        with deleter.transaction():
            deleter.execute("delete from auth.users where id = %s", (user,))  # uncommitted

            def insert():
                try:
                    self.save_run(worker, user, "run-late")
                    outcome.append("inserted")
                except pg_errors.ForeignKeyViolation:
                    outcome.append("refused")
                except BaseException as exc:  # noqa: BLE001 - reported below
                    outcome.append(exc)

            t = threading.Thread(target=insert)
            t.start()
            wait_until_lock_waiting(self.admin, worker.info.backend_pid)  # the insert waits for the delete
        t.join(30)
        self.assertFalse(t.is_alive())
        self.assertEqual(outcome, ["refused"])
        self.assertEqual(self.user_rows(user), {})
        self.assertIsNone(self.one("select 1 from auth.users where id = %s", (user,)))


if __name__ == "__main__":
    unittest.main()
