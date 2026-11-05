# Subscription Radar and Duplicate Billing Prevention

Recurring billing health requires monitoring three signals simultaneously: active Monthly Recurring Revenue (MRR), involuntary churn (`past_due` / `unpaid` dunning), and accidental duplicate subscriptions.

## Running Subscription Radar (`subscription-radar`)

```bash
legends-stripe subscription-radar --redact-pii
```

### 1. Normalized MRR Calculation
`subscription-radar` normalizes active and trialing recurring items into monthly minor units per currency:
- `month`: `unit_amount * quantity / interval_count`
- `year`: `(unit_amount * quantity) / (12 * interval_count)`
- `week`: `(unit_amount * quantity * 52) / (12 * interval_count)`
- `day`: `(unit_amount * quantity * 365) / (12 * interval_count)`

### 2. Dunning Queue (`past_due` and `unpaid`)
Lists every subscription currently in `past_due` or `unpaid` status along with `latest_invoice` and `current_period_end` so operators can trigger payment method recovery before final cancellation.

### 3. Pending Churn (`cancel_at_period_end`)
Lists active subscriptions where `cancel_at_period_end` is `true` so retention workflows know exactly when access will soft-land.

### 4. Double-Billed Customer Detector
If a checkout button is clicked twice without a customer-level idempotency check, or if your checkout endpoint creates a new Stripe Customer (`cus_...`) on every checkout session instead of reusing the existing Customer record for that email address, a customer can end up with two concurrent active subscriptions.

`subscription-radar` detects both failure modes:
- `same_customer_id`: A single `cus_...` has multiple active/trialing/past_due subscriptions.
- `same_email_multiple_customers`: Multiple distinct `cus_...` records share the same normalized email address and each holds an active subscription.

## Related Runbooks

- [Index](index.md)
- [Webhooks and Soft-Land Subscription Lifecycle](webhooks-and-soft-land-lifecycle.md)
- [Treasury Telemetry and Stripe Drag](treasury-and-stripe-drag.md)
