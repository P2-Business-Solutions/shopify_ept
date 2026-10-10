# Accounting review — 18.0.3.18

## Order-scoped standalone refund credits — 18.0.3.22

Reviewed October 9, 2026 in a disposable Odoo 18 Community/PostgreSQL database with
the actual connector, common connector library, sale order type and delivery hold
dependencies. Module installation and upgrade completed. No production records changed.

- **45 accounting tests passed; 1 Enterprise widget test skipped.** The Odoo runner
  reported zero failures and errors across 46 cash synchronization tests.
- **19 standalone payment-planning tests passed.** Syntax and whitespace checks passed.
- A $414.95 original invoice plus a standalone $9.95 amount-only credit was repaired
  both before and after applying the credit to the invoice. The preview reused the
  named credit without posting anything; apply created only the $414.95 incoming
  payment and $9.95 outgoing payment. Invoice and credit receivables closed, each
  payment's gross outstanding entry remained open for payout matching, and rerunning
  created no further payments or audit records.
- A standalone credit must explicitly select this sales order in **Shopify Order**.
  Credits with the same refund ID on another order, or without an order link, are not
  candidates. The order's own credits still require matching Shopify source evidence.
- Wrong customers, stores, currencies, amounts and source refund IDs; conflicting
  order lines or reversal links; allocations to other invoices; duplicate/draft
  credits; and changes to a reviewed order link or allocation were rejected.

The existing Enterprise widget/staging checks below remain necessary for that UI.
The repair does not add sale-line links to standalone credits; their explicit Shopify
order link makes them available to repair without changing sales quantities.

## Earlier review

Reviewed September 9, 2026. Changes are local; no production accounting or Shopify
records were modified. Tests used synthetic transactions and a disposable PostgreSQL
database with the existing Odoo 18.0-20260723 Community image and the actual connector,
common connector library, sale order type, and delivery hold dependencies.

## Findings corrected

- **Subsequent refunds after a net-invoice repair:** the audit retains which refund
  IDs were already embedded in the invoice. Later refunds create only their own
  credit notes and outgoing payments. An unknown historical mixture still requires
  review; the system does not infer it from a matching net total.
- **Accounting permissions and audit provenance:** accounting managers can apply
  repairs and read the audit without superuser access. Ordinary connector users can
  run payment workflows. Audits are created only through a private synchronization
  entry point, retain the initiating user, and cannot be forged through ordinary
  create/write/delete calls.
- **Payout links:** a transaction with a statement line cannot be deleted or have
  its amounts or identity changed. Statement lines cannot lose their originating
  payout links, and setting a payout back to Draft cannot bypass these protections.
- **Orders imported later:** payout repair previews resolve a missing order link
  using Shopify order ID, store, and company. Ambiguous or inconsistent references
  stop the preview.
- **Refund reconciliation in one run:** processing checks for the payment again
  after refund import, including a net-invoice refund that creates no credit note.
- **Ambiguous legacy payments:** identical charges are not assigned to an untagged
  legacy payment arbitrarily. Ambiguity among source events or imported payout
  transactions requires a verified Shopify transaction ID.
- **Refund quantities and posting periods:** cumulative item refunds cannot exceed
  the original invoice quantity. New settlement transfers stop at a locked payout
  date instead of silently moving to another accounting period.

## Executed validation

- Fresh module installation and subsequent module upgrade completed in Odoo.
  This exercised model registration, fields, XML views, external references, actions,
  and access-control loading.
- **54 Odoo database tests passed; 3 Enterprise-only tests skipped.** The runner
  reported zero failures and zero errors across 57 database tests.
- **81 standalone tests passed.** Standalone discovery also identifies the 57
  database tests and skips them outside Odoo; those were executed separately above.
- Python syntax, manifest data paths, XML parsing, and `git diff --check` passed.

Database coverage includes normal orders; gross invoices and credit notes; invoice
tax preservation; net invoices; later refunds after repair; original payment
reversals; duplicate execution; stale source/ledger previews; permissions; protected
links; ambiguous identity; rollback across multiple orders; positive and negative
settlements; reuse of imported payout outflows; foreign-currency carrying amounts;
and partial, full, and removed bank matches.

### Complete ledger example

The combined test starts with a 197.10 invoice and legacy payment, reverses that
payment, and records the source receipt of 242.10 and next-day refund of 45.00.
It reconciles real Odoo journal items through two payout records and then through
bank statement entries. It deliberately uses ledger reconciliation directly;
Enterprise widget behavior is tested separately.

| Verification | Result |
| --- | --- |
| Original invoice | Remains 197.10; customer balance zero |
| Legacy payment | Canceled; original posted entry and one reversal retained |
| Original receipt | 242.10, matched independently |
| First payout | 242.10 less 5.75 fees = 236.35 |
| Refund payout | -45.00, matched independently |
| Net receiving-bank movement | 191.35 |
| Receivables, outstanding payments, Shopify clearing, transit | Each zero |
| Settlement statuses | Both Bank Matched |

## Canceled-order import verification — October 10, 2026

Version 18.0.3.24 was installed and upgraded in an isolated Odoo 18 Community /
PostgreSQL 16 database with Payment Payout Reconciliation 18.0.1.18.0 and the
connector's dependencies. The final connector test run reported **zero failures and
zero errors across 96 database tests: 93 passed and 3 Enterprise widget checks skipped**.
The standalone suite passed **85 tests**; its 96 database tests were executed separately.
Python syntax, edited XML parsing and `git diff --check` passed.

The canceled-order cases verify an original 242.10 receipt and next-day 242.10
refund without an invoice or credit note. Both customer receivable entries close;
each outstanding entry remains available to its own payout. Reimporting the order
reuses its payments and audit, while partial refunds and missing configuration keep
the queue retryable and the order canceled. Tests also cover retaining refunded order
lines, using the paid gateway workflow when no refunded workflow is configured,
requesting canceled orders without a fulfillment filter, processing subsequent pages,
skipping fulfillment requests, reviewed cash-only repair, preserving a posted original
invoice with only the confirmed partial refund, and refusing allocations to another order.

The PayPal integration test uses the actual payout app's reference-backfill wizard and
activity importer/matcher/poster. It verifies separate capture/refund references,
clears both payment outstanding entries, retains the 3.00 processing fee in the PayPal
balance, and reuses the settled payments without adding ledger entries. Shopify and
processor source responses are controlled fixtures; these checks do not call a live
merchant account. Enterprise bank reconciliation UI validation remains a staging check.

## Remaining release check

Run the three widget tests in the actual Odoo Enterprise staging environment:

- `TestShopifyCashSync.test_net_refund_payment_matches_payout_without_credit_note`
- `TestShopifyPayoutSettlement.test_charge_and_next_day_refund_reconcile_to_separate_payouts`
- `TestShopifyPayoutSettlement.test_legacy_payments_on_refunded_order_remain_independently_matchable`

Use a staging copy of the affected order and both real Shopify payout payloads to
exercise the repair preview, apply, Reconcile Bank Statement, settlement transfer,
and bank match with the production configuration. The isolated tests validate the
ledger behavior; they do not validate the live Shopify API, Enterprise widget, or
every production customization. Complete this staging check before production rollout.

See [configuration and repair instructions](shopify-payment-repair.md).
