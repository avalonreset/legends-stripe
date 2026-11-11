# Stripe Operational Knowledge Vault

This vault captures source-cited Stripe engineering patterns, API limits, treasury reconciliation math, catalog governance, and webhook security gates for `legends-stripe`.

## Core Runbooks

1. [Overview and Architecture](overview.md): Why `legends-stripe` uses direct `urllib.request` form-encoded calls, preview-first SHA-256 plan locking, and strict `sk_test_` vs `sk_live_` separation.
2. [Authentication, Keys, and Modes](authentication-and-modes.md): Classifying `sk_test_`, `sk_live_`, `rk_test_`, `rk_live_`, `pk_`, and `whsec_` tokens, `.env` key-mode parity auditing (`env-audit`), and preventing test/live contamination.
3. [Catalog Governance and Price Ladders](catalog-and-price-ladders.md): Stripe's 500-character Product description limit, 22-character ASCII statement descriptors, immutable Prices, `lookup_key` portability, exponential price ladders (`ladder-plan`), and cascading child-price deactivation on product archive (`product-upsert --inactive`).
4. [Hosted Checkout, Customer Portal, and Custom Domains](checkout-and-custom-domains.md): Disposable zero-charge Checkout Session canaries (`checkout-canary`), branding image Test Mode traps, Customer Portal configuration, and Cloudflare DNS-only CNAME + `_acme-challenge` TXT verification (`custom-domain-check`).
5. [Webhooks and Soft-Land Subscription Lifecycle](webhooks-and-soft-land-lifecycle.md): Raw-body `Stripe-Signature` HMAC-SHA256 verification (`sign-webhook`, `verify-webhook`), replay tolerance (`300s`), unsigned POST gate probing (`webhook-probe`), and the 5-event Soft-Land Subscription Lifecycle contract (`webhook-audit`).
6. [Treasury Telemetry and Stripe Drag](treasury-and-stripe-drag.md): Cent-level reconciliation of `/v1/balance` (`available + pending`) against `/v1/balance_transactions` and `/v1/payouts`, separating card processing fees from Stripe Billing usage drag, and filtering `$1.00` 3DS card setup authorizations.
7. [Subscription Radar and Duplicate Billing Prevention](subscriptions-and-dunning.md): Scanning active, trialing, `past_due`, and `unpaid` subscriptions (`subscription-radar`), computing normalized MRR, and catching double-billed customers across identical `customer_id` or duplicate Customer records sharing one email.
8. [B2B Hosted Invoicing and Scheduled Payment Governance](invoicing-and-b2b-settlement.md): Orphan-free 4-step B2B invoice creation (`invoice-create`), custom-field 30-character validation, receivables aging (`invoice-radar`), invoice voiding (`invoice-void`), and governance for Customer-Scheduled Invoice Payments.
9. [Troubleshooting and Failure Modes](troubleshooting.md): Diagnosing common Stripe HTTP 400/401/402/404/429 responses, form-encoding bracket mistakes, webhook signature mismatches, and `.env` price drift.

## Sources and Templates

- [Primary Sources](../references/sources.md)
- [Private Operation Receipt Template](../templates/private-operation-receipt.md)
- [Treasury Telemetry Report Template](../templates/treasury-telemetry.md)
- [Webhook Fortification Receipt Template](../templates/webhook-fortification-receipt.md)
