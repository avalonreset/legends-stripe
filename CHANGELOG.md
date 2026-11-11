# Changelog

## 0.1.3 (2026-11-11)

- Added first-class B2B Hosted Invoicing lifecycle commands (`invoice-radar`, `invoice-create`, `invoice-void`) and Python exports (`scan_invoice_radar`, `create_b2b_invoice`, `void_b2b_invoice`, `validate_invoice_custom_fields`, `validate_b2b_invoice_fields`).
- Enforced orphan-free 4-step B2B invoice creation (`Customer -> Draft send_invoice -> Bound InvoiceItem with invoice=draft.id -> Finalize + Readback`) locked by preview-first SHA-256 `plan_sha256` confirmation and per-step idempotency keys.
- Added offline validation for Stripe Invoicing `custom_fields` (maximum 4 fields, 30 printable characters per `name` and `value`), 500-character invoice descriptions/footers, and `days_until_due` (`1..365`).
- Added the `astro-b2b-invoice` server blueprint (`src/pages/api/stripe/b2b-invoice.ts`) and the `invoicing-and-b2b-settlement.md` knowledge vault runbook covering Hosted Invoice Page mechanics and Customer-Scheduled Invoice Payments governance.

## 0.1.2 (2026-11-10)

- Made `checkout-canary` (`run_checkout_canary`) preview-by-default across Test and Live modes, requiring `--apply --confirm-plan <plan_sha256>` (plus `--live` on live keys) before creating or expiring a disposable Checkout Session.
- Hardened `sign-webhook` and `verify-webhook` (`_resolve_webhook_secret_arg`) to refuse raw `whsec_...` secret literals on CLI `argv` (CWE-214), accepting secrets via `STRIPE_WEBHOOK_SECRET`, `--secret @<file>`, `--secret-file <file>`, or `--secret-stdin`.
- Hardened `validate_public_https_host`, `upsert_webhook_endpoint`, `probe_unsigned_webhook_gate`, and `check_custom_domain` with fail-closed DNS resolution, CGNAT (`100.64.0.0/10`) and non-global IP refusal, `ProxyHandler({})`, and pinned-IP TLS connections (CWE-918).
- Updated `python-webhook` blueprint to evaluate all `v1=` signature headers during webhook secret rotation and handle malformed `t=` values cleanly, and added `sk_`/`rk_` key prefix validation to `astro-portal`.
- Synced `README.md` subcommands (`catalog-export`, `product-upsert`) and Python API exports (`audit_env_file`, `build_price_map`, `build_treasury_telemetry`) with `legends_stripe/__init__.py`, restricted `_MACOS_SYSTEM_SYMLINKS` exemptions to `sys.platform == "darwin"`, and added `.legends-module`, `.legends-router-pin`, `setup.py`, control-char checks, and `whsec_` scanning to `MANIFEST.in` and `scripts/check_release.py`.

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
