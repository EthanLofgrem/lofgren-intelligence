"""Q8: one versioned plan catalog, and everything that shows, sells or grants a plan agrees with it."""

import json
import os
import re
import unittest
from pathlib import Path
from unittest.mock import patch

from lofgren_intelligence.billing import catalog as cat
from lofgren_intelligence.billing.catalog import (
    AVAILABLE,
    CATALOG,
    CATALOG_VERSION,
    PLANNED,
    UNDECIDED,
    catalog_json,
    checkout_plans,
    effective_allowance_units,
    plan_for_price,
)
from lofgren_intelligence.billing.pricing import PLANS
from lofgren_intelligence.hosted import site
from lofgren_intelligence.hosted import stripe as li_stripe
from lofgren_intelligence.hosted.economics import certify_paid_plan
from lofgren_intelligence.hosted.entitlements import access_for_run, founder_weekly_units, paid_weekly_units
from lofgren_intelligence.hosted.service import PaymentRequired, PublicService, PublicServiceError
from lofgren_intelligence.hosted.stripe import StripeError, apply_webhook, create_checkout

from .helpers import STRIPE_TEST_ENV, TEST_PRICE_ID, active_researcher, open_researcher_catalog
from .test_public_hosted import FakeStore

ROOT = Path(__file__).resolve().parent.parent
MIGRATION = ROOT / "supabase" / "migrations" / "20261004201650_public_mcp.sql"
PAID = ("payg", "researcher", "good_idea")
CLEAN_ENV = {k: "" for k in (
    "LI_STRIPE_MODE", "LI_FOUNDER_WEEKLY_UNITS", "LI_PAID_WEEKLY_UNITS", "LI_BILLING_ENABLED",
    *(f"LI_STRIPE_PRICE_ID_{p.upper()}_{m}" for p in PAID for m in ("TEST", "LIVE")),
)}


def _env(**extra):
    return patch.dict(os.environ, {**CLEAN_ENV, **extra}, clear=False)


class CatalogDocumentTests(unittest.TestCase):
    def test_generated_json_equals_the_catalog(self):
        self.assertEqual((ROOT / "docs" / "PLAN_CATALOG.json").read_text(encoding="utf-8"), catalog_json())
        self.assertEqual(json.loads(catalog_json())["catalog_version"], CATALOG_VERSION)
        self.assertRegex(CATALOG_VERSION, r"^\d{4}-\d{2}-\d{2}\.\d+$")

    def test_every_plan_has_every_field(self):
        for plan in json.loads(catalog_json())["plans"]:
            with self.subTest(plan=plan["id"]):
                for key in ("id", "name", "status", "price_usd", "currency", "billing_interval", "billable_unit",
                            "allowance", "heavy_work", "concurrency", "overage", "cancellation",
                            "stripe_price_env", "checkout_mode"):
                    self.assertIn(key, plan)
                self.assertIn(plan["status"], (AVAILABLE, PLANNED))
                self.assertEqual(plan["currency"], "USD")

    def test_no_price_ids_or_secrets_in_the_catalog(self):
        text = catalog_json()
        self.assertNotRegex(text, r"\b(price|prod|sk|rk|pk|whsec)_[A-Za-z0-9]{6,}")
        for plan in CATALOG.plans:
            if plan.stripe_price_env:
                for mode, name in plan.stripe_price_env.items():
                    self.assertRegex(name, rf"^LI_STRIPE_PRICE_ID_[A-Z_]+_{mode.upper()}$")

    def test_statuses_prices_and_undecided_allowances(self):
        free = CATALOG.get("founding_free")
        self.assertEqual(free.status, AVAILABLE)
        self.assertEqual(free.price_usd, 0.0)
        self.assertEqual(free.allowance.units, 500.0)
        self.assertEqual(free.allowance.window["seconds"], 604800)
        self.assertEqual(free.allowance.window["time_zone"], "UTC")
        self.assertIsNone(free.stripe_price_env)
        for pid in PAID:
            plan = CATALOG.get(pid)
            with self.subTest(plan=pid):
                self.assertEqual(plan.status, PLANNED)
                self.assertEqual(plan.allowance.units, UNDECIDED)
                self.assertIsNone(effective_allowance_units(plan, {}))
                self.assertEqual(plan.overage["policy"], UNDECIDED)
                self.assertEqual(plan.heavy_work["conversion_to_allowance"], UNDECIDED)
                # Current monetary prices are kept, read from billing.pricing.
                self.assertEqual(plan.price_usd, PLANS[pid].monthly_fee)
                self.assertEqual(plan.rate_usd_per_work_unit, PLANS[pid].rate)
                self.assertEqual(plan.heavy_job_price_usd, PLANS[pid].heavy_price)
                self.assertEqual(plan.included_heavy_jobs, PLANS[pid].included_heavy)
                # The old unenforced entry figure is recorded as a proposal, not adopted.
                self.assertEqual(plan.allowance.unenforced_proposal["value"], PLANS[pid].entry_limit)
        self.assertEqual(CATALOG.get("researcher").price_usd, 49.99)
        self.assertEqual(CATALOG.get("good_idea").price_usd, 79.99)
        self.assertEqual(CATALOG.get("payg").checkout_mode, UNDECIDED)

    def test_entry_to_unit_conversion_is_undecided(self):
        doc = json.loads(catalog_json())
        self.assertEqual(doc["units"]["entry"]["units_per_entry"], UNDECIDED)
        self.assertEqual(doc["units"]["intelligence_unit"]["work_unit_ratio"], 1.0)

    def test_founding_free_matches_the_activation_migration(self):
        sql = MIGRATION.read_text(encoding="utf-8")
        m = re.search(r"case when v_num <= (\d+) then (\d+) else 0 end", sql)
        self.assertIsNotNone(m)
        self.assertEqual(int(m.group(1)), cat.FOUNDING_FREE_MAX_ACTIVATION)
        self.assertEqual(float(m.group(2)), CATALOG.get("founding_free").allowance.units)
        self.assertIn("quota_units_per_week numeric not null default 500", sql)
        self.assertIn("p_window_seconds\": 604800", (ROOT / "lofgren_intelligence" / "hosted" / "store.py")
                      .read_text(encoding="utf-8"))


