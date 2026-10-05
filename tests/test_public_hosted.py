import hashlib
import hmac
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from lofgren_intelligence.hosted.auth import OAuthService, Principal, code_challenge_s256, token_hash
from lofgren_intelligence.hosted.costing import actual_run_cost
from lofgren_intelligence.hosted.economics import certify_paid_plan
from lofgren_intelligence.kernel.ledger import CostLedger
from lofgren_intelligence.hosted.security import PublicInputError, validate_remote_args
from lofgren_intelligence.hosted.service import PaymentRequired, PublicService
from lofgren_intelligence.hosted.stripe import apply_webhook, verify_webhook
from lofgren_intelligence.discovery.fixtures import warehouse_design

from .helpers import TEXTS

OBJECTIVE = "Is industrial construction in the Phoenix metro increasing?"


class FakeStore:
    def __init__(self, activation_number=1, kind="founding_free", quota=500.0):
        self.activation_number = activation_number
        self.account = {"user_id": "u1", "email": "u@example.com", "activation_number": activation_number}
        self.entitlement = {
            "user_id": "u1", "kind": kind, "plan_id": kind, "active": kind != "paid_required",
            "quota_units_per_week": quota,
        }
        self.oauth_clients = {}
        self.oauth_codes = {}
        self.access_tokens = {}
        self.refresh_tokens = {}
        self.runs = {}
        self.discoveries = {}
        self.artifacts = {}
        self.actions = {}
        self.outcomes = {}
        self.improvements = {}
        self.usage = []
        self.reservations = {}
        self.billing_events = {}
        self.verified_user = {"id": "u1", "email": "u@example.com"}

    def verify_supabase_user(self, token):
        if token != "supabase-session":
            raise RuntimeError("bad session")
        return self.verified_user

    def activate_account(self, user_id, email=None):
        return self.account

    def get_account(self, user_id):
        return self.account if user_id == "u1" else None

    def get_entitlement(self, user_id):
        return self.entitlement if user_id == "u1" else None

    def put_oauth_client(self, row):
        self.oauth_clients[row["client_id"]] = dict(row)

    def get_oauth_client(self, client_id):
        return self.oauth_clients.get(client_id)

    def put_oauth_code(self, row):
        self.oauth_codes[row["code_hash"]] = dict(row)

    def consume_oauth_code(self, digest):
        row = self.oauth_codes.pop(digest, None)
        return row

    def put_access_token(self, row):
        self.access_tokens[row["token_hash"]] = dict(row)

    def get_access_token(self, digest):
        return self.access_tokens.get(digest)

    def put_refresh_token(self, row):
        self.refresh_tokens[row["token_hash"]] = dict(row)

    def consume_refresh_token(self, digest):
        return self.refresh_tokens.pop(digest, None)

    def save_run(self, row):
        self.runs[(row["user_id"], row["run_id"])] = dict(row)

    def get_run(self, user_id, run_id):
        return self.runs.get((user_id, run_id))

    def save_discovery(self, row):
        self.discoveries[(row["user_id"], row["discovery_id"])] = dict(row)

    def get_discovery(self, user_id, discovery_id):
        return self.discoveries.get((user_id, discovery_id))

    def list_discoveries(self, user_id, limit=1000):
        return [row for (uid, _), row in self.discoveries.items() if uid == user_id][:limit]

    def save_artifact(self, row):
        self.artifacts[(row["user_id"], row["artifact_id"])] = dict(row)

    def get_artifact(self, user_id, artifact_id):
        return self.artifacts.get((user_id, artifact_id))

    def list_artifacts(self, user_id, limit=1000):
        return [row for (uid, _), row in self.artifacts.items() if uid == user_id][:limit]

    def save_action(self, row):
        self.actions[(row["user_id"], row["action_id"])] = dict(row)

    def get_action(self, user_id, action_id):
        return self.actions.get((user_id, action_id))

    def list_actions(self, user_id, limit=1000):
        return [row for (uid, _), row in self.actions.items() if uid == user_id][:limit]

    def save_outcome(self, row):
        self.outcomes[(row["user_id"], row["outcome_id"])] = dict(row)

    def get_outcome(self, user_id, outcome_id):
        return self.outcomes.get((user_id, outcome_id))

    def list_outcomes(self, user_id, limit=1000):
        return [row for (uid, _), row in self.outcomes.items() if uid == user_id][:limit]

    def save_improvement(self, row):
        self.improvements[(row["user_id"], row["improvement_id"])] = dict(row)

    def get_improvement(self, user_id, improvement_id):
        return self.improvements.get((user_id, improvement_id))

    def list_improvements(self, user_id, limit=1000):
        return [row for (uid, _), row in self.improvements.items() if uid == user_id][:limit]

    def reserve_usage(self, reservation_id, user_id, operation, units):
        quota = float(self.entitlement.get("quota_units_per_week") or 0)
        active = bool(self.entitlement.get("active"))
        used = sum(float(x.get("units") or 0) for x in self.usage if x["user_id"] == user_id)
        reserved = sum(float(x["units"]) for x in self.reservations.values()
                       if x["user_id"] == user_id and x["status"] == "reserved")
        if not active or used + reserved + float(units) > quota + 1e-9:
            return False
        self.reservations[reservation_id] = {
            "user_id": user_id, "operation": operation, "units": float(units), "status": "reserved",
        }
        return True

    def finalize_usage(self, reservation_id, run_id, actual_units, known_cost_usd, unpriced_components):
        row = self.reservations.get(reservation_id)
        if not row or row["status"] != "reserved":
            return False
        quota = float(self.entitlement.get("quota_units_per_week") or 0)
        used = sum(float(x.get("units") or 0) for x in self.usage if x["user_id"] == row["user_id"])
        other = sum(float(x["units"]) for rid, x in self.reservations.items()
                    if rid != reservation_id and x["user_id"] == row["user_id"] and x["status"] == "reserved")
        if used + other + float(actual_units) > quota + 1e-9:
            return False
        self.usage.append({
            "id": reservation_id,
            "user_id": row["user_id"],
            "run_id": run_id,
            "operation": row["operation"],
            "units": float(actual_units),
            "known_cost_usd": float(known_cost_usd),
            "unpriced_components": list(unpriced_components),
        })
        row["status"] = "settled"
        return True

    def release_usage(self, reservation_id):
        row = self.reservations.get(reservation_id)
        if not row or row["status"] != "reserved":
            return False
        row["status"] = "released"
        return True

    def take_rate_limit(self, user_id, bucket="mcp", limit=60, window_seconds=60):
        return True

    def list_runs(self, user_id, limit=1000):
        return [row for (uid, _), row in self.runs.items() if uid == user_id][:limit]

    def list_usage(self, user_id, limit=5000):
        return [row for row in self.usage if row["user_id"] == user_id][:limit]

    def delete_auth_user(self, user_id):
        self.deleted_user = user_id
        self.account = None
        self.entitlement = None
        self.runs = {k: v for k, v in self.runs.items() if k[0] != user_id}
        self.discoveries = {k: v for k, v in self.discoveries.items() if k[0] != user_id}
        self.artifacts = {k: v for k, v in self.artifacts.items() if k[0] != user_id}
        self.actions = {k: v for k, v in self.actions.items() if k[0] != user_id}
        self.outcomes = {k: v for k, v in self.outcomes.items() if k[0] != user_id}
        self.improvements = {k: v for k, v in self.improvements.items() if k[0] != user_id}
        self.usage = [row for row in self.usage if row["user_id"] != user_id]

    def record_usage(self, row):
        self.usage.append(dict(row))

    def usage_units_since(self, user_id, since_iso):
        return sum(float(x.get("units") or 0) for x in self.usage if x["user_id"] == user_id)

    def stripe_event_seen(self, event_id):
        return event_id in self.billing_events

    def record_stripe_event(self, row):
        self.billing_events[row["stripe_event_id"]] = dict(row)

    def get_entitlement_by_subscription(self, subscription_id):
        if self.entitlement.get("stripe_subscription_id") == subscription_id:
            return self.entitlement
        return None

    def apply_stripe_entitlement_event(
        self, *, event_id, event_type, payload_hash, user_id, customer_id,
        subscription_id, active, plan_id, quota_units_per_week
    ):
        if event_id in self.billing_events:
            return False
        self.billing_events[event_id] = {
            "stripe_event_id": event_id,
            "event_type": event_type,
            "payload_hash": payload_hash,
        }
        if user_id is not None:
            self.entitlement = {
                **self.entitlement,
                "user_id": user_id,
                "kind": "paid" if active else "paid_required",
                "active": active,
                "plan_id": plan_id,
                "quota_units_per_week": quota_units_per_week if active else 0,
                "stripe_customer_id": customer_id or self.entitlement.get("stripe_customer_id"),
                "stripe_subscription_id": subscription_id or self.entitlement.get("stripe_subscription_id"),
            }
        return True

    def set_paid_entitlement(self, user_id, **kwargs):
        self.entitlement = {
            **self.entitlement,
            "user_id": user_id,
            "kind": "paid" if kwargs["active"] else "paid_required",
            "active": kwargs["active"],
            "plan_id": kwargs["plan_id"],
            "quota_units_per_week": kwargs["quota_units_per_week"],
            "stripe_customer_id": kwargs["customer_id"],
            "stripe_subscription_id": kwargs["subscription_id"],
        }

    def cost_samples(self, limit=5000):
        return []


