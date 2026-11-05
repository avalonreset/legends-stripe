# Treasury Telemetry and Stripe Drag

Stripe Dashboard summaries often show gross volume without immediately surfacing the combined impact of card processing fees, international card surcharges, fixed `$0.30` per-charge fees on micro-transactions, and standalone Stripe Billing usage fees (`0.5%` to `0.7%`). `legends-stripe` calls this combined fee deduction **Stripe Drag**.

## Cent-Level Balance Reconciliation (`telemetry`)

Run a read-only treasury reconciliation report:

```bash
legends-stripe telemetry --redact-pii
legends-stripe telemetry --markdown --redact-pii --output treasury-report.md
```

### Reconciliation Formula

Across all sampled `/v1/balance_transactions`:
- **Gross Revenue**: Sum of `amount` on `charge` and `payment` balance transactions.
- **Card Processing Fees**: Sum of `fee` deducted directly on `charge` and `payment` balance transactions.
- **Billing and Platform Usage Fees**: Sum of standalone `stripe_fee` balance transactions (such as Stripe Billing `0.7%` recurring invoice fees or `$10/month` custom domain fees).
- **Total Stripe Drag**: `card_processing_fees + billing_usage_fees + other_fees`.
- **Effective Drag %**: `(total_fees / gross_revenue) * 100`.
- **Net Retained Cash**: Sum of `net` across all non-payout balance transactions.
- **Reconciliation Check**:
  `(available_balance + pending_balance) == (net_retained_cash - executed_payouts)`
  When `reconciliation_variance_cents == 0`, `reconciled` is `true`.

## Filtering Zero-Fee 3DS Card Setup Verifications

Platforms that verify customer cards via `$1.00` 3D Secure (`"Add new card: 3DS verification"`) create a `$1.00` charge that is immediately refunded/voided with `$0.00` fee. `legends-stripe telemetry` automatically filters these zero-fee 3DS setup pings out of `itemized_events` (counting them separately in `card_verifications_3ds_count`) so the itemized ledger reflects true settled customer revenue.

## Revenue Stream Drag Breakdown

`telemetry` groups settled charges into three streams:
1. `direct_checkout`: Standard one-time or checkout charges (typically `~2.9% + $0.30`).
2. `subscription`: Recurring subscription and invoice charges (card processing + Stripe Billing usage fee).
3. `wallet_auto_recharge`: Small wallet top-ups (such as `$10.00` sub-account auto-recharges), where the fixed `$0.30` per-transaction fee and international card fees often push effective drag above `5.9%` to `7.4%`.

## Related Runbooks

- [Index](index.md)
- [Subscription Radar and Duplicate Billing Prevention](subscriptions-and-dunning.md)
- [Treasury Telemetry Report Template](../templates/treasury-telemetry.md)