class PricingPageTests(unittest.TestCase):
    def _card(self, page, html_id):
        m = re.search(rf'<article class="plan" id="plan-{html_id}".*?</article>', page, re.S)
        self.assertIsNotNone(m, html_id)
        return m.group(0)

    def test_page_values_equal_catalog_values(self):
        page = site.pricing_page("n")
        cards = re.findall(r'<article class="plan" id="plan-([a-z-]+)"', page)
        self.assertEqual(cards, [p.id.replace("_", "-") for p in CATALOG.plans])
        for plan in CATALOG.plans:
            card = self._card(page, plan.id.replace("_", "-"))
            with self.subTest(plan=plan.id):
                self.assertIn(f">{plan.name}<", card)
                if plan.billing_interval == "usage":
                    self.assertIn(f"${plan.rate_usd_per_work_unit:.4f}", card)
                    self.assertIn(f"${plan.heavy_job_price_usd:.2f}", card)
                elif plan.billing_interval == "month":
                    self.assertIn(f"${plan.price_usd:.2f}", card)
                    self.assertIn(f"${plan.rate_usd_per_work_unit:.4f}", card)
                else:
                    self.assertIn('<span class="price">$0</span>', card)
                if plan.allowance.units == UNDECIDED:
                    self.assertIn("Allowance: not yet decided.", card)
                else:
                    self.assertIn(f"{plan.allowance.units:,.0f} intelligence units per rolling 7 days (UTC)", card)
                self.assertEqual("Planned" in card, plan.status == PLANNED)
                for feature in plan.features:
                    self.assertIn(feature, card)

    def test_page_shows_the_catalog_value_not_an_operator_override(self):
        with _env(LI_FOUNDER_WEEKLY_UNITS="9999"):
            page = site.pricing_page("n")
        self.assertIn("500 intelligence units", page)
        self.assertNotIn("9,999", page)


