"""B2B invoice governance, Hosted Invoice Page radar, custom-field lint, and preview-first invoice lifecycle."""
from __future__ import annotations

from datetime import datetime, timezone
import re
import time
from typing import Any

from .api import (
    StripeClient,
    StripeError,
    plan_digest,
    validate_metadata,
    validate_stripe_id,
)
from .subscriptions import ts_to_iso
from .telemetry import format_usd, mask_email, mask_name

MAX_INVOICE_CUSTOM_FIELDS = 4
MAX_INVOICE_CUSTOM_FIELD_LEN = 30
MAX_INVOICE_DESCRIPTION_LEN = 500
MAX_INVOICE_FOOTER_LEN = 500
_CONTROL_CHAR_RE = re.compile(r"[\x00-\x1f\x7f]")
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def validate_invoice_custom_fields(custom_fields: list[dict[str, str]] | None) -> list[str]:
    """Validate Stripe Invoicing custom_fields (maximum 4 fields, each name and value 1..30 chars)."""
    if not custom_fields:
        return []
    issues: list[str] = []
    if len(custom_fields) > MAX_INVOICE_CUSTOM_FIELDS:
        issues.append(
            f"Stripe invoices allow at most {MAX_INVOICE_CUSTOM_FIELDS} custom_fields (got {len(custom_fields)})."
        )
    for idx, entry in enumerate(custom_fields):
        if not isinstance(entry, dict):
            issues.append(f"custom_fields[{idx}] must be an object with 'name' and 'value'.")
            continue
        extra_keys = sorted(set(entry.keys()) - {"name", "value"})
        if extra_keys:
            issues.append(f"custom_fields[{idx}] has unsupported keys: {', '.join(str(k) for k in extra_keys)}.")
        raw_name = "" if entry.get("name") is None else str(entry.get("name"))
        raw_val = "" if entry.get("value") is None else str(entry.get("value"))
        name = raw_name.strip()
        val = raw_val.strip()
        if not name:
            issues.append(f"custom_fields[{idx}].name cannot be empty.")
        elif len(raw_name) > MAX_INVOICE_CUSTOM_FIELD_LEN or len(name) > MAX_INVOICE_CUSTOM_FIELD_LEN:
            issues.append(
                f"custom_fields[{idx}].name '{name}' is {max(len(raw_name), len(name))} chars (exceeds {MAX_INVOICE_CUSTOM_FIELD_LEN}-char Stripe limit)."
            )
        elif _CONTROL_CHAR_RE.search(raw_name):
            issues.append(f"custom_fields[{idx}].name contains control characters.")

        if not val:
            issues.append(f"custom_fields[{idx}].value cannot be empty.")
        elif len(raw_val) > MAX_INVOICE_CUSTOM_FIELD_LEN or len(val) > MAX_INVOICE_CUSTOM_FIELD_LEN:
            issues.append(
                f"custom_fields[{idx}].value '{val}' is {max(len(raw_val), len(val))} chars (exceeds {MAX_INVOICE_CUSTOM_FIELD_LEN}-char Stripe limit)."
            )
        elif _CONTROL_CHAR_RE.search(raw_val):
            issues.append(f"custom_fields[{idx}].value contains control characters.")
    return issues


def parse_custom_field_specs(raw_specs: list[str] | None) -> list[dict[str, str]]:
    """Parse KEY=VALUE strings into Stripe custom_fields dicts."""
    if not raw_specs:
        return []
    out: list[dict[str, str]] = []
    for raw in raw_specs:
        if "=" not in raw:
            raise StripeError(f"Invalid --custom-field '{raw}': expected NAME=VALUE format (max 30 chars each).")
        k, v = raw.split("=", 1)
        out.append({"name": k.strip(), "value": v.strip()})
    return out


