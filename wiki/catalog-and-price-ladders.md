# Catalog Governance and Price Ladders

Stripe separates **Products** (`/v1/products`, representing what a customer buys) from **Prices** (`/v1/prices`, defining how much and how often they pay).

## Hard Field Constraints

Before mutating a catalog, `legends-stripe` enforces Stripe's schema limits locally:

| Field | Limit | Enforcement Rule |
|---|---|---|
| `product.name` | 1 to 250 chars | Non-empty string |
| `product.description` | Max 500 chars | Stripe rejects or truncates overly long checkout descriptions; keep descriptions concise and customer-facing |
| `product.statement_descriptor` | 5 to 22 ASCII chars | Must contain at least one letter; cannot contain `<`, `>`, `\`, `'`, `"`, or `*` |
| `metadata` | Max 50 keys | Keys max 40 chars; values max 500 chars |
| `price.currency` | 3-letter ISO code | Lowercase ISO 4217 code such as `usd` or `eur` |
| `price.unit_amount` | Non-negative integer | Minor currency units (cents for USD: `$195.00` = `19500`) |

## Why `lookup_key` Matters

Hardcoding `price_1...` IDs across codebases breaks whenever you switch between Test Mode and Live Mode or adjust pricing. Setting a deterministic `lookup_key` (for example `pro_monthly_usd`) on each Price and passing `transfer_lookup_key=true` during Price rotation lets your checkout endpoint resolve the active Price dynamically by `lookup_key`.

To generate ready-to-paste `.env` or TypeScript constants for an existing catalog:

```bash
legends-stripe price-map --format dotenv
legends-stripe price-map --format ts
```

## Exponential and Tiered Price Ladders (`ladder-plan` and `catalog-sync`)

When provisioning a multi-tier subscription or credit ladder:

```bash
legends-stripe ladder-plan --prefix "SaaS Tier" --amounts "16,32,64,128" --intervals "month,year" --statement-descriptor "SAAS TIER" --output ladder.json
legends-stripe catalog-diff --spec ladder.json
legends-stripe catalog-sync --spec ladder.json
```

`catalog-sync` computes a deterministic `plan_sha256` (`plan_id`) and sends `Idempotency-Key` headers for every Product and Price creation so network retries never create duplicate catalog items.

## Cascading Price Deactivation When Archiving Products

In Stripe's API, setting `product.active=false` does **not** automatically deactivate child Prices attached to that Product, leaving orphaned active Prices (`orphaned_active_price_on_archived_product` in `catalog-audit`).

When you run:

```bash
legends-stripe product-upsert --product-id prod_Example123 --name "Retired Plan" --inactive
```

`legends-stripe` automatically discovers all active child Prices on `prod_Example123`, lists them in `planned.prices_to_deactivate`, and deactivates every child Price before archiving the Product when `--apply --confirm-plan <sha256>` is executed.

## Related Runbooks

- [Index](index.md)
- [Hosted Checkout, Customer Portal, and Custom Domains](checkout-and-custom-domains.md)
- [Primary Sources](../references/sources.md)
