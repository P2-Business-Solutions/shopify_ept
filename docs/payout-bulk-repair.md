# Bulk payout exception processing

### Focused backlog controls and evidence (18.0.3.37)

The saved workspace has **Show Cases** and **Issue** filters, **Clear Selection**,
**Select Ready in This Filter** and **Select First 10 Ready**. Selection controls
exclude matched cases and clear previous hidden selections; Apply refuses a
selection hidden by the current filters. A selected-count message states the run's
scope. Lists use short summaries, with full explanations in **Details**.

Blocked Details retain Shopify cash, invoice/credit, payment and fulfillment
evidence from preview, including the document/source difference and unproven
shipping quantities. Repair history retains these snapshots. Source/ledger
revalidation on Apply remains mandatory. Customer-import and credit-link buttons
appear only for their relevant cases. Missing Shop Cash allocations open the
exact dependency in Shopify and prefill the existing specific-order import
wizard for review, without importing on open. Historical migration treatment
still needs review before importing an order or creating an invoice.

See [workflow and 5–10 case pilot](payout-guided-closeout.md). Refresh old cases
after upgrading to populate their evidence and exact dependency links. No new
accounting route or automatic historical sales/fulfillment import is added.

### Guided closeout and invoice-sized receipts (18.0.3.36)

Saved cases now show **Next Step** for common customer, payment, document, refund
allocation and fulfillment errors. **Review Payments** opens the existing payment
preview for that one order; **Invoices / Credits** opens its accounting documents.
After a payment repair, **Refresh / Return to Payout Case** takes you back to the
same saved workspace. Review the ready case and apply it to finish payout matching.

A proven single invoice-sized receipt in a fully invoiced, single-capture sale
can use **Preview Invoice Receipt Correction** before **Apply Reviewed Repair**.
The correction retains the original receipt and its reversal, then records the
exact source capture. Ordinary import/sync and bulk Apply do not authorize this
additional replacement. See [eligibility and closeout steps](payout-guided-closeout.md).
Invoice corrections, multi-account/processor credit allocations and missing source
proof still use their existing review routes; no automatic allocation or write-off
is introduced.

### Import the customer for a blocked historical refund (18.0.3.35)

When a missing-order refund says **Import or correct the Shopify customer mapping**,
use **Import Customer** on its saved case in the bulk workspace, **Details**, or
the payout's **Bulk Repair Audit → Current Repair Progress**. This reads the exact
Shopify customer named by that order and reuses the existing customer importer.
It creates/reuses the customer mapping and refreshes only this case; the normal
customer import checkpoint stays unchanged. It avoids importing the old sales order.

The action requires an accounting manager with the existing connector/customer
permissions, including contact creation rights when the customer is new. A unique
existing email can identify the customer; a name alone cannot. Conflicting or
duplicate mappings, ambiguous email identities, inactive customers, other-company
candidates, missing email and a different returned customer stay for manual review.
An existing valid mapping is reused without rewriting that customer's details.
New mappings use the normal importer's tags and customer-channel handling.

Customer import creates no invoice, credit, payment, delivery or manufacturing order,
and never applies the accounting preview. The permanent repair history records the
source customer, Odoo customer and initiating user. Read the refreshed **Details**,
review accounts/taxes, then use **Apply Selected Ready Cases**. Any remaining source,
document or period blocker stays visible. Other cases and successful matches remain
available in the same saved workspace. Settlement and bank matching follow the
existing steps below.

Version **18.0.3.28** adds **Bulk Repair Transactions** on a Shopify payout and
**Actions → Bulk Repair Payout Transactions** on selected payouts. These actions
record money already charged/refunded in Shopify; they never charge a customer or
issue a new Shopify refund.

Version **18.0.3.33** saves the repair workspace and its cases permanently. Leaving
to edit an order, closing the dialog, or returning another day retains the results,
settings, selections and document links. **Bulk Repair Transactions** resumes the
saved workspace without reading Shopify again. A payout processed with several
others can resume that existing shared workspace; its payout tags show the scope.

The payout's **Bulk Repair Audit** tab now contains:

- **Current Repair Progress:** ready, unresolved and applied cases, live payout
  matching status, order/Shopify links, document Details and **Refresh Case**.
- **Repair History:** permanent snapshots of previews, errors, applications and
  matching results, with user/date, explanations and accounting links.
- **Applied Accounting:** the existing immutable accounting trail, now including
  order-payment repairs and their underlying order audit, as well as historical
  credits and opening-balance entries.

