# Shopify payout settlement

Payout transaction reconciliation and bank deposit reconciliation are separate steps.
The connector first matches each charge or refund to its payment, and books fees and
other adjustments. It then creates one net settlement entry for matching in the
receiving bank journal. The settlement entry does not book sales, refunds or fees again.

## Setup

Upgrade `shopify_ept` to version `18.0.3.27`. On the Shopify instance's **Payout
Configurations** tab, configure:

1. **Payout Report Journal:** the separate Shopify settlement bank journal.
2. **Receiving Bank Journal:** the actual bank receiving the deposits. Its currency
   must match the payout currency; its liquidity account must differ from Shopify's.
3. **Settlement Transfer Journal:** a miscellaneous journal in the same company.
4. **Payouts in Transit Account:** a dedicated, reconcilable current asset account,
   separate from both bank liquidity and suspense accounts. Also configure this
   account on the receiving bank journal's incoming payment method as its outstanding
   receipts account. For negative payouts, configure it on an outgoing payment method
   as an outstanding payments account too.

For imported transactions of type **Payout**, use the same transit account in the
instance's transaction account mapping.

Enable **Automatically Create Settlement Transfers** to post a transfer when a payout
successfully validates. It is off by default; upgrading the module does not post
historical settlements. Configuration is checked again when a transfer is requested.

## Workflow

1. Import payouts and generate their statement lines. Bulk generation remains available
   under **Action → Generate Bank Statement**.
2. Use **Reconcile Bank Statement** or the existing payout processing scheduler.
   Charge and refund matching uses Shopify transaction IDs first; legacy matching
   requires a unique applicable payment. Order ID alone is not a sufficient match.
3. Review exceptions. Every nonzero payout transaction must have exactly one posted,
   reconciled statement line. Fees must agree to the imported transaction fees, and
   the net activity must equal Shopify's payout amount using the currency's precision.
4. If automatic transfers are disabled, use **Create Settlement Transfer**, or select
   completed payouts and choose **Action → Create Settlement Transfers**.
5. Use **Reconcile Bank Deposit** to open the receiving bank journal. Match its imported
   deposit against the existing open settlement entry, labeled `Shopify payout <ID>`.
   Do not create another revenue or fee entry for this deposit.

For $1,000 of collections, $100 of refunds and $30 of fees:

| Entry | Debit | Credit |
| --- | --- | --- |
| Net settlement | Payouts in transit $870 | Shopify clearing $870 |
| Actual bank receipt | Bank $870 | Payouts in transit $870 |

Foreign-currency transfers retain the payout-currency amount and use the exact company-
currency balance already booked in the Shopify liquidity entries. Odoo handles exchange
differences when the bank entry is reconciled. This feature requires the receiving bank
journal to use the payout currency; it does not infer bank conversions into a different
currency.

## Refund on a later payout

A $100 charge on Monday and its $100 refund on Tuesday are two separate transactions.
Monday's payout matches the incoming payment. Tuesday's payout matches the outgoing
refund payment or the open credit note. The later refund must not reduce the amount
available to reconcile the original charge. Each payout includes only its own activity.

For example, Monday can settle $97 after $3 in fees. Tuesday could settle $400 from
$500 in other charges less the $100 refund. If a payout is negative, its transfer reverses
the debit/credit direction and matches a bank withdrawal. Zero payouts require no transfer.

The fallback now finds payments independently of an invoice's current payment status,
including after a refund, and uses outstanding residuals rather than combining an
order's payments. Ambiguous, missing or already consumed payments remain exceptions;
do not modify invoice amounts or write off a difference just to balance a payout.

## Audit trail and repeat runs

The payout shows its settlement entry, receiving bank, matched bank transactions and
bank status. It distinguishes awaiting, partial and completed bank matches. Undoing
reconciliation removes the match status. A non-bank write-off, reversal, canceled entry
or unreconciled source statement is reported as **Needs Review**. Statements marked
for review must be checked before settlement. The default Remaining Reports filter
includes these exceptions, even when the transit item has already been reconciled;
**Settlement Needs Review** shows them separately.
Settlement status is stored and indexed, and payout links are indexed for statement
lookups and bulk processing.

Payout locks and database uniqueness constraints prevent a second linked settlement
entry, including across duplicate reports for the same Shopify instance and payout
reference. Repeating creation opens the existing entry. A correctly booked, imported payout
outflow is reused; an outflow booked to another account blocks creation of a second
transfer. Existing manual transfers without a payout link need accounting review before
requesting a historical settlement—the connector cannot identify unrelated manual entries.

Validation and transfer creation use one transaction for each processed payout. The
scheduler isolates configuration/validation failures, records the reason on the payout,
and skips it until it is corrected and processed manually. Bulk manual creation is atomic:
an invalid selection raises an error rather than leaving some new transfers posted.
Processing also locks the payout and rolls back failed statement operations individually.
Database conflicts propagate to Odoo's retry handling instead of being recorded as a
payment mismatch. Zero-value transactions do not create statement lines; generation
completion uses the payout currency's rounding precision.

