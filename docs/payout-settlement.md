# Shopify payout settlement

Payout transaction reconciliation and bank deposit reconciliation are separate steps.
The connector first matches each charge or refund to its payment, and books fees and
other adjustments. It then creates one net settlement entry for matching in the
receiving bank journal. The settlement entry does not book sales, refunds or fees again.

## Setup

Upgrade `shopify_ept` to version `18.0.3.16`. On the Shopify instance's **Payout
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
or unreconciled source statement is reported as **Needs Review**.

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

## Validation

Standalone tests cover independent charge/refund matching, residuals, ambiguity, and
complete pagination, including exactly 250 transactions and the final page. Odoo
integration tests cover balanced and repeated transfers, missing and
unreconciled lines, imported outflow reuse, partial and undone bank matches, non-bank
write-offs, negative payouts, foreign currency, automatic creation, and charge/refund
payments on different payouts with and without Shopify transaction IDs.

Run integration tests in an Odoo 18 test database with the connector's dependencies and
Enterprise accounting installed. The local repository alone has no Odoo runtime.
