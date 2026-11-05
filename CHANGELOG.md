# Changelog

## 0.1.1 (2026-11-05)

- Aligned `README.md` command tables and examples with CLI subcommand names (`subscription-radar`, `checkout-canary`, `webhook-upsert`, `verify-webhook --payload @event.json --signature`, and `--apply --confirm-plan <plan_hash> --live`).
- Fixed `MANIFEST.in` source distribution inclusion rule (`include NOTICE.md`).
- Bumped package and attachable knowledge pack version to `0.1.1`.

## 0.1.0 (2026-11-05)

- Initial release of `legends-stripe` CLI, attachable `legends.knowledge-pack/v1` Stripe reference vault (`knowledge/stripe/pack.json`), and drop-in server blueprints (`astro-checkout`, `astro-webhook`, `astro-portal`, `python-webhook`).
- Added pure Python 3.11+ standard-library Stripe API v1 client with recursive bracket form-encoder, `sk_test_`/`sk_live_`/`rk_`/`pk_`/`whsec_` key classifier, `Idempotency-Key` support, and preview-first SHA-256 plan locking.
- Added `telemetry` for cent-level balance reconciliation, Stripe Drag fee breakdown, Revenue Stream drag analysis, and automatic zero-fee 3DS card verification filtering.
- Added `env-audit` and `price-map` for `.env` test/live key parity checking, empty variable detection, live `price_...`/`prod_...` verification, and `.env` / TypeScript constant generation.
- Added `catalog-audit`, `catalog-export`, `ladder-plan`, `catalog-diff`, `catalog-sync`, `product-upsert` (with cascading child-price deactivation on `--inactive`), and `checkout-canary`.
- Added `subscription-radar` for normalized MRR, `past_due` dunning queues, and double-billed customer detection across `customer_id` and customer email.
- Added `webhook-audit`, `webhook-upsert`, `webhook-probe`, `sign-webhook`, `verify-webhook`, and `custom-domain-check`.
