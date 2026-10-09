# Shopify cash transactions and payment repair

Version 18.0.3.19 can record successful Shopify sales/captures and refunds separately.
It reads Shopify; it never initiates a Shopify charge or refund. Payout settlement
transfers remain a separate step after the underlying cash movements are reconciled.

## Configure the forward flow

Transaction-based recording is **off by default**. Enable **Record Payments from
Shopify Transactions** under Shopify → Configuration → Settings for the instance.
Upgrading does not enable it. While it is off, the workflow registers one payment for
the invoice balance exactly as before, and refunds follow the previous credit-note flow.

While it is on, an order whose Shopify history cannot be recorded automatically (no
successful charge, a gift card, an unsupported currency, a partial invoice, a journal
without a Manual method) does **not** block delivery validation or order import. No
payment is created, the invoice stays open, and the reason is posted on the order and
in the Shopify logs. Review such orders with **Preview / Repair Shopify Payments**.
Zero-value invoices are skipped because there is no cash to record.

On the order's automatic workflow, enable **Register Payment**. Its gateway must
identify one bank payment journal in the order company and currency. The journal
needs one **Manual** incoming payment method and one **Manual** outgoing payment
method with reconcilable outstanding accounts. Outstanding accounts must be separate
from the journal's bank and suspense accounts. A configured **Refund Payment Journal**
overrides the gateway journal for outgoing refunds and needs the same configuration.

The former credit-note payment checkbox no longer governs cash recording. The workflow's
Register Payment setting governs both receipts and refunds; the settings screen explains
this and exposes the optional refund journal. Existing records are not changed by upgrade.

Each workflow run fetches the order and all pages of its transaction history. Only
successful sale/capture/refund transactions become payments. Authorizations, voids,
and unsuccessful transactions do not. Each payment retains its Shopify transaction,
parent transaction, gateway, original timestamp, currency, and order. The accounting
date for a new payment is the transaction's date in the timezone supplied by Shopify.
From version 18.0.3.23, an existing payment with the exact Shopify transaction ID can
be reused with a different accounting date, including payments recorded when the order
shipped. Its original posting date and existing payout reconciliations are preserved;
the Shopify timestamp remains separate source metadata. Amount, currency, direction,
company, customer, journal, outstanding account and order/document checks still apply.
Legacy payments without a transaction ID still require an exact date match; repair
does not infer their identity from the amount alone. The preview shows both the Shopify
transaction date and the payment's accounting date.

The same Shopify transaction cannot create a second payment for the same instance.
Calls for an order are serialized. Repeated runs reuse correct payments; an old
invoice-sized net payment is an exception that requires the repair preview below.
Gateway records also retain the original successful charges after a refund. Refundable
amounts are recomputed from recorded cash history, so webhook retries and older queue
payloads do not subtract a refund twice or restore an amount already refunded.

## Invoice treatment

For an original charge of 242.10 and a subsequent refund of 45.00:

| Existing documents | Cash and invoice application |
| --- | --- |
| Final invoice 197.10, with evidence that refunded quantities are excluded | Receipt 242.10, refund 45.00; apply 197.10 to the invoice and clear the remaining customer credit with the refund. No extra credit note. |
| Original invoice 242.10 | Receipt 242.10; reuse or create the supported 45.00 credit note and record the 45.00 refund. |

Only customer receivable entries are reconciled during this step. Each payment's
outstanding account remains available to match the charge/refund in its own payout.
The payout matcher can locate an order-linked refund even when the net invoice has
no credit note. Fees and the payout transfer are not booked again by payment repair.

If another refund arrives after a net invoice has been synchronized or repaired,
the immutable audit identifies the refunds already embedded in that invoice. Only
the new refund gets a credit note and outgoing payment. Repeated runs reuse both.

A net amount alone is insufficient proof: the refund's item quantities and monetary
components must explain why the invoice already excludes those items. For original
invoices, automatic credit-note preparation supports uniquely identifiable item lines,
verified tax calculations or the existing separate Shopify tax line, and shipping with
explicitly available refund amounts/taxes. Existing credit notes require the matching
Shopify Refund ID. Presentment-currency components must explicitly match the cash currency.

## Repair existing orders

An accounting manager can open **Preview / Repair Shopify Payments** on an order or
from selected orders' Action menu. Payout forms and their list Action menu also offer
**Preview / Repair Payments**, which collects their linked orders without duplicating
an order present in multiple payouts. Orders imported after the payout are resolved
by their Shopify ID, store and company; ambiguous references require review.

1. Generate the preview. This fetches source data and checks existing accounting, but
   creates no payments, reversals, credit notes or payout statements.
2. Review the exact transaction amounts/dates/journals, reused payments, proposed
   credit notes, and any legacy payment entry proposed for reversal.
3. Click **Apply Reviewed Repair**. The source and ledger are checked again. A changed
   source, payment, allocation, document, configuration or order revision invalidates
   the preview. A blocked order prevents applying the entire selected batch.
