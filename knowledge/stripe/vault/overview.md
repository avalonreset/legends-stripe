# Overview and Architecture

`legends-stripe` is a dependency-free Python 3.11+ CLI and knowledge pack for operating Stripe payment infrastructure, auditing product catalogs, reconciling treasury balances, and fortifying webhook receivers.

## Design Principles

1. **Zero Third-Party Runtime Dependencies**:
   - Stripe's REST API (`/v1/*`) accepts `application/x-www-form-urlencoded` requests with bracket-indexed nested keys (`line_items[0][price]=price_123`, `metadata[slug]=tier_pro`, `expand[0]=data.customer`) and returns standard JSON.
   - `legends-stripe` implements a deterministic recursive form-encoder (`flatten_stripe_params` and `encode_stripe_form`) over Python's standard library (`urllib.request`, `hmac`, `hashlib`, `json`).
2. **Test Mode vs Live Mode Safety**:
   - Every secret or restricted key is classified by prefix (`sk_test_`, `rk_test_`, `sk_live_`, `rk_live_`) before any network request is made.
   - Secret values are never echoed to stdout, stderr, or receipt files.
   - Mutating commands (`catalog-sync`, `product-upsert`, `webhook-upsert`) default to a read-only preview that outputs a deterministic SHA-256 `plan_sha256` (`plan_id`).
   - Executing a mutation requires `--apply --confirm-plan <sha256>`, and mutating a `live` account requires an explicit additional `--live` flag.
3. **Readback Verification**:
   - Creation and update operations perform an immediate `GET` readback from Stripe (`GET /v1/products/<id>`, `GET /v1/prices/<id>`, `GET /v1/checkout/sessions/<id>`) rather than trusting local assumptions.

## Related Runbooks

- [Catalog Governance and Price Ladders](catalog-and-price-ladders.md)
- [Webhooks and Soft-Land Subscription Lifecycle](webhooks-and-soft-land-lifecycle.md)
- [Treasury Telemetry and Stripe Drag](treasury-and-stripe-drag.md)
- [Primary Sources](sources.md)
