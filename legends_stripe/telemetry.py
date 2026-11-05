"""Treasury health, Stripe Drag fee breakdown, and cent-level cash reconciliation engine."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from .api import StripeClient


def mask_email(email: str | None, *, redact: bool = False) -> str | None:
    """Optionally mask an email address for PII-safe sharing."""
    if not email:
        return None
    if not redact:
        return email
    if "@" not in email:
        return "***"
    local, domain = email.split("@", 1)
    prefix = local[:1] if local else "*"
    return f"{prefix}***@{domain}"


def mask_name(name: str | None, *, redact: bool = False) -> str | None:
    """Optionally mask a customer name for PII-safe sharing."""
    if not name:
        return None
    if not redact:
        return name
    parts = name.strip().split()
    if not parts:
        return "***"
    return " ".join(p[0] + "***" for p in parts if p)


def format_usd(cents: int, currency: str = "usd") -> str:
    """Format minor units into a signed currency string."""
    cur = (currency or "usd").upper()
    sign = "-" if cents < 0 else ""
    abs_val = abs(cents) / 100.0
    return f"{sign}${abs_val:,.2f} {cur}"


format_cents = format_usd


def classify_charge_stream(description: str) -> str:
    """Classify a settled charge description into subscription, wallet_auto_recharge, or direct_checkout."""
    low = (description or "").lower()
    if "auto-recharge" in low or "sub-account" in low or "wallet" in low:
        return "wallet_auto_recharge"
    if "subscription" in low or "invoice" in low:
        return "subscription"
    return "direct_checkout"


def build_treasury_telemetry(
    client: StripeClient,
    *,
    max_transactions: int = 500,
    redact_pii: bool = False,
) -> dict[str, Any]:
    """Fetch account, balance, balance_transactions, charges, and payouts and reconcile to the cent."""
    account = client.request("GET", "/v1/account")
    balance = client.request("GET", "/v1/balance")
    txns = client.list_all("/v1/balance_transactions", {"limit": 100}, max_items=max_transactions)
    charges = client.list_all("/v1/charges", {"limit": 100}, max_items=min(max_transactions, 200))
    payouts = client.list_all("/v1/payouts", {"limit": 100}, max_items=100)

    default_currency = (account.get("default_currency") or "usd").lower()
    avail_list = balance.get("available") or []
    pend_list = balance.get("pending") or []
    available_cents = sum(
        int(x.get("amount", 0))
        for x in avail_list
        if (x.get("currency") or default_currency).lower() == default_currency
    )
    pending_cents = sum(
        int(x.get("amount", 0))
        for x in pend_list
        if (x.get("currency") or default_currency).lower() == default_currency
    )

    gross_revenue_cents = 0
    card_processing_fees_cents = 0
    billing_usage_fees_cents = 0
    other_fees_cents = 0
    refunds_gross_cents = 0
    disputes_cents = 0
    payouts_cents = 0
    net_all_non_payout_cents = 0

    for txn in txns:
        t_type = txn.get("type") or ""
        amount = int(txn.get("amount", 0))
        fee = int(txn.get("fee", 0))
        net = int(txn.get("net", 0))

        if t_type == "payout":
            payouts_cents += abs(amount)
            continue

        net_all_non_payout_cents += net

        if t_type in ("charge", "payment"):
            gross_revenue_cents += amount
            card_processing_fees_cents += fee
        elif t_type == "stripe_fee":
            # Standalone Stripe fee (e.g. Stripe Billing 0.5%/0.7% usage fee or custom domain fee)
            billing_drag = abs(amount) + fee
            billing_usage_fees_cents += billing_drag
        elif t_type in ("refund", "payment_refund"):
            refunds_gross_cents += abs(amount)
            if fee:
                other_fees_cents += fee
        elif t_type in ("adjustment", "dispute"):
            disputes_cents += abs(amount)
            if fee:
                other_fees_cents += fee
        else:
            if fee:
                other_fees_cents += fee

    total_fees_cents = card_processing_fees_cents + billing_usage_fees_cents + other_fees_cents
    effective_fee_rate_pct = (
        round((total_fees_cents / gross_revenue_cents) * 100.0, 2) if gross_revenue_cents > 0 else 0.0
    )

    expected_balance_cents = net_all_non_payout_cents - payouts_cents
    actual_balance_cents = available_cents + pending_cents
    variance_cents = actual_balance_cents - expected_balance_cents

    # Map balance_transaction IDs to fee/net for itemized charge enrichment
    txn_by_id = {t.get("id"): t for t in txns if t.get("id")}
    itemized_events: list[dict[str, Any]] = []
    card_verifications_3ds_count = 0
    streams: dict[str, dict[str, int]] = {}

    for ch in charges:
        if not ch.get("paid") or ch.get("status") != "succeeded":
            continue
        bt_id = ch.get("balance_transaction")
        bt = txn_by_id.get(bt_id, {}) if isinstance(bt_id, str) else (bt_id if isinstance(bt_id, dict) else {})
        amount = int(ch.get("amount", 0))
        fee = int(bt.get("fee", 0))
        net = int(bt.get("net", amount - fee))
        desc = ch.get("description") or (ch.get("calculated_statement_descriptor") or "Stripe Charge")
        refunded = bool(ch.get("refunded"))

        # Filter temporary $1.00 3DS card setup verifications that are immediately refunded/voided with 0 fee
        if refunded and fee == 0 and ("3ds verification" in desc.lower() or not bt_id):
            card_verifications_3ds_count += 1
            continue

        stream = classify_charge_stream(desc)
        bucket = streams.setdefault(stream, {"count": 0, "gross_cents": 0, "fee_cents": 0, "net_cents": 0})
        bucket["count"] += 1
        bucket["gross_cents"] += amount
        bucket["fee_cents"] += fee
        bucket["net_cents"] += net

        billing_details = ch.get("billing_details") or {}
        created_ts = int(ch.get("created", 0))
        created_date = (
            datetime.fromtimestamp(created_ts, tz=timezone.utc).strftime("%Y-%m-%d") if created_ts else "unknown"
        )
        itemized_events.append(
            {
                "charge_id": ch.get("id"),
                "date": created_date,
                "stream": stream,
                "customer_name": mask_name(billing_details.get("name"), redact=redact_pii),
                "customer_email": mask_email(billing_details.get("email") or ch.get("receipt_email"), redact=redact_pii),
                "gross_cents": amount,
                "fee_cents": fee,
                "net_cents": net,
                "currency": (ch.get("currency") or default_currency).lower(),
                "description": desc,
                "refunded": refunded,
            }
        )

    revenue_streams: dict[str, Any] = {}
    for s_name, s_data in sorted(streams.items()):
        s_gross = s_data["gross_cents"]
        s_fee = s_data["fee_cents"]
        s_net = s_data["net_cents"]
        revenue_streams[s_name] = {
            "count": s_data["count"],
            "gross_cents": s_gross,
            "gross_formatted": format_usd(s_gross, default_currency),
            "card_fee_cents": s_fee,
            "card_fee_formatted": format_usd(-s_fee, default_currency),
            "net_cents": s_net,
            "net_formatted": format_usd(s_net, default_currency),
            "card_drag_pct": round((s_fee / s_gross) * 100.0, 2) if s_gross > 0 else 0.0,
        }

    settings = account.get("settings") or {}
    payout_settings = (settings.get("payouts") or {}).get("schedule") or {}
    business_profile = account.get("business_profile") or {}
    dashboard_settings = settings.get("dashboard") or {}

    return {
        "ok": True,
        "mode": client.mode,
        "account": {
            "account_id": account.get("id"),
            "business_name": business_profile.get("name") or dashboard_settings.get("display_name") or account.get("id"),
            "display_name": dashboard_settings.get("display_name"),
            "email": mask_email(account.get("email") or business_profile.get("support_email"), redact=redact_pii),
            "country": account.get("country"),
            "default_currency": default_currency,
            "charges_enabled": bool(account.get("charges_enabled")),
            "payouts_enabled": bool(account.get("payouts_enabled")),
            "payout_schedule": {
                "interval": payout_settings.get("interval", "unknown"),
                "delay_days": payout_settings.get("delay_days"),
            },
            "type": account.get("type"),
        },
        "balance": {
            "currency": default_currency,
            "available_cents": available_cents,
            "available_formatted": format_usd(available_cents, default_currency),
            "pending_cents": pending_cents,
            "pending_formatted": format_usd(pending_cents, default_currency),
            "total_balance_cents": actual_balance_cents,
            "total_balance_formatted": format_usd(actual_balance_cents, default_currency),
        },
        "revenue_and_drag": {
            "transactions_sampled": len(txns),
            "verified_charges_count": len(itemized_events),
            "card_verifications_3ds_count": card_verifications_3ds_count,
            "executed_payouts_count": len(payouts),
            "gross_revenue_cents": gross_revenue_cents,
            "gross_revenue_formatted": format_usd(gross_revenue_cents, default_currency),
            "total_fees_cents": total_fees_cents,
            "total_fees_formatted": format_usd(-total_fees_cents, default_currency),
            "card_processing_fees_cents": card_processing_fees_cents,
            "card_processing_fees_formatted": format_usd(-card_processing_fees_cents, default_currency),
            "billing_usage_fees_cents": billing_usage_fees_cents,
            "billing_usage_fees_formatted": format_usd(-billing_usage_fees_cents, default_currency),
            "other_fees_cents": other_fees_cents,
            "effective_fee_rate_pct": effective_fee_rate_pct,
            "refunds_gross_cents": refunds_gross_cents,
            "disputes_cents": disputes_cents,
            "net_retained_cash_cents": net_all_non_payout_cents,
            "net_retained_cash_formatted": format_usd(net_all_non_payout_cents, default_currency),
            "total_payouts_cents": payouts_cents,
            "total_payouts_formatted": format_usd(payouts_cents, default_currency),
            "reconciled": variance_cents == 0,
            "reconciliation_variance_cents": variance_cents,
            "reconciliation_variance_formatted": format_usd(variance_cents, default_currency),
        },
        "revenue_streams": revenue_streams,
        "itemized_events": itemized_events,
    }


def render_telemetry_markdown(report: dict[str, Any]) -> str:
    """Render a source-cited Markdown treasury report without em-dashes or en-dashes."""
    acct = report["account"]
    bal = report["balance"]
    rev = report["revenue_and_drag"]
    cur = bal["currency"]
    sched = acct["payout_schedule"].get("interval", "unknown").capitalize()
    lines = [
        "---",
        "type: telemetry",
        f"title: \"{acct['business_name']} Stripe Direct Telemetry\"",
        "status: active",
        "tags:",
        "  - stripe",
        "  - telemetry",
        "---",
        "",
        f"# {acct['business_name']} Stripe Direct Telemetry",
        "",
        f"Direct telemetry ingested from Stripe account `{acct['account_id']}` (`{acct['business_name']}`, mode: `{report['mode']}`).",
        "",
        "## Live Account Health",
        f"- **Account Name**: {acct['business_name']}",
        f"- **Account ID**: `{acct['account_id']}`",
        f"- **Liquid Available Balance**: **{bal['available_formatted']}**",
        f"- **Pending Balance**: **{bal['pending_formatted']}**",
        f"- **Charges Enabled**: {acct['charges_enabled']}",
        f"- **Payouts Enabled**: {acct['payouts_enabled']}",
        f"- **Payout Schedule**: **{sched}** ({rev['executed_payouts_count']} payouts executed)",
        f"- **Historical Gross Revenue**: **{rev['gross_revenue_formatted']}** across {rev['verified_charges_count']} verified client payments ({rev.get('card_verifications_3ds_count', 0)} zero-fee 3DS card setup verifications filtered)",
        f"- **Stripe Drag (Fees)**: **{rev['total_fees_formatted']}** ({rev['card_processing_fees_formatted']} card processing fees, {rev['billing_usage_fees_formatted']} billing/platform usage fees, effective drag: {rev['effective_fee_rate_pct']}%)",
        f"- **Net Retained Cash**: **{rev['net_retained_cash_formatted']}** (reconciliation variance: {rev['reconciliation_variance_formatted']})",
        "",
        "## Revenue Streams and Drag Breakdown",
    ]
    streams = report.get("revenue_streams") or {}
    if not streams:
        lines.append("- No settled revenue streams in sampled history.")
    else:
        for s_name, s_data in streams.items():
            lines.append(
                f"- **{s_name}**: {s_data['count']} charges | Gross: **{s_data['gross_formatted']}** | Card Fees: {s_data['card_fee_formatted']} ({s_data['card_drag_pct']}% drag) | Net: **{s_data['net_formatted']}**"
            )
    lines.extend(["", "## Itemized Client Revenue Events"])
    if not report["itemized_events"]:
        lines.append("- No succeeded payment charges in sampled history.")
    else:
        for idx, ev in enumerate(report["itemized_events"], start=1):
            raw_who = ev.get("customer_name") or "Customer"
            who = f"`{raw_who}`" if "*" in raw_who else f"**{raw_who}**"
            email_part = f" (`{ev['customer_email']}`)" if ev.get("customer_email") else ""
            gross_s = format_usd(ev["gross_cents"], cur)
            fee_s = format_usd(-ev["fee_cents"], cur)
            net_s = format_usd(ev["net_cents"], cur)
            clean_desc = " ".join(str(ev.get("description") or "Stripe Charge").split())
            lines.append(
                f"{idx}. {who}{email_part}: **+{gross_s}** ({ev['date']}) | Fee: {fee_s} | Net: +{net_s} | {clean_desc}"
            )
    return "\n".join(lines) + "\n"
