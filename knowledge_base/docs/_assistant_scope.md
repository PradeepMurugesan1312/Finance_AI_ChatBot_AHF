# What the AHF Finance Assistant Can Help With

> Reference card for humans. The leading `_` means `kb_ingest` does **not**
> index this file — capability ("what can you do?") answers come from the system
> prompt, not retrieval, so this card never competes with real policy docs.

## In scope

This assistant answers routine, read-only finance questions for AHF accounts
payable, procurement, and finance staff, across these areas:

- **Accounts Payable** — supplier invoice status and line items, payment
  blocks, payment terms.
- **Procurement** — purchase order status and release/approval state, goods
  receipts against a PO, purchase requisition status, approval thresholds.
- **Vendor / Business Partner** — onboarding and setup status, onboarding steps.
- **Payments & Clearing** — whether a payment has actually cleared, payment run
  schedule.
- **General Ledger / Journal Entries** — new G/L account requests, period close,
  parking vs posting, manual JE approval.
- **G/L Accounts & Balances** — how to read balances and the trial balance
  (policy and process).
- **Accounts Receivable** — customer terms, credit limits, dunning and
  collections, cash application, bad-debt write-off.
- **Cost Centers (CO-CCA) and Profit Centers (CO-PCA)** — master data requests,
  allocations, repostings.
- **Fixed Assets (FI-AA)** — capitalization threshold, useful life and
  depreciation, AUC settlement, retirement and transfer.
- **Bank & Cash (TR-CM)** — bank statement import and reconciliation, house
  banks, cash position, payment runs.
- **Tax** — tax code selection, input/output tax, filing calendar, withholding,
  exemption certificates.

## What has live S/4HANA status lookups today

Right now the assistant can look up a **specific record's live status** for:
supplier invoices (header and line items), payments/clearing, purchase orders
(including release/approval state), goods receipts posted against a purchase
order, purchase requisitions, and vendors. Still knowledge-base-only within
procure-to-pay: the dedicated approval-workflow history, the formal
PO–GR–invoice three-way-match result, payment documents, and bank statements.
For every other area above it answers the **policy and process** question from
approved documents and points you to the relevant team or S/4HANA report for
the live figure — until that area's lookup is connected.

## Out of scope

- Any change in SAP: approvals, postings, releases, payments, blocks/unblocks.
  The assistant is strictly read-only.
- Personal data or vendor/customer banking details.
- Multi-step financial analysis, forecasting, or advice.
- Non-finance questions.

When the assistant is not confident, or the answer isn ot in the approved
documents, it hands off to the AHF finance support team.