def validate_b2b_invoice_fields(
    *,
    customer_id: str | None = None,
    customer_email: str | None = None,
    amount_cents: int,
    currency: str = "usd",
    title: str,
    description: str | None = None,
    invoice_description: str | None = None,
    footer: str | None = None,
    days_until_due: int = 14,
    custom_fields: list[dict[str, str]] | None = None,
    metadata: dict[str, Any] | None = None,
) -> list[str]:
    """Validate all B2B Hosted Invoice parameters against Stripe API v1 limits before preview or execution."""
    issues: list[str] = []
    if customer_id:
        try:
            validate_stripe_id(customer_id, expected_prefix="cus_", field_name="customer_id")
        except StripeError as exc:
            issues.append(str(exc))
    elif customer_email:
        clean_email = customer_email.strip()
        if len(clean_email) > 254 or not _EMAIL_RE.fullmatch(clean_email) or _CONTROL_CHAR_RE.search(clean_email):
            issues.append(f"Invalid customer_email: {customer_email!r}")
    else:
        issues.append("Either customer_id (cus_...) or customer_email is required.")

    cur = (currency or "").strip().lower()
    if len(cur) != 3 or not cur.isalpha() or not cur.isascii():
        issues.append(f"Invalid 3-letter ISO currency code: {currency!r}")

    if not isinstance(amount_cents, int) or isinstance(amount_cents, bool) or amount_cents <= 0 or amount_cents > 99_999_999:
        issues.append(f"amount_cents must be a positive integer between 1 and 99,999,999 (got {amount_cents!r}).")

    clean_title = (title or "").strip()
    if not clean_title:
        issues.append("Line-item title cannot be empty.")
    elif len(clean_title) > 250 or _CONTROL_CHAR_RE.search(clean_title):
        issues.append("Line-item title must be 1..250 printable characters.")

    combined_item_desc = f"{clean_title} - {description.strip()}" if (description and description.strip()) else clean_title
    if len(combined_item_desc) > MAX_INVOICE_DESCRIPTION_LEN:
        issues.append(
            f"Combined line-item title and description is {len(combined_item_desc)} chars (exceeds {MAX_INVOICE_DESCRIPTION_LEN}-char limit)."
        )

    if invoice_description and len(invoice_description) > MAX_INVOICE_DESCRIPTION_LEN:
        issues.append(
            f"Invoice description is {len(invoice_description)} chars (exceeds {MAX_INVOICE_DESCRIPTION_LEN}-char limit)."
        )

    if footer and len(footer) > MAX_INVOICE_FOOTER_LEN:
        issues.append(f"Invoice footer is {len(footer)} chars (exceeds {MAX_INVOICE_FOOTER_LEN}-char limit).")

    if not isinstance(days_until_due, int) or isinstance(days_until_due, bool) or days_until_due < 1 or days_until_due > 365:
        issues.append(f"days_until_due must be an integer between 1 and 365 (got {days_until_due!r}).")

    issues.extend(validate_invoice_custom_fields(custom_fields))
    issues.extend(validate_metadata(metadata))
    return issues