class OAuthTests(unittest.TestCase):
    def test_dynamic_registration_pkce_exchange_and_refresh_rotation(self):
        store = FakeStore()
        oauth = OAuthService(store)
        client = oauth.register_client({
            "client_name": "Test MCP",
            "redirect_uris": ["http://127.0.0.1/callback"],
            "token_endpoint_auth_method": "none",
        })
        verifier = "a" * 64
        with patch.dict(os.environ, {"LI_PUBLIC_BASE_URL": "https://li.example"}, clear=False):
            code = oauth.authorize_from_supabase_session(
                supabase_access_token="supabase-session",
                client_id=client["client_id"],
                redirect_uri="http://127.0.0.1/callback",
                code_challenge=code_challenge_s256(verifier),
                scope="mcp",
                resource="https://li.example/mcp",
            )
        tokens = oauth.exchange_code(
            code=code,
            code_verifier=verifier,
            client_id=client["client_id"],
            redirect_uri="http://127.0.0.1/callback",
        )
        self.assertTrue(tokens["access_token"].startswith("lit_"))
        self.assertTrue(tokens["refresh_token"].startswith("lir_"))
        principal = oauth.authenticate(tokens["access_token"])
        self.assertEqual(principal.user_id, "u1")
        refreshed = oauth.refresh(refresh_token=tokens["refresh_token"], client_id=client["client_id"])
        self.assertNotEqual(refreshed["access_token"], tokens["access_token"])
        with self.assertRaises(Exception):
            oauth.refresh(refresh_token=tokens["refresh_token"], client_id=client["client_id"])

    def test_redirect_must_be_registered(self):
        store = FakeStore()
        oauth = OAuthService(store)
        client = oauth.register_client({"redirect_uris": ["https://client.example/callback"]})
        with self.assertRaises(Exception):
            oauth.authorize_from_supabase_session(
                supabase_access_token="supabase-session",
                client_id=client["client_id"],
                redirect_uri="https://evil.example/callback",
                code_challenge=code_challenge_s256("b" * 64),
                scope="mcp",
                resource="https://li.example/mcp",
            )


