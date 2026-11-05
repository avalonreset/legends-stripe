# Troubleshooting and Failure Modes

## Common Stripe API and Integration Errors

| Symptom or Error Code | Root Cause | Remediation |
|---|---|---|
| `resource_missing` (`No such price: 'price_1...'`) | `.env` references a Test Mode `price_...` while `STRIPE_SECRET_KEY` is Live Mode (or vice versa) | Run `legends-stripe env-audit --target-env .env --live` and `legends-stripe price-map` |
| `invalid_request_error` on `description` | Product description exceeds 500 characters | Shorten `description` to <= 500 characters; `product-upsert` and `ladder-plan` validate this offline |
| `invalid_request_error` on `statement_descriptor` | Statement descriptor is `< 5` or `> 22` chars or contains `< > \ ' " *` | Use 5 to 22 ASCII characters with at least one letter |
| Live Checkout fails with `sk_test_` file error | Account branding logo/icon was uploaded in Test Mode | Upload a fresh logo/icon in Live Mode Dashboard -> Settings -> Branding |
| Webhook returns HTTP 400 `signature_mismatch` | Web framework parsed JSON body before signature verification, or wrong `whsec_...` secret is configured | Read raw request text (`await request.text()`) before HMAC verification; test with `sign-webhook` and `verify-webhook` |
| `orphaned_active_price_on_archived_product` | Product was archived (`active=false`) while its child Prices remained `active=true` | Run `legends-stripe product-upsert --product-id <prod_...> --name "<Name>" --inactive` to cascade deactivation to child Prices |
| `custom-domain-check` returns `NXDOMAIN` or `cname_verified: false` | DNS CNAME is missing or Cloudflare orange-cloud proxy is enabled | Create a DNS-only (`proxied: false`) CNAME to `hosted-checkout.stripecdn.com` with TTL 300 |

## Related Runbooks

- [Index](index.md)
- [Authentication, Keys, and Modes](authentication-and-modes.md)
- [Catalog Governance and Price Ladders](catalog-and-price-ladders.md)
- [Primary Sources](sources.md)
