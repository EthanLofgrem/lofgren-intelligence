"""Account deletion: the /account page script and the server-side lifecycle (queue item Q18).

The /account page script used to read ``confirm.value``. No variable named ``confirm``
was declared, so the name resolved to ``window.confirm`` (the dialog function, whose
``value`` is undefined) instead of the #confirm input, and every deletion failed with
"Confirmation phrase does not match". The input is now bound explicitly, and a static
check refuses any inline page script that reads a form field through an implicit
(element-id) global.

Server side, deletion now stops new work, cancels research jobs per the job model,
settles or releases usage, refuses (deleting nothing) until everything has settled,
cancels the Stripe subscription, deletes the identity (database cascade), and verifies.
Every store is in-memory and every Stripe call is mocked: no network, Stripe or
Supabase call is made. All accounts are synthetic.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import unittest
import uuid
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from lofgren_intelligence.hosted import service as service_module
from lofgren_intelligence.hosted import web_app
from lofgren_intelligence.hosted.auth import AuthError, OAuthService, token_hash
from lofgren_intelligence.hosted.service import (
    ACCOUNT_DELETION_PHRASE,
    AccountDeletionPending,
    JobNotFound,
    PublicService,
    PublicServiceError,
)
from lofgren_intelligence.hosted.store import StoreError
from lofgren_intelligence.hosted.stripe import StripeAPIError, StripeError, apply_webhook

from .helpers import STRIPE_TEST_ENV, active_researcher, open_researcher_catalog
from .test_durable_jobs import SETTINGS, JobTestBase, at_stage
from .test_public_hosted import FakeStore
from .test_vendor_supabase import _supabase_pages

ROOT = Path(__file__).resolve().parent.parent
HOSTED = ROOT / "lofgren_intelligence" / "hosted"
RESOURCE = "https://li.example/mcp"


# ---- static check: inline page scripts never read form fields through implicit globals ----------

_SCRIPT = re.compile(r"<script\b([^>]*)>(.*?)</script>", re.S | re.I)
_STRING = re.compile(r"'(?:\\.|[^'\\\n])*'|\"(?:\\.|[^\"\\\n])*\"|`(?:\\.|[^`\\])*`", re.S)
_COMMENT = re.compile(r"/\*.*?\*/|//[^\n]*", re.S)
_IDENT = re.compile(r"(?<![\w$.])([A-Za-z_$][\w$]*)(?![\w$])")
_FIELD_ATTR = re.compile(r"\b(?:id|name)=\"([^\"]+)\"")


def inline_scripts(html: str) -> list[str]:
    return [body for attrs, body in _SCRIPT.findall(html) if "src=" not in attrs]


def _strip(js: str) -> str:
    return _COMMENT.sub(" ", _STRING.sub("''", js))


def declared_names(js: str) -> set[str]:
    code = _strip(js)
    names: set[str] = set()
    for decl in re.findall(r"\b(?:const|let|var)\s+([^;]*)", code):
        names.update(re.findall(r"(?:^|,)\s*([A-Za-z_$][\w$]*)\s*=(?!=)", decl))
    for name, params in re.findall(r"\bfunction\s*([A-Za-z_$][\w$]*)?\s*\(([^)]*)\)", code):
        if name:
            names.add(name)
        names.update(re.findall(r"[A-Za-z_$][\w$]*", params))
    for params in re.findall(r"\(([^()]*)\)\s*=>", code):
        names.update(re.findall(r"[A-Za-z_$][\w$]*", params))
    names.update(re.findall(r"([A-Za-z_$][\w$]*)\s*=>", code))
    names.update(re.findall(r"\bcatch\s*\(\s*([A-Za-z_$][\w$]*)\s*\)", code))
    return names


def implicit_field_globals(html: str) -> dict[str, list[str]]:
    """Per inline script: identifiers that name a page element (id/name) but are never declared.

    Such an identifier can only resolve through the window's named-element access, or,
    as with ``confirm``, to an unrelated browser global that shadows the element.
    """
    fields = set(_FIELD_ATTR.findall(html))
    found: dict[str, list[str]] = {}
    for index, body in enumerate(inline_scripts(html)):
        code = _strip(body)
        declared = declared_names(body)
        bad = set()
        for match in _IDENT.finditer(code):
            name = match.group(1)
            if name not in fields or name in declared:
                continue
            before = code[:match.start()].rstrip()
            after = code[match.end():].lstrip()
            if before.endswith(("{", ",")) and after.startswith(":") and not after.startswith("::"):
                continue  # an object-literal key, not a variable
            bad.add(name)
        if bad:
            found[f"script[{index}]"] = sorted(bad)
    return found


class AccountPageScriptTests(unittest.TestCase):
    def _account_script(self):
        pages = _supabase_pages()
        html = pages["account"].body.decode("utf-8")
        scripts = inline_scripts(html)
        self.assertEqual(len(scripts), 1)
        return html, scripts[0]

    def test_the_confirmation_input_is_bound_explicitly(self):
        html, script = self._account_script()
        self.assertIn('<input id="confirm"', html)
        self.assertIn("const confirmInput=document.querySelector('#confirm');", script)
        code = _strip(script)
        # Both the check and the request body read the bound element.
        self.assertEqual(code.count("confirmInput.value"), 2)
        # `confirm` is never used as a bare identifier (that is window.confirm).
        self.assertNotRegex(code, r"(?<![\w$.])confirm(?![\w$])")
        self.assertEqual(implicit_field_globals(html), {})

    def test_the_checker_catches_the_original_defect(self):
        html, script = self._account_script()
        broken = html.replace("const confirmInput=document.querySelector('#confirm');\n", "")
        broken = broken.replace("confirmInput.value", "confirm.value")
        self.assertEqual(implicit_field_globals(broken), {"script[0]": ["confirm"]})
        # Object keys and property names that equal a field id are not variable uses.
        ok = '<input id="email"><script>const x={email:1};y.email=2;</script>'
        self.assertEqual(implicit_field_globals(ok), {})

    def test_no_inline_script_on_any_page_relies_on_implicit_globals_for_form_fields(self):
        pages = _supabase_pages()
        rendered = 0
        for name, response in pages.items():
            html = response.body.decode("utf-8")
            rendered += len(inline_scripts(html))
            with self.subTest(page=name):
                self.assertEqual(implicit_field_globals(html), {})
        # Every inline script in the hosted package is on one of the pages checked above:
        # a new page with an inline script must be added to the rendered set.
        source_inline = 0
        for path in sorted(HOSTED.glob("*.py")):
            for attrs in re.findall(r"<script\b([^>]*)>", path.read_text(encoding="utf-8")):
                if "src=" not in attrs:
                    source_inline += 1
        self.assertEqual(source_inline, rendered)
        self.assertEqual(rendered, 4)


# ---- server-side lifecycle --------------------------------------------------------------------

class TwoTenantStore(FakeStore):
    """FakeStore with a second synthetic tenant (u2) that has its own account and entitlement."""

    def __init__(self, **kw):
        super().__init__(**kw)
        self.others = {
            "u2": {
                "account": {"user_id": "u2", "email": "u2@example.com", "activation_number": 2},
                "entitlement": {"user_id": "u2", "kind": "founding_free", "plan_id": "founding_free",
                                "active": True, "quota_units_per_week": 5000.0},
            }
        }

    def get_account(self, user_id):
        if user_id == "u1":
            return self.account
        other = self.others.get(user_id)
        return other["account"] if other else None

    def get_entitlement(self, user_id):
        if user_id == "u1":
            return self.entitlement
        other = self.others.get(user_id)
        return other["entitlement"] if other else None

    def _as(self, user_id, fn, *args):
        if user_id == "u1":
            return fn(*args)
        saved = self.entitlement
        self.entitlement = self.others[user_id]["entitlement"]
        try:
            return fn(*args)
        finally:
            self.entitlement = saved

    def reserve_usage(self, reservation_id, user_id, operation, units):
        return self._as(user_id, super().reserve_usage, reservation_id, user_id, operation, units)

    def finalize_usage(self, reservation_id, *args):
        row = self.reservations.get(reservation_id)
        owner = row["user_id"] if row else "u1"
        return self._as(owner, super().finalize_usage, reservation_id, *args)

    def close_entitlement(self, user_id):
        if user_id == "u1":
            return super().close_entitlement(user_id)
        if user_id in self.others:
            self.others[user_id]["entitlement"]["active"] = False


class DeletionTestBase(JobTestBase):
    def setUp(self):
        super().setUp()
        self.store = TwoTenantStore(quota=5000)
        self.store.job_clock = self.clock
        self.store.case_clock = self.clock
        self.service = PublicService(self.store, clock=self.clock)
        self.service.job_settings = SETTINGS
        self.calls = []
        self._seed()

    def _seed(self):
        """Synthetic tenant data for u1 (to delete) and u2 (must be untouched)."""
        for uid in ("u1", "u2"):
            self.store.save_run({"user_id": uid, "run_id": "RR-SAME", "snapshot": {"owner": uid}})
            self.store.save_discovery({"user_id": uid, "discovery_id": "D-1", "owner": uid})
            self.store.save_artifact({"user_id": uid, "artifact_id": "A-1", "owner": uid})
            self.store.save_action({"user_id": uid, "action_id": "X-1", "owner": uid})
            self.store.save_outcome({"user_id": uid, "outcome_id": "O-1", "owner": uid})
            self.store.save_improvement({"user_id": uid, "improvement_id": "I-1", "owner": uid})
            case_id = str(uuid.uuid4())
            self.store.create_case(case_id, uid, "objective", "clarifying", "h" * 64, {}, {})
            self.store.case_approvals["ap-" + uid] = {"id": "ap-" + uid, "case_id": case_id, "user_id": uid,
                                                      "consumed_at": None, "revoked_at": None}
            self.store.usage.append({"id": "ev-" + uid, "user_id": uid, "run_id": "RR-SAME",
                                     "operation": "investigate", "units": 1.0, "known_cost_usd": 0.0,
                                     "unpriced_components": []})
        self.tokens = {uid: OAuthService(self.store)._issue_tokens(uid, "client-1", "mcp", RESOURCE)
                       for uid in ("u1", "u2")}
        for uid in ("u1", "u2"):
            self.store.put_oauth_code({"code_hash": "code-" + uid, "user_id": uid, "client_id": "client-1"})

    def delete(self, user_id="u1"):
        return self.service.delete_account(user_id, ACCOUNT_DELETION_PHRASE)

    def record(self, name, fn):
        def wrapper(*a, **k):
            self.calls.append(name)
            return fn(*a, **k)
        return wrapper

    def assert_u1_gone(self):
        s = self.store
        self.assertIsNone(s.get_account("u1"))
        self.assertIsNone(s.get_entitlement("u1"))
        for table in (s.runs, s.discoveries, s.artifacts, s.actions, s.outcomes, s.improvements):
            self.assertFalse([k for k in table if k[0] == "u1"])
        for table in (s.cases, s.case_approvals, s.reservations, s.access_tokens, s.refresh_tokens,
                      s.oauth_codes, s.jobs):
            self.assertFalse([k for k, v in table.items() if v.get("user_id") == "u1"])
        self.assertFalse([e for e in s.case_events if e.get("user_id") == "u1"])
        self.assertFalse([e for e in s.usage if e["user_id"] == "u1"])

    def assert_u2_intact(self):
        s = self.store
        self.assertEqual(s.get_account("u2")["user_id"], "u2")
        self.assertTrue(s.get_entitlement("u2")["active"])
        self.assertEqual(s.get_run("u2", "RR-SAME")["snapshot"]["owner"], "u2")
        for getter, key in ((s.get_discovery, "D-1"), (s.get_artifact, "A-1"), (s.get_action, "X-1"),
                            (s.get_outcome, "O-1"), (s.get_improvement, "I-1")):
            self.assertEqual(getter("u2", key)["owner"], "u2")
        self.assertEqual(len(s.list_cases("u2")), 1)
        self.assertIn("ap-u2", s.case_approvals)
        self.assertEqual([e["id"] for e in s.usage if e["user_id"] == "u2"], ["ev-u2"])
        self.assertEqual(OAuthService(s).authenticate(self.tokens["u2"]["access_token"]).user_id, "u2")
        self.assertIsNone(s.oauth_codes["code-u2"].get("used_at"))


class DeletionLifecycleTests(DeletionTestBase):
    def test_wrong_phrase_changes_nothing(self):
        with self.assertRaises(PublicServiceError):
            self.service.delete_account("u1", "delete my lofgren intelligence account")
        self.assertTrue(self.store.get_entitlement("u1")["active"])
        self.assertEqual(OAuthService(self.store).authenticate(self.tokens["u1"]["access_token"]).user_id, "u1")

    def test_complete_deletion_removes_every_tenant_record_and_leaves_the_other_tenant_untouched(self):
        u2_job = self.service.start_research("u2", {"objective": "Other account objective",
                                                    "texts": {"doc (fictional)": "Synthetic text."},
                                                    "idempotency_key": "u2-k"}, "https://li.example")["job_id"]
        out = self.delete()
        self.assertEqual(out, {"deleted": True, "subscription_cancelled": False, "research_jobs_cancelled": 0,
                               "usage_settled": 0, "usage_released": 0})
        self.assert_u1_gone()
        self.assert_u2_intact()
        self.assertEqual(self.store.jobs[u2_job]["status"], "queued")
        self.assertEqual(self.store.reservations[self.store.jobs[u2_job]["reservation_id"]]["status"], "reserved")

    def test_the_users_data_is_inaccessible_afterwards(self):
        job_id = self.start("before")["job_id"]
        self.drain()
        self.delete()
        with self.assertRaises(PublicServiceError):
            self.service.export_account_data("u1")
        with self.assertRaises(JobNotFound):
            self.service.get_job_status("u1", {"job_id": job_id})
        self.assertIsNone(self.store.get_run("u1", "RR-SAME"))
        self.assertEqual(self.store.list_runs("u1"), [])
        self.assertEqual(self.store.list_cases("u1"), [])
        self.assertEqual(self.store.list_research_jobs("u1"), [])
        with self.assertRaises(PublicServiceError):  # no entitlement: no new metered work
            self.service.start_research("u1", {"objective": "x", "texts": {"d": "y"}, "idempotency_key": "after"},
                                        "https://li.example")
        with self.assertRaises(AuthError):
            OAuthService(self.store).authenticate(self.tokens["u1"]["access_token"])

    def test_tokens_are_revoked_before_anything_else_even_when_deletion_must_wait(self):
        self.store.reserve_usage("res-live", "u1", "investigate", 5.0)  # an inline call in flight
        with self.assertRaises(AccountDeletionPending):
            self.delete()
        # Nothing deleted, but the account is closed: no MCP access, no refresh, no code exchange,
        # and no new reservation.
        self.assertIsNotNone(self.store.get_account("u1"))
        with self.assertRaises(AuthError):
            OAuthService(self.store).authenticate(self.tokens["u1"]["access_token"])
        self.assertIsNone(self.store.consume_refresh_token(token_hash(self.tokens["u1"]["refresh_token"])))
        self.assertIsNone(self.store.consume_oauth_code("code-u1"))
        self.assertFalse(self.store.get_entitlement("u1")["active"])
        self.assertFalse(self.store.reserve_usage("res-new", "u1", "investigate", 1.0))
        self.assert_u2_intact()

    def test_a_queued_job_is_cancelled_and_its_reservation_released(self):
        job_id = self.start("queued")["job_id"]
        reservation_id = self.store.jobs[job_id]["reservation_id"]
        seen = {}
        original = self.store.delete_auth_user

        def snapshot(user_id):
            seen["job"] = dict(self.store.jobs[job_id])
            seen["reservation"] = dict(self.store.reservations[reservation_id])
            return original(user_id)

        self.store.delete_auth_user = snapshot
        with patch.object(service_module, "run_investigation") as run:
            out = self.delete()
        run.assert_not_called()
        self.assertEqual(out["research_jobs_cancelled"], 1)
        self.assertEqual((seen["job"]["status"], seen["job"]["error_code"]), ("cancelled", "CANCELLED"))
        self.assertEqual(seen["reservation"]["status"], "released")
        self.assert_u1_gone()
        self.assertEqual(self.drain(), [])  # nothing left for a worker to pick up

    def test_a_running_job_stops_and_deletion_waits_until_it_settles(self):
        job_id = self.start("running")["job_id"]
        seen = {}

        def delete_mid_run(ledger):
            seen["units"] = ledger.total_units
            with patch.object(service_module, "cancel_subscription") as cancel:
                try:
                    self.delete()
                except AccountDeletionPending as exc:
                    seen["refused"] = str(exc)
                seen["stripe_called"] = cancel.called
            seen["job_status"] = self.store.jobs[job_id]["status"]

        with at_stage("verify", delete_mid_run):
            results = self.drain()
        self.assertIn("1 research job(s) stopping", seen["refused"])
        self.assertFalse(seen["stripe_called"])
        self.assertEqual(seen["job_status"], "cancel_requested")
        # The worker honoured the cancel at its next stage and charged the partial work once.
        self.assertEqual(results[0]["status"], "cancelled")
        reservation = self.store.reservations[self.store.jobs[job_id]["reservation_id"]]
        self.assertEqual(reservation["status"], "settled")
        charged = [e for e in self.store.usage if e["id"] == self.store.jobs[job_id]["reservation_id"]]
        self.assertEqual(len(charged), 1)
        self.assertAlmostEqual(charged[0]["units"], seen["units"])
        self.assertIsNotNone(self.store.get_account("u1"))  # nothing was deleted by the refused attempt
        out = self.delete()
        self.assertTrue(out["deleted"])
        self.assertEqual(out["research_jobs_cancelled"], 0)
        self.assert_u1_gone()
        self.assert_u2_intact()

    def test_an_open_reservation_waits_for_its_call_then_settles_before_deletion(self):
        rid = "res-inline"
        self.assertTrue(self.store.reserve_usage(rid, "u1", "investigate", 5.0))
        with self.assertRaises(AccountDeletionPending) as ctx:
            self.delete()
        self.assertIn("1 usage reservation(s) open", str(ctx.exception))
        self.assertEqual(self.store.reservations[rid]["status"], "reserved")
        # The in-flight call finishes and settles its own reservation (finalize ignores `active`).
        self.assertEqual(self.service._settle_work(rid, run_id=None, actual_units=3.0), "settled")
        seen = {}
        original = self.store.delete_auth_user
        self.store.delete_auth_user = lambda uid: (seen.setdefault("usage", [dict(e) for e in self.store.usage
                                                                             if e["id"] == rid]),
                                                   original(uid))[1]
        out = self.delete()
        self.assertTrue(out["deleted"])
        self.assertEqual(seen["usage"][0]["units"], 3.0)
        self.assert_u1_gone()

    def test_an_abandoned_open_reservation_is_released_as_li_reserve_usage_would_expire_it(self):
        rid = "res-stale"
        self.assertTrue(self.store.reserve_usage(rid, "u1", "investigate", 5.0))
        self.clock.advance(3601)
        statuses = {}
        original = self.store.delete_auth_user
        self.store.delete_auth_user = lambda uid: (statuses.setdefault(rid, self.store.reservations[rid]["status"]),
                                                   original(uid))[1]
        out = self.delete()
        self.assertEqual(out["usage_released"], 1)
        self.assertEqual(statuses[rid], "released")
        self.assertFalse([e for e in self.store.usage if e["id"] == rid])  # released, never charged

    def test_a_reservation_with_an_unreadable_timestamp_is_never_released(self):
        self.assertTrue(self.store.reserve_usage("res-odd", "u1", "investigate", 5.0))
        self.store.reservations["res-odd"]["created_at"] = "not-a-time"
        self.clock.advance(7200)
        with self.assertRaises(AccountDeletionPending):
            self.delete()
        self.assertEqual(self.store.reservations["res-odd"]["status"], "reserved")

    def test_an_unsettled_usage_marker_is_reconciled_exactly_once_before_deletion(self):
        rid = "res-marker"
        self.assertTrue(self.store.reserve_usage(rid, "u1", "investigate", 5.0))
        self.assertTrue(self.store.mark_usage_unsettled(rid, "RR-SAME", 4.0, 0.02, ["partial_run"]))
        self.store.settle_usage = self.record("settle", self.store.settle_usage)
        self.store.delete_auth_user = self.record("delete", self.store.delete_auth_user)
        charged = {}
        original = self.store.delete_auth_user
        self.store.delete_auth_user = lambda uid: (charged.setdefault(
            "events", [dict(e) for e in self.store.usage if e["id"] == rid]), original(uid))[1]
        out = self.delete()
        self.assertEqual(out["usage_settled"], 1)
        self.assertEqual(self.calls, ["settle", "delete"])
        self.assertEqual(len(charged["events"]), 1)
        self.assertEqual((charged["events"][0]["units"], charged["events"][0]["known_cost_usd"]), (4.0, 0.02))
        self.assert_u1_gone()

    def test_a_marker_that_cannot_be_settled_stops_deletion(self):
        rid = "res-marker-2"
        self.assertTrue(self.store.reserve_usage(rid, "u1", "investigate", 5.0))
        self.assertTrue(self.store.mark_usage_unsettled(rid, None, 4.0, 0.0, []))
        with patch.object(self.store, "settle_usage", side_effect=StoreError("database is unreachable")):
            with self.assertRaises(StoreError):
                self.delete()
        self.assertIsNotNone(self.store.get_account("u1"))
        self.assertEqual(self.store.reservations[rid]["status"], "unsettled")
        with patch.object(self.store, "settle_usage", return_value="missing"):
            with self.assertRaises(AccountDeletionPending):
                self.delete()
        self.assertIsNotNone(self.store.get_account("u1"))
        self.assertTrue(self.delete()["deleted"])  # the retry reconciles and completes

    def test_a_deletion_that_does_not_take_effect_is_not_reported_as_done(self):
        with patch.object(self.store, "delete_auth_user", return_value=True):
            with self.assertRaises(PublicServiceError):
                self.delete()


class DeletionStripeTests(DeletionTestBase):
    def setUp(self):
        super().setUp()
        self.store.entitlement.update({"kind": "paid", "plan_id": "researcher",
                                       "stripe_subscription_id": "sub_test", "stripe_customer_id": "cus_test"})

    def test_the_subscription_is_cancelled_before_identity_and_data_are_removed(self):
        self.store.delete_auth_user = self.record("delete", self.store.delete_auth_user)
        with patch.object(service_module, "cancel_subscription",
                          side_effect=lambda sub: self.calls.append("cancel:" + sub)) as cancel:
            out = self.delete()
        cancel.assert_called_once_with("sub_test")
        self.assertEqual(self.calls, ["cancel:sub_test", "delete"])
        self.assertTrue(out["subscription_cancelled"])
        self.assert_u1_gone()
        self.assert_u2_intact()

    def test_a_failed_cancellation_stops_deletion_and_a_retry_completes_it(self):
        with patch.object(service_module, "cancel_subscription",
                          side_effect=StripeAPIError(500, "api_error", "")):
            with self.assertRaises(StripeError):
                self.delete()
        # Nothing was removed; the account stays closed to new work.
        self.assertIsNotNone(self.store.get_account("u1"))
        self.assertEqual(self.store.get_entitlement("u1")["stripe_subscription_id"], "sub_test")
        self.assertEqual(self.store.get_run("u1", "RR-SAME")["snapshot"]["owner"], "u1")
        self.assertFalse(hasattr(self.store, "deleted_user"))
        with patch.object(service_module, "cancel_subscription") as cancel:
            self.assertTrue(self.delete()["deleted"])
        cancel.assert_called_once_with("sub_test")
        self.assert_u1_gone()

    def test_stripe_is_not_called_while_deletion_must_wait(self):
        self.store.reserve_usage("res-live", "u1", "investigate", 5.0)
        with patch.object(service_module, "cancel_subscription") as cancel:
            with self.assertRaises(AccountDeletionPending):
                self.delete()
        cancel.assert_not_called()

    def test_retry_after_identity_deletion_failed_is_idempotent(self):
        original = self.store.delete_auth_user
        self.store.delete_auth_user = lambda uid: (_ for _ in ()).throw(StoreError("database is unreachable"))
        with patch.object(service_module, "cancel_subscription") as cancel:
            with self.assertRaises(StoreError):
                self.delete()
            self.store.delete_auth_user = original
            self.assertTrue(self.delete()["deleted"])
            # Stripe treats the second cancellation of an ended subscription as cancelled
            # (cancel_subscription); it is called again rather than skipped on a guess.
            self.assertEqual(cancel.call_count, 2)
            # A further retry after success: the identity is already gone.
            self.assertTrue(self.delete()["deleted"])
        self.assert_u1_gone()
        self.assert_u2_intact()

    def test_webhooks_after_deletion_never_recreate_or_grant_an_entitlement(self):
        with patch.object(service_module, "cancel_subscription"):
            self.delete()
        ended = {"id": "evt_deleted", "type": "customer.subscription.deleted",
                 "data": {"object": {"id": "sub_test", "customer": "cus_test",
                                     "metadata": {"li_user_id": "u1"}}}}
        self.assertEqual(apply_webhook(self.store, ended, subscription_status=lambda _s: "canceled"), "processed")
        self.assertIsNone(self.store.get_entitlement("u1"))
        self.assertIn("evt_deleted", self.store.billing_events)
        # A paid activation for the deleted account is refused and not receipted (operator decision).
        paid = {"id": "evt_paid", "type": "checkout.session.completed",
                "data": {"object": {"client_reference_id": "u1", "subscription": "sub_new",
                                    "customer": "cus_test", "payment_status": "paid"}}}
        with patch.dict(os.environ, STRIPE_TEST_ENV):
            with self.assertRaises(StripeError):
                apply_webhook(self.store, paid, subscription_status=active_researcher,
                              catalog=open_researcher_catalog())
        self.assertIsNone(self.store.get_entitlement("u1"))
        self.assertNotIn("evt_paid", self.store.billing_events)



# ---- concurrent deletion vs a research worker (owner review, Q18) --------------------------------

class DeletionVersusWorkerTests(DeletionTestBase):
    """A worker that is mid-job when deletion starts must recreate nothing for the deleted user.

    Deletion refuses while a job is open (queued/running/cancel_requested), so the only worker
    that can still be running after the cascade is one whose job ended under it: its lease
    expired and li_reclaim_research_jobs ended the job (no attempts left). That is exactly the
    worker modelled here, at three moments: inside a research stage, after the last stage but
    before the result is saved, and after the result is saved but before the job is completed.
    Every write it then attempts names a deleted auth user; the auth.users(id) foreign key
    refuses it (FakeStore models the refusal, tests/pg proves it on PostgreSQL), and every job
    RPC finds no job. The worker ends with status 'lease_lost' and never raises.
    """

    def _end_the_job_under_the_worker_then_delete(self, job_id, seen):
        self.clock.advance(SETTINGS.lease_seconds + 1)
        reclaimed = self.store.reclaim_research_jobs(100, SETTINGS.queue_ttl_seconds, 3)
        self.assertIn(job_id, reclaimed)
        job = self.store.jobs[job_id]
        self.assertEqual((job["status"], job["error_code"]), ("failed", "LEASE_EXPIRED"))
        with patch.object(service_module, "cancel_subscription") as cancel:
            seen["deleted"] = self.delete()
        cancel.assert_not_called()  # u1 has no subscription in this fixture
        seen["u1_rows_at_deletion"] = self.u1_rows()
        self.assertEqual(seen["u1_rows_at_deletion"], {})

    def u1_rows(self):
        s = self.store
        rows = {
            "runs": [k for k in s.runs if k[0] == "u1"],
            "jobs": [k for k, v in s.jobs.items() if v["user_id"] == "u1"],
            "reservations": [k for k, v in s.reservations.items() if v["user_id"] == "u1"],
            "usage": [e["id"] for e in s.usage if e["user_id"] == "u1"],
            "discoveries": [k for k in s.discoveries if k[0] == "u1"],
            "artifacts": [k for k in s.artifacts if k[0] == "u1"],
            "actions": [k for k in s.actions if k[0] == "u1"],
            "outcomes": [k for k in s.outcomes if k[0] == "u1"],
            "improvements": [k for k in s.improvements if k[0] == "u1"],
            "cases": [k for k, v in s.cases.items() if v.get("user_id") == "u1"],
            "case_events": [e for e in s.case_events if e.get("user_id") == "u1"],
        }
        return {k: v for k, v in rows.items() if v}

    def _run_zombie(self, moment):
        job_id = self.start("zombie-" + moment)["job_id"]
        self.store.jobs[job_id]["max_attempts"] = 1  # the reclaim ends the job instead of requeueing it
        seen = {"refused_writes": []}
        original_save_run = self.store.save_run

        def save_run(row):
            try:
                return original_save_run(row)
            except StoreError:
                seen["refused_writes"].append(("save_run", row["user_id"], row["run_id"]))
                raise

        self.store.save_run = save_run
        original_persist = PublicService._persist_run

        def persist(service, user_id, objective, attempt):
            if moment == "before_save":
                self._end_the_job_under_the_worker_then_delete(job_id, seen)
            out = original_persist(service, user_id, objective, attempt)
            seen["saved_run_id"] = attempt.snap["run_id"]
            if moment == "after_save":
                self.assertIsNotNone(self.store.get_run("u1", attempt.snap["run_id"]))
                self._end_the_job_under_the_worker_then_delete(job_id, seen)
            return out

        with patch.object(PublicService, "_persist_run", persist):
            if moment == "mid_research":
                with at_stage("verify", lambda _ledger: self._end_the_job_under_the_worker_then_delete(job_id, seen)):
                    results = self.drain()
            else:
                results = self.drain()
        return job_id, seen, results

    def _assert_nothing_recreated(self, job_id, seen, results):
        self.assertTrue(seen["deleted"]["deleted"])
        # The completion failed cleanly: no exception, no success, no crash.
        self.assertEqual([r["status"] for r in results], ["lease_lost"])
        self.assertNotIn("run_id", results[0])
        # Nothing exists for the deleted user afterwards: no run, job, usage, reservation or artifact.
        self.assertEqual(self.u1_rows(), {})
        self.assertNotIn(job_id, self.store.jobs)
        self.assert_u1_gone()
        self.assert_u2_intact()
        self.assertEqual(self.store.list_research_jobs("u1"), [])
        self.assertEqual(self.store.list_usage("u1"), [])

    def test_a_worker_inside_a_research_stage_recreates_nothing(self):
        job_id, seen, results = self._run_zombie("mid_research")
        self._assert_nothing_recreated(job_id, seen, results)
        self.assertNotIn("saved_run_id", seen)  # it stopped at the next stage boundary
        self.assertEqual(seen["refused_writes"], [])

    def test_a_worker_about_to_save_its_result_recreates_nothing(self):
        job_id, seen, results = self._run_zombie("before_save")
        self._assert_nothing_recreated(job_id, seen, results)
        # It did try to save: the foreign key refused the write for the deleted user.
        self.assertEqual(len(seen["refused_writes"]), 1)
        self.assertEqual(seen["refused_writes"][0][1], "u1")
        self.assertNotIn("saved_run_id", seen)

    def test_a_worker_that_saved_before_deletion_has_its_result_cascaded_and_cannot_complete(self):
        job_id, seen, results = self._run_zombie("after_save")
        self._assert_nothing_recreated(job_id, seen, results)
        self.assertIsNone(self.store.get_run("u1", seen["saved_run_id"]))
        self.assertEqual(seen["refused_writes"], [])

    def test_every_late_write_for_the_deleted_user_is_refused(self):
        with patch.object(service_module, "cancel_subscription"):
            self.assertTrue(self.delete()["deleted"])
        s = self.store
        for name, call in (
            ("save_run", lambda: s.save_run({"user_id": "u1", "run_id": "RR-LATE", "snapshot": {}})),
            ("save_discovery", lambda: s.save_discovery({"user_id": "u1", "discovery_id": "D-LATE"})),
            ("save_artifact", lambda: s.save_artifact({"user_id": "u1", "artifact_id": "A-LATE"})),
            ("save_action", lambda: s.save_action({"user_id": "u1", "action_id": "X-LATE"})),
            ("save_outcome", lambda: s.save_outcome({"user_id": "u1", "outcome_id": "O-LATE"})),
            ("save_improvement", lambda: s.save_improvement({"user_id": "u1", "improvement_id": "I-LATE"})),
            ("record_usage", lambda: s.record_usage({"id": "ev-late", "user_id": "u1", "units": 1.0})),
            ("enqueue_research_job", lambda: s.enqueue_research_job("job-late", "u1", None, "investigate", "late",
                                                                    {}, "res-late", 1, 600)),
            ("create_case", lambda: s.create_case(str(uuid.uuid4()), "u1", "o", "clarifying", "h" * 64, {}, {})),
        ):
            with self.subTest(write=name):
                with self.assertRaises(StoreError):
                    call()
        self.assertFalse(s.reserve_usage("res-late", "u1", "investigate", 1.0))
        self.assertIsNone(s.heartbeat_research_job("job-late", "w1", 60, 60, None, 1.0))
        self.assertEqual(self.u1_rows(), {})
        self.assert_u2_intact()


# ---- provider failure partway through deletion (owner review, Q18) -------------------------------

class DeletionPartialFailureTests(DeletionTestBase):
    """A failure at any step stops deletion without reporting success; a retry is safe and completes.

    Each step that talks to the database or Stripe is failed in turn, on a paid account that has
    a queued job, an unsettled usage marker and an abandoned reservation, so every step runs.
    """

    STEPS = (
        ("revoke_user_credentials", "store", StoreError("database is unreachable")),
        ("close_entitlement", "store", StoreError("database is unreachable")),
        ("list_research_jobs", "store", StoreError("database is unreachable")),
        ("request_cancel_research_job", "store", StoreError("database is unreachable")),
        ("list_open_usage_reservations", "store", StoreError("database is unreachable")),
        ("settle_usage", "store", StoreError("database is unreachable")),
        ("release_usage", "store", StoreError("database is unreachable")),
        ("cancel_subscription", "stripe", StripeAPIError(500, "api_error", "")),
        ("cancel_subscription", "stripe", StripeError("Stripe API is unreachable")),
        ("cancel_subscription", "stripe", StripeError("Stripe did not confirm that the subscription was cancelled")),
        ("delete_auth_user", "store", StoreError("database is unreachable")),
    )
    BEFORE_STRIPE = {"revoke_user_credentials", "close_entitlement", "list_research_jobs",
                     "request_cancel_research_job", "list_open_usage_reservations", "settle_usage",
                     "release_usage"}

    def fixture(self):
        self.store.entitlement.update({"kind": "paid", "plan_id": "researcher",
                                       "stripe_subscription_id": "sub_test", "stripe_customer_id": "cus_test"})
        job_id = self.start("queued")["job_id"]
        self.assertTrue(self.store.reserve_usage("res-marker", "u1", "investigate", 5.0))
        self.assertTrue(self.store.mark_usage_unsettled("res-marker", "RR-SAME", 4.0, 0.02, ["partial_run"]))
        self.assertTrue(self.store.reserve_usage("res-stale", "u1", "investigate", 5.0))
        self.clock.advance(3601)
        return job_id

    def test_a_failure_at_any_step_reports_no_success_and_a_retry_completes_safely(self):
        for step, target, error in self.STEPS:
            with self.subTest(step=step, error=str(error)):
                self.setUp()
                job_id = self.fixture()
                if target == "store":
                    first = patch.object(self.store, step, side_effect=error)
                    stripe_first = patch.object(service_module, "cancel_subscription")
                else:
                    first = patch.object(self.store, "get_account", wraps=self.store.get_account)  # no-op
                    stripe_first = patch.object(service_module, "cancel_subscription", side_effect=error)
                with first, stripe_first as cancel:
                    with self.assertRaises((StoreError, StripeError, PublicServiceError)):
                        self.delete()
                    stripe_calls = cancel.call_count
                # No success reported, nothing removed, and Stripe untouched before its step.
                self.assertEqual(stripe_calls, 0 if step in self.BEFORE_STRIPE else 1)
                self.assertIsNotNone(self.store.get_account("u1"))
                self.assertEqual(self.store.get_entitlement("u1")["stripe_subscription_id"], "sub_test")
                self.assertEqual(self.store.get_run("u1", "RR-SAME")["snapshot"]["owner"], "u1")
                self.assertFalse(hasattr(self.store, "deleted_user"))
                # The retry completes; the marker is charged exactly once across both attempts.
                charged = {}
                original = self.store.delete_auth_user

                def snapshot(uid, original=original, charged=charged):
                    charged["marker"] = [dict(e) for e in self.store.usage if e["id"] == "res-marker"]
                    charged["stale"] = [dict(e) for e in self.store.usage if e["id"] == "res-stale"]
                    charged["job"] = dict(self.store.jobs[job_id])
                    return original(uid)

                self.store.delete_auth_user = snapshot
                with patch.object(service_module, "cancel_subscription") as cancel:
                    out = self.delete()
                cancel.assert_called_once_with("sub_test")
                self.assertTrue(out["deleted"])
                self.assertTrue(out["subscription_cancelled"])
                self.assertEqual(len(charged["marker"]), 1)
                self.assertEqual((charged["marker"][0]["units"], charged["marker"][0]["known_cost_usd"]), (4.0, 0.02))
                self.assertEqual(charged["stale"], [])  # released, never charged
                self.assertEqual(charged["job"]["status"], "cancelled")
                self.assert_u1_gone()
                self.assert_u2_intact()

    def test_a_deletion_whose_identity_removal_had_no_effect_is_not_reported_as_done(self):
        self.fixture()
        with patch.object(service_module, "cancel_subscription"):
            with patch.object(self.store, "delete_auth_user", return_value=True):
                with self.assertRaises(PublicServiceError):
                    self.delete()
            self.assertIsNotNone(self.store.get_account("u1"))
            self.assertTrue(self.delete()["deleted"])
        self.assert_u1_gone()

    def test_an_unconfirmed_stripe_cancellation_stops_deletion(self):
        from lofgren_intelligence.hosted import stripe as stripe_module
        self.fixture()
        for answer in ({"id": "sub_test", "status": "active"}, {"id": "sub_test"}, None, []):
            with self.subTest(answer=answer):
                with patch.object(stripe_module, "stripe_delete", return_value=answer), \
                        patch.object(stripe_module, "stripe_get") as get:
                    with self.assertRaises(StripeError) as ctx:
                        self.delete()
                get.assert_not_called()
                self.assertIn("did not confirm", str(ctx.exception))
                self.assertIsNotNone(self.store.get_account("u1"))
                self.assertFalse(hasattr(self.store, "deleted_user"))
        with patch.object(stripe_module, "stripe_delete", return_value={"id": "sub_test", "status": "canceled"}):
            self.assertTrue(self.delete()["deleted"])
        self.assert_u1_gone()

    def test_the_route_never_reports_success_for_a_failed_step(self):
        store = FakeStore(quota=500)
        store.entitlement.update({"stripe_subscription_id": "sub_test"})
        for error in (StripeAPIError(500, "api_error", ""), StripeError("Stripe API is unreachable")):
            with self.subTest(error=str(error)):
                with patch.object(service_module, "cancel_subscription", side_effect=error):
                    status, body = _post(store, {"confirmation": ACCOUNT_DELETION_PHRASE})
                self.assertEqual((status, body["error"]), (400, "account_deletion_refused"))
                self.assertNotIn("deleted", body)
                self.assertIsNotNone(store.account)
        with patch.object(store, "delete_auth_user", side_effect=StoreError("database is unreachable")), \
                patch.object(service_module, "cancel_subscription"):
            status, body = _post(store, {"confirmation": ACCOUNT_DELETION_PHRASE})
        self.assertEqual((status, body["error"]), (400, "account_deletion_refused"))
        self.assertNotIn("deleted", body)
        with patch.object(service_module, "cancel_subscription"):
            status, body = _post(store, {"confirmation": ACCOUNT_DELETION_PHRASE})
        self.assertEqual((status, body["deleted"]), (200, True))


# ---- HTTP route -------------------------------------------------------------------------------

def _post(store, body, token="supabase-session"):
    scope = {
        "type": "http", "method": "POST", "path": "/account/delete", "raw_path": b"/account/delete",
        "query_string": b"", "headers": [(b"authorization", f"Bearer {token}".encode()),
                                         (b"content-type", b"application/json")],
        "scheme": "https", "server": ("li.example", 443), "client": ("203.0.113.9", 1234), "root_path": "",
    }
    sent = {"done": False}

    async def receive():
        if sent["done"]:
            return {"type": "http.disconnect"}
        sent["done"] = True
        return {"type": "http.request", "body": json.dumps(body).encode(), "more_body": False}

    from starlette.requests import Request
    with patch.object(web_app, "SupabaseStore", lambda *a, **k: store):
        response = asyncio.run(web_app.account_delete(Request(scope, receive)))
    return response.status_code, json.loads(response.body)


class AccountDeleteRouteTests(unittest.TestCase):
    def test_pending_deletion_is_a_409_and_completion_a_200(self):
        store = FakeStore(quota=500)
        store.reserve_usage("res-live", "u1", "investigate", 5.0)
        status, body = _post(store, {"confirmation": ACCOUNT_DELETION_PHRASE})
        self.assertEqual((status, body["error"]), (409, "account_deletion_pending"))
        self.assertIn("Retry shortly", body["error_description"])
        store.release_usage("res-live")
        status, body = _post(store, {"confirmation": ACCOUNT_DELETION_PHRASE})
        self.assertEqual((status, body["deleted"]), (200, True))
        self.assertEqual(store.deleted_user, "u1")

    def test_wrong_phrase_and_bad_session_are_refused(self):
        store = FakeStore()
        self.assertEqual(_post(store, {"confirmation": "DELETE"})[0], 400)
        with patch.object(store, "verify_supabase_user", side_effect=StoreError("invalid Supabase user session")):
            self.assertEqual(_post(store, {"confirmation": ACCOUNT_DELETION_PHRASE}, token="forged")[0], 400)
        self.assertIsNotNone(store.account)


# ---- store contract ---------------------------------------------------------------------------

class _Recorder:
    def __init__(self, fail=None):
        self.calls, self.fail = [], fail

    def __call__(self, url, method="GET", headers=None, body=None, **kw):
        from lofgren_intelligence.hosted.http import JSONResponse
        self.calls.append((method, url, body))
        if self.fail is not None:
            raise self.fail
        return JSONResponse(200, {}, [] if method == "GET" else None)


class DeletionStoreContractTests(unittest.TestCase):
    def _store(self):
        from lofgren_intelligence.hosted import store as li_store
        return li_store, li_store.SupabaseStore("https://db.example", "service-role-test", "publishable-test")

    def test_credentials_are_revoked_for_exactly_one_user(self):
        li_store, store = self._store()
        rec = _Recorder()
        with patch.object(li_store, "json_request", rec):
            store.revoke_user_credentials("u1")
            store.close_entitlement("u1")
            store.list_open_usage_reservations("u1")
        methods = [(m, u.split("/rest/v1/")[1].split("?")[0]) for m, u, _ in rec.calls]
        self.assertEqual(methods, [("PATCH", "li_access_tokens"), ("PATCH", "li_refresh_tokens"),
                                   ("PATCH", "li_oauth_codes"), ("PATCH", "li_entitlements"),
                                   ("GET", "li_usage_reservations")])
        for method, url, body in rec.calls:
            self.assertIn("user_id=eq.u1", url)
        self.assertIn("revoked_at=is.null", rec.calls[0][1])
        self.assertEqual(set(rec.calls[0][2]), {"revoked_at"})
        self.assertIn("used_at=is.null", rec.calls[1][1])
        self.assertIn("used_at=is.null", rec.calls[2][1])
        self.assertEqual(rec.calls[3][2]["active"], False)
        self.assertIn("status=in.%28reserved%2Cunsettled%29", rec.calls[4][1])

    def test_delete_auth_user_treats_an_already_deleted_user_as_deleted(self):
        from lofgren_intelligence.hosted.http import HTTPError
        li_store, store = self._store()
        with patch.object(li_store, "json_request", _Recorder()):
            self.assertTrue(store.delete_auth_user("u1"))
        with patch.object(li_store, "json_request", _Recorder(HTTPError(404, "User not found"))):
            self.assertFalse(store.delete_auth_user("u1"))
        with patch.object(li_store, "json_request", _Recorder(HTTPError(500, "boom", {"message": "li_x"}))):
            with self.assertRaises(StoreError) as ctx:
                store.delete_auth_user("u1")
        self.assertNotIn("li_x", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