class HostedRunTests(unittest.TestCase):
    def test_durable_run_survives_service_instance(self):
        store = FakeStore()
        with patch.dict(os.environ, {
            "LOFGREN_PROVIDER": "heuristic",
            "LI_INFRA_USD_PER_RUN": "0",
            "LI_RETRIEVAL_USD_PER_CALL": "0",
        }, clear=False):
            first = PublicService(store)
            out = first.investigate("u1", {"objective": OBJECTIVE, "texts": TEXTS})
            self.assertTrue(out["run_id"].startswith("RR-"))
            self.assertIn(("u1", out["run_id"]), store.runs)
            second = PublicService(store)
            receipt = second.get_receipt("u1", {"run_id": out["run_id"]})
            self.assertTrue(receipt["intact"])
            report = second.render_report("u1", {"run_id": out["run_id"]})
            self.assertIn("Lofgren Intelligence report", report["report"])
            self.assertGreater(store.usage[0]["units"], 0)

    def test_paid_required_account_cannot_run(self):
        store = FakeStore(activation_number=1001, kind="paid_required", quota=0)
        service = PublicService(store)
        with self.assertRaises(PaymentRequired):
            service.investigate("u1", {"objective": OBJECTIVE, "texts": TEXTS})

    def test_same_research_id_is_tenant_scoped(self):
        store = FakeStore()
        row = {"run_id": "RR-SAME", "user_id": "u1", "snapshot": {"owner": "u1"}}
        store.save_run(row)
        store.save_run({"run_id": "RR-SAME", "user_id": "u2", "snapshot": {"owner": "u2"}})
        self.assertEqual(store.get_run("u1", "RR-SAME")["snapshot"]["owner"], "u1")
        self.assertEqual(store.get_run("u2", "RR-SAME")["snapshot"]["owner"], "u2")


    def test_account_export_is_tenant_scoped(self):
        store = FakeStore()
        store.save_run({"user_id": "u1", "run_id": "RR-1", "snapshot": {"owner": "u1"}})
        store.save_run({"user_id": "u2", "run_id": "RR-2", "snapshot": {"owner": "u2"}})
        service = PublicService(store)
        exported = service.export_account_data("u1")
        self.assertEqual([x["run_id"] for x in exported["runs"]], ["RR-1"])

    def test_account_delete_requires_exact_phrase_and_cancels_paid_subscription_first(self):
        store = FakeStore(activation_number=1001, kind="paid", quota=2000)
        store.entitlement.update({"stripe_subscription_id": "sub_test", "stripe_customer_id": "cus_test"})
        service = PublicService(store)
        with self.assertRaises(Exception):
            service.delete_account("u1", "DELETE")
        with patch("lofgren_intelligence.hosted.service.cancel_subscription") as cancel:
            out = service.delete_account("u1", "DELETE MY LOFGREN INTELLIGENCE ACCOUNT")
        cancel.assert_called_once_with("sub_test")
        self.assertTrue(out["deleted"])
        self.assertEqual(store.deleted_user, "u1")


