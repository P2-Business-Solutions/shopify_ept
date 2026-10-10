# Bulk payout exception processing

Version **18.0.3.28** adds **Bulk Repair Transactions** on a Shopify payout and
**Actions → Bulk Repair Payout Transactions** on selected payouts. These actions
record money already charged/refunded in Shopify; they never charge a customer or
issue a new Shopify refund.

1. Select the payouts and open the bulk action. Set **Fulfillment Cutover** (August
   1, 2026 for this migration), the business time zone and the customer invoice
   journal for historical refund credits. Posting proven historical credits and
   their outgoing payments is enabled by default. It can be disabled to prepare
   draft credits for review instead.
2. Click **Preview / Refresh**. This reads complete order/transaction history and
   existing accounting, without posting. Ready cases are selected; each exception
   remains in the same list with its source order and explanation. **Details**
   shows exact cash amounts, dates, invoice/credit treatment and account IDs.
3. Use **Open Order** to correct Odoo orders/invoices directly. **Shopify** opens
   the original source order, including orders absent from Odoo. Payout transaction
   rows also have these shortcuts. Refresh after making corrections.
4. Review the selected cases and click **Apply Selected Ready Cases**. Each case
   rechecks the source and ledger under locks. A changed or failed case remains
   for review without preventing other ready cases from completing. Failure rolls
   back that case's credits, payments, reversals and audit together.
5. Existing payout statements are reconciled after repair by default. Correct
   existing matches are retained. A payout validates only when all its statement
   lines reconcile; remaining exceptions continue to keep it partially processed.
   Creating a settlement transfer and matching the actual bank deposit remain the
   existing separate workflow.

## Historical fulfilled orders

A historical refund requires complete successful fulfillment **before** the
selected cutover, original successful collections before cutover, and the exact
successful refund transaction **on/after** cutover. Source order creation or delivery
status alone is insufficient. Fulfillment comparisons use the selected business
time zone. New credits/payments retain source refund dates; locked dates are refused.

The tool prepares/reuses one standalone credit per Shopify refund document and one
outgoing Manual payment per successful Shopify refund transaction. Multiple cash
refunds in the same document share one credit, including when the selected payouts
contain several parts. The refund payments clear that credit's receivable and keep
their outstanding entries available for their own payout transactions. No historical
receipt, sales order, original revenue invoice, delivery or manufacturing order is
created. The original cash collection remains represented by opening balances.

Items require exact Shopify variant mappings and a mapped existing customer.
Shipping and discrepancy lines use the instance's configured shipping/refund
adjustment products and income/returns accounts. Shopify discrepancy amounts are
subtracted from calculated refund components: for order **#129128**, returned value
224.10 less an adjustment of 8.75 explains the 215.35 cash refund. An unknown
adjustment type or unexplained difference stays in review. Explicit refund tax
uses the existing Shopify credit tax account/separate tax product configuration.
This does not change production tax configuration.

Historical credit lines contain account, description and quantity rather than stock
product links. Inventory returns and any required inventory/COGS adjustments must
be accounted for separately; this action does not infer a physical stock return.
Any return/credit already included in opening balances must not be posted again.
Identify/link the existing credit instead of creating a second one. Refunds before
cutover and collections awaiting historical settlement remain opening-balance review
cases; this action does not book historical collections.

## Retroactive discounts and order edits

For a final net invoice (197.10 after a 242.10 capture and 45.00 refund), repair
retains the invoice and replaces an eligible legacy 197.10 payment with the actual
242.10 receipt and 45.00 outgoing refund. Item refund proof remains supported.
Amount-only/order-edit refunds additionally require the final Shopify total,
invoice, final quantities and taxes to agree, and each cash refund to have one
source document whose explicit components explain it.

For an original gross invoice (242.10), repair retains it and creates/reuses the
45.00 credit using the configured refund adjustment account, then records the same
gross receipt/refund pair. An eligible old net payment can be replaced in either
document state. The original payment entry is reversed and retained in the audit.
It is never changed to a different amount in place. Arbitrary invoice-total
differences remain for manual correction using **Open Order**.

Credits already tagged with the same source refund require review/reuse, including
credits missing an order link. An unidentified outgoing payment that could already
represent a historical refund blocks automatic creation. Existing identified cash
is reused; bank-matched legacy replacements, complex allocations and closed periods
remain for review. Repeated previews/applies do not create duplicate cash payments.

## Audit and scope

Historical repairs retain immutable records on the payout's **Bulk Repair Audit**
tab, including reviewer/time, source order/refund identities, fulfillment evidence,
component amounts, posting choices and links to credits/payments. Normal order
repairs retain the existing **Shopify Payment Audit**. Only accounting managers can
apply the bulk action. Internal notes/audits are retained without sending customer
notifications.

Shop Cash rows use the existing grouped order/cash repair and matching workflow.
Missing Shop Cash breakdowns are shown as review cases; reimport that payout to
fetch the breakdown and refresh the preview. The historical standalone-credit path
supports Shopify Payments; PayPal uses Payment Payout Reconciliation's separate
historical activity/credit linking actions.

## Validation

The local Odoo 18 module upgrade and suite completed with 148 passing tests and
three skipped Enterprise bank-reconciliation-widget tests. All 101 standalone
checks passed. The 14 bulk repair tests also passed after the final multi-payout
audit and stable-preview changes, including one refund document split across
two payouts. Enterprise widget behavior still needs validation in staging.
Production payouts were inspected without changing their accounting.
