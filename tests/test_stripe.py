"""Unit and contract tests for legends-stripe."""
from __future__ import annotations

import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from legends_stripe.api import (
    StripeClient,
    StripeError,
    classify_key,
    encode_stripe_form,
    flatten_stripe_params,
    scrub_secrets,
    validate_metadata,
    validate_price_fields,
    validate_product_fields,
    validate_public_https_host,
    validate_statement_descriptor,
    validate_stripe_id,
)
from legends_stripe.blueprints import get_blueprint, list_blueprints
from legends_stripe.catalog import (
    audit_catalog,
    diff_catalog_spec,
    export_catalog_spec,
    generate_ladder_spec,
    run_checkout_canary,
    sync_catalog_spec,
    upsert_product,
)
from legends_stripe.cli import main as cli_main
from legends_stripe.env_tools import audit_env_file, build_price_map
from legends_stripe.invoices import (
    create_b2b_invoice,
    parse_custom_field_specs,
    scan_invoice_radar,
    validate_b2b_invoice_fields,
    validate_invoice_custom_fields,
    void_b2b_invoice,
)
from legends_stripe.resources import resolve as resolve_resources, status as resources_status
from legends_stripe.subscriptions import scan_subscription_radar
from legends_stripe.telemetry import build_treasury_telemetry, render_telemetry_markdown
from legends_stripe.webhooks import (
    audit_webhooks,
    probe_unsigned_webhook_gate,
    sign_stripe_webhook_payload,
    upsert_webhook_endpoint,
    verify_stripe_webhook_signature,
)
from scripts.check_release import check as check_release


class FakeStripeClient(StripeClient):
    """Deterministic offline StripeClient stub for unit tests."""

    def __init__(self, key: str = "sk_test_fake1234567890", *, responses: dict | None = None) -> None:
        super().__init__(key)
        self.responses = responses or {}
        self.calls: list[tuple[str, str, dict | None, str | None]] = []

    def request(
        self,
        method: str,
        path: str,
        params: dict | None = None,
        *,
        idempotency_key: str | None = None,
    ) -> dict:
        self.calls.append((method, path, params, idempotency_key))
        key = (method, path)
        if key in self.responses:
            val = self.responses[key]
            if isinstance(val, Exception):
                raise val
            if callable(val):
                return val(method, path, params, idempotency_key)
            return val
        if path in self.responses:
            val = self.responses[path]
            if isinstance(val, Exception):
                raise val
            if callable(val):
                return val(method, path, params, idempotency_key)
            return val
        return {"id": "stub_id", "object": "stub"}

    def list_all(self, path: str, params: dict | None = None, *, max_items: int = 500) -> list[dict]:
        self.calls.append(("LIST", path, params, None))
        val = self.responses.get(("LIST", path), self.responses.get(path, []))
        if callable(val):
            return val(params)
        return list(val)[:max_items]


