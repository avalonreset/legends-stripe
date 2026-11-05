# Operating legends-stripe

This is an on-demand module recipe, not a registered skill. The central `cto-legends` router owns discovery.

## Select knowledge first

Read [the Stripe knowledge vault](../wiki/index.md). Choose the runbook matching the requested outcome. Follow its [primary sources](../references/sources.md) when Stripe API versions, field limits, or webhook event semantics matter. The vault is reusable knowledge; the user's private workspace holds account IDs, `.env` files, receipts, and business decisions.

## Readiness

1. Run `legends-stripe doctor` (or `cto-legends run legends-stripe -- doctor`). This is offline, free, and verifies Python 3.11+, local knowledge pack integrity, and Stripe key prefix classification (`sk_test_`, `sk_live_`, `rk_test_`, `rk_live_`).
2. For authorized live reads or Test Mode work, ensure `STRIPE_SECRET_KEY` is supplied in the environment or via `--env-file <path>`. Never echo secret keys (`sk_`, `rk_`, `whsec_`) to terminal output or logs.
3. Run `legends-stripe doctor --live` to verify connectivity against `GET /v1/account` and confirm whether the key operates in `test` or `live` mode.
4. Mutating commands (`catalog-sync`, `product-upsert`, `webhook-upsert`) are preview-first and return a deterministic `plan_sha256` (`plan_id`). Applying requires `--apply --confirm-plan <sha256>`, and mutating a `live` account requires an explicit additional `--live` flag.

| Goal | Command | Interpretation |
|---|---|---|
| Offline readiness | `doctor` | Checks Python 3.11+, key mode classification, and knowledge pack hashes |
| Live account check | `doctor --live` | Verifies `/v1/account` charges/payouts readiness and mode (`test` vs `live`) |
| Knowledge & blueprints | `resources` | Lists verified knowledge pack files and integration blueprints |
| Treasury reconciliation | `telemetry [--markdown] [--redact-pii]` | Reconciles balance, Gross Revenue, Stripe Drag fees, Revenue Streams, and Net Cash |
| Audit `.env` Stripe vars | `env-audit --target-env .env [--live]` | Checks test/live key parity, empty vars, and live `price_...`/`prod_...` status |
| Map active Prices | `price-map [--format json\|dotenv\|ts]` | Exports active Products and Prices to JSON, `.env` lines, or TypeScript constants |
| Audit catalog health | `catalog-audit` | Flags >500-char descriptions, bad statement descriptors, orphans, and missing `lookup_key` |
| Export catalog spec | `catalog-export [--prefix NAME]` | Exports active Products and Prices to `stripe-catalog-spec/v1` JSON |
| Plan price ladder | `ladder-plan --prefix NAME --amounts ...` | Generates a validated `stripe-catalog-spec/v1` price ladder offline |
| Diff catalog spec | `catalog-diff --spec FILE` | Compares a `stripe-catalog-spec/v1` file against the Stripe account |
| Sync catalog spec | `catalog-sync --spec FILE [--apply --confirm-plan HASH]` | Preview-first idempotent Product and Price creation with readback |
| Upsert / archive Product | `product-upsert --name NAME [--inactive]` | Preview-first Product create/update; `--inactive` cascades deactivation to child Prices |
| Zero-charge Checkout test | `checkout-canary` | Creates a disposable Checkout Session, verifies URL, and immediately expires it |
| Subscription & MRR radar | `subscription-radar [--redact-pii]` | Calculates MRR, lists `past_due` dunning, and detects double-billed customers |
| Audit webhook endpoints | `webhook-audit [--expected-url URL]` | Verifies the 5-event Soft-Land Subscription Lifecycle contract and flags duplicates |
| Upsert webhook endpoint | `webhook-upsert --url URL [--apply --confirm-plan HASH]` | Preview-first webhook endpoint creation or update |
| Probe unsigned gate | `webhook-probe --url URL` | Sends an unsigned `POST {}` to verify HTTP 400/401/403 rejection |
| Sign / verify webhook | `sign-webhook` / `verify-webhook` | Offline HMAC-SHA256 `Stripe-Signature` generator and constant-time verifier |
| Check custom domain DNS | `custom-domain-check --hostname HOST` | Checks public DoH CNAME (`hosted-checkout.stripecdn.com`), ACME TXT, and HTTPS |
| Drop-in code blueprints | `blueprint --name astro-checkout\|astro-webhook\|astro-portal\|python-webhook` | Outputs production-ready server endpoints with raw-body signature verification |

## Optional knowledge pack

The vault also ships as an attachable Empire knowledge pack (`knowledge/stripe/pack.json`, schema `legends.knowledge-pack/v1`, suggested home `wiki/library/legends-stripe/stripe`). Attachment is optional and explicit: preview the transaction into a selected vault, review it, then apply with the reviewed plan hash. See [knowledge packs](KNOWLEDGE-PACK.md) for the exact commands and ownership table.

## Limits and safety

- HTTPS `api.stripe.com` origin only for Stripe API traffic.
- Never print `sk_live_`, `sk_test_`, `rk_`, or `whsec_` values in terminal output, logs, or receipts.
- Never execute mutations against a `live` key without explicit user authorization for the exact reviewed `plan_sha256` and `--live`.
- Archiving a Product with `product-upsert --inactive` automatically cascades `active=false` to its active child Prices so orphaned active Prices are not left behind.
