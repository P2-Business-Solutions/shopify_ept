# Guided Shopify payout closeout — 18.0.3.36

Work from **Bulk Repair Transactions** or **Bulk Repair Audit → Current Repair
Progress**. Start with an unmatched case's **Next Step**, review/correct that case,
then return to the same workspace to finish its statement matching. The aim is to
remove the remaining blockers on a deposit using the existing accounting tools.

| Blocker | In-system route | Change in this release |
| --- | --- | --- |
| Missing historical-refund customer | **Import Customer**, review Details, apply the ready case. | Existing 18.0.3.35 action; now identified in Next Step. |
| Receipt identity/amount mismatch | **Review Payments** from the case; inspect the exact source history and proposed accounting. | Direct entry to the existing payment preview, with a return to the saved case. |
| One invoice-sized receipt within a fully invoiced, single-capture sale | **Review Payments → Preview Invoice Receipt Correction → Apply Reviewed Repair → Refresh / Return to Payout Case**. Then apply the ready case to match its payout. | New, separately reviewed receipt-correction route described below. |
| Missing/draft invoice, document total/tax/customer mismatch | **Invoices / Credits** opens that order's documents. If no document exists, it opens the order for the normal invoicing review. Correct the document and **Refresh Case**. | Direct document navigation and specific Next Step; existing invoicing rules remain. |
| Refund spanning revenue accounts or processors | Review/prepare an allocated credit with the existing accounting tools, then **Link Credit Note** on the individual refund. | Specific Next Step; allocation and processor review remain required. No automatic split/write-off. |
| Fulfillment/cancellation evidence missing | **Shopify** opens the source order. Verify its fulfillment, cancellation and cash history; use the appropriate current-period or historical route and **Refresh Case**. | Specific Next Step; historical eligibility remains unchanged. |

**Invoices / Credits** includes order invoices/credits, explicitly order-linked
standalone credits and credits already retained on the case. It does not create a
document. **Review Payments** previews one case's current order using the existing
payment wizard. Only accounting managers can review/apply corrections. After
applying, **Refresh / Return to Payout Case** refreshes only that case and returns
to the saved workspace. Its payment repair history links the actual payments and
names the order audit. A later source-read failure during Refresh leaves the
already completed payment repair available for a retry.

## Invoice receipt correction

Example: Shopify captured 240 once. Two posted invoices total 240, but an older
90 receipt pays only the 90 invoice while the 150 invoice remains open. Ordinary
cash repair stays blocked. The additional preview can propose reversing the 90
receipt and recording the exact 240 source capture, retaining both invoices and
closing their receivables. Apply retains the old entry and its reversal in the
existing payment audit. It never changes a posted payment's amount in place.

The additional route requires all of the following evidence:

- Complete successful Shopify cash history has one Shopify Payments sale/capture,
  no successful refunds, and agrees with the posted invoices' total.
- The source customer has exactly one active store mapping to the order's
  commercial customer. Documents and the existing receipt use that same customer,
  company, currency and the expected receivable/outstanding accounts.
- There are at least two posted invoices. Exactly one untagged Manual incoming
  receipt fully reconciles exactly one invoice of the same amount; that receipt
  is smaller than the source capture and was not recorded before it. No other
  receipt represents the source transaction.
- The existing receipt has no bank/payout match, write-off, outside allocation,
  conflicting cash metadata, protected entry or lock. Affected payouts, including
  source-linked rows without an Odoo order link, have no settlement transfer.

The first payment preview only offers **Preview Invoice Receipt Correction** when
these checks pass. That button creates a preview, not accounting. **Apply Reviewed
Repair** re-reads source and ledger under the existing order lock and compares
fingerprints before making any change. Failures roll back the reversal, new receipt,
allocations and audit together. Repeating a completed repair reuses its exact source
receipt; ordinary imports/webhooks cannot authorize this additional replacement.

Multiple captures, multiple existing receipts, arbitrary partial payments, refunds,
mixed processors and uncertain customer/source identities remain for accounting
review. This route is deliberately limited to the proven invoice-sized receipt.

## Finish the deposit

Review the refreshed case's **Details**, then **Apply Selected Ready Cases** with
statement reconciliation enabled. Once every statement line is matched, use the
existing settlement transfer and bank matching workflow. An already matched case's
Next Step says to continue with the next unmatched case; a case with applied
accounting but an open match directs a retry. Successful matches are retained.

Routes travel with the actual validation error; translated error text is never
parsed to decide accounting eligibility. Unknown errors keep **Accounting Review**.
Saved pre-upgrade cases retain their explanation and default to that route until
**Refresh Case** produces a new typed issue. These shortcuts do not add a new queue,
subledger, historical sales import or general override.

See [accounting validation](accounting-review.md). Upgrade the module in Enterprise
staging and exercise the actual buttons and bank widget before production use.
This document describes the implementation, not a claim that a live payout closed.