class ApiAndEncodingTests(unittest.TestCase):
    def test_flatten_and_encode_stripe_form(self) -> None:
        payload = {
            "name": "Pro Plan",
            "active": True,
            "line_items": [{"price": "price_123", "quantity": 2}],
            "metadata": {"slug": "pro_plan", "tier": "1"},
        }
        pairs = flatten_stripe_params(payload)
        self.assertIn(("name", "Pro Plan"), pairs)
        self.assertIn(("active", "true"), pairs)
        self.assertIn(("line_items[0][price]", "price_123"), pairs)
        self.assertIn(("line_items[0][quantity]", "2"), pairs)
        self.assertIn(("metadata[slug]", "pro_plan"), pairs)
        encoded = encode_stripe_form(payload).decode("utf-8")
        self.assertIn("line_items%5B0%5D%5Bprice%5D=price_123", encoded)

    def test_key_classification_and_mutation_gates(self) -> None:
        self.assertEqual(classify_key("sk_test_123")["mode"], "test")
        self.assertTrue(classify_key("sk_test_123")["valid_secret"])
        self.assertEqual(classify_key("sk_live_123")["mode"], "live")
        self.assertFalse(classify_key("pk_live_123")["valid_secret"])
        self.assertEqual(classify_key("whsec_123")["kind"], "webhook_secret")

        live_client = FakeStripeClient("sk_live_fake123")
        with self.assertRaises(StripeError):
            live_client.require_mutation_gate(allow_live=False)
        live_client.require_mutation_gate(allow_live=True)

    def test_field_validators(self) -> None:
        self.assertEqual(validate_statement_descriptor("LEGENDS PRO"), [])
        self.assertTrue(validate_statement_descriptor("AB"))
        self.assertTrue(validate_statement_descriptor("A" * 25))
        self.assertTrue(validate_statement_descriptor("BAD*CHAR"))

        self.assertTrue(validate_product_fields(name="Valid", description="x" * 501))
        self.assertTrue(validate_metadata({f"k{i}": "v" for i in range(55)}))
        self.assertTrue(validate_price_fields(currency="usd", unit_amount=-5))

    def test_ssrf_id_and_secret_scrub_defenses(self) -> None:
        self.assertEqual(validate_stripe_id("price_123Abc", expected_prefix="price_"), "price_123Abc")
        with self.assertRaises(StripeError):
            validate_stripe_id("price_../../v1/charges", expected_prefix="price_")
        with self.assertRaises(StripeError):
            validate_public_https_host("127.0.0.1", resolve_dns=False)
        with self.assertRaises(StripeError):
            validate_public_https_host("169.254.169.254", resolve_dns=False)
        with self.assertRaises(StripeError):
            validate_public_https_host("192.168.1.10", resolve_dns=False)
        with self.assertRaises(StripeError):
            validate_public_https_host("localhost", resolve_dns=False)
        with self.assertRaises(StripeError):
            probe_unsigned_webhook_gate("http://example.com/webhook")
        with self.assertRaises(StripeError):
            probe_unsigned_webhook_gate("https://127.0.0.1/webhook")

        redacted = scrub_secrets("Failed with key sk_test_abc123456 and whsec_xyz987654")
        self.assertNotIn("sk_test_abc123456", redacted)
        self.assertNotIn("whsec_xyz987654", redacted)
        self.assertIn("[REDACTED_STRIPE_SECRET]", redacted)


class TelemetryTests(unittest.TestCase):
    def test_treasury_reconciliation_and_3ds_filtering(self) -> None:
        client = FakeStripeClient(
            responses={
                "/v1/account": {
                    "id": "acct_1Test123",
                    "default_currency": "usd",
                    "charges_enabled": True,
                    "payouts_enabled": True,
                    "business_profile": {"name": "Test SaaS LLC"},
                    "settings": {"payouts": {"schedule": {"interval": "manual", "delay_days": 2}}},
                },
                "/v1/balance": {
                    "available": [{"amount": 18000, "currency": "usd"}],
                    "pending": [{"amount": 941, "currency": "usd"}],
                },
                "/v1/balance_transactions": [
                    {"id": "txn_1", "type": "charge", "amount": 20000, "fee": 610, "net": 19390},
                    {"id": "txn_2", "type": "charge", "amount": 1000, "fee": 59, "net": 941},
                    {"id": "txn_3", "type": "stripe_fee", "amount": -140, "fee": 0, "net": -140},
                    {"id": "txn_4", "type": "payout", "amount": -1250, "fee": 0, "net": -1250},
                ],
                "/v1/charges": [
                    {
                        "id": "ch_1",
                        "paid": True,
                        "status": "succeeded",
                        "amount": 20000,
                        "balance_transaction": "txn_1",
                        "created": 1762300000,
                        "description": "Subscription creation",
                        "billing_details": {"name": "Alice Smith", "email": "alice@example.com"},
                        "refunded": False,
                    },
                    {
                        "id": "ch_2",
                        "paid": True,
                        "status": "succeeded",
                        "amount": 1000,
                        "balance_transaction": "txn_2",
                        "created": 1762300100,
                        "description": "Auto-Recharge for Sub-Account - Client Wallet",
                        "billing_details": {"name": "Bob Jones", "email": "bob@example.com"},
                        "refunded": False,
                    },
                    {
                        "id": "ch_3ds",
                        "paid": True,
                        "status": "succeeded",
                        "amount": 100,
                        "balance_transaction": None,
                        "created": 1762300200,
                        "description": "Add new card: 3DS verification",
                        "billing_details": {"name": "Bob Jones", "email": "bob@example.com"},
                        "refunded": True,
                    },
                ],
                "/v1/payouts": [{"id": "po_1", "amount": 1250, "status": "paid"}],
            }
        )
        report = build_treasury_telemetry(client, redact_pii=True)
        rev = report["revenue_and_drag"]
        self.assertTrue(rev["reconciled"])
        self.assertEqual(rev["reconciliation_variance_cents"], 0)
        self.assertEqual(rev["gross_revenue_cents"], 21000)
        self.assertEqual(rev["card_processing_fees_cents"], 669)
        self.assertEqual(rev["billing_usage_fees_cents"], 140)
        self.assertEqual(rev["total_fees_cents"], 809)
        self.assertEqual(rev["verified_charges_count"], 2)
        self.assertEqual(rev["card_verifications_3ds_count"], 1)
        self.assertIn("subscription", report["revenue_streams"])
        self.assertIn("wallet_auto_recharge", report["revenue_streams"])

        md = render_telemetry_markdown(report)
        self.assertNotIn("\u2014", md)
        self.assertNotIn("alice@example.com", md)
        self.assertIn("a***@example.com", md)