After fixing an order, use **Refresh Case** on its saved row to review only that
case. A full **Preview / Refresh** updates outstanding cases while retaining
completed results; changed groupings archive their older rows and preserve their
history. Importing a previously missing order makes its link available on return,
without another preview. The current progress counts and matching status reflect
the statement; the last run result and history describe their recorded runs.
Apply still rechecks source/ledger fingerprints and period locks. An old preview
cannot authorize changed accounting, even when only another case was refreshed.

Upgrading preserves any temporary workspaces/cases still present in the database
and makes them permanent. Previews already deleted by Odoo cannot be recovered;
previously posted accounting and its existing audits are retained.

1. Select the payouts and open the bulk action. Set **Fulfillment Cutover** (August
   1, 2026 for this migration), the business time zone and the customer invoice
   journal for historical refund credits. Posting proven historical credits and
   their outgoing payments is enabled by default. It can be disabled to prepare
   draft credits for review instead.
   Version **18.0.3.32** supports both pre-cutover collections and refunds settling
   in later payouts. Select **Opening-Balance Cash Account** (110010 WEB CC for
   this migration) and enable **Clear Cash Included in Opening Balances** when
   their original sales, returns and taxes are already included in migration
   balances and the unsettled net cash is carried in that account. This option
   authorizes missing balance-sheet clearing entries. The historical refund
   draft/post option controls new post-cutover credits separately.
2. Click **Preview / Refresh**. This reads complete order/transaction history and
   existing accounting, without posting. Ready cases are selected; each exception
   remains in the same list with its source order and explanation. **Details**
   shows exact cash amounts, dates, invoice/credit treatment and account IDs.
   In version **18.0.3.29**, **Back to Bulk Repair** returns from Details to the
   same saved preview and selected cases. **Apply Selected Ready Cases** is on that
   main dialog; Details alone does not create accounting entries. On 18.0.3.28,
   closing Details dismisses the dialog, so reopen the bulk action and preview
   again to reach Apply.
3. Use **Open Order** to correct Odoo orders/invoices directly. **Shopify** opens
   the original source order, including orders absent from Odoo. Payout transaction
   rows also have these shortcuts. Refresh after making corrections.
4. Review the selected cases and click **Apply Selected Ready Cases**. Each case
   rechecks the source and ledger under locks. A changed or failed case remains
   for review without preventing other ready cases from completing. Failure rolls
   back that case's credits, payments, reversals and audit together.
5. **Reconcile Payout Statements in This Run** is enabled by default, so Apply
   creates/reuses the accounting and matches the payout together. Correct
   existing matches are retained. A payout validates only when all its statement
   lines reconcile; remaining exceptions continue to keep it partially processed.
   Each case shows **Matched** or **Awaiting Match**, separately from whether its
   accounting was applied. Matching failures retain posted accounting, do not
   undo successful matches on other cases, and appear in the result and payout
   warning. Refresh/Apply retries reuse the same entries. Disabling reconciliation
   explicitly reports that only accounting was applied and matching remains pending.
   Creating a settlement transfer and matching the actual bank deposit remain the
   existing separate workflow.

## Historical fulfilled orders

### Existing reviewed credit notes (18.0.3.34)

If Accounting has already posted the refund credit (including a standalone Loop
return), use **Link Credit Note** on its saved repair case, or open the individual
payout refund and click **Link Refund Credit Note**. Select the existing credit,
record the accounting review, **Preview / Refresh**, then **Link and Reconcile**.
Loop Returns 18.0.1.13.0 also provides **Link Shopify Refund Credit** directly on
the return and its posted credit. The action suggests a source-linked credit when
there is one candidate; amounts alone never select an accounting document.

This workflow reads the exact successful Shopify Payments refund and its original
charge, checks the source order, customer, currency, full refund-document cash
total, current credit ledger and open periods. A missing Shopify customer mapping
can be proven by a unique existing customer email from Shopify. It reuses the
reviewed credit's accounts and taxes without reconstructing the historical sale,
requiring an imported SO, or generating another credit/payment. Only a source
refund that actually succeeded can close a credit; receiving returned goods alone
does not prove a cash refund.

One credit can cover several successful cash transactions in the same Shopify
refund document, even across payouts. Existing exact outgoing payments are reused
and their outstanding cash is matched; otherwise the statement settles the open
credit receivable directly. Other allocations, competing processor references,
changed source/ledger, ambiguous customer identity and closed periods remain for
review. Order cash repair stops for orders with these direct payout refund links
to prevent creating the same refund cash again; retry their matching from the payout.

