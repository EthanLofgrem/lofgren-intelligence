"""Isolated execution and recovery proof on a REAL PostgreSQL 16 server (evidence type: POSTGRES).

The real PublicService and the real Worker run against the migrated schema through the real
SupabaseStore. tests/pg/test_migrations_pg.py's PgRpcStore already replaces only the HTTP RPC
transport (SQL function called as service_role through authenticator); PgRestStore below also
replaces the PostgREST table transport (_table) with the equivalent SQL, run as service_role, one
transaction per call. Nothing else in the store or the service is replaced.

Like the migration proof, every test here is SKIPPED without LI_PG_DSN, which is not a pass, and
the postgres-proof CI job sets LI_PG_REQUIRED=1 so a missing DSN or psycopg is an error and any
skip fails the job. Research uses the deterministic heuristic provider over inline texts: no
network, provider, Stripe or Supabase call is made and no external effect is performed.

Proof map:
  R1 approved Case Charter before a case job ......... test_r1_*
  R2 claim with a lease .............................. test_r2_*
  R3 saved checkpoint ................................ test_r3_*
  R4 response lost after commit, retry, one charge ... test_r4_*
  R5 competing workers, exactly one wins ............. test_r5_*
  R6 cancellation .................................... test_r6_*
  R7 lease expiry then reclaim by another worker ..... test_r7_*
  R8 actual usage settlement (release / keep cost) ... test_r8_*
  R9 reopen a saved result after a restart ........... test_r9_*
  V4 external execution refused, no effect ........... test_v4_*

The V4 proof builds a real approved action through the real store (job -> run -> discovery ->
artifact -> proposal -> browser approval, all persisted in PostgreSQL) and shows execute_action
refuses it with LI_EXTERNAL_EXECUTION_ENABLED unset or "false": no executor, adapter or HTTP call,
and the stored action row is left exactly as approved.

What this does not prove: PostgREST's own HTTP/JSON mapping and JWT role switch, Supabase hosted
auth, and a hosted worker process or real SIGTERM delivery (see tests/test_worker_hardening.py
for the local-process shutdown proof).
"""

from __future__ import annotations

import os
import re
import unittest
import uuid
from unittest.mock import patch

from tests.pg.test_migrations_pg import (
    DSN,
    SKIP_REASON,
    PgCase,
    PgRpcStore,
    StoreError,
    provision,
    psycopg,
    role_connect,
    run_concurrently,
    tearDownModule as _drop_created_databases,
)

from lofgren_intelligence.discovery.fixtures import warehouse_design
from lofgren_intelligence.hosted import service as service_module
from lofgren_intelligence.hosted.jobs import JobLease, JobSettings, LeaseLost
from lofgren_intelligence.hosted.service import CaseApprovalRequired, PublicService, PublicServiceError
from tests.helpers import TEXTS as V4_TEXTS
from lofgren_intelligence.hosted.worker import Worker

# psycopg is optional outside postgres-proof (the default suite only loads this module by name).
if psycopg is not None:
    import psycopg.types.json  # noqa: F401 - Jsonb
    from psycopg import sql as pgsql
else:  # pragma: no cover - depends on the environment
    pgsql = None

pg_test = unittest.skipUnless(DSN, SKIP_REASON)


def tearDownModule():  # noqa: N802 - unittest hook
    _drop_created_databases()


BASE = "https://li.example"
RUN_ENV = {
    "LOFGREN_PROVIDER": "heuristic",
    "LI_INFRA_USD_PER_RUN": "0",
    "LI_RETRIEVAL_USD_PER_CALL": "0",
    "LI_PUBLIC_BASE_URL": BASE,
    "LI_EXTERNAL_EXECUTION_ENABLED": "false",
}
CLEARED_ENV = ("LI_CASE_DEFAULT_MAX_SPEND_USD", "LI_CASE_DEFAULT_MAX_UNITS", "LI_CASE_APPROVAL_TTL_MINUTES",
               "LI_SYNC_MAX_WORK_UNITS", "LI_SYNC_MAX_NETWORK_TASKS", "LI_RUNTIME_PLAN")