class EnvToolsTests(unittest.TestCase):
    def test_env_audit_detects_mode_mismatch_empty_vars_and_live_prices(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env_path = Path(tmp) / ".env"
            env_path.write_text(
                "\n".join(
                    [
                        "STRIPE_SECRET_KEY=sk_test_1234567890",
                        "STRIPE_PUBLISHABLE_KEY=pk_live_1234567890",
                        "STRIPE_WEBHOOK_SECRET=whsec_1234567890",
                        "NEXT_PUBLIC_STRIPE_PRICE_PRO=price_1TestActive",
                        "NEXT_PUBLIC_STRIPE_PRICE_EMPTY=",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            client = FakeStripeClient(
                "sk_test_1234567890",
                responses={
                    "/v1/prices/price_1TestActive": {
                        "id": "price_1TestActive",
                        "active": True,
                        "unit_amount": 4900,
                        "currency": "usd",
                        "recurring": {"interval": "month"},
                        "livemode": False,
                        "product": {"id": "prod_1Test", "name": "Pro Plan", "active": True},
                    }
                },
            )
            audit = audit_env_file(str(env_path), client=client, verify_live=True)
            self.assertFalse(audit["ok"])
            self.assertEqual(audit["summary"]["empty_stripe_vars_count"], 1)
            error_kinds = [e["kind"] for e in audit["errors"]]
            self.assertIn("key_mode_mismatch", error_kinds)
            self.assertEqual(audit["referenced_ids"][0]["formatted_amount"], "$49.00 USD")

    def test_price_map_formats(self) -> None:
        client = FakeStripeClient(
            responses={
                "/v1/products": [{"id": "prod_1", "name": "Starter Tier", "active": True}],
                "/v1/prices": [
                    {
                        "id": "price_1StarterMo",
                        "product": "prod_1",
                        "unit_amount": 1900,
                        "currency": "usd",
                        "recurring": {"interval": "month"},
                        "lookup_key": "starter_monthly",
                        "active": True,
                    }
                ],
            }
        )
        pmap = build_price_map(client)
        self.assertEqual(pmap["count"], 1)
        self.assertIn("STRIPE_PRICE_STARTER_MONTHLY=price_1StarterMo", pmap["dotenv"])
        self.assertIn('"starter_monthly": "price_1StarterMo"', pmap["typescript"])


class CatalogAndCascadingArchiveTests(unittest.TestCase):
    def test_ladder_diff_sync_and_cascading_archive(self) -> None:
        spec = generate_ladder_spec(
            prefix="Test Ladder",
            amounts_usd=[10, 20],
            intervals=["month"],
            currency="usd",
            statement_descriptor="TEST LADDER",
        )
        self.assertEqual(spec["product_count"], 2)
        self.assertEqual(spec["price_count"], 2)

        client = FakeStripeClient(
            responses={
                "/v1/products": [],
                "/v1/prices": [{"id": "price_child_1", "product": "prod_existing", "active": True}],
                ("POST", "/v1/prices/price_child_1"): {"id": "price_child_1", "active": False},
                ("POST", "/v1/products/prod_existing"): {
                    "id": "prod_existing",
                    "name": "Old Product",
                    "active": False,
                },
                ("GET", "/v1/products/prod_existing"): {
                    "id": "prod_existing",
                    "name": "Old Product",
                    "active": False,
                },
            }
        )
        preview = upsert_product(
            client,
            name="Old Product",
            product_id="prod_existing",
            active=False,
            apply=False,
        )
        self.assertTrue(preview["preview"])
        self.assertEqual(preview["planned"]["prices_to_deactivate"], ["price_child_1"])

        applied = upsert_product(
            client,
            name="Old Product",
            product_id="prod_existing",
            active=False,
            apply=True,
            confirm_plan=preview["plan_sha256"],
        )
        self.assertFalse(applied["preview"])
        self.assertEqual(applied["deactivated_prices"], ["price_child_1"])
        self.assertFalse(applied["product"]["active"])


class SubscriptionAndWebhookTests(unittest.TestCase):
    def test_subscription_radar_detects_duplicates_and_mrr(self) -> None:
        client = FakeStripeClient(
            responses={
                "/v1/subscriptions": [
                    {
                        "id": "sub_1",
                        "status": "active",
                        "customer": {"id": "cus_A", "email": "dup@example.com", "name": "Dup User"},
                        "items": {
                            "data": [
                                {
                                    "quantity": 1,
                                    "price": {
                                        "id": "price_1",
                                        "unit_amount": 10000,
                                        "currency": "usd",
                                        "recurring": {"interval": "month", "interval_count": 1},
                                    },
                                }
                            ]
                        },
                    },
                    {
                        "id": "sub_2",
                        "status": "active",
                        "customer": {"id": "cus_B", "email": "DUP@example.com", "name": "Dup User"},
                        "items": {
                            "data": [
                                {
                                    "quantity": 1,
                                    "price": {
                                        "id": "price_2",
                                        "unit_amount": 120000,
                                        "currency": "usd",
                                        "recurring": {"interval": "year", "interval_count": 1},
                                    },
                                }
                            ]
                        },
                    },
                ]
            }
        )
        radar = scan_subscription_radar(client, redact_pii=True)
        self.assertEqual(radar["summary"]["mrr_cents_by_currency"]["usd"], 20000)
        self.assertEqual(radar["summary"]["duplicate_billing_alerts_count"], 1)
        self.assertEqual(radar["duplicate_billing_alerts"][0]["kind"], "same_email_multiple_customers")

    def test_webhook_sign_verify_and_audit(self) -> None:
        payload = '{"id":"evt_1","type":"checkout.session.completed"}'
        secret = "whsec_unit_test_secret"
        signed = sign_stripe_webhook_payload(payload, secret, timestamp=1762350000)
        verified = verify_stripe_webhook_signature(
            payload,
            signed["stripe_signature"],
            secret,
            tolerance_seconds=300,
            now_timestamp=1762350100,
        )
        self.assertTrue(verified["valid"])

        expired = verify_stripe_webhook_signature(
            payload,
            signed["stripe_signature"],
            secret,
            tolerance_seconds=300,
            now_timestamp=1762350500,
        )
        self.assertFalse(expired["valid"])
        self.assertEqual(expired["reason"], "timestamp_outside_tolerance")

        client = FakeStripeClient(
            responses={
                "/v1/webhook_endpoints": [
                    {
                        "id": "we_1",
                        "url": "https://app.example.com/api/stripe/webhook",
                        "status": "enabled",
                        "enabled_events": [
                            "checkout.session.completed",
                            "invoice.paid",
                            "invoice.payment_failed",
                            "customer.subscription.updated",
                            "customer.subscription.deleted",
                        ],
                    }
                ]
            }
        )
        waud = audit_webhooks(client, expected_url="https://app.example.com/api/stripe/webhook")
        self.assertTrue(waud["has_active_soft_land_endpoint"])
        self.assertTrue(waud["expected_url_status"]["enabled_and_covers_soft_land"])


class ResourcesAndReleaseHygieneTests(unittest.TestCase):
    def test_resources_and_blueprints_and_release_gate(self) -> None:
        res = resolve_resources()
        self.assertTrue(res["complete"], res.get("note"))
        self.assertEqual(resources_status()["status"], "available")

        bps = list_blueprints()["blueprints"]
        self.assertEqual(len(bps), 5)
        for bp in bps:
            code = get_blueprint(bp["name"])["code"]
            self.assertTrue(len(code) > 100)

        buf = io.StringIO()
        with patch("sys.stdout", buf):
            rc = cli_main(["doctor"])
        self.assertEqual(rc, 0)
        doc_out = json.loads(buf.getvalue())
        self.assertTrue(doc_out["ok"])

        self.assertEqual(check_release(), [])

    def test_security_hardening_canary_preview_argv_secrets_ssrf_and_blueprints(self) -> None:
        import socket

        # 1. checkout-canary preview-first + confirm_plan gate (STR-P1-02)
        client = FakeStripeClient(
            responses={
                ("POST", "/v1/checkout/sessions"): {"id": "cs_test_123", "status": "open", "url": "https://checkout.stripe.com/c/pay/cs_test_123"},
                ("GET", "/v1/checkout/sessions/cs_test_123"): {"id": "cs_test_123", "status": "open", "url": "https://checkout.stripe.com/c/pay/cs_test_123", "amount_total": 100, "currency": "usd", "livemode": False},
                ("POST", "/v1/checkout/sessions/cs_test_123/expire"): {"id": "cs_test_123", "status": "expired"},
            }
        )
        preview = run_checkout_canary(client, amount_cents=100, apply=False)
        self.assertTrue(preview["preview"])
        self.assertEqual(len(client.calls), 0)
        with self.assertRaises(StripeError):
            run_checkout_canary(client, amount_cents=100, apply=True, confirm_plan="wrong")
        applied = run_checkout_canary(client, amount_cents=100, apply=True, confirm_plan=preview["plan_sha256"])
        self.assertFalse(applied["preview"])
        self.assertTrue(applied["cleaned_up"])

        # 2. CWE-214 CLI argv raw secret rejection (STR-P1-03)
        buf = io.StringIO()
        with patch("sys.stdout", buf):
            rc_bad = cli_main(["sign-webhook", "--payload", "{}", "--secret", "whsec_raw_arg"])
        self.assertEqual(rc_bad, 1)
        self.assertIn("CWE-214", buf.getvalue())

        with tempfile.TemporaryDirectory() as tmp:
            sec_file = Path(tmp) / "whsec.txt"
            sec_file.write_text("whsec_file_secret\n", encoding="utf-8")
            buf2 = io.StringIO()
            with patch("sys.stdout", buf2):
                rc_ok = cli_main(["sign-webhook", "--payload", "{}", "--secret", f"@{sec_file}"])
            self.assertEqual(rc_ok, 0)

        # 3. CWE-918 DNS fail-closed and CGNAT rejection (STR-P2-01)
        with self.assertRaises(StripeError):
            validate_public_https_host("100.64.0.1", resolve_dns=False)
        with patch("socket.getaddrinfo", side_effect=socket.gaierror("NXDOMAIN")):
            with self.assertRaises(StripeError):
                validate_public_https_host("unresolvable.example.invalid", resolve_dns=True)

        # 4. Blueprints: python-webhook multiple v1 rotation + malformed t, astro-portal sk_/rk_ check (STR-P2-02)
        portal_code = get_blueprint("astro-portal")["code"]
        self.assertIn('secretKey.startsWith("sk_")', portal_code)
        py_code = get_blueprint("python-webhook")["code"]
        ns: dict = {}
        exec(py_code, ns)
        classify_fn = ns["classify_soft_land_event"]
        raw_body = b'{"id":"evt_rot","type":"invoice.paid","data":{"object":{"status":"paid"}}}'
        signed = sign_stripe_webhook_payload(raw_body, "whsec_new_key")
        multi_sig = f"t={signed['timestamp']},v1=deadbeef,v1={signed['v1']}"
        res_rot = classify_fn(raw_body, multi_sig, "whsec_new_key")
        self.assertEqual(res_rot["transition"], "activate")
        with self.assertRaises(ValueError):
            classify_fn(raw_body, "t=not_an_int,v1=deadbeef", "whsec_new_key")

    def test_b2b_invoice_radar_create_void_and_blueprint(self) -> None:
        # 1. Custom field validation (max 4 fields, max 30 chars each, no padded overflow or extra keys)
        self.assertEqual(
            validate_invoice_custom_fields([{"name": "Invoice Reference", "value": "INV-2026-11-001"}]),
            [],
        )
        self.assertTrue(
            validate_invoice_custom_fields([{"name": "X" * 31, "value": "OK"}]),
        )
        self.assertTrue(
            validate_invoice_custom_fields([{"name": "  " + ("A" * 29), "value": "OK"}]),
        )
        self.assertTrue(
            validate_invoice_custom_fields([{"name": "OK", "value": "OK", "extra": "bad"}]),
        )
        self.assertTrue(
            validate_invoice_custom_fields([{"name": f"F{i}", "value": "V"} for i in range(5)]),
        )
        parsed = parse_custom_field_specs(["Client VAT=DE123456789", "PO Number=PO-99"])
        self.assertEqual(len(parsed), 2)
        self.assertEqual(parsed[0]["name"], "Client VAT")

        # 2. Invoice radar (open send_invoice overdue vs paid vs draft + scheduled payments governance)
        radar_client = FakeStripeClient(
            responses={
                "/v1/invoices": [
                    {
                        "id": "in_open_b2b",
                        "number": "INV-001",
                        "status": "open",
                        "collection_method": "send_invoice",
                        "subscription": None,
                        "customer": {"id": "cus_b2b1", "name": "Acme GmbH", "email": "ap@acme.example"},
                        "currency": "usd",
                        "amount_due": 102400,
                        "amount_paid": 0,
                        "amount_remaining": 102400,
                        "created": 1700000000,
                        "due_date": 1700086400,
                        "hosted_invoice_url": "https://invoice.stripe.com/i/acct_1/test_1",
                        "invoice_pdf": "https://pay.stripe.com/invoice/acct_1/test_1/pdf",
                        "custom_fields": [{"name": "Invoice Reference", "value": "INV-2026-11-001"}],
                    },
                    {
                        "id": "in_paid_sub",
                        "number": "SUB-002",
                        "status": "paid",
                        "collection_method": "charge_automatically",
                        "subscription": "sub_123",
                        "customer": "cus_sub1",
                        "currency": "usd",
                        "amount_due": 6400,
                        "amount_paid": 6400,
                        "amount_remaining": 0,
                        "created": 1700000000,
                    },
                    {
                        "id": "in_draft_orphan",
                        "status": "draft",
                        "collection_method": "send_invoice",
                        "subscription": None,
                        "customer": "cus_b2b1",
                        "currency": "usd",
                        "amount_due": 50000,
                        "amount_paid": 0,
                        "amount_remaining": 50000,
                        "created": 1700000000,
                    },
                ]
            }
        )
        radar = scan_invoice_radar(radar_client, redact_pii=True, now_timestamp=1700259200)
        self.assertTrue(radar["ok"])
        self.assertEqual(radar["summary"]["standalone_b2b_count"], 2)
        self.assertEqual(radar["summary"]["subscription_invoice_count"], 1)
        self.assertEqual(radar["summary"]["open_hosted_invoices_count"], 1)
        self.assertEqual(radar["summary"]["overdue_invoices_count"], 1)
        self.assertEqual(radar["summary"]["draft_invoices_count"], 1)
        self.assertEqual(radar["open_hosted_invoices"][0]["days_overdue"], 2)
        self.assertIn("***", radar["open_hosted_invoices"][0]["customer_email"])
        self.assertFalse(radar["scheduled_payments_governance"]["emits_scheduling_webhook"])

        # 3. Preview-first 4-step B2B invoice creation + bound invoiceitem
        inv_client = FakeStripeClient(
            responses={
                ("POST", "/v1/customers"): {"id": "cus_newb2b"},
                ("POST", "/v1/invoices"): {"id": "in_b2bdraft123", "status": "draft"},
                ("POST", "/v1/invoiceitems"): {"id": "ii_bound123"},
                ("POST", "/v1/invoices/in_b2bdraft123/finalize"): {"id": "in_b2bdraft123", "status": "open"},
                ("GET", "/v1/invoices/in_b2bdraft123"): {
                    "id": "in_b2bdraft123",
                    "number": "B2B-2026-001",
                    "status": "open",
                    "collection_method": "send_invoice",
                    "amount_due": 102400,
                    "due_date": 1701200000,
                    "hosted_invoice_url": "https://invoice.stripe.com/i/acct_1/b2b",
                    "invoice_pdf": "https://pay.stripe.com/invoice/acct_1/b2b/pdf",
                },
            }
        )
        preview = create_b2b_invoice(
            inv_client,
            customer_email="billing@client.example",
            company_name="Client Corp",
            amount_cents=102400,
            title="Completed Architecture Engagement",
            invoice_number_ref="INV-2026-11-001",
            custom_fields=[{"name": "Client VAT / Tax ID", "value": "CHE-123.456.789"}],
            apply=False,
        )
        self.assertTrue(preview["preview"])
        self.assertEqual(len(inv_client.calls), 0)
        with self.assertRaises(StripeError):
            create_b2b_invoice(
                inv_client,
                customer_email="billing@client.example",
                company_name="Client Corp",
                amount_cents=102400,
                title="Completed Architecture Engagement",
                invoice_number_ref="INV-2026-11-001",
                custom_fields=[{"name": "Client VAT / Tax ID", "value": "CHE-123.456.789"}],
                apply=True,
                confirm_plan="bad_hash",
            )
        created = create_b2b_invoice(
            inv_client,
            customer_email="billing@client.example",
            company_name="Client Corp",
            amount_cents=102400,
            title="Completed Architecture Engagement",
            invoice_number_ref="INV-2026-11-001",
            custom_fields=[{"name": "Client VAT / Tax ID", "value": "CHE-123.456.789"}],
            apply=True,
            confirm_plan=preview["plan_sha256"],
        )
        self.assertFalse(created["preview"])
        self.assertEqual(created["invoice"]["status"], "open")
        self.assertEqual(created["invoice"]["hosted_invoice_url"], "https://invoice.stripe.com/i/acct_1/b2b")
        # Verify Step 3 explicitly bound invoice=in_b2bdraft123 on /v1/invoiceitems
        ii_calls = [c for c in inv_client.calls if c[1] == "/v1/invoiceitems"]
        self.assertEqual(len(ii_calls), 1)
        self.assertEqual(ii_calls[0][2]["invoice"], "in_b2bdraft123")

        # 4. Preview-first invoice-void
        void_client = FakeStripeClient(
            responses={
                ("POST", "/v1/invoices/in_b2bdraft123/void"): {"id": "in_b2bdraft123", "status": "void"},
                ("GET", "/v1/invoices/in_b2bdraft123"): {"id": "in_b2bdraft123", "number": "B2B-2026-001", "status": "void"},
            }
        )
        v_prev = void_b2b_invoice(void_client, invoice_id="in_b2bdraft123", apply=False)
        self.assertTrue(v_prev["preview"])
        v_done = void_b2b_invoice(void_client, invoice_id="in_b2bdraft123", apply=True, confirm_plan=v_prev["plan_sha256"])
        self.assertEqual(v_done["invoice"]["status"], "void")

        # 5. Blueprint astro-b2b-invoice check
        b2b_bp = get_blueprint("astro-b2b-invoice")
        self.assertIn("collection_method", b2b_bp["code"])
        self.assertIn("send_invoice", b2b_bp["code"])
        self.assertIn("STRIPE_B2B_INVOICE_TOKEN", b2b_bp["code"])
        self.assertIn("timingSafeEqual", b2b_bp["code"])


if __name__ == "__main__":
    unittest.main()