## Validation

Standalone tests cover independent charge/refund matching, residuals, ambiguity, and
complete pagination, including exactly 250 transactions and the final page. Odoo
integration tests cover balanced and repeated transfers, missing and
unreconciled lines, imported outflow reuse, partial and undone bank matches, non-bank
write-offs, negative payouts, foreign currency, automatic creation, and charge/refund
payments on different payouts with and without Shopify transaction IDs.

Run integration tests in an Odoo 18 test database with the connector's dependencies and
Enterprise accounting installed. The local repository alone has no Odoo runtime.

Before enabling automatic transfers in production, test a normal payout, a next-day
refund, a negative payout, repeated bulk actions, and partial/full bank matching with
undo and review. Confirm that the Shopify clearing balance is cleared by the transfer,
and that the transit balance clears only when the actual bank entry is matched. For
foreign currency, also check the exchange difference and company-currency balances.

## Payments created from net invoices

For invoices that already exclude refunded items, use the transaction payment flow
and the Preview / Repair Payments action described in [Shopify payment repair](shopify-payment-repair.md).
Reimporting payout reports alone does not correct invoice-sized legacy payments.

## Shop Cash payments and grouped settlements

Version `18.0.3.25` matches Shop Cash credits and refund debits against the actual
Shop Cash payment entries. Card payments on the same invoice continue to match
independently. Fees are booked once from the balance transaction fee fields.

Shopify can settle several orders in one Shop Cash credit, and the credit can arrive
on a different payout from the card payment. The connector retains the order-level
breakdown and matches every included capture or refund. If REST omits that breakdown,
it reads the exact balance transaction through GraphQL, verifies the payout ID,
currency and gross/fee/net amounts, and retains the order transaction IDs. The existing
Shopify credentials need permission to read payouts. No customer charge or refund is
sent by this matching process.

Matching verifies company, store, currency, payment direction and the `shop_cash`
gateway. An order-only breakdown requires unambiguous Shop Cash payments; it does not
match unrelated payments by amount. Missing orders/payments, incomplete breakdowns,
ambiguous legacy records, unexpected signs and already consumed payments remain
exceptions. Shop Cash settlement reversals also require accounting review. Historical
Shop Campaign billing adjustments are not treated as customer Shop Cash payments.

Payout validation checks the actual reconciliations between statement and payment
journal items. Posting a Shop Cash credit to a generic account cannot pass this check.
Matched payments and imported breakdowns are visible on the payout transaction.
**Preview / Repair Payments** includes the orders from grouped Shop Cash transactions.

After upgrading, reimport existing payout reports to retrieve the Shop Cash breakdown.
Version `18.0.3.26` retries outstanding statement matches and automatically resets a
plain generic Shop Cash credit/debit posting before matching its actual payments.
Grouped credits and refunds use the same checks. Each reset and replacement match is
atomic: if matching fails, the original posting is restored. Correct payment matches,
fees and existing settlement transfers are preserved; no duplicate statement lines or
Shop Cash payments are created. Lines with linked manual accounting or nonstandard
postings remain for review. The payout shows the reason when reprocessing needs review.

Version `18.0.3.27` also refreshes transaction fee/net metadata and corrects an older
aggregate fee deduction when it omitted adjustment fees. Shop Cash payments still
match at their gross amount; processing and adjustment fees are deducted once using
the existing **Fees** account mapping. A payout with $667.38 in processing fees and
$1.80 in Shop Cash adjustment fees therefore books a $669.18 fee deduction. A plain
fee posting can be corrected even after it was validated, preserving existing card,
grouped Shop Cash and refund payment matches. Repeating reimport does not add fees
again. Fee corrections respect accounting locks and roll back if rebooking fails;
linked manual accounting or nonstandard fee postings remain for review.

Global and hard accounting locks are respected, including the original statement and
settlement entry dates. Temporary user lock exceptions do not allow automatic repair
of a closed period. Locked imports retain their Shopify metadata without changing
accounting, creating entries in a later period or changing the payout's processing state.

For an invoice paid with $225.91 by card and $40.00 by Shop Cash, matching clears those
two gross payments independently. Fees of $5.38 and $0.90 and a $21.96 marketplace-tax
deduction yield a $237.67 payout contribution. Existing marketplace-tax configuration
is unchanged.

## Reimport selected payouts

In **Shopify → Processes → Shopify Operations**, choose **Import Specific Payout(s)**,
select the store and enter one or more Shopify payout IDs separated by commas or new
lines. Use the **Payout Reference ID**, such as `140763300066`, rather than Odoo's report
number (`PTR00055`). Only the requested Paid payouts are fetched. This import does not
change the date-range scheduler's last-import checkpoint.

For existing reports, use **Reimport / Reconcile Payout** on a report, or select several
reports and choose **Action → Reimport / Reconcile Payouts**. The date-range payout import
also uses this repair path for reports already imported, including Partially Processed
and Validated payouts. Review messages appear on the report and remain visible in the
Remaining Reports filter. New open-period payouts still generate statement lines for
the usual reconciliation workflow.