OBJECTIVE = "Design a new low-cost water purification system for remote communities."
EVIDENCE = {
    "field report (fictional)": (
        "Slow sand filtration in remote communities reduced microbial contamination in 2025. "
        "A village slow sand filter served about 100 people and produced 600 liters per day."
    ),
    "cost survey (fictional)": (
        "Installed cost of a village slow sand filter was 450 dollars in 2025 using locally serviceable parts."
    ),
}
ANSWERS = {
    "location": "Rural northern Kenya",
    "users": "A village system serving about 100 people",
    "problem": "Microbial contamination and turbidity",
    "success": "At least 500 liters/day for prototype testing",
    "cost": "Maximum installed prototype cost $500",
    "constraints": "No reliable grid power; locally serviceable parts",
    "source_or_environment": "Seasonal surface water and shallow wells",
}
# Heartbeat threads never fire (stage boundaries beat); leases are real database time.
SETTINGS = JobSettings(lease_seconds=60, heartbeat_seconds=3600.0, hold_grace_seconds=60, max_attempts=3,
                       queue_ttl_seconds=3600, backoff_base_seconds=0, backoff_max_seconds=0,
                       concurrency=1, poll_seconds=0.1)
SHORT_LEASE = JobSettings(**{**SETTINGS.__dict__, "lease_seconds": 5})
REAL_RUN = service_module.run_investigation


class Crash(BaseException):
    """The worker process dies (not an Exception: nothing in the worker may catch it)."""


def at_stage(stage, action):
    """Run `action(ledger)` at the first `stage` boundary of the research core."""
    seen = {"n": 0}

    def run(contract, registry, provider, plan_id, approved=False, ledger=None, stage_hook=None):
        def hook(name):
            if name == stage:
                seen["n"] += 1
                if seen["n"] == 1:
                    action(ledger)
            if stage_hook is not None:
                stage_hook(name)
        return REAL_RUN(contract, registry, provider, plan_id, approved=approved, ledger=ledger, stage_hook=hook)

    return patch.object(service_module, "run_investigation", run)


def counting_run(calls):
    def run(contract, registry, provider, plan_id, approved=False, ledger=None, stage_hook=None):
        calls.append(contract.objective)
        return REAL_RUN(contract, registry, provider, plan_id, approved=approved, ledger=ledger,
                        stage_hook=stage_hook)
    return patch.object(service_module, "run_investigation", run)


# ---------------------------------------------------------------------------
# PostgREST-like table access through the real SupabaseStore
# ---------------------------------------------------------------------------

_IDENT = re.compile(r"^[a-z_][a-z0-9_]*$")
_OPS = {"eq": "=", "neq": "<>", "lt": "<", "lte": "<=", "gt": ">", "gte": ">="}
_COLUMNS_SQL = ("select a.attname::text, format_type(a.atttypid, a.atttypmod) from pg_attribute a "
                "where a.attrelid = %s::regclass and a.attnum > 0 and not a.attisdropped")
_PK_SQL = ("select a.attname::text from pg_index i join pg_attribute a "
           "on a.attrelid = i.indrelid and a.attnum = any(i.indkey) where i.indrelid = %s::regclass and i.indisprimary")