def scan_invoice_radar(
    client: StripeClient,
    *,
    max_items: int = 500,
    redact_pii: bool = False,
    now_timestamp: int | None = None,
) -> dict[str, Any]:
    """Scan Stripe invoices for B2B send_invoice receivables, overdue aging, draft orphans, and Hosted Invoice readiness."""
    invoices = client.list_all(
        "/v1/invoices",
        {"limit": 100, "expand": ["data.customer"]},
        max_items=max_items,
    )
    now_ts = int(now_timestamp if now_timestamp is not None else time.time())

    counts_by_status: dict[str, int] = {}
    counts_by_collection_method: dict[str, int] = {}
    standalone_b2b_count = 0
    subscription_invoice_count = 0

    open_receivables_cents_by_currency: dict[str, int] = {}
    paid_volume_cents_by_currency: dict[str, int] = {}
    standalone_open_cents_by_currency: dict[str, int] = {}
    standalone_paid_cents_by_currency: dict[str, int] = {}

    open_hosted_invoices: list[dict[str, Any]] = []
    overdue_invoices: list[dict[str, Any]] = []
    draft_invoices: list[dict[str, Any]] = []
    governance_warnings: list[dict[str, Any]] = []

    for inv in invoices:
        inv_id = inv.get("id") or "unknown"
        status = inv.get("status") or "unknown"
        collection_method = inv.get("collection_method") or "unknown"
        sub_ref = inv.get("subscription")
        is_standalone = sub_ref is None

        counts_by_status[status] = counts_by_status.get(status, 0) + 1
        counts_by_collection_method[collection_method] = counts_by_collection_method.get(collection_method, 0) + 1
        if is_standalone:
            standalone_b2b_count += 1
        else:
            subscription_invoice_count += 1

        cust = inv.get("customer")
        if isinstance(cust, dict):
            cust_id = cust.get("id") or "unknown"
            cust_email = (cust.get("email") or inv.get("customer_email") or "").strip().lower() or None
            cust_name = cust.get("name") or inv.get("customer_name")
        else:
            cust_id = str(cust or "unknown")
            cust_email = (inv.get("customer_email") or "").strip().lower() or None
            cust_name = inv.get("customer_name")

        currency = (inv.get("currency") or "usd").lower()
        amount_due = int(inv.get("amount_due") or 0)
        amount_paid = int(inv.get("amount_paid") or 0)
        amount_remaining = int(inv.get("amount_remaining") if inv.get("amount_remaining") is not None else amount_due)
        due_date_ts = inv.get("due_date")
        created_ts = inv.get("created")
        hosted_url = inv.get("hosted_invoice_url")
        pdf_url = inv.get("invoice_pdf")
        cfields = inv.get("custom_fields") or []

        is_overdue = bool(status == "open" and due_date_ts and int(due_date_ts) < now_ts and amount_remaining > 0)
        days_overdue = max(0, int((now_ts - int(due_date_ts)) // 86400)) if is_overdue and due_date_ts else 0

        row = {
            "invoice_id": inv_id,
            "number": inv.get("number"),
            "status": status,
            "collection_method": collection_method,
            "standalone_b2b": is_standalone,
            "customer_id": cust_id,
            "customer_name": mask_name(cust_name, redact=redact_pii),
            "customer_email": mask_email(cust_email, redact=redact_pii),
            "amount_due_cents": amount_due,
            "amount_paid_cents": amount_paid,
            "amount_remaining_cents": amount_remaining,
            "amount_formatted": format_usd(amount_due, currency),
            "currency": currency,
            "created": ts_to_iso(created_ts),
            "due_date": ts_to_iso(due_date_ts),
            "is_overdue": is_overdue,
            "days_overdue": days_overdue,
            "hosted_invoice_url": hosted_url,
            "invoice_pdf": pdf_url,
            "custom_fields_count": len(cfields),
        }

        if status == "open":
            open_receivables_cents_by_currency[currency] = (
                open_receivables_cents_by_currency.get(currency, 0) + amount_remaining
            )
            if is_standalone:
                standalone_open_cents_by_currency[currency] = (
                    standalone_open_cents_by_currency.get(currency, 0) + amount_remaining
                )
            if collection_method == "send_invoice":
                open_hosted_invoices.append(row)
                if not due_date_ts:
                    governance_warnings.append({
                        "kind": "open_send_invoice_missing_due_date",
                        "invoice_id": inv_id,
                        "message": f"Open send_invoice {inv_id} has no due_date set.",
                    })
                if not hosted_url:
                    governance_warnings.append({
                        "kind": "open_send_invoice_missing_hosted_url",
                        "invoice_id": inv_id,
                        "message": f"Open send_invoice {inv_id} is missing hosted_invoice_url.",
                    })
            if is_overdue:
                overdue_invoices.append(row)

        elif status == "paid":
            paid_volume_cents_by_currency[currency] = paid_volume_cents_by_currency.get(currency, 0) + amount_paid
            if is_standalone:
                standalone_paid_cents_by_currency[currency] = (
                    standalone_paid_cents_by_currency.get(currency, 0) + amount_paid
                )

        elif status == "draft":
            draft_invoices.append(row)
            governance_warnings.append({
                "kind": "unfinalized_draft_invoice",
                "invoice_id": inv_id,
                "message": f"Draft invoice {inv_id} ({format_usd(amount_due, currency)}) has not been finalized.",
            })

        cf_issues = validate_invoice_custom_fields(
            [{"name": str(c.get("name") or ""), "value": str(c.get("value") or "")} for c in cfields if isinstance(c, dict)]
        )
        for issue in cf_issues:
            governance_warnings.append({
                "kind": "invalid_invoice_custom_field",
                "invoice_id": inv_id,
                "message": f"Invoice {inv_id}: {issue}",
            })

    return {
        "ok": True,
        "mode": client.mode,
        "summary": {
            "total_invoices_sampled": len(invoices),
            "standalone_b2b_count": standalone_b2b_count,
            "subscription_invoice_count": subscription_invoice_count,
            "counts_by_status": counts_by_status,
            "counts_by_collection_method": counts_by_collection_method,
            "open_hosted_invoices_count": len(open_hosted_invoices),
            "overdue_invoices_count": len(overdue_invoices),
            "draft_invoices_count": len(draft_invoices),
            "governance_warnings_count": len(governance_warnings),
            "open_receivables_cents_by_currency": open_receivables_cents_by_currency,
            "open_receivables_formatted": {
                cur: format_usd(val, cur) for cur, val in open_receivables_cents_by_currency.items()
            },
            "paid_volume_cents_by_currency": paid_volume_cents_by_currency,
            "paid_volume_formatted": {
                cur: format_usd(val, cur) for cur, val in paid_volume_cents_by_currency.items()
            },
            "standalone_open_cents_by_currency": standalone_open_cents_by_currency,
            "standalone_paid_cents_by_currency": standalone_paid_cents_by_currency,
        },
        "scheduled_payments_governance": {
            "dashboard_only_toggle": True,
            "emits_scheduling_webhook": False,
            "api_status_while_scheduled": "open",
            "settlement_webhook_event": "invoice.paid",
            "note": (
                "Customer-scheduled payments on Stripe Hosted Invoice Pages are toggled in the Stripe Dashboard "
                "(Settings > Billing > Invoices), emit no webhook when scheduled, and remain status='open' in "
                "GET /v1/invoices until the scheduled charge executes and 'invoice.paid' fires. For post-completion "
                "B2B settlements where work is already rendered in full and immediate settlement is expected, "
                "keep scheduled payments off or use a short days_until_due window so ready-to-pay buyers are not "
                "prompted to defer cash settlement."
            ),
        },
        "open_hosted_invoices": open_hosted_invoices,
        "overdue_invoices": overdue_invoices,
        "draft_invoices": draft_invoices,
        "governance_warnings": governance_warnings,
    }


def create_b2b_invoice(
    client: StripeClient,
    *,
    customer_id: str | None = None,
    customer_email: str | None = None,
    customer_name: str | None = None,
    company_name: str | None = None,
    amount_cents: int,
    currency: str = "usd",
    title: str,
    description: str | None = None,
    invoice_description: str | None = None,
    invoice_number_ref: str | None = None,
    days_until_due: int = 14,
    auto_advance: bool = False,
    footer: str | None = None,
    custom_fields: list[dict[str, str]] | None = None,
    metadata: dict[str, str] | None = None,
    apply: bool = False,
    confirm_plan: str | None = None,
    allow_live: bool = False,
) -> dict[str, Any]:
    """Preview-first creation of an orphan-free B2B Stripe Hosted Invoice (send_invoice)."""
    clean_cid = customer_id.strip() if customer_id else None
    clean_email = customer_email.strip().lower() if customer_email else None
    clean_name = customer_name.strip() if customer_name else None
    clean_company = company_name.strip() if company_name else None
    clean_cur = (currency or "usd").strip().lower()
    clean_title = (title or "").strip()
    clean_desc = description.strip() if description else None
    clean_ref = invoice_number_ref.strip() if invoice_number_ref else None
    clean_footer = footer.strip() if footer else None

    resolved_cfields: list[dict[str, str]] = []
    for cf in custom_fields or []:
        if isinstance(cf, dict) and set(cf.keys()) <= {"name", "value"}:
            resolved_cfields.append(
                {
                    "name": str(cf.get("name") or "").strip(),
                    "value": str(cf.get("value") or "").strip(),
                }
            )
        else:
            resolved_cfields.append(cf)
    if clean_ref and not any(isinstance(cf, dict) and cf.get("name") == "Invoice Reference" for cf in resolved_cfields):
        if len(resolved_cfields) < MAX_INVOICE_CUSTOM_FIELDS:
            ref_val = clean_ref[:MAX_INVOICE_CUSTOM_FIELD_LEN].strip()
            if ref_val:
                resolved_cfields.insert(0, {"name": "Invoice Reference", "value": ref_val})

    resolved_meta = dict(metadata or {})
    if clean_ref:
        resolved_meta.setdefault("invoice_reference", clean_ref)
    if clean_company:
        resolved_meta.setdefault("company_name", clean_company)

    resolved_inv_desc = (
        invoice_description.strip()
        if invoice_description and invoice_description.strip()
        else (
            f"Invoice {clean_ref} - {clean_title}" if clean_ref else f"B2B Commercial Invoice - {clean_title}"
        )
    )

    issues = validate_b2b_invoice_fields(
        customer_id=clean_cid,
        customer_email=clean_email,
        amount_cents=amount_cents,
        currency=clean_cur,
        title=clean_title,
        description=clean_desc,
        invoice_description=resolved_inv_desc,
        footer=clean_footer,
        days_until_due=days_until_due,
        custom_fields=resolved_cfields,
        metadata=resolved_meta,
    )
    if issues:
        raise StripeError("B2B invoice validation failed: " + "; ".join(issues))

    display_entity = clean_company or clean_name or clean_email or clean_cid or "B2B Client"
    customer_display_name = (
        f"{clean_company} - Attn: {clean_name}"
        if (clean_company and clean_name and clean_company != clean_name)
        else display_entity
    )
    line_item_description = f"{clean_title} - {clean_desc}" if clean_desc else clean_title

    planned = {
        "action": "create_b2b_invoice",
        "mode": client.mode,
        "customer_id": clean_cid,
        "customer_email": clean_email,
        "customer_display_name": customer_display_name if not clean_cid else None,
        "collection_method": "send_invoice",
        "days_until_due": days_until_due,
        "auto_advance": bool(auto_advance),
        "amount_cents": amount_cents,
        "amount_formatted": format_usd(amount_cents, clean_cur),
        "currency": clean_cur,
        "line_item_description": line_item_description,
        "invoice_description": resolved_inv_desc,
        "footer": clean_footer,
        "custom_fields": resolved_cfields,
        "metadata": resolved_meta,
        "steps": [
            "1. Resolve existing cus_... or create Customer via POST /v1/customers",
            "2. Create draft send_invoice via POST /v1/invoices (auto_advance=false)",
            "3. Attach line item bound explicitly to draft invoice ID via POST /v1/invoiceitems (prevents orphaned pending items)",
            "4. Finalize invoice via POST /v1/invoices/{id}/finalize and verify hosted_invoice_url + invoice_pdf readback",
        ],
    }
    digest = plan_digest("invoice-create", planned)

    if not apply:
        return {
            "ok": True,
            "preview": True,
            "mode": client.mode,
            "plan_sha256": digest,
            "planned": planned,
            "next": (
                f"Re-run with --apply --confirm-plan {digest}"
                + (" --live" if client.mode == "live" else "")
                + " to issue and finalize the B2B invoice."
            ),
        }

    client.require_mutation_gate(allow_live=allow_live)
    if not confirm_plan or confirm_plan.strip() != digest:
        raise StripeError(
            f"Confirmation hash mismatch for invoice-create. Expected --confirm-plan {digest}, got {confirm_plan!r}."
        )

    # Step 1: Customer
    target_customer_id = clean_cid
    if not target_customer_id:
        cust_payload: dict[str, Any] = {
            "name": customer_display_name,
            "email": clean_email,
            "metadata": resolved_meta,
        }
        created_cust = client.request(
            "POST",
            "/v1/customers",
            cust_payload,
            idempotency_key=f"legends-stripe-b2b-cust-{digest[:24]}",
        )
        target_customer_id = validate_stripe_id(
            str(created_cust.get("id") or ""),
            expected_prefix="cus_",
            field_name="created_customer_id",
        )

    # Step 2: Draft Invoice
    draft_payload: dict[str, Any] = {
        "customer": target_customer_id,
        "collection_method": "send_invoice",
        "days_until_due": days_until_due,
        "auto_advance": False,
        "description": resolved_inv_desc,
        "metadata": resolved_meta,
    }
    if clean_footer:
        draft_payload["footer"] = clean_footer
    if resolved_cfields:
        draft_payload["custom_fields"] = resolved_cfields

    draft = client.request(
        "POST",
        "/v1/invoices",
        draft_payload,
        idempotency_key=f"legends-stripe-b2b-draft-{digest[:24]}",
    )
    draft_id = validate_stripe_id(
        str(draft.get("id") or ""),
        expected_prefix="in_",
        field_name="draft_invoice_id",
    )

    # Step 3: Invoice Item explicitly bound to draft_id (prevents floating pending items)
    item = client.request(
        "POST",
        "/v1/invoiceitems",
        {
            "customer": target_customer_id,
            "invoice": draft_id,
            "amount": amount_cents,
            "currency": clean_cur,
            "description": line_item_description,
        },
        idempotency_key=f"legends-stripe-b2b-item-{digest[:24]}",
    )

    # Step 4: Finalize & Readback
    client.request(
        "POST",
        f"/v1/invoices/{draft_id}/finalize",
        {"auto_advance": bool(auto_advance)},
        idempotency_key=f"legends-stripe-b2b-fin-{digest[:24]}",
    )
    readback = client.request("GET", f"/v1/invoices/{draft_id}")

    return {
        "ok": True,
        "preview": False,
        "mode": client.mode,
        "plan_sha256": digest,
        "customer_id": target_customer_id,
        "invoice_item_id": item.get("id"),
        "invoice": {
            "id": readback.get("id") or draft_id,
            "number": readback.get("number"),
            "status": readback.get("status"),
            "collection_method": readback.get("collection_method"),
            "amount_due_cents": int(readback.get("amount_due") or amount_cents),
            "amount_formatted": format_usd(int(readback.get("amount_due") or amount_cents), clean_cur),
            "currency": clean_cur,
            "due_date": ts_to_iso(readback.get("due_date")),
            "hosted_invoice_url": readback.get("hosted_invoice_url"),
            "invoice_pdf": readback.get("invoice_pdf"),
            "custom_fields": readback.get("custom_fields") or resolved_cfields,
        },
    }


def void_b2b_invoice(
    client: StripeClient,
    *,
    invoice_id: str,
    apply: bool = False,
    confirm_plan: str | None = None,
    allow_live: bool = False,
) -> dict[str, Any]:
    """Preview-first voiding of an open Stripe invoice with readback verification."""
    clean_id = validate_stripe_id(invoice_id, expected_prefix="in_", field_name="invoice_id")
    planned = {
        "action": "void_b2b_invoice",
        "mode": client.mode,
        "invoice_id": clean_id,
        "endpoint": f"POST /v1/invoices/{clean_id}/void",
    }
    digest = plan_digest("invoice-void", planned)

    if not apply:
        return {
            "ok": True,
            "preview": True,
            "mode": client.mode,
            "plan_sha256": digest,
            "planned": planned,
            "next": (
                f"Re-run with --apply --confirm-plan {digest}"
                + (" --live" if client.mode == "live" else "")
                + f" to void invoice {clean_id}."
            ),
        }

    client.require_mutation_gate(allow_live=allow_live)
    if not confirm_plan or confirm_plan.strip() != digest:
        raise StripeError(
            f"Confirmation hash mismatch for invoice-void. Expected --confirm-plan {digest}, got {confirm_plan!r}."
        )

    client.request(
        "POST",
        f"/v1/invoices/{clean_id}/void",
        {},
        idempotency_key=f"legends-stripe-b2b-void-{digest[:24]}",
    )
    readback = client.request("GET", f"/v1/invoices/{clean_id}")
    return {
        "ok": True,
        "preview": False,
        "mode": client.mode,
        "plan_sha256": digest,
        "invoice": {
            "id": readback.get("id") or clean_id,
            "number": readback.get("number"),
            "status": readback.get("status"),
        },
    }