4. Inspect the order's **Shopify Payment Audit** tab. It retains the source plan and
   links to reused/new payments, replaced payments and new credit notes.
5. Return to the existing payouts and run **Reconcile Bank Statement**. Then create
   or reuse the net settlement transfers and match the real bank transactions.

Automatic replacement is deliberately limited to one demonstrable legacy incoming
net payment, linked to this order's documents, with no Shopify transaction ID, no bank
match/write-off, and no settlement transfer for an affected payout. Its original posted
entry is retained and reversed at its original date. Its invoice application is removed,
the old payment is marked canceled, and the actual gross receipt/refund are recorded.
The invoice and payout statements are retained. No difference is written off.
Transactions with statement lines cannot be deleted or have their source amounts
changed, and statement lines retain their originating payout links.

Repair uses a transaction savepoint: failure while posting or applying any selected
order rolls back the whole batch. Duplicate clicks and reruns cannot replace the same
payment twice. The audit is immutable through ordinary editing/deletion.

## Cases that require review

### Amount-only refunds with an original invoice

For a successful amount-only refund, keep the original gross invoice and prepare a
reviewed partial credit note for the cash refund amount, with the appropriate account
and tax treatment. Creating it from the original invoice retains its sales-order line
links. From version 18.0.3.22, a standalone credit note can instead be explicitly linked
using **Shopify Order**, even when it is absent from the sales order's invoice list.
Applying a credit to an invoice does not itself create those sales-order links. Repair
searches standalone credits only by this order field, then validates their store and
refund identity against this order's source history. It never searches for credits on
other orders by customer, amount or refund ID alone.

From version 18.0.3.21, accounting managers can open the credit note's **Other Info →
Shopify Refund** section to select the **Shopify Store**, enter the **Shopify Refund ID**,
and check **Refund Already Issued in Shopify**. The ID is Shopify's refund document ID,
not the Shopify cash transaction ID or PayPal refund reference. These metadata fields
are editable on draft and posted credit notes; setting them does not issue another refund
or create accounting entries.

From version 18.0.3.22, also select the original **Shopify Order** for a standalone
credit note. No inferred link or change to invoice lines is needed.

Post the credit note before generating a new payment-repair preview. It may already be
applied to the original invoice: a reduced outstanding balance does not change the
invoice's original total or the gross cash receipt that repair records. The preview
names the credit note it will reuse. The repair checks the linked refund and credit-note
amount, company, currency and customer; conflicting invoice/order links, allocations
outside this order and duplicate or draft refund credits require review. It reuses the
reviewed credit note and records the original gross receipt and separate outgoing
refund payment. Then use the PayPal
reference backfill and payout reconciliation in Payment Payout Reconciliation to clear
the payment outstanding entries and reconcile the bank withdrawal/deposit.

### Other exceptions

The preview blocks replacements of locked or protected entries or payments already matched at the bank;
applications involving another order; unidentified or ambiguous legacy payments;
missing refund-parent history; unsupported currency conversion; gift-card transactions;
missing/ambiguous gateway journals or payment methods; and invoice/credit-note totals
that do not fit the proven original or final-net document states. Correct payments
identified by their Shopify transaction ID can be reused even after payout reconciliation.
An invoice awaiting additional captures, or a fully refunded order with no posted
invoice, also requires review rather than manufacturing a balancing document.

Unproven mixed document histories (some reductions embedded in the invoice and other reductions
represented by credit notes), goodwill/order-adjustment refunds, duties/additional fees,
net-invoice shipping refunds, and taxes that cannot be reconstructed from explicit
source data require reviewed documents rather than guessed entries. A linked, correct
credit note can be reused in the original-invoice flow. Locked or bank-matched historical
corrections remain a manual accounting task; this wizard does not rewrite them.
Later refunds of a net invoice are supported when its prior treatment is established
by this feature's audit. An unknown historical mixture is not inferred from totals.

## Validation and rollout

See the [executed accounting review](accounting-review.md) for database test results,
the complete ledger example, and the remaining Enterprise staging checks.

Run the Odoo integration tests in a staging database with Enterprise accounting and
this connector's dependencies. The standalone tests cover transaction identities,
original/net invoice decisions, refund evidence, duplicate pages, failed transactions,
multiple refunds, currency selection and preview fingerprints. Integration tests cover
receivables vs. outstanding balances, original-invoice credit notes, legacy reversal,
preview nonmutation, stale previews, rollback, duplicate execution, bank-match refusal,
and matching net-invoice refund payments to payouts.

First stage the customer's 242.10 / 45.00 / 197.10 case. Verify the customer receivable
balance is zero, each cash payment has its original gross amount and date, original and
reversal net-payment entries offset, and both payouts match separately. Test ordinary
unrefunded orders and original-invoice/credit-note refunds before enabling this broadly.