class PgRestStore(PgRpcStore):
    """PgRpcStore plus the store's PostgREST table calls (select/insert/upsert/update) as direct SQL.

    Supports exactly the query shapes hosted/store.py sends: select=*, column filters
    eq/neq/lt/lte/gt/gte/is, order=col.asc|desc[,...], limit and offset; POST (optionally
    resolution=merge-duplicates on the primary key) and PATCH with filters. Any other shape fails
    the test instead of being guessed. Runs as service_role, one transaction per call.
    """

    def __init__(self, conn):
        super().__init__(conn)
        self._columns: dict[str, dict[str, str]] = {}
        self._pkeys: dict[str, list[str]] = {}

    def _meta(self, table):
        if not _IDENT.match(table) or not table.startswith("li_"):
            raise AssertionError(f"unexpected table {table!r}")
        if table not in self._columns:
            self._columns[table] = dict(self.conn.execute(_COLUMNS_SQL, (f"public.{table}",)).fetchall())
            self._pkeys[table] = [r[0] for r in self.conn.execute(_PK_SQL, (f"public.{table}",)).fetchall()]
        return self._columns[table], self._pkeys[table]

    @staticmethod
    def _col(cols, name):
        if name not in cols:
            raise AssertionError(f"unknown column {name!r}")
        return pgsql.Identifier(name)

    def _where(self, cols, filters):
        parts, params = [], []
        for name, expr in filters.items():
            ident = self._col(cols, name)
            op, _, value = str(expr).partition(".")
            if op == "is" and value in ("null", "true", "false"):
                parts.append(pgsql.SQL("{} is " + value).format(ident))
            elif op in _OPS:
                parts.append(pgsql.SQL("{} " + _OPS[op] + " {}::{}").format(
                    ident, pgsql.Placeholder(), pgsql.SQL(cols[name])))
                params.append(value)
            else:
                raise AssertionError(f"unsupported filter {name}={expr!r}")
        where = pgsql.SQL(" where ") + pgsql.SQL(" and ").join(parts) if parts else pgsql.SQL("")
        return where, params

    def _table(self, table, method="GET", *, query=None, body=None, prefer=None):
        self.calls.append(f"{method} {table}")
        cols, pkey = self._meta(table)
        query = dict(query or {})
        target = pgsql.SQL("public.{}").format(pgsql.Identifier(table))
        try:
            if method == "GET":
                if query.pop("select", "*") != "*":
                    raise AssertionError("only select=* is used by the store")
                order, limit, offset = query.pop("order", None), query.pop("limit", None), query.pop("offset", None)
                where, params = self._where(cols, query)
                sql = pgsql.SQL("select to_jsonb(t) from {} t").format(target) + where
                if order:
                    keys = []
                    for item in order.split(","):
                        name, _, direction = item.partition(".")
                        if direction not in ("asc", "desc"):
                            raise AssertionError(f"unsupported order {order!r}")
                        keys.append(pgsql.SQL("{} " + direction).format(self._col(cols, name)))
                    sql += pgsql.SQL(" order by ") + pgsql.SQL(", ").join(keys)
                if limit is not None:
                    sql += pgsql.SQL(" limit {}").format(pgsql.Literal(int(limit)))
                if offset is not None:
                    sql += pgsql.SQL(" offset {}").format(pgsql.Literal(int(offset)))
                return [r[0] for r in self.conn.execute(sql, params).fetchall()]
            if method == "POST":
                if query:
                    raise AssertionError("POST filters are not used by the store")
                merge = "resolution=merge-duplicates" in (prefer or "")
                for row in body if isinstance(body, list) else [body]:
                    names = list(row)
                    idents = pgsql.SQL(", ").join(self._col(cols, n) for n in names)
                    sql = pgsql.SQL("insert into {} ({}) select {} from jsonb_populate_record(null::{}, %s::jsonb)"
                                    ).format(target, idents, idents, target)
                    if merge:
                        rest = [n for n in names if n not in pkey]
                        conflict = pgsql.SQL(", ").join(pgsql.Identifier(k) for k in pkey)
                        if rest:
                            sets = pgsql.SQL(", ").join(
                                pgsql.SQL("{} = excluded.{}").format(pgsql.Identifier(n), pgsql.Identifier(n))
                                for n in rest)
                            sql += pgsql.SQL(" on conflict ({}) do update set {}").format(conflict, sets)
                        else:
                            sql += pgsql.SQL(" on conflict ({}) do nothing").format(conflict)
                    self.conn.execute(sql, [psycopg.types.json.Jsonb(row)])
                return None
            if method == "PATCH":
                where, params = self._where(cols, query)
                if not params and "is" not in str(query):
                    raise AssertionError("an unfiltered PATCH is never sent by the store")
                sets = pgsql.SQL(", ").join(
                    pgsql.SQL("{} = (select r.{} from jsonb_populate_record(null::{}, %s::jsonb) r)").format(
                        self._col(cols, n), pgsql.Identifier(n), target) for n in body)
                sql = pgsql.SQL("update {} set ").format(target) + sets + where
                self.conn.execute(sql, [psycopg.types.json.Jsonb(body)] * len(body) + params)
                return None
        except psycopg.Error as exc:
            raise StoreError(f"table {method} {table} failed: {exc.sqlstate}: {exc}") from exc
        raise AssertionError(f"unsupported method {method}")


# ---------------------------------------------------------------------------
# the proof
# ---------------------------------------------------------------------------

