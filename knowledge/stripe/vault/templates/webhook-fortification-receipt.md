# Webhook Fortification Receipt Template

- **Date**: YYYY-MM-DD
- **Target Endpoint URL**: `https://app.example.com/api/stripe/webhook`
- **Mode**: `test` | `live`

## Verification Gates

1. **Unsigned Traffic Gate (`webhook-probe`)**:
   - `legends-stripe webhook-probe --url https://app.example.com/api/stripe/webhook`
   - Expected: `http_status: 400`, `rejects_unsigned_traffic: true`
2. **Offline HMAC-SHA256 Signature Verification (`sign-webhook` + `verify-webhook`)**:
   - Verified constant-time `t=<ts>,v1=<hmac>` acceptance and replay-window rejection (`tolerance: 300s`).
3. **Soft-Land Subscription Lifecycle Audit (`webhook-audit`)**:
   - Confirmed registration of all 5 required events:
     - `checkout.session.completed`
     - `invoice.paid`
     - `invoice.payment_failed`
     - `customer.subscription.updated`
     - `customer.subscription.deleted`