The exact source links, reviewer, credit and transaction IDs are permanent and
appear in **Shopify Refund Matches** on the credit and **Bulk Repair Audit** on
the payout. Existing repair cases retain their credit links and matching errors.
Reprocessing or refreshing/applying the saved case reuses those links. Statement
matching failures retain the link for retry and leave the credit open; the result
explains what remains. A payout validates when all its statement lines reconcile.
The final bank-deposit match follows the existing settlement workflow.

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
Shipping lines use the configured shipping income/returns account. In version
**18.0.3.30**, discrepancy adjustments follow the refunded components' revenue
account when they share one GL. A Readers return and its discrepancy therefore
both use the Readers GL. Current-period returns follow the original invoice
account; historical returns follow the mapped product/category account. A refund
spanning different revenue accounts requires a reviewed allocation. Amount-only
refunds still use the configured refund-adjustment product/account. Shopify discrepancy amounts are
subtracted from calculated refund components: for order **#129128**, returned value
224.10 less an adjustment of 8.75 explains the 215.35 cash refund. An unknown
adjustment type or unexplained difference stays in review. Explicit refund tax
uses the existing Shopify credit tax account/separate tax product configuration.
This does not change production tax configuration.

If an existing historical credit uses different accounts or allocations from
the reviewed source plan, it stays in review instead of silently reusing the
incorrect GLs. Posted credits are not rewritten by this repair. Account summaries
list each GL once; Details retains the individual source components, including
offsetting pending/refund discrepancy entries.

Historical credit lines contain account, description and quantity rather than stock
product links. Inventory returns and any required inventory/COGS adjustments must
be accounted for separately; this action does not infer a physical stock return.
Any return/credit already included in opening balances must not be posted again.
Refunds before cutover use the opening-balance cash treatment below. Existing Odoo
credits/payments must be reviewed/linked rather than recording the same cash twice.

## Pre-cutover refunds settling after cutover

Version **18.0.3.32** classifies individual Shopify Payments refunds automatically
in the same bulk preview. A successful refund before the selected cutover, on an
order collected and completely fulfilled before cutover, is **Opening-Balance
Refund** when its payout is on/after cutover. The shared opening-balance checkbox
confirms that its original return and tax have already been accounted for in
migration. Product/tax mappings for a new return are not needed because this path
creates no new return document.

For a 283.30 refund included in WEB CC's net opening balance, the clearing entry
on the payout date **debits 110010 WEB CC 283.30 and credits the Shopify journal's
Manual outstanding payments account 283.30**. Matching its negative statement
line debits that same outstanding account and credits WEB CC. The temporary
outstanding amount clears; no new revenue/returns, tax, customer receivable,
credit note or customer refund payment is recorded. The actual payout/fee postings
continue their normal treatment. Pre-cutover source periods may remain closed;
the payout posting period must be open.

This requires the exact successful refund transaction, its original capture and
gateway, one source refund document, matching amount/currency and proven historical
fulfillment. Multiple pre-cutover cash refunds in one source document are cleared
individually against their exact payout rows. A document mixing pre/post-cutover
refunds, duplicate payout references, existing Odoo credit/refund payments,
changed/reversed entries or allocations outside the exact payout remain for review.
The existing source transaction uniqueness constraint and immutable audit are
shared with historical receipt clearing, so retries never create a second entry.

For production payout **PTR00062 / 140602114274** (August 2, 2026), the four
pre-cutover refund deductions total 1089.06: 283.30, 279.80, 277.24 and 248.72.
Shopify shows order **#132642** refunded on July 31 and deducted from this payout.
This fixes the previous blanket “This refund predates cutover” review message.
Production was inspected without posting or matching those transactions.

## Historical collections settling after cutover

In version **18.0.3.31**, a missing-order collection is checked against the exact
successful Shopify Payments transaction and complete fulfillment before cutover.
The collection must predate cutover in the selected business time zone and settle
in a payout on/after cutover. Current-period collections instead explain how to
import the specific order and refresh. Pre-cutover creation/payment dates alone
do not prove that the sale/fulfillment belongs to the old system.

After explicit opening-balance confirmation, the repair posts one balance-sheet
entry per original receipt on the payout date: **debit Shopify outstanding
receipts; credit the selected opening-balance cash account**. It uses the
configured Shopify clearing journal and its dedicated Manual outstanding receipt
account. The outstanding debit matches the exact gross payout statement line.
No customer payment, receivable, invoice, sales order, delivery or manufacturing
order is created. Existing fees, taxes and bank-deposit settlement follow the
payout's existing workflow.

