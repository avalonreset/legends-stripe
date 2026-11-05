"""Subscription dunning radar, duplicate active-subscription detector, and customer billing lookup."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from .api import StripeClient
from .telemetry import format_usd, mask_email, mask_name


def ts_to_iso(ts: int | None) -> str | None:
    """Convert a Unix timestamp to an ISO-8601 UTC date string."""
    if not ts:
        return None
    return datetime.fromtimestamp(int(ts), tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def scan_subscription_radar(
    client: StripeClient,
    *,
    max_items: int = 500,
    redact_pii: bool = False,
) -> dict[str, Any]:
    """Scan all subscriptions for dunning/past_due risk, pending cancellations, and duplicate active billing."""
    subs = client.list_all(
        "/v1/subscriptions",
        {"status": "all", "expand": ["data.customer"], "limit": 100},
        max_items=max_items,
    )

    counts_by_status: dict[str, int] = {}
    active_by_customer_id: dict[str, list[dict[str, Any]]] = {}
    active_by_email: dict[str, list[dict[str, Any]]] = {}
    dunning_queue: list[dict[str, Any]] = []
    canceling_at_period_end: list[dict[str, Any]] = []
    mrr_cents_by_currency: dict[str, int] = {}

    for sub in subs:
        status = sub.get("status") or "unknown"
        counts_by_status[status] = counts_by_status.get(status, 0) + 1

        cust = sub.get("customer")
        if isinstance(cust, dict):
            cust_id = cust.get("id") or "unknown"
            cust_email = (cust.get("email") or "").strip().lower() or None
            cust_name = cust.get("name")
        else:
            cust_id = str(cust or "unknown")
            cust_email = None
            cust_name = None

        items_data = ((sub.get("items") or {}).get("data")) or []
        first_price = (items_data[0].get("price") or {}) if items_data else {}
        unit_amount = int(first_price.get("unit_amount") or 0)
        currency = (first_price.get("currency") or sub.get("currency") or "usd").lower()
        rec = first_price.get("recurring") or {}
        interval = rec.get("interval")
        interval_count = int(rec.get("interval_count") or 1)

        summary_row = {
            "subscription_id": sub.get("id"),
            "status": status,
            "customer_id": cust_id,
            "customer_name": mask_name(cust_name, redact=redact_pii),
            "customer_email": mask_email(cust_email, redact=redact_pii),
            "unit_amount_cents": unit_amount,
            "amount_formatted": format_usd(unit_amount, currency),
            "currency": currency,
            "interval": interval,
            "interval_count": interval_count,
            "cancel_at_period_end": bool(sub.get("cancel_at_period_end")),
            "current_period_end": ts_to_iso(sub.get("current_period_end")),
        }

        if status in ("active", "trialing", "past_due"):
            active_by_customer_id.setdefault(cust_id, []).append(summary_row)
            if cust_email:
                active_by_email.setdefault(cust_email, []).append(summary_row)

        if status == "active":
            # Normalize recurring price into approximate MRR in minor units
            if interval == "month" and interval_count > 0:
                mrr_add = int(round(unit_amount / interval_count))
            elif interval == "year" and interval_count > 0:
                mrr_add = int(round(unit_amount / (12 * interval_count)))
            elif interval == "week" and interval_count > 0:
                mrr_add = int(round((unit_amount * 52) / (12 * interval_count)))
            elif interval == "day" and interval_count > 0:
                mrr_add = int(round((unit_amount * 365) / (12 * interval_count)))
            else:
                mrr_add = 0
            mrr_cents_by_currency[currency] = mrr_cents_by_currency.get(currency, 0) + mrr_add

        if status in ("past_due", "unpaid", "incomplete"):
            dunning_queue.append(summary_row)

        if status in ("active", "trialing") and sub.get("cancel_at_period_end"):
            canceling_at_period_end.append(summary_row)

    duplicate_customer_subs: list[dict[str, Any]] = []
    for cid, rows in active_by_customer_id.items():
        if len(rows) > 1:
            duplicate_customer_subs.append({
                "kind": "same_customer_id",
                "match_type": "same_customer_id",
                "customer_id": cid,
                "customer_email": rows[0].get("customer_email"),
                "active_subscription_count": len(rows),
                "subscription_ids": [r["subscription_id"] for r in rows],
            })

    for email, rows in active_by_email.items():
        distinct_cids = sorted({r["customer_id"] for r in rows})
        if len(rows) > 1 and len(distinct_cids) > 1:
            duplicate_customer_subs.append({
                "kind": "same_email_multiple_customers",
                "match_type": "same_email_multiple_customers",
                "customer_email": mask_email(email, redact=redact_pii),
                "customer_ids": distinct_cids,
                "active_subscription_count": len(rows),
                "subscription_ids": [r["subscription_id"] for r in rows],
            })

    return {
        "ok": True,
        "mode": client.mode,
        "summary": {
            "total_subscriptions_sampled": len(subs),
            "counts_by_status": counts_by_status,
            "mrr_cents_by_currency": mrr_cents_by_currency,
            "mrr_formatted": {cur: format_usd(val, cur) for cur, val in mrr_cents_by_currency.items()},
            "dunning_risk_count": len(dunning_queue),
            "canceling_at_period_end_count": len(canceling_at_period_end),
            "duplicate_billing_alerts_count": len(duplicate_customer_subs),
        },
        "dunning_queue": dunning_queue,
        "canceling_at_period_end": canceling_at_period_end,
        "duplicate_billing_alerts": duplicate_customer_subs,
    }
