# Hosted Checkout, Customer Portal, and Custom Domains

## Zero-Charge Checkout Canary (`checkout-canary`)

Never make a real credit card charge in production merely to test whether Stripe Hosted Checkout is reachable. Instead, run a disposable zero-charge Checkout Session canary:

```bash
legends-stripe checkout-canary --amount-cents 100 --currency usd
```

`checkout-canary` performs three steps:
1. Creates a Checkout Session (`POST /v1/checkout/sessions`) with `status: "open"`.
2. Reads back the session (`GET /v1/checkout/sessions/<id>`) to confirm that a hosted `url` was provisioned and `livemode` matches the key mode.
3. Immediately expires the session (`POST /v1/checkout/sessions/<id>/expire`) and verifies `expired_status == "expired"` (`cleaned_up: true`).

## The Test Mode Branding Image Trap

If a Stripe account uploads a business icon or logo while viewing **Test Mode** in the Stripe Dashboard, Stripe stores that image as a `file_...` object scoped to Test Mode (`sk_test_`). If a Live Mode (`sk_live_`) Checkout Session attempts to render that Test Mode branding asset, Stripe returns an error stating that the `sk_test_` file cannot be accessed in Live Mode.

**Rule**: Upload business logos and icons separately in Live Mode Dashboard -> Settings -> Branding, and never pass Test Mode `file_...` IDs in Live Mode checkout payloads.

## Custom Payment Domains (`custom-domain-check`)

When configuring a branded Stripe Checkout domain (such as `pay.example.com`):
1. Both the `CNAME` record (`pay.example.com` -> `hosted-checkout.stripecdn.com`) and the ACME verification `TXT` record (`_acme-challenge.pay.example.com`) **must be DNS-only (`proxied: false`)** with a short TTL (`300` seconds).
2. If Cloudflare orange-cloud proxying is enabled on `pay.example.com`, Stripe cannot validate the CNAME target or renew the Let's Encrypt TLS certificate.

Verify public DNS resolution and HTTPS status at any time:

```bash
legends-stripe custom-domain-check --hostname pay.example.com
```

If the record is missing (`dns_status: "NXDOMAIN"`), `custom-domain-check` outputs the exact preview-first `legends-cloudflare upsert` commands needed to provision the records.

## Related Runbooks

- [Index](index.md)
- [Webhooks and Soft-Land Subscription Lifecycle](webhooks-and-soft-land-lifecycle.md)
- [Primary Sources](../references/sources.md)