class QuotaDefaultTests(unittest.TestCase):
    def test_founder_allowance_comes_from_the_catalog_with_its_override(self):
        with _env():
            self.assertEqual(founder_weekly_units(), 500.0)
        with _env(LI_FOUNDER_WEEKLY_UNITS="750"):
            self.assertEqual(founder_weekly_units(), 750.0)
        with _env(LI_FOUNDER_WEEKLY_UNITS="nonsense"):
            self.assertEqual(founder_weekly_units(), 500.0)

    def test_founding_free_without_stored_quota_uses_catalog_allowance(self):
        store = FakeStore()
        store.entitlement["quota_units_per_week"] = None
        with _env():
            decision = access_for_run(store, "u1", 10)
        self.assertTrue(decision.allowed)
        self.assertEqual(decision.quota_units, 500.0)

    def test_undecided_paid_allowance_fails_closed(self):
        store = FakeStore(activation_number=1001, kind="paid", quota=None)
        store.entitlement["plan_id"] = "researcher"
        # The old env default (and an explicit env value) no longer grants anything.
        for env in ({}, {"LI_PAID_WEEKLY_UNITS": "2000"}):
            with self.subTest(env=env), _env(**env):
                self.assertIsNone(paid_weekly_units("researcher"))
                decision = access_for_run(store, "u1", 1)
                self.assertFalse(decision.allowed)
                self.assertEqual(decision.reason, "plan allowance is undecided")
        # Even a stored quota does not open an undecided plan.
        store.entitlement["quota_units_per_week"] = 2000
        with _env():
            self.assertFalse(access_for_run(store, "u1", 1).allowed)

    def test_unknown_paid_plan_fails_closed(self):
        store = FakeStore(activation_number=1001, kind="paid", quota=2000)
        store.entitlement["plan_id"] = "enterprise"
        with _env():
            self.assertFalse(access_for_run(store, "u1", 1).allowed)

    def test_decided_paid_plan_uses_catalog_then_override(self):
        catalog = open_researcher_catalog(1234)
        store = FakeStore(activation_number=1001, kind="paid", quota=None)
        store.entitlement["plan_id"] = "researcher"
        with _env():
            self.assertEqual(access_for_run(store, "u1", 1, catalog=catalog).quota_units, 1234.0)
        with _env(LI_PAID_WEEKLY_UNITS="300"):
            self.assertEqual(access_for_run(store, "u1", 1, catalog=catalog).quota_units, 300.0)


class FoundingFreeUnchangedTests(unittest.TestCase):
    def test_account_1000_is_free_and_1001_requires_payment(self):
        with _env(LOFGREN_PROVIDER="heuristic"):
            last = FakeStore(activation_number=1000, kind="founding_free", quota=500.0)
            self.assertTrue(access_for_run(last, "u1", 10).allowed)
            self.assertEqual(PublicService(last).account_status("u1")["founding_free"], True)
            first_paid = FakeStore(activation_number=1001, kind="paid_required", quota=0)
            decision = access_for_run(first_paid, "u1", 1)
            self.assertFalse(decision.allowed)
            self.assertEqual(decision.reason, "paid entitlement required")
            with self.assertRaises(PaymentRequired):
                PublicService(first_paid)._require_open_quota("u1")
        sql = MIGRATION.read_text(encoding="utf-8")
        self.assertIn("case when v_num <= 1000 then 'founding_free' else 'paid_required' end", sql)