@pg_test
class RecoveryPgTest(PgCase):
    @classmethod
    def setUpClass(cls):
        # Claims and reclaims are global: this proof gets its own clean database.
        cls.dbname, _ = provision("recovery")

    def setUp(self):
        super().setUp()
        env = patch.dict(os.environ, RUN_ENV, clear=False)
        env.start()
        self.addCleanup(env.stop)
        for name in CLEARED_ENV:
            os.environ.pop(name, None)
        self.admin.execute("delete from public.li_research_jobs")
        self.store = PgRestStore(self.svc)
        self.service = self.new_service(self.store)
        self.user = self.new_user()
        # Case plans are larger than the 500-unit founding default; the quota is not under test here.
        self.admin.execute("update public.li_entitlements set quota_units_per_week = 100000 where user_id = %s",
                           (self.user,))

    # ---- helpers ---------------------------------------------------------------------

    def new_rest_store(self):
        conn = role_connect(self.dbname)
        self.addCleanup(conn.close)
        return PgRestStore(conn)

    @staticmethod
    def new_service(store, settings=SETTINGS):
        service = PublicService(store)
        service.job_settings = settings
        return service

    def worker(self, name, settings=SETTINGS):
        return Worker(self.new_rest_store(), settings, worker_id=name)

    def start(self, key, settings=SETTINGS):
        service = self.new_service(self.store, settings)
        return service.start_research(self.user, {"objective": "Is industrial construction increasing?",
                                                  "texts": dict(EVIDENCE), "idempotency_key": key}, BASE)

    def job_row(self, job_id):
        return self.one("select status, attempts, lease_owner, lease_expires_at, error_code, result_run_id, "
                        "cost_so_far, checkpoint, reservation_id, case_id, input from public.li_research_jobs "
                        "where id = %s", (job_id,))

    def counts(self):
        """(runs, usage events) for this test's user, read on the admin connection."""
        return (self.one("select count(*) from public.li_runs where user_id = %s", (self.user,))[0],
                self.one("select count(*) from public.li_usage_events where user_id = %s", (self.user,))[0])

    def event(self, rid):
        return self.one("select units, run_id, known_cost_usd, unpriced_components from public.li_usage_events "
                        "where id = %s", (rid,))

    # ---- R1 ----------------------------------------------------------------------------

    def test_r1_case_job_requires_a_server_recorded_approval_of_the_latest_charter(self):
        case = self.service.clarify_objective(
            self.user, {"objective": OBJECTIVE, "answers": dict(ANSWERS), "texts": dict(EVIDENCE)}, BASE)
        args = {"objective": OBJECTIVE, "case_id": case["case_id"], "idempotency_key": "case-key"}
        with self.assertRaises(CaseApprovalRequired):
            self.service.start_research(self.user, dict(args), BASE)
        self.assertEqual(self.one("select count(*) from public.li_research_jobs where user_id = %s",
                                  (self.user,))[0], 0)
        self.assertEqual(self.one("select count(*) from public.li_usage_reservations where user_id = %s",
                                  (self.user,))[0], 0)
        self.service.approve_case_charter(self.user, case["case_id"], case["charter_version"], case["content_hash"])
        out = self.service.start_research(self.user, dict(args), BASE)
        self.assertTrue(out["created"])
        row = self.job_row(out["job_id"])
        self.assertEqual((str(row[9]), row[10]["execution_inputs"]), (case["case_id"], "approved_charter"))
        self.assertEqual(row[10]["case"]["charter_version"], case["charter_version"])
        consumed = self.one("select consumed_at is not null, idempotency_key from public.li_case_approvals "
                            "where case_id = %s", (case["case_id"],))
        self.assertEqual(consumed, (True, "job:case-key"))
        # The same key is the same job; nothing else may start from the consumed approval.
        self.assertEqual(self.service.start_research(self.user, dict(args), BASE)["job_id"], out["job_id"])
        self.assertEqual(self.one("select count(*) from public.li_research_jobs where user_id = %s",
                                  (self.user,))[0], 1)

    # ---- R2 ----------------------------------------------------------------------------

    def test_r2_claim_takes_a_time_bounded_lease_held_by_one_worker(self):
        job_id = self.start("lease")["job_id"]
        claimed = self.new_rest_store().claim_research_job("w1", 60, 60)
        self.assertEqual(claimed["id"], job_id)
        status, attempts, owner, lease, *_ = self.job_row(job_id)
        self.assertEqual((status, attempts, owner), ("running", 1, "w1"))
        remaining = (lease - self.db_now()).total_seconds()
        self.assertTrue(50 < remaining <= 60, remaining)
        self.assertIsNone(self.new_rest_store().claim_research_job("w2", 60, 60))
        self.assertIsNone(self.new_rest_store().heartbeat_research_job(job_id, "w2", 60, 60))
        self.assertEqual(self.job_row(job_id)[2], "w1")

    # ---- R3 ----------------------------------------------------------------------------

    def test_r3_stage_checkpoints_are_persisted_and_survive_a_worker_crash(self):
        job_id = self.start("checkpoint")["job_id"]
        crashed = self.worker("w1")
        with at_stage("verify", lambda _l: (_ for _ in ()).throw(Crash())), self.assertRaises(Crash):
            crashed.run_once()
        status, _a, owner, _l, _e, run_id, cost, checkpoint, *_ = self.job_row(job_id)
        self.assertEqual((status, owner, run_id), ("running", "w1", None))
        self.assertEqual((checkpoint["phase"], checkpoint["attempt"]), ("running", 1))
        self.assertIn(checkpoint["stage"], {"sense", "verify"})
        self.assertGreater(float(cost), 0)
        self.assertEqual(self.counts(), (0, 0))

    # ---- R4 ----------------------------------------------------------------------------

    def test_r4_result_and_settlement_responses_lost_after_commit_never_rerun_or_double_charge(self):
        first = self.start("lost-response")
        self.assertEqual(self.start("lost-response")["job_id"], first["job_id"])  # client retry, same job
        job_id = first["job_id"]
        w1 = self.worker("w1", SHORT_LEASE)
        save = w1.store.save_research_job_result

        def save_then_lose(*a):
            save(*a)
            raise StoreError("database is unreachable")  # committed; the answer never arrived

        calls = []
        with counting_run(calls), patch.object(w1.store, "save_research_job_result", save_then_lose):
            self.assertEqual(w1.run_once()["jobs"][0]["status"], "persistence_unknown")
        self.assertEqual(self.job_row(job_id)[7]["phase"], "result_saved")
        self.assertEqual(self.counts(), (1, 0))
        self.wait_db_past(self.job_row(job_id)[3])

        w2 = self.worker("w2")
        finalize = w2.store.finalize_usage

        def finalize_then_lose(*a):
            finalize(*a)
            raise StoreError("database is unreachable")

        with counting_run(calls), patch.object(w2.store, "finalize_usage", finalize_then_lose):
            self.assertEqual([r["status"] for r in w2.run_once()["jobs"]], ["succeeded"])
        self.assertEqual(len(calls), 1, "research ran exactly once")
        self.new_service(self.new_rest_store()).reconcile_unsettled_usage()
        status, attempts, _o, _l, _e, run_id, cost, *_rest = self.job_row(job_id)
        rid = self.job_row(job_id)[8]
        self.assertEqual((status, attempts), ("succeeded", 2))
        self.assertEqual(self.counts(), (1, 1))
        self.assertEqual(self.reservation(rid)[0], "settled")
        units, event_run, _k, _u = self.event(rid)
        self.assertEqual((float(units), event_run), (float(cost), run_id))

    # ---- R5 ----------------------------------------------------------------------------

    def test_r5_two_competing_workers_exactly_one_runs_the_job(self):
        job_id = self.start("race")["job_id"]
        w1, w2 = self.worker("w1"), self.worker("w2")
        calls = []
        with counting_run(calls):
            outcomes = run_concurrently([lambda: w1.run_once()["jobs"], lambda: w2.run_once()["jobs"]])
        self.assertEqual(sorted(len(o) for o in outcomes), [0, 1], outcomes)
        self.assertEqual(next(o for o in outcomes if o)[0]["status"], "succeeded")
        self.assertEqual(len(calls), 1)
        status, attempts, *_ = self.job_row(job_id)
        self.assertEqual((status, attempts), ("succeeded", 1))
        self.assertEqual(self.counts(), (1, 1))

    # ---- R6 ----------------------------------------------------------------------------

    def test_r6_cancel_queued_releases_and_cancel_running_stops_at_the_next_stage(self):
        queued = self.start("cancel-queued")["job_id"]
        self.assertEqual(self.service.cancel_research(self.user, {"job_id": queued})["status"], "cancelled")
        self.assertEqual(self.job_row(queued)[0], "cancelled")
        self.assertEqual(self.reservation(self.job_row(queued)[8])[0], "released")

        running = self.start("cancel-running")["job_id"]
        effect = {}

        def cancel(_ledger):
            effect["out"] = self.service.cancel_research(self.user, {"job_id": running})["cancel_effect"]

        with at_stage("verify", cancel):
            results = self.worker("w1").run_once()["jobs"]
        self.assertEqual(effect["out"], "will_stop_at_next_stage")
        self.assertEqual([r["status"] for r in results], ["cancelled"])
        status, _a, _o, _l, error, run_id, cost, _c, rid, *_ = self.job_row(running)
        self.assertEqual((status, error, run_id), ("cancelled", "CANCELLED", None))
        self.assertEqual(self.reservation(rid)[0], "settled")
        units, event_run, _k, unpriced = self.event(rid)
        self.assertGreater(float(units), 0)
        self.assertEqual((float(units), event_run), (float(cost), None))
        self.assertIn("partial_run", unpriced)
        self.assertEqual(self.counts(), (0, 1))

    # ---- R7 ----------------------------------------------------------------------------

    def test_r7_expired_lease_is_reclaimed_by_another_worker_and_the_old_one_is_fenced(self):
        job_id = self.start("reclaim")["job_id"]
        w1 = self.worker("w1", SHORT_LEASE)
        with at_stage("verify", lambda _l: (_ for _ in ()).throw(Crash())), self.assertRaises(Crash):
            w1.run_once()
        self.assertEqual(self.new_rest_store().reclaim_research_jobs(100, 3600, 3), [],
                         "a live lease is never reclaimed")
        self.wait_db_past(self.job_row(job_id)[3])
        w2 = self.worker("w2")
        results = w2.run_once()
        self.assertIn(job_id, results["reclaimed"])
        self.assertEqual([r["status"] for r in results["jobs"]], ["succeeded"])
        status, attempts, owner, *_ = self.job_row(job_id)
        self.assertEqual((status, attempts, owner), ("succeeded", 2, None))
        self.assertEqual(self.counts(), (1, 1))
        with self.assertRaises(LeaseLost):
            JobLease(w1.store, {"id": job_id}, "w1", SHORT_LEASE).beat()
        self.assertIsNone(w1.store.complete_research_job(job_id, "w1", "run-x", 1.0))
        self.assertEqual(self.counts(), (1, 1))

    # ---- R8 ----------------------------------------------------------------------------

    def test_r8_refusal_before_work_releases_the_reservation(self):
        job_id = self.start("refused")["job_id"]
        self.admin.execute("update public.li_research_jobs set input = jsonb_set(input, '{schema}', '\"tampered\"') "
                           "where id = %s", (job_id,))
        with patch.object(service_module, "run_investigation", side_effect=AssertionError("no work may run")):
            results = self.worker("w1").run_once()["jobs"]
        self.assertEqual(results[0]["status"], "failed")
        status, _a, _o, _l, error, *_rest = self.job_row(job_id)
        self.assertEqual(status, "failed")
        self.assertEqual(self.reservation(self.job_row(job_id)[8])[0], "released")
        self.assertEqual(self.counts(), (0, 0))

    def test_r8_failure_after_work_keeps_the_incurred_cost_and_marks_it_not_fully_priced(self):
        once = JobSettings(**{**SETTINGS.__dict__, "max_attempts": 1})
        job_id = self.start("fails-late", once)["job_id"]

        def boom(_ledger):
            raise RuntimeError("provider failed after work")

        with at_stage("verify", boom):
            results = self.worker("w1", once).run_once()["jobs"]
        self.assertEqual((results[0]["status"], results[0]["error_code"]), ("failed", "EXECUTION_FAILED"))
        status, _a, _o, _l, error, run_id, cost, _c, rid, *_ = self.job_row(job_id)
        self.assertEqual((status, error, run_id), ("failed", "EXECUTION_FAILED", None))
        self.assertEqual(self.reservation(rid)[0], "settled")
        units, event_run, _known, unpriced = self.event(rid)
        self.assertGreater(float(units), 0, "incurred work is charged, never recorded as 0")
        self.assertEqual(float(units), float(cost))
        self.assertIn("partial_run", unpriced, "an unknown cost component is declared unpriced, never priced at 0")
        self.assertEqual(self.counts(), (0, 1))

    # ---- R9 ----------------------------------------------------------------------------

    def test_r9_saved_result_reopens_identically_after_a_restart(self):
        job_id = self.start("reopen")["job_id"]
        self.assertEqual([r["status"] for r in self.worker("w1").run_once()["jobs"]], ["succeeded"])
        before = self.service.get_job_result(self.user, {"job_id": job_id})
        self.assertTrue(before["receipt"]["intact"])
        # Restart: drop every connection and in-memory object, then a new store, service and worker.
        self.svc.close()
        fresh = self.new_rest_store()
        restarted = self.new_service(fresh)
        with patch.object(service_module, "run_investigation", side_effect=AssertionError("reran")):
            self.assertEqual(Worker(self.new_rest_store(), SETTINGS, worker_id="w-restart").run_once()["jobs"], [])
            after = restarted.get_job_result(self.user, {"job_id": job_id})
            status = restarted.get_job_status(self.user, {"job_id": job_id})
        self.assertTrue(after["result_available"])
        self.assertTrue(after["receipt"]["intact"])
        for key in ("status", "run_id", "report", "findings", "receipt"):
            self.assertEqual(after[key], before[key], key)
        self.assertEqual((status["status"], status["run_id"]), ("succeeded", before["run_id"]))
        self.assertEqual(self.counts(), (1, 1))
        self.assertEqual(self.one("select result_run_id from public.li_research_jobs where id = %s",
                                  (job_id,))[0], before["run_id"])

    # ---- V4 -----------------------------------------------------------------------------

    def test_v4_external_execution_is_refused_through_the_real_store_and_performs_no_effect(self):
        no_network = patch("urllib.request.urlopen", side_effect=AssertionError("no HTTP call may be made"))
        no_network.start()
        self.addCleanup(no_network.stop)
        os.environ["LI_PUBLIC_MAX_DISCOVERY_UNITS"] = "100"
        # A real approved action, every step persisted in PostgreSQL through the real store.
        job_id = self.new_service(self.store).start_research(self.user, {
            "objective": "Is industrial construction in the Phoenix metro increasing?",
            "texts": dict(V4_TEXTS), "idempotency_key": "v4"}, BASE)["job_id"]
        self.assertEqual([r["status"] for r in self.worker("w1").run_once()["jobs"]], ["succeeded"])
        run_id = self.job_row(job_id)[5]
        discovery = self.service.discover(self.user, {"run_id": run_id, "objective": "Choose a fictional warehouse size",
                                                      "design": warehouse_design()})
        built = self.service.build_artifact(self.user, {"discovery_id": discovery["discovery_id"],
                                                        "kind": "structured_bundle"})
        with patch.object(service_module.HTTPSWebhookAdapter, "preflight", return_value={"ready": True}):
            proposed = self.service.propose_action(self.user, {
                "artifact_id": built["artifact_id"], "target": "https://example.com/hook",
                "payload": {"hello": "world"}, "cost_usd": 0}, BASE)
        action_id = proposed["action_id"]
        self.assertEqual(self.service.approve_action(self.user, action_id)["status"], "approved")
        row_sql = ("select status, receipt, v5_handoff, executed_at, grant_record, approval_record "
                   "from public.li_action_proposals where user_id = %s and action_id = %s")
        approved = self.one(row_sql, (self.user, action_id))
        self.assertEqual(approved[0], "approved")
        self.assertIsNotNone(approved[4])
        self.assertIsNotNone(approved[5])
        counts_before = self.counts()

        for setting in (None, "false"):
            with self.subTest(LI_EXTERNAL_EXECUTION_ENABLED=setting),                     patch.dict(os.environ, {}, clear=False),                     patch.object(service_module, "execute_authorized") as execute,                     patch.object(service_module, "HTTPSWebhookAdapter") as adapter:
                if setting is None:
                    os.environ.pop("LI_EXTERNAL_EXECUTION_ENABLED", None)
                else:
                    os.environ["LI_EXTERNAL_EXECUTION_ENABLED"] = setting
                store = self.new_rest_store()
                with self.assertRaisesRegex(PublicServiceError, "EXTERNAL_EXECUTION_DISABLED"):
                    self.new_service(store).execute_action(self.user, {"action_id": action_id})
                execute.assert_not_called()
                adapter.assert_not_called()
                # The refusal read the real row and wrote nothing.
                self.assertEqual(store.calls, ["GET li_action_proposals"])
                self.assertEqual(self.one(row_sql, (self.user, action_id)), approved)
                self.assertEqual(self.one("select count(*) from public.li_outcomes where user_id = %s",
                                          (self.user,))[0], 0)
                self.assertEqual(self.counts(), counts_before, "a refusal charges nothing")


if __name__ == "__main__":
    unittest.main()
