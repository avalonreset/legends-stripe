# Authentication, Keys, and Modes

Stripe accounts operate in two isolated environments: **Test Mode** and **Live Mode**. Mixing credentials or resource IDs across modes is one of the most frequent causes of broken production checkouts.

## Key Classification

| Prefix | Kind | Mode | Valid for Server API (`/v1/*`) | Purpose |
|---|---|---|---|---|
| `sk_test_` | `secret` | `test` | Yes | Full server-side access in Test Mode |
| `rk_test_` | `restricted` | `test` | Yes (scoped) | Least-privilege server access in Test Mode |
| `sk_live_` | `secret` | `live` | Yes | Full server-side access in Live Mode |
| `rk_live_` | `restricted` | `live` | Yes (scoped) | Least-privilege server access in Live Mode |
| `pk_test_` | `publishable` | `test` | No | Client-side Stripe.js / Elements in Test Mode |
| `pk_live_` | `publishable` | `live` | No | Client-side Stripe.js / Elements in Live Mode |
| `whsec_` | `webhook_secret` | `agnostic` | No | HMAC-SHA256 signing secret for `Stripe-Signature` verification |

## Auditing Project `.env` Files (`env-audit`)

Applications frequently store `STRIPE_SECRET_KEY`, `STRIPE_PUBLISHABLE_KEY`, `STRIPE_WEBHOOK_SECRET`, and multiple `STRIPE_PRICE_*` variables in `.env`. Use `env-audit` to validate an environment file without exposing secret values:

```bash
legends-stripe env-audit --target-env .env --live
```

`env-audit` checks:
1. **Key Role Integrity**: Flags if a `pk_...` or `whsec_...` token is accidentally assigned to `STRIPE_SECRET_KEY`.
2. **Mode Parity**: Flags `key_mode_mismatch` if a `test` secret key is paired with a `live` publishable key (or vice versa).
3. **Empty Stripe Variables**: Flags any `*STRIPE*` variable with an empty value.
4. **Live Price and Product Verification (`--live`)**: Queries each `price_...` and `prod_...` ID against the configured Stripe account and confirms that the resource exists in that mode, `price.active` is `true`, and `product.active` is `true`.

## Related Runbooks

- [Index](index.md)
- [Catalog Governance and Price Ladders](catalog-and-price-ladders.md)
- [Troubleshooting and Failure Modes](troubleshooting.md)
