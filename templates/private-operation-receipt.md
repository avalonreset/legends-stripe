# Receipt: Stripe Operation

- **Date**: YYYY-MM-DD
- **Operator**: <operator>
- **Mode**: `test` | `live`
- **Command Executed**: `legends-stripe <subcommand>`
- **Plan SHA-256 (`plan_id`)**: `<64-hex-sha256>`

## Scope and Authorization

- Describe the approved scope and whether the operation is read-only, Test Mode mutation, or explicitly authorized Live Mode mutation.

## Pre-State and Preview

- Record the preview output and `plan_sha256` before any `--apply` execution.

## Post-State Readback Proof

- Record the Stripe resource IDs (`prod_...`, `price_...`, `we_...`, `cs_test_...`) and readback verification fields (`active`, `livemode`, `reconciled`).
- Never include `sk_live_`, `sk_test_`, `rk_`, or `whsec_` secret values in receipts.
