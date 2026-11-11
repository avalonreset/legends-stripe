# Webhooks and Soft-Land Subscription Lifecycle

Webhook endpoints are the authoritative fulfillment bridge between Stripe and your application database.

## The 5-Event Soft-Land Subscription Lifecycle Contract

Every production subscription or recurring billing webhook endpoint should subscribe to at least these 5 core events (`SOFT_LAND_WEBHOOK_EVENTS`):

1. `checkout.session.completed`: Provision initial access and link `customer` (`cus_...`) + `subscription` (`sub_...`) to your internal user record.
2. `invoice.paid`: Confirm recurring renewal payment and extend the entitlement period through `period_end`.
3. `invoice.payment_failed`: Enter dunning state (`past_due`) and notify the customer to update their payment method without immediately deleting their workspace data.
4. `customer.subscription.updated`: Synchronize plan upgrades/downgrades and `cancel_at_period_end` state transitions.
5. `customer.subscription.deleted`: Revoke active paid entitlement cleanly ("soft-land" the account to a free/read-only tier) once the final billing period ends or dunning is exhausted.

Audit all registered endpoints in your Stripe account against this contract:

```bash
legends-stripe webhook-audit --expected-url https://app.example.com/api/stripe/webhook
```

## Raw-Body HMAC-SHA256 Signature Verification

Stripe signs webhook payloads using the endpoint's `whsec_...` secret:
- Header format: `Stripe-Signature: t=<unix_timestamp>,v1=<hex_hmac_sha256>`
- Signed message: `<unix_timestamp>.<raw_utf8_request_body>`

**Critical Rule**: Never parse and re-serialize the JSON body before verifying the signature. Whitespace or key-order changes invalidate the HMAC digest. Always read `await request.text()` (or raw bytes) first, verify the HMAC-SHA256 using constant-time comparison (`hmac.compare_digest` or `crypto.timingSafeEqual`), and enforce a timestamp replay window (default `300` seconds).

Test your signature verification offline without needing external network tunnels:

```bash
legends-stripe sign-webhook --payload '{"id":"evt_test_1","type":"checkout.session.completed"}' --secret @webhook-secret.txt --timestamp 1762350000
legends-stripe verify-webhook --payload '{"id":"evt_test_1","type":"checkout.session.completed"}' --signature "t=1762350000,v1=..." --secret @webhook-secret.txt --now 1762350050
```

## Probing the Unsigned Webhook Gate (`webhook-probe`)

Verify that a deployed production webhook endpoint rejects unsigned POST requests with HTTP `400`, `401`, or `403`:

```bash
legends-stripe webhook-probe --url https://app.example.com/api/stripe/webhook
```

## Related Runbooks

- [Index](index.md)
- [Subscription Radar and Duplicate Billing Prevention](subscriptions-and-dunning.md)
- [Webhook Fortification Receipt Template](templates/webhook-fortification-receipt.md)