class HostedDiscoveryTests(unittest.TestCase):
    def _research(self, store):
        with patch.dict(os.environ, {
            "LOFGREN_PROVIDER": "heuristic",
            "LI_INFRA_USD_PER_RUN": "0",
            "LI_RETRIEVAL_USD_PER_CALL": "0",
            "LI_PUBLIC_MAX_DISCOVERY_UNITS": "100",
        }, clear=False):
            return PublicService(store).investigate("u1", {"objective": OBJECTIVE, "texts": TEXTS})

    def test_v2_discovery_is_durable_across_service_instances(self):
        store = FakeStore(quota=1000)
        run = self._research(store)
        first = PublicService(store)
        out = first.discover("u1", {
            "run_id": run["run_id"],
            "objective": "Choose a fictional warehouse size",
            "design": warehouse_design(),
        })
        self.assertEqual(out["kind"], "discovery")
        self.assertTrue(out["discovery_id"].startswith("DR-"))
        second = PublicService(store)
        receipt = second.get_discovery_receipt("u1", {"discovery_id": out["discovery_id"]})
        self.assertTrue(receipt["intact"])
        report = second.render_discovery_report("u1", {"discovery_id": out["discovery_id"]})
        self.assertIn("Discovery", report["report"])
        verification = second.verify_discovery("u1", {"discovery_id": out["discovery_id"]})
        self.assertTrue(verification["receipt_intact"])

    def test_same_discovery_id_is_tenant_scoped(self):
        store = FakeStore()
        row = {"user_id": "u1", "discovery_id": "DR-SAME", "snapshot": {"owner": "u1"}}
        store.save_discovery(row)
        store.save_discovery({"user_id": "u2", "discovery_id": "DR-SAME", "snapshot": {"owner": "u2"}})
        self.assertEqual(store.get_discovery("u1", "DR-SAME")["snapshot"]["owner"], "u1")
        self.assertEqual(store.get_discovery("u2", "DR-SAME")["snapshot"]["owner"], "u2")

    def test_account_export_contains_only_callers_discoveries(self):
        store = FakeStore()
        store.save_discovery({"user_id": "u1", "discovery_id": "DR-1", "snapshot": {}})
        store.save_discovery({"user_id": "u2", "discovery_id": "DR-2", "snapshot": {}})
        exported = PublicService(store).export_account_data("u1")
        self.assertEqual([x["discovery_id"] for x in exported["discoveries"]], ["DR-1"])