class CheckoutAllowlistTests(unittest.TestCase):
    def test_shipped_catalog_sells_nothing_even_when_prices_are_configured(self):
        env = {f"LI_STRIPE_PRICE_ID_{p.upper()}_{m}": f"price_{p}_{m.lower()}" for p in PAID for m in ("TEST", "LIVE")}
        for mode in ("test", "live"):
            with self.subTest(mode=mode), _env(LI_STRIPE_MODE=mode, **env):
                self.assertEqual(checkout_plans(), {})
                self.assertIsNone(plan_for_price(f"price_researcher_{mode}"))

    def test_only_available_decided_configured_plans_are_sellable(self):
        catalog = open_researcher_catalog()
        with _env(**STRIPE_TEST_ENV):
            self.assertEqual(checkout_plans(catalog), {"researcher": TEST_PRICE_ID})
        with _env(**STRIPE_TEST_ENV, LI_STRIPE_PRICE_ID_GOOD_IDEA_TEST="price_gi"):
            self.assertEqual(checkout_plans(catalog), {"researcher": TEST_PRICE_ID})  # good_idea is planned
        with _env(LI_STRIPE_PRICE_ID_RESEARCHER_TEST=TEST_PRICE_ID):  # no LI_STRIPE_MODE
            self.assertEqual(checkout_plans(catalog), {})
        with _env(LI_STRIPE_MODE="live", LI_STRIPE_PRICE_ID_RESEARCHER_TEST=TEST_PRICE_ID):
            self.assertEqual(checkout_plans(catalog), {})
        undecided = CATALOG.replace_plan("researcher", status=AVAILABLE)
        with _env(**STRIPE_TEST_ENV):
            self.assertEqual(checkout_plans(undecided), {})

    def test_duplicate_price_ids_make_every_ambiguous_plan_unsellable(self):
        catalog = open_researcher_catalog()
        catalog = catalog.replace_plan(
            "good_idea",
            status=AVAILABLE,
            allowance=cat.Allowance(units=3000, window=cat.QUOTA_WINDOW),
            checkout_mode="subscription",
        )
        env = {**STRIPE_TEST_ENV, "LI_STRIPE_PRICE_ID_GOOD_IDEA_TEST": TEST_PRICE_ID}
        with _env(**env):
            self.assertEqual(checkout_plans(catalog), {})
            self.assertIsNone(plan_for_price(TEST_PRICE_ID, catalog))

    def test_duplicate_price_id_cannot_create_checkout(self):
        catalog = open_researcher_catalog()
        catalog = catalog.replace_plan(
            "good_idea",
            status=AVAILABLE,
            allowance=cat.Allowance(units=3000, window=cat.QUOTA_WINDOW),
            checkout_mode="subscription",
        )
        with _env(**STRIPE_TEST_ENV, LI_STRIPE_PRICE_ID_GOOD_IDEA_TEST=TEST_PRICE_ID,
                  STRIPE_SECRET_KEY="sk_test_dummy"), patch.object(li_stripe, "stripe_post") as post:
            for plan_id in ("researcher", "good_idea"):
                with self.subTest(plan=plan_id), self.assertRaisesRegex(StripeError, "not available"):
                    create_checkout("u1", plan_id=plan_id, success_url="s", cancel_url="c", catalog=catalog)
        post.assert_not_called()

    def test_create_checkout_uses_the_server_price_and_never_takes_one(self):
        sent = {}
        with _env(**STRIPE_TEST_ENV, STRIPE_SECRET_KEY="sk_test_dummy"), \
                patch.object(li_stripe, "stripe_post", side_effect=lambda path, params: sent.update(params) or {"id": "cs"}):
            create_checkout("u1", plan_id="researcher", success_url="s", cancel_url="c",
                            catalog=open_researcher_catalog())
            with self.assertRaises(TypeError):
                create_checkout("u1", plan_id="researcher", price_id="price_evil", success_url="s", cancel_url="c")
            with self.assertRaises(StripeError):
                create_checkout("u1", plan_id="researcher", success_url="s", cancel_url="c")  # shipped catalog
        self.assertEqual(sent["line_items[0][price]"], TEST_PRICE_ID)
        self.assertEqual(sent["metadata[li_plan_id]"], "researcher")
        self.assertEqual(sent["metadata[li_catalog_version]"], CATALOG_VERSION)

    def test_mode_must_match_the_secret_key(self):
        with _env(**STRIPE_TEST_ENV, STRIPE_SECRET_KEY="sk_live_dummy"), \
                patch.object(li_stripe, "stripe_post") as post:
            with self.assertRaises(StripeError):
                create_checkout("u1", plan_id="researcher", success_url="s", cancel_url="c",
                                catalog=open_researcher_catalog())
        post.assert_not_called()

    def test_client_supplied_price_plan_values_and_entitlement_are_rejected(self):
        store = FakeStore(activation_number=1001, kind="paid_required", quota=0)
        service = PublicService(store)
        forged = {
            "price_id": "price_evil", "price": "price_evil", "line_items": [{"price": "price_evil"}],
            "quota_units_per_week": 1e9, "allowance": 1e9, "units": 1e9,
            "entitlement": {"kind": "paid", "active": True}, "kind": "paid", "active": True,
            "plan": {"id": "researcher", "allowance": 1e9},
        }
        with _env(**STRIPE_TEST_ENV, LI_BILLING_ENABLED="1"), patch.object(li_stripe, "stripe_post") as post:
            for key, value in forged.items():
                with self.subTest(field=key), self.assertRaisesRegex(PublicServiceError, "accepts only plan_id"):
                    service.checkout("u1", "https://li.example", {"plan_id": "researcher", key: value})
            with self.assertRaisesRegex(PublicServiceError, "plan_id must be a string"):
                service.checkout("u1", "https://li.example", {"plan_id": {"id": "researcher", "allowance": 9}})
            for plan_id in ("researcher", "founding_free", "enterprise", "payg"):
                with self.subTest(plan=plan_id), self.assertRaisesRegex(PublicServiceError, "not available"):
                    service.checkout("u1", "https://li.example", {"plan_id": plan_id})
            with self.assertRaisesRegex(PublicServiceError, "no single paid plan"):
                service.checkout("u1", "https://li.example")
        post.assert_not_called()
        self.assertEqual(store.entitlement["kind"], "paid_required")