For this migration, WEB CC also serves as the Shopify clearing journal's cash
account. Its debit on the imported gross statement line offsets the historical
entry's credit to WEB CC. This prevents those imported receipts from increasing
cash a second time. The payout withdrawal, fees and any marketplace-tax entries
then reduce the original clearing balance through the normal settlement process.

Details shows both clearing accounts, debit/credit amounts, source collection
timestamp and posting date. **Bulk Repair Audit** retains immutable source
evidence and links to the exact payout row and clearing entry. Repeated runs reuse
that entry, including when a previous run posted it but could not finish matching.
Ordinary payout reprocessing can also match reviewed clearing entries. A later
order import cannot create duplicate customer cash through transaction cash sync.

Existing or unidentified customer receipts, duplicate source transaction rows,
changed/reversed clearing entries, allocations to another statement, nonstandard
statement accounting, mixed cutover collections and closed periods remain for
review. This path requires the payout currency to equal the company currency;
historical foreign-currency conversion needs a reviewed allocation. Missing-order
Shop Cash activity still uses its separate grouped-credit workflow and remains
for review when the original Odoo order/payment or source breakdown is absent.

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
component amounts, posting choices and links to credits/payments/collection clearing entries. Normal order
repairs retain the existing **Shopify Payment Audit**. Only accounting managers can
apply the bulk action. Internal notes/audits are retained without sending customer
notifications.

Shop Cash rows use the existing grouped order/cash repair and matching workflow.
Missing Shop Cash breakdowns are shown as review cases; reimport that payout to
fetch the breakdown and refresh the preview. The historical standalone-credit path
supports Shopify Payments; PayPal uses Payment Payout Reconciliation's separate
historical activity/credit linking actions.

## Validation

Version **18.0.3.34** adds 12 existing-credit accounting tests and three Loop
integration tests. They cover standalone credits without an SO, split refunds
across payouts, exact allocated/unallocated payment reuse, source/customer proof,
repeat runs, matching failures/retries, persistent audit links and conflicts with
opening-balance cash. Shopify/Loop regressions and the unchanged Stripe/PayPal
reconciliation app suite passed. The 101 standalone checks also passed. The local
Odoo runtime lacks Enterprise's bank widget: these new tests reconcile real move
lines through a replacement adapter, and the three existing Enterprise-only tests
remain skipped. Verify the production widget path in staging after upgrading both
Shopify and Loop. Live accounting was inspected without posting or reconciling.

The 18 bulk refund repair tests cover one refund document split across
two payouts, category-inherited Readers accounts distinct from the generic
adjustment account, offsetting pending adjustments, current-period credits,
mixed-account review and existing historical credits with incorrect GLs.
The 19 historical collection tests cover exact source proof, missing source links, cutover/time-zone
boundaries, opening-balance confirmation, missing/current orders, existing receipts,
duplicate prevention, statement reuse, atomic posting failures, closed periods,
changed/reversed entries, other-statement allocations and leaving unreviewed
transactions open. They verify real Odoo journal entries and reconciled move
lines. When Enterprise's bank widget is unavailable, only that adapter is replaced
with account.move.line reconciliation; the production widget runs when present.
The 13 opening-refund tests cover payout-date clearing, no duplicate return/cash,
all four deductions applied together while retaining an existing receipt match,
reuse after post-only processing, partial match failures visible in the result,
regular reprocessing without order import, confirmation/account setup, source
identity, duplicate credits/payments, time-zone boundaries, mixed-period documents
closed source versus payout periods, and retaining exact matches when only final
payout validation fails. Enterprise widget behavior still needs
validation in staging.
The 13 saved-progress tests cover reopening without Shopify reads, permanent
preview/error history, retained completed cases and document links, one-case
refresh, imported-order navigation, order-payment accounting audits and normal
payout matching, rollback/failure history, immutable history, accountant read-only
access, shared workspaces, latest-run display, regrouping and stale-case settings.
An upgrade from 18.0.3.32 with an existing temporary workspace/case preserved its
settings, result, issue, selection and payout links and made both records permanent.
The Odoo 18 upgrade and full suite completed with **197 passing tests and three
existing Enterprise-widget skips**. All **101 standalone checks** passed.
Production payouts were inspected without changing their accounting.