class HostedLifecycleTests(HostedDiscoveryTests):
    def test_v3_artifact_is_durable_and_tenant_scoped(self):
        store = FakeStore(quota=5000)
        run = self._research(store)
        discovery = PublicService(store).discover("u1", {
            "run_id": run["run_id"],
            "objective": "Choose a fictional warehouse size",
            "design": warehouse_design(),
        })
        built = PublicService(store).build_artifact("u1", {
            "discovery_id": discovery["discovery_id"],
            "kind": "structured_bundle",
        })
        self.assertTrue(built["verified"])
        second = PublicService(store).get_artifact("u1", {"artifact_id": built["artifact_id"]})
        self.assertTrue(second["verified"])
        self.assertTrue(second["receipt_intact"])
        row = store.get_artifact("u1", built["artifact_id"])
        store.save_artifact({**row, "user_id": "u2"})
        self.assertIsNotNone(store.get_artifact("u1", built["artifact_id"]))
        self.assertIsNotNone(store.get_artifact("u2", built["artifact_id"]))

    def test_stored_then_reloaded_discovery_still_builds_an_artifact(self):
        # A durable store hands back JSON, never the in-memory discovery: round-trip every row through JSON
        # and build from a fresh service instance. The V3 upstream check must still pass untouched.
        store = FakeStore(quota=5000)
        run = self._research(store)
        discovery = PublicService(store).discover("u1", {
            "run_id": run["run_id"],
            "objective": "Choose a fictional warehouse size",
            "design": warehouse_design(),
        })
        for key, row in list(store.discoveries.items()):
            store.discoveries[key] = json.loads(json.dumps(row))
        for key, row in list(store.runs.items()):
            store.runs[key] = json.loads(json.dumps(row))
        snap = store.get_discovery("u1", discovery["discovery_id"])["snapshot"]
        self.assertEqual({x["data"]["id"] for x in snap["context_objects"]}, set(snap["receipt"]["objects"]))
        built = PublicService(store).build_artifact("u1", {
            "discovery_id": discovery["discovery_id"],
            "kind": "structured_bundle",
        })
        self.assertTrue(built["verified"])

    def test_tampered_or_incomplete_stored_discovery_is_refused(self):
        store = FakeStore(quota=5000)
        run = self._research(store)
        discovery = PublicService(store).discover("u1", {
            "run_id": run["run_id"],
            "objective": "Choose a fictional warehouse size",
            "design": warehouse_design(),
        })
        original = json.loads(json.dumps(store.get_discovery("u1", discovery["discovery_id"])))
        args = {"discovery_id": discovery["discovery_id"]}

        tampered = json.loads(json.dumps(original))
        assumption = next(x for x in tampered["snapshot"]["context_objects"] if x["type"] == "Assumption")
        assumption["data"]["value"] = float(assumption["data"]["value"]) * 2 + 1
        store.save_discovery(tampered)
        with self.assertRaises(ValueError):
            PublicService(store).build_artifact("u1", args)

        dropped = json.loads(json.dumps(original))
        dropped["snapshot"]["context_objects"] = [
            x for x in dropped["snapshot"]["context_objects"] if x["type"] != "Simulation"
        ]
        store.save_discovery(dropped)
        with self.assertRaises(ValueError):
            PublicService(store).build_artifact("u1", args)

        missing = json.loads(json.dumps(original))
        del missing["snapshot"]["context_objects"]
        store.save_discovery(missing)
        with self.assertRaises(ValueError):
            PublicService(store).build_artifact("u1", args)
        self.assertEqual(store.artifacts, {})

    def test_v4_action_requires_browser_approval_before_execution(self):
        store = FakeStore(quota=5000)
        run = self._research(store)
        discovery = PublicService(store).discover("u1", {
            "run_id": run["run_id"],
            "objective": "Choose a fictional warehouse size",
            "design": warehouse_design(),
        })
        built = PublicService(store).build_artifact("u1", {
            "discovery_id": discovery["discovery_id"],
            "kind": "structured_bundle",
        })
        service = PublicService(store)
        with patch("lofgren_intelligence.hosted.service.HTTPSWebhookAdapter.preflight", return_value={"ready": True}):
            proposed = service.propose_action("u1", {
                "artifact_id": built["artifact_id"],
                "target": "https://example.com/hook",
                "payload": {"hello": "world"},
                "cost_usd": 0,
            }, "https://li.example")
        self.assertEqual(proposed["status"], "awaiting_human_approval")
        with self.assertRaises(Exception):
            service.execute_action("u1", {"action_id": proposed["action_id"]})
        approved = service.approve_action("u1", proposed["action_id"])
        self.assertEqual(approved["status"], "approved")
        status = service.action_status("u1", {"action_id": proposed["action_id"]})
        self.assertEqual(status["status"], "approved")

    def test_v5_and_v6_are_durable_after_verified_action(self):
        from lofgren_intelligence.execution.certification import NOW, _base as v4_base
        from lofgren_intelligence.execution.core import InMemoryAdapter, execute_authorized

        store = FakeStore(quota=5000)
        product, request, grant, approval = v4_base()
        executed = execute_authorized(
            product.v4_handoff, request, grant, approval, InMemoryAdapter(),
            subject="user-cert", now=NOW,
        )
        self.assertIsNotNone(executed.v5_handoff)
        store.save_action({
            "user_id": "u1",
            "action_id": request.action_id,
            "artifact_id": request.artifact_id,
            "request": {},
            "status": "executed",
            "grant_record": None,
            "approval_record": None,
            "receipt": executed.receipt,
            "v5_handoff": executed.v5_handoff,
            "created_at": NOW.isoformat(),
            "approved_at": NOW.isoformat(),
            "executed_at": NOW.isoformat(),
        })
        measurements = [
            {
                "metric": item["metric"],
                "value": item["mean"],
                "unit": item.get("unit", ""),
                "observed_at": "2026-11-04T12:00:00+00:00",
                "source": "hosted-test",
                "observation_id": f"OBS-{i}",
            }
            for i, item in enumerate(executed.v5_handoff["expected_outcomes"], 1)
        ]
        service = PublicService(store)
        measured = service.measure_outcome("u1", {
            "action_id": request.action_id,
            "measurements": measurements,
        })
        self.assertTrue(measured["receipt_intact"])
        self.assertTrue(measured["v6_improvement_allowed"])
        outcome = service.get_outcome("u1", {"outcome_id": measured["outcome_id"]})
        self.assertTrue(outcome["receipt_intact"])

        improved = service.evaluate_improvement("u1", {
            "outcome_id": measured["outcome_id"],
            "proposal": {
                "proposal_id": "IMP-hosted",
                "baseline_id": "BASE-1",
                "candidate_id": "CAND-2",
                "change_summary": "Improve routing threshold.",
                "evaluation_dataset": {
                    "dataset_id": "DS-heldout",
                    "content_hash": "sha256:" + "a" * 64,
                    "sample_count": 100,
                    "held_out": True,
                },
                "primary_metric": {
                    "metric": "task_success",
                    "baseline": 0.75,
                    "candidate": 0.82,
                    "direction": "higher_is_better",
                },
                "min_gain": 0.03,
                "safety_constraints": [
                    {
                        "metric": "evidence_integrity",
                        "baseline": 0.99,
                        "candidate": 0.99,
                        "max_regression": 0.01,
                    }
                ],
            },
        })
        self.assertTrue(improved["receipt_intact"])
        self.assertTrue(improved["human_review_required"])
        self.assertFalse(improved["mutation_performed"])
        recovered = PublicService(store).get_improvement("u1", {"improvement_id": improved["improvement_id"]})
        self.assertTrue(recovered["receipt_intact"])


