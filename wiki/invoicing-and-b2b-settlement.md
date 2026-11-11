# B2B Hosted Invoicing and Scheduled Payment Governance

This runbook covers Stripe B2B Invoicing (`collection_method="send_invoice"`), Hosted Invoice Page generation (`hosted_invoice_url` and `invoice_pdf`), orphan-free 4-step invoice creation (`invoice-create`), receivables aging (`invoice-radar`), invoice voiding (`invoice-void`), and governance for Stripe's **Customer-Scheduled Invoice Payments** feature.

## 1. Orphan-Free 4-Step B2B Invoice Lifecycle (`invoice-create`)

A classic Stripe Invoicing trap occurs when an integration creates an Invoice Item (`POST /v1/invoiceitems`) without passing an explicit `invoice` parameter first. Stripe leaves that item floating as a pending customer balance item, which can be swept into an unrelated subscription renewal or fail to attach if draft creation errors out.

`legends-stripe invoice-create` enforces a strict, deterministic 4-step sequence locked by a preview-first `plan_sha256` digest and per-step idempotency keys:

1. **Resolve or Create Customer (`POST /v1/customers`)**: Uses an existing `cus_...` ID or creates a dedicated B2B Customer record formatted with the legal entity and attention line (`Company Name - Attn: Contact Name`).
2. **Create Draft Invoice (`POST /v1/invoices`)**: Sets `collection_method="send_invoice"`, `days_until_due`, `auto_advance=false`, `description`, `footer`, and `custom_fields` before any line item is created.
3. **Attach Bound Invoice Item (`POST /v1/invoiceitems`)**: Explicitly binds `invoice=draft.id` alongside `customer`, `amount`, `currency`, and `description`. Because `invoice` is bound at creation time, failed drafts never leave orphaned pending items on the customer account.
4. **Finalize and Readback (`POST /v1/invoices/{id}/finalize` + `GET /v1/invoices/{id}`)**: Finalizes the draft invoice and reads back `status`, `number`, `hosted_invoice_url`, and `invoice_pdf`.

## 2. Stripe Invoice Field Limits

`legends-stripe` validates all B2B invoice inputs offline before issuing any network call:

- **`custom_fields`**: Maximum **4** entries per invoice. Each `name` and `value` must be **1 to 30 printable characters** (`custom_fields[i][name]`, `custom_fields[i][value]`). Exceeding 30 characters triggers an immediate Stripe HTTP 400 error if not caught before draft creation.
- **Line-item and Invoice `description`**: Maximum **500 characters**.
- **`footer`**: Maximum **500 characters**.
- **`days_until_due`**: Integer from **1 to 365** when `collection_method="send_invoice"`.

## 3. Customer-Scheduled Invoice Payments Governance

Stripe provides a Dashboard toggle (**Settings > Billing > Invoices > Let customers schedule invoice payments**) that allows buyers on a Stripe Hosted Invoice Page (`invoice.stripe.com`) to pick a future date on or before the invoice `due_date` to have their card or bank account automatically charged.

### API and Webhook Mechanics

- **Dashboard-Only Toggle**: The setting is controlled globally in the Stripe Dashboard under **Settings > Billing > Invoices**; it is not an API boolean on `POST /v1/invoices`.
- **No Scheduling Webhook**: When a customer schedules an invoice payment for a future date, Stripe emits **no dedicated webhook event** for the scheduling action.
- **Invoice Status Remains `open`**: While a payment is scheduled for a future date, `GET /v1/invoices/{id}` continues to report `status: "open"` and `amount_remaining > 0`.
- **No Authorization Hold**: Scheduling a payment on a Hosted Invoice Page does **not** place a card authorization hold or lock funds. The buyer can return to the Hosted Invoice Page at any time before the scheduled date to change the payment method, move the date, or cancel the schedule.
- **Settlement Event**: The invoice only transitions to `status: "paid"` when the scheduled date arrives, the charge succeeds, and Stripe emits `invoice.paid` (alongside `invoice.payment_succeeded`).

### When to Keep Scheduled Invoice Payments OFF vs. ON

1. **Post-Completion B2B Settlements (Recommended: OFF or Short `days_until_due: 1..3`)**:
   - When work is already 100% delivered prior to invoice issuance and the client opens your portal to enter their company tax/VAT details, download the accounting PDF, and settle immediately, turning Scheduled Invoice Payments ON with a 14-day or 30-day due window introduces a self-inflicted cash-flow delay.
   - Ready-to-pay buyers are presented with a secondary UI option on `invoice.stripe.com` encouraging them to defer payment until the `due_date`, while your portal still shows the invoice as `OPEN` (unpaid) with zero funds locked.
2. **Traditional Net-30 / Net-60 Corporate Accounts Payable (Recommended: ON)**:
   - When billing traditional enterprise Accounts Payable departments on Net-30 or Net-60 terms prior to or after delivery, where AP clerks process invoices in weekly batches and refuse to release cash early, Scheduled Invoice Payments lets the clerk enter payment details on Day 1 and schedule settlement for the due date instead of forgetting the invoice in an inbox.

## 4. Scanning Receivables with `invoice-radar`

Run `legends-stripe invoice-radar` to inspect `/v1/invoices` across your Stripe account:

- Separates **standalone B2B invoices** (`subscription == null`) from recurring **subscription invoices**.
- Computes `open_receivables_cents_by_currency` and `paid_volume_cents_by_currency`.
- Lists all open `send_invoice` receivables with their `hosted_invoice_url`, `invoice_pdf`, `due_date`, and overdue aging (`is_overdue`, `days_overdue`).
- Flags unfinalized `draft` invoices and any `custom_fields` violating the 4-field / 30-character rule.
