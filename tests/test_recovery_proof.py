"""Isolated execution and recovery proof (evidence type: FAKE-STORE).

Every test here drives the real PublicService and the real Worker over the in-memory
FakeStore (tests/test_public_hosted.py), which mirrors the li_research_jobs, usage and
case RPCs. Time is a simulated clock; no network, Supabase, Stripe or provider call is
made, and no external effect is ever performed. This is fake-store evidence only: the
same proofs on a real PostgreSQL 16 server, through the real SupabaseStore, are in
tests/pg/test_recovery_pg.py (run by the postgres-proof CI job, never skipped there).

Proof map (one test per requirement):
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
"""

from __future__ import annotations

import os
import threading
import unittest
from unittest.mock import patch

from lofgren_intelligence.hosted import jobs as job_model
from lofgren_intelligence.hosted import service as service_module
from lofgren_intelligence.hosted.jobs import JobLease, JobSettings
from lofgren_intelligence.hosted.service import CaseApprovalRequired, PublicService, PublicServiceError
from lofgren_intelligence.hosted.worker import Worker

from .test_case_approval import ANSWERS, EVIDENCE, OBJECTIVE as CASE_OBJECTIVE
from .test_durable_jobs import BASE, REAL_RUN, SETTINGS, Crash, JobTestBase, at_stage