class QuotaReservationTests(unittest.TestCase):
    def test_inflight_reservation_prevents_concurrent_oversubscription(self):
        store = FakeStore(quota=500)
        self.assertTrue(store.reserve_usage("r1", "u1", "research", 300))
        self.assertFalse(store.reserve_usage("r2", "u1", "research", 300))
        self.assertTrue(store.finalize_usage("r1", None, 250, 0, []))
        self.assertTrue(store.reserve_usage("r3", "u1", "research", 250))
        self.assertFalse(store.reserve_usage("r4", "u1", "research", 1))

    def test_release_returns_reserved_capacity(self):
        store = FakeStore(quota=100)
        self.assertTrue(store.reserve_usage("r1", "u1", "research", 100))
        self.assertFalse(store.reserve_usage("r2", "u1", "research", 1))
        self.assertTrue(store.release_usage("r1"))
        self.assertTrue(store.reserve_usage("r3", "u1", "research", 100))


class PublicSecurityTests(unittest.TestCase):
    def test_remote_rejects_server_file_paths(self):
        with self.assertRaises(PublicInputError):
            validate_remote_args({"objective": "x", "files": ["/etc/passwd"]})

    def test_ssrf_local_targets_are_refused(self):
        for url in ("http://127.0.0.1/x", "http://169.254.169.254/latest/meta-data", "http://[::1]/"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                validate_remote_args({"objective": "x", "urls": [url]})


class StripeWebhookTests(unittest.TestCase):
    def test_signature_and_idempotent_entitlement(self):
        store = FakeStore(activation_number=1001, kind="paid_required", quota=0)
        event = {
            "id": "evt_test",
            "type": "checkout.session.completed",
            "data": {"object": {
                "id": "cs_test", "customer": "cus_test", "subscription": "sub_test",
                "client_reference_id": "u1", "metadata": {"li_user_id": "u1"}, "payment_status": "paid",
            }},
        }
        raw = json.dumps(event, separators=(",", ":")).encode()
        with patch.dict(os.environ, {
            "STRIPE_WEBHOOK_SECRET": "whsec_test",
            "LI_PAID_PLAN_ID": "researcher",
            "LI_PAID_WEEKLY_UNITS": "2000",
        }, clear=False):
            ts = 1_800_000_000
            sig = hmac.new(b"whsec_test", str(ts).encode() + b"." + raw, hashlib.sha256).hexdigest()
            parsed = verify_webhook(raw, f"t={ts},v1={sig}", now_s=ts)
            self.assertEqual(
                apply_webhook(store, parsed, subscription_status=lambda _: "active"),
                "processed",
            )
            self.assertEqual(store.entitlement["kind"], "paid")
            self.assertEqual(
                apply_webhook(store, parsed, subscription_status=lambda _: "active"),
                "duplicate",
            )



    def test_failed_invoice_disables_paid_access_and_subscription_update_recovers(self):
        store = FakeStore(activation_number=1001, kind="paid", quota=2000)
        store.entitlement.update({
            "stripe_subscription_id": "sub_test",
            "stripe_customer_id": "cus_test",
            "active": True,
        })
        with patch.dict(os.environ, {
            "LI_PAID_PLAN_ID": "researcher",
            "LI_PAID_WEEKLY_UNITS": "2000",
        }, clear=False):
            failed = {
                "id": "evt_failed",
                "type": "invoice.payment_failed",
                "data": {"object": {"customer": "cus_test", "subscription": "sub_test"}},
            }
            self.assertEqual(
                apply_webhook(store, failed, subscription_status=lambda _: "past_due"),
                "processed",
            )
            self.assertEqual(store.entitlement["kind"], "paid_required")
            self.assertFalse(store.entitlement["active"])

            recovered = {
                "id": "evt_recovered",
                "type": "customer.subscription.updated",
                "data": {"object": {
                    "id": "sub_test",
                    "customer": "cus_test",
                    "status": "active",
                    "metadata": {"li_user_id": "u1"},
                }},
            }
            self.assertEqual(
                apply_webhook(store, recovered, subscription_status=lambda _: "active"),
                "processed",
            )
            self.assertEqual(store.entitlement["kind"], "paid")
            self.assertTrue(store.entitlement["active"])


class CostingTests(unittest.TestCase):
    def test_external_data_cost_is_reconstructed_without_counting_customer_rate_as_cogs(self):
        ledger = CostLedger(0.0312)
        ledger.record("sense", "external_data", "licensed-provider", work_units=2, external_usd=0.75)
        with patch.dict(os.environ, {"LI_INFRA_USD_PER_RUN": "0"}, clear=False):
            result = actual_run_cost({"name": "heuristic", "usage": {"calls": 0}}, ledger)
        self.assertAlmostEqual(result.known_cost_usd, 0.75, places=6)
        self.assertTrue(result.fully_priced)


class EconomicGateTests(unittest.TestCase):
    def test_paid_plan_gate_fails_closed_without_samples(self):
        with patch.dict(os.environ, {
            "LI_PAID_MONTHLY_USD": "49.99",
            "LI_PAID_WEEKLY_UNITS": "2000",
            "LI_PAYMENT_FEE_PERCENT": "0.029",
            "LI_PAYMENT_FEE_FIXED_USD": "0.30",
            "LI_ECON_MIN_SAMPLES": "100",
            "LI_TARGET_GROSS_MARGIN": "0.65",
        }, clear=False):
            gate = certify_paid_plan([])
        self.assertFalse(gate.passed)
        self.assertIn("insufficient_samples:0/100", gate.reasons)

    def test_paid_plan_gate_uses_p95_cost_not_average(self):
        samples = [
            {"units": 1, "known_cost_usd": 0.001, "unpriced_components": []}
            for _ in range(94)
        ] + [
            {"units": 1, "known_cost_usd": 0.02, "unpriced_components": []}
            for _ in range(6)
        ]
        with patch.dict(os.environ, {
            "LI_PAID_MONTHLY_USD": "49.99",
            "LI_PAID_WEEKLY_UNITS": "2000",
            "LI_PAYMENT_FEE_PERCENT": "0.029",
            "LI_PAYMENT_FEE_FIXED_USD": "0.30",
            "LI_ECON_MIN_SAMPLES": "100",
            "LI_TARGET_GROSS_MARGIN": "0.65",
        }, clear=False):
            gate = certify_paid_plan(samples)
        self.assertFalse(gate.passed)
        self.assertIn("p95_cost_exceeds_margin_ceiling", gate.reasons)


class MigrationContractTests(unittest.TestCase):
    def test_first_1000_rule_and_rls_are_present(self):
        sql = Path("supabase/migrations/20261004201650_public_mcp.sql").read_text(encoding="utf-8")
        self.assertIn("v_num <= 1000", sql)
        self.assertIn("'founding_free'", sql)
        self.assertIn("'paid_required'", sql)
        self.assertGreaterEqual(sql.count("enable row level security"), 10)
        self.assertIn("for update", sql.lower())
        self.assertNotIn("nextval('public.li_activation_seq')", sql)
        self.assertIn("primary key (user_id, run_id)", sql)
        self.assertIn("li_take_rate_limit", sql)
        self.assertIn("create table if not exists public.li_discoveries", sql.lower())
        self.assertIn("primary key (user_id, discovery_id)", sql)


if __name__ == "__main__":
    unittest.main()