class WebhookCatalogTests(unittest.TestCase):
    def _checkout_event(self, event_id="evt_c", **extra):
        return {"id": event_id, "type": "checkout.session.completed", "data": {"object": {
            "customer": "cus_t", "subscription": "sub_t", "metadata": {"li_user_id": "u1"},
            "payment_status": "paid"}}, **extra}

    def _store(self):
        return FakeStore(activation_number=1001, kind="paid_required", quota=0)

    def test_known_price_grants_the_catalog_allowance(self):
        store = self._store()
        with _env(**STRIPE_TEST_ENV):
            apply_webhook(store, self._checkout_event(), subscription_status=active_researcher,
                          catalog=open_researcher_catalog(1500))
        self.assertEqual(store.entitlement["kind"], "paid")
        self.assertEqual(store.entitlement["plan_id"], "researcher")
        self.assertEqual(store.entitlement["quota_units_per_week"], 1500.0)

    def test_unknown_price_grants_nothing(self):
        store = self._store()
        before = dict(store.entitlement)
        with _env(**STRIPE_TEST_ENV):
            out = apply_webhook(store, self._checkout_event(),
                                subscription_status=lambda s: {"status": "active", "price_ids": ["price_unknown"]},
                                catalog=open_researcher_catalog())
        self.assertEqual(out, "processed")
        self.assertIn("evt_c", store.billing_events)
        self.assertEqual(store.entitlement, before)

    def test_shipped_catalog_grants_nothing_for_a_configured_price(self):
        store = self._store()
        before = dict(store.entitlement)
        with _env(**STRIPE_TEST_ENV, LI_PAID_WEEKLY_UNITS="2000"):
            apply_webhook(store, self._checkout_event(), subscription_status=active_researcher)
        self.assertEqual(store.entitlement, before)

    def test_mode_mismatch_multiple_prices_and_missing_prices_grant_nothing(self):
        cases = {
            "livemode": (self._checkout_event(livemode=True), active_researcher),
            "two prices": (self._checkout_event(),
                           lambda s: {"status": "active", "price_ids": [TEST_PRICE_ID, "price_other"]}),
            "no prices": (self._checkout_event(), lambda s: "active"),
        }
        for name, (event, lookup) in cases.items():
            store = self._store()
            with self.subTest(case=name), _env(**STRIPE_TEST_ENV):
                apply_webhook(store, event, subscription_status=lookup, catalog=open_researcher_catalog())
                self.assertFalse(store.entitlement["active"])
                self.assertEqual(store.entitlement["kind"], "paid_required")

    def test_unknown_price_on_the_granted_subscription_revokes_it(self):
        store = FakeStore(activation_number=1001, kind="paid", quota=2000)
        store.entitlement.update({"plan_id": "researcher", "stripe_subscription_id": "sub_t", "active": True})
        event = {"id": "evt_u", "type": "customer.subscription.updated", "data": {"object": {
            "id": "sub_t", "customer": "cus_t", "status": "active", "metadata": {"li_user_id": "u1"}}}}
        with _env(**STRIPE_TEST_ENV):
            apply_webhook(store, event, subscription_status=lambda s: {"status": "active", "price_ids": ["price_x"]},
                          catalog=open_researcher_catalog())
        self.assertFalse(store.entitlement["active"])
        self.assertEqual(store.entitlement["quota_units_per_week"], 0)

    def test_default_lookup_reads_status_and_price_ids_from_stripe(self):
        sub = {"id": "sub_x", "status": "active", "items": {"data": [{"price": {"id": "price_a"}}]}}
        with patch.object(li_stripe, "stripe_get", return_value=sub):
            self.assertEqual(li_stripe.current_subscription("sub_x"), {"status": "active", "price_ids": ["price_a"]})

    def test_existing_subscription_owner_wins_and_conflicting_metadata_is_rejected(self):
        store = FakeStore(activation_number=1001, kind="paid", quota=2000)
        store.entitlement.update({"plan_id": "researcher", "stripe_subscription_id": "sub_t", "active": True})
        event = {"id": "evt_owner", "type": "customer.subscription.updated", "livemode": False,
                 "data": {"object": {"id": "sub_t", "customer": "cus_t", "status": "active",
                                     "metadata": {"li_user_id": "u2"}}}}
        with _env(**STRIPE_TEST_ENV), self.assertRaisesRegex(StripeError, "stored LI owner"):
            apply_webhook(store, event, subscription_status=active_researcher,
                          catalog=open_researcher_catalog())
        self.assertNotIn("evt_owner", store.billing_events)
        self.assertEqual(store.entitlement["user_id"], "u1")