class RecoveryProofFakeStore(JobTestBase):
    # ---- helpers ---------------------------------------------------------------------

    def open_case(self):
        return self.service.clarify_objective(
            "u1", {"objective": CASE_OBJECTIVE, "answers": dict(ANSWERS), "texts": dict(EVIDENCE)}, BASE)

    def approve(self, case):
        self.service.approve_case_charter("u1", case["case_id"], case["charter_version"], case["content_hash"])

    def start_case(self, case, key):
        return self.service.start_research(
            "u1", {"objective": CASE_OBJECTIVE, "case_id": case["case_id"], "idempotency_key": key}, BASE)

    def counting_run(self, calls):
        def run(contract, registry, provider, plan_id, approved=False, ledger=None, stage_hook=None):
            calls.append(contract.objective)
            return REAL_RUN(contract, registry, provider, plan_id, approved=approved, ledger=ledger,
                            stage_hook=stage_hook)
        return patch.object(service_module, "run_investigation", run)

    # ---- R1 ----------------------------------------------------------------------------

    def test_r1_case_job_requires_a_server_recorded_approval_of_the_latest_charter(self):
        case = self.open_case()
        with self.assertRaises(CaseApprovalRequired):
            self.start_case(case, "unapproved")
        self.assertEqual(self.store.jobs, {}, "no job may exist before approval")
        self.assertEqual(self.store.reservations, {}, "no allowance may be held before approval")
        self.approve(case)
        out = self.start_case(case, "approved")
        self.assertTrue(out["created"])
        job = self.job(out["job_id"])
        self.assertEqual((job["case_id"], job["input"]["execution_inputs"]), (case["case_id"], "approved_charter"))
        self.assertEqual(job["input"]["case"]["charter_version"], case["charter_version"])
        self.assertEqual(self.store.latest_case_approval("u1", case["case_id"])["idempotency_key"], "job:approved")

    # ---- R2 ----------------------------------------------------------------------------

    def test_r2_claim_takes_a_time_bounded_lease_held_by_one_worker(self):
        job_id = self.start("lease")["job_id"]
        claimed = self.store.claim_research_job("w1", SETTINGS.lease_seconds, SETTINGS.hold_grace_seconds)
        self.assertEqual(claimed["id"], job_id)
        row = self.job(job_id)
        self.assertEqual((row["status"], row["lease_owner"], row["attempts"]), ("running", "w1", 1))
        self.assertEqual((row["lease_expires_at"] - self.clock.now).total_seconds(), SETTINGS.lease_seconds)
        self.assertIsNone(self.store.claim_research_job("w2", 120, 300), "a leased job is not claimable")
        self.assertIsNone(self.store.heartbeat_research_job(job_id, "w2", 120, 300), "only the owner extends it")

    # ---- R3 ----------------------------------------------------------------------------

    def test_r3_each_stage_boundary_saves_a_checkpoint_that_survives_a_crash(self):
        job_id = self.start("checkpoint")["job_id"]
        seen = []

        def snapshot(_ledger):
            seen.append(dict(self.job(job_id)["checkpoint"]))
            raise Crash()

        with at_stage("verify", snapshot), self.assertRaises(Crash):
            self.worker("w1").run_once()
        saved = self.job(job_id)["checkpoint"]
        self.assertEqual((saved["phase"], saved["attempt"]), ("running", 1))
        self.assertIn(saved["stage"], {"sense", "verify"})
        self.assertGreater(self.job(job_id)["cost_so_far"], 0)
        self.assertEqual(saved, seen[0])

    # ---- R4 ----------------------------------------------------------------------------

    def test_r4_result_and_settlement_responses_lost_after_commit_never_rerun_or_double_charge(self):
        first = self.start("lost-response")
        job_id = first["job_id"]
        # The client's start response is lost too: the retry with the same key is the same job.
        self.assertEqual(self.start("lost-response")["job_id"], job_id)
        self.assertEqual(len(self.store.jobs), 1)
        save, finalize = self.store.save_research_job_result, self.store.finalize_usage

        def save_then_lose(*a):
            save(*a)
            raise ConnectionError("result committed, response lost")

        calls = []
        with self.counting_run(calls), patch.object(self.store, "save_research_job_result", save_then_lose):
            out = self.worker("w1").run_once()["jobs"][0]
        self.assertEqual(out["status"], "persistence_unknown")
        self.assertEqual((len(self.store.runs), len(self.store.usage)), (1, 0))
        self.clock.advance(SETTINGS.lease_seconds + 1)

        def finalize_then_lose(*a):
            finalize(*a)
            raise ConnectionError("settlement committed, response lost")

        with self.counting_run(calls), patch.object(self.store, "finalize_usage", finalize_then_lose):
            results = self.drain(self.worker("w2"))
        self.assertEqual([r["status"] for r in results], ["succeeded"])
        self.assertEqual(len(calls), 1, "research ran exactly once")
        self.assertEqual(len(self.store.runs), 1)
        self.assertEqual(len(self.store.usage), 1, "usage settled exactly once")
        self.service.reconcile_unsettled_usage()  # a later reconcile pass charges nothing more
        self.assertEqual(len(self.store.usage), 1)
        self.assertEqual(self.reservation(job_id)["status"], "settled")
        self.assertAlmostEqual(self.store.usage[0]["units"], self.job(job_id)["cost_so_far"])

    # ---- R5 ----------------------------------------------------------------------------

    def test_r5_two_competing_workers_exactly_one_runs_the_job(self):
        job_id = self.start("race")["job_id"]
        barrier = threading.Barrier(2)
        outcomes, calls = {}, []

        def compete(name):
            barrier.wait(5)
            outcomes[name] = self.worker(name).run_once()["jobs"]

        with self.counting_run(calls):
            threads = [threading.Thread(target=compete, args=(n,)) for n in ("w1", "w2")]
            for t in threads:
                t.start()
            for t in threads:
                t.join(30)
        self.assertEqual(sorted(len(v) for v in outcomes.values()), [0, 1], outcomes)
        winner = next(v[0] for v in outcomes.values() if v)
        self.assertEqual(winner["status"], "succeeded")
        self.assertEqual(len(calls), 1)
        self.assertEqual((self.job(job_id)["attempts"], len(self.store.runs), len(self.store.usage)), (1, 1, 1))

    # ---- R6 ----------------------------------------------------------------------------

    def test_r6_cancel_queued_releases_and_cancel_running_stops_at_the_next_stage(self):
        queued = self.start("cancel-queued")["job_id"]
        self.assertEqual(self.service.cancel_research("u1", {"job_id": queued})["status"], "cancelled")
        self.assertEqual(self.reservation(queued)["status"], "released")
        running = self.start("cancel-running")["job_id"]
        with at_stage("verify", lambda _l: self.service.cancel_research("u1", {"job_id": running})):
            results = self.drain()
        self.assertEqual([r["status"] for r in results], ["cancelled"])
        self.assertEqual(self.job(running)["error_code"], "CANCELLED")
        self.assertEqual(self.store.runs, {})
        self.assertEqual(len(self.store.usage), 1)
        self.assertGreater(self.store.usage[0]["units"], 0)
        self.assertIn("partial_run", self.store.usage[0]["unpriced_components"])

    # ---- R7 ----------------------------------------------------------------------------

    def test_r7_expired_lease_is_reclaimed_by_another_worker_and_the_old_one_is_fenced(self):
        job_id = self.start("reclaim")["job_id"]
        with at_stage("verify", lambda _l: (_ for _ in ()).throw(Crash())), self.assertRaises(Crash):
            self.worker("w1").run_once()
        self.assertEqual(self.store.reclaim_research_jobs(10, 86_400), [], "a live lease is not reclaimable")
        self.clock.advance(SETTINGS.lease_seconds + 1)
        self.assertEqual(self.store.reclaim_research_jobs(10, 86_400), [job_id])
        self.assertEqual((self.job(job_id)["status"], self.job(job_id)["lease_owner"]), ("queued", None))
        self.assertEqual([r["status"] for r in self.drain(self.worker("w2"))], ["succeeded"])
        self.assertEqual((self.job(job_id)["attempts"], len(self.store.usage)), (2, 1))
        with self.assertRaises(job_model.LeaseLost):
            JobLease(self.store, {"id": job_id}, "w1", SETTINGS).beat()
        self.assertIsNone(self.store.complete_research_job(job_id, "w1", "run-x", 1.0))

    # ---- R8 ----------------------------------------------------------------------------

    def test_r8_refusal_before_work_releases_the_reservation(self):
        job_id = self.start("refused")["job_id"]
        self.job(job_id)["input"]["schema"] = "tampered"
        with patch.object(service_module, "run_investigation", side_effect=AssertionError("no work")):
            results = self.drain()
        self.assertEqual(results[0]["status"], "failed")
        self.assertEqual(self.reservation(job_id)["status"], "released")
        self.assertEqual((self.store.usage, self.store.runs), ([], {}))

    def test_r8_failure_after_work_keeps_the_incurred_cost_and_marks_it_not_fully_priced(self):
        self.service.job_settings = JobSettings(**{**SETTINGS.__dict__, "max_attempts": 1})
        job_id = self.start("fails-late")["job_id"]

        def boom(_ledger):
            raise RuntimeError("provider failed after work")

        with at_stage("verify", boom):
            results = self.drain()
        self.assertEqual((results[0]["status"], results[0]["error_code"]), ("failed", "EXECUTION_FAILED"))
        self.assertEqual(self.reservation(job_id)["status"], "settled")
        self.assertEqual(len(self.store.usage), 1)
        event = self.store.usage[0]
        self.assertGreater(event["units"], 0, "incurred work is charged, never recorded as 0")
        self.assertAlmostEqual(event["units"], self.job(job_id)["cost_so_far"])
        self.assertIn("partial_run", event["unpriced_components"],
                      "an unknown cost component is declared unpriced, never priced at 0")

    # ---- R9 ----------------------------------------------------------------------------

    def test_r9_saved_result_reopens_identically_after_a_restart(self):
        job_id = self.start("reopen")["job_id"]
        self.drain()
        before = self.service.get_job_result("u1", {"job_id": job_id})
        # Restart: a new service and a new worker over the same stored data (no in-memory carry-over).
        restarted = PublicService(self.store, clock=self.clock)
        restarted.job_settings = SETTINGS
        with patch.object(service_module, "run_investigation", side_effect=AssertionError("reran")):
            self.assertEqual(Worker(self.store, SETTINGS, worker_id="w-restart").run_once()["jobs"], [])
            after = restarted.get_job_result("u1", {"job_id": job_id})
        self.assertTrue(after["result_available"])
        self.assertTrue(after["receipt"]["intact"])
        for key in ("status", "run_id", "report", "findings", "receipt"):
            self.assertEqual(after[key], before[key], key)
        self.assertEqual(len(self.store.usage), 1)

    # ---- V4 -----------------------------------------------------------------------------

    def test_v4_external_execution_is_refused_with_the_staging_setting_and_performs_no_effect(self):
        for env in ({}, {"LI_EXTERNAL_EXECUTION_ENABLED": "false"}):
            with self.subTest(env=env), patch.dict(os.environ, env), \
                    patch.object(service_module, "execute_authorized") as execute, \
                    patch.object(service_module, "HTTPSWebhookAdapter") as adapter:
                if not env:
                    os.environ.pop("LI_EXTERNAL_EXECUTION_ENABLED", None)
                self.store.save_action({"user_id": "u1", "action_id": "act-1", "artifact_id": "art-1",
                                        "status": "approved", "request": {}, "grant_record": {"g": 1},
                                        "approval_record": {"a": 1}})
                with self.assertRaisesRegex(PublicServiceError, "EXTERNAL_EXECUTION_DISABLED"):
                    self.service.execute_action("u1", {"action_id": "act-1"})
                execute.assert_not_called()
                adapter.assert_not_called()
                self.assertEqual(self.store.get_action("u1", "act-1")["status"], "approved")


if __name__ == "__main__":
    unittest.main()
