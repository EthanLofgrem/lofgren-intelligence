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
from lofgren_intelligence.kernel.ledger import CostLedger
from lofgren_intelligence.hosted.security import PublicInputError, validate_remote_args
from lofgren_intelligence.hosted.service import PaymentRequired, PublicService
from lofgren_intelligence.hosted.stripe import apply_webhook, verify_webhook

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
        self.usage = []
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

    def take_rate_limit(self, user_id, bucket="mcp", limit=60, window_seconds=60):
        return True

    def record_usage(self, row):
        self.usage.append(dict(row))

    def usage_units_since(self, user_id, since_iso):
        return sum(float(x.get("units") or 0) for x in self.usage if x["user_id"] == user_id)

    def stripe_event_seen(self, event_id):
        return event_id in self.billing_events

    def record_stripe_event(self, row):
        self.billing_events[row["stripe_event_id"]] = dict(row)

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
                "client_reference_id": "u1", "metadata": {"li_user_id": "u1"},
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
            self.assertEqual(apply_webhook(store, parsed), "processed")
            self.assertEqual(store.entitlement["kind"], "paid")
            self.assertEqual(apply_webhook(store, parsed), "duplicate")


class CostingTests(unittest.TestCase):
    def test_external_data_cost_is_reconstructed_without_counting_customer_rate_as_cogs(self):
        ledger = CostLedger(0.0312)
        ledger.record("sense", "external_data", "licensed-provider", work_units=2, external_usd=0.75)
        with patch.dict(os.environ, {"LI_INFRA_USD_PER_RUN": "0"}, clear=False):
            result = actual_run_cost({"name": "heuristic", "usage": {"calls": 0}}, ledger)
        self.assertAlmostEqual(result.known_cost_usd, 0.75, places=6)
        self.assertTrue(result.fully_priced)


class MigrationContractTests(unittest.TestCase):
    def test_first_1000_rule_and_rls_are_present(self):
        sql = Path("supabase/migrations/20261004190000_public_mcp.sql").read_text(encoding="utf-8")
        self.assertIn("v_num <= 1000", sql)
        self.assertIn("'founding_free'", sql)
        self.assertIn("'paid_required'", sql)
        self.assertGreaterEqual(sql.count("enable row level security"), 10)
        self.assertIn("for update", sql.lower())
        self.assertNotIn("nextval('public.li_activation_seq')", sql)
        self.assertIn("primary key (user_id, run_id)", sql)
        self.assertIn("li_take_rate_limit", sql)


if __name__ == "__main__":
    unittest.main()