class EconomicGatePlanTests(unittest.TestCase):
    ENV = {"LI_PAID_MONTHLY_USD": "49.99", "LI_PAID_WEEKLY_UNITS": "2000", "LI_PAYMENT_FEE_PERCENT": "0.029",
           "LI_PAYMENT_FEE_FIXED_USD": "0.30", "LI_ECON_MIN_SAMPLES": "100", "LI_TARGET_GROSS_MARGIN": "0.65"}
    SAMPLES = [{"units": 1, "known_cost_usd": 0.0001, "unpriced_components": []} for _ in range(100)]

    def test_gate_fails_closed_for_a_planned_undecided_plan(self):
        with _env(**self.ENV):
            gate = certify_paid_plan(self.SAMPLES, CATALOG.get("researcher"))
        self.assertFalse(gate.passed)
        self.assertIn("plan_not_available:researcher", gate.reasons)
        self.assertIn("undecided_allowance:researcher", gate.reasons)

    def test_gate_inputs_must_equal_the_catalog_plan(self):
        catalog = open_researcher_catalog(2000)
        with _env(**{**self.ENV, "LI_PAID_MONTHLY_USD": "19.99", "LI_PAID_WEEKLY_UNITS": "5000"}):
            gate = certify_paid_plan(self.SAMPLES, catalog.get("researcher"))
        self.assertIn("mismatch:LI_PAID_MONTHLY_USD", gate.reasons)
        self.assertFalse(gate.passed)
        with _env(**self.ENV):
            gate = certify_paid_plan(self.SAMPLES, catalog.get("researcher"))
        self.assertTrue(gate.passed, gate.reasons)

    def test_plan_checks_never_relax_the_sample_rules(self):
        catalog = open_researcher_catalog(2000)
        with _env(**self.ENV):
            few = certify_paid_plan(self.SAMPLES[:99], catalog.get("researcher"))
            unpriced = certify_paid_plan(self.SAMPLES + [{"units": 1, "known_cost_usd": 0, "unpriced_components": ["x"]}],
                                         catalog.get("researcher"))
        self.assertIn("insufficient_samples:99/100", few.reasons)
        self.assertIn("unpriced_sample", unpriced.reasons)
        self.assertFalse(few.passed or unpriced.passed)


class PricingToolTests(unittest.TestCase):
    def test_pricing_tool_publishes_the_catalog(self):
        out = PublicService(FakeStore()).pricing({})
        self.assertEqual(out["catalog"], json.loads(catalog_json()))


if __name__ == "__main__":
    unittest.main()
