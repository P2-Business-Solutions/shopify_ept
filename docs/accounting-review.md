# Accounting review — 18.0.3.18

## Backlog workflow and blocked-case evidence — 18.0.3.37

Validated October 12, 2026 in a disposable local Odoo **18.0-20260908 Community** /
PostgreSQL 16 database with Shopify, common connector library, sale order type,
delivery hold, Manufacturing, Payment Payout Reconciliation and Loop Returns.
Fresh installation and upgrades loaded the new workspace filters, filtered saved
case field, selection actions, dependency fields, evidence snapshots and views.

- **241 database tests passed; 3 existing Enterprise widget tests skipped.** The
  final full Shopify run reported **0 failures and 0 errors across 244 tests**.
  Eight new database tests cover the workflow and ten-case batch. A focused run
  after the behavior changes passed **40 tests**, including Shop Cash; a final
  **8-test workflow run** passed after the evidence display refinements.
- **105 standalone tests passed.** Discovery reported 473 tests with 368
  database/fixture aliases skipped outside Odoo. Python compilation, XML parsing,
  manifest version/data paths and `git diff --check` passed.
- One synthetic payout had **five historical collections, five opening-balance
  refunds and one blocked order**. The new pilot selection selected ten Ready
  cases. Apply posted/reused ten exact clearing entries and fully matched their
  statements; the unrelated case stayed Needs Review and the payout stayed open
  without a settlement transfer. No sales order, invoice credit, customer payment
  or fulfillment was created. Refresh retained the entries and matches. The
  Community test adapter reconciled real ledger items; it does not validate the
  Enterprise bank widget or real Shopify API data.
- Filtered selection capped the pilot at ten, cleared hidden selections, excluded
  matched cases and preserved all saved rows. Editing a child through the filtered
  x2many retained the original workspace. Apply refused a hidden selection before
  source/accounting work, and a repeated fully matched workspace did no work.
- A Shop Cash error naming another order retained its exact dependency. Source
  navigation and the existing specific-order wizard used that dependency/store,
  creating no order on open. Changed allocations invalidated the shortcut.
- A blocked draft-invoice preview retained source collections/refunds/net,
  document state/totals/residuals and their comparison using one source read,
  without creating accounting. History retained the evidence. HTML escaped source
  and document values. Missing identity was rejected before an unspecified source
  read. Unproven collection and refund fulfillment used consistent typed guidance;
  proof failures and customer-import eligibility remained enforced.
- Source-only tests covered partial shipping quantities, failed/post-cutover
  fulfillment in the business time zone, invalid currency/order history and
  separate collection/refund totals. Managers retained the existing write scope;
  accounting readers were denied selection changes before any mutation.

The user took responsibility for staging/submodule updates during this work.
**No staging or production accounting was changed, and the 5–10 case pilot on
business records was not run.** After upgrading, follow the
[short staging procedure](payout-guided-closeout.md#five-to-ten-case-staging-pilot-after-upgrade),
including the actual Enterprise widget and selected statements. This release
improves the existing workspace; it adds no accounting eligibility override,
automatic Shop Cash historical route, write-off or historical sales import.

## Guided closeout and invoice receipt correction — 18.0.3.36

Validated October 11, 2026 in a disposable Odoo **18.0-20260908 Community** /
PostgreSQL 16 database with the actual Shopify connector, common connector library,
sale order type, delivery hold, Manufacturing, Payment Payout Reconciliation and
Loop Returns. Fresh installation and subsequent module upgrades loaded the new
fields, error routes, history selection and workspace/payment views. Production
accounting inspection remained read-only; no live repair or closeout was applied.

- **233 database tests passed; 3 existing Enterprise widget tests skipped.** The
  final module upgrade/full Shopify run reported **0 failures and 0 errors across
  236 tests**. This includes 15 new tests for guided routes and invoice receipts.
- **101 standalone tests passed.** Discovery reported 411 tests with 310
  database/fixture cases skipped outside Odoo. Changed Python compiled, edited XML
  parsed, manifest/version/data paths validated and `git diff --check` passed.
- A synthetic single Shopify capture of **240**, two posted invoices of **90**
  and **150**, and one fully allocated untagged **90** receipt first remained
  blocked. The separate **Preview Invoice Receipt Correction** changed only the
  preview. Apply retained the original receipt and reversal, recorded one exact
  **240** receipt, retained both invoice totals and closed both receivables.
  Repeating ordinary cash sync created no further move or audit.
- The complete saved-case flow used **Review Payments**, applied that correction,
  retained payment history/order audit, refreshed only its original case, matched
  the payout statement and fee, posted its **237** net settlement and matched one
  bank receipt. Transit residual was zero; repeat settlement reused the same move.
  Community matching reconciled actual ledger items through the existing test
  adapter. This is not verification of the Enterprise bank widget or live UI.
- Automatic cash sync stayed blocked before the separate review. Multiple
  captures/receipts, an unreconciled receipt identified only by amount, missing or
  changed source customer mapping, a bank match added after preview and a settled
  source-linked payout without an Odoo order link were refused. Changed capture
  identity invalidated approval. A posting failure rolled back the reversal,
  payment creation and allocations together.
- Typed guidance was independent of translated error text. Missing historical
  fulfillment produced the source-evidence route, unknown issues retained manual
  review, and existing permission tests denied the new payment shortcut before a
  source read. Old saved explanations/history remain; their guidance updates on
  Refresh Case. Invoice/credit navigation creates no accounting document.

In Enterprise staging, verify the new buttons with a normal accounting manager,
the eligible invoice-receipt case through the actual payout and bank widget, and
one remaining document/allocation/source blocker. Confirm one source receipt,
one retained reversal, unchanged invoice totals and one deposit match. No live
payout is claimed closed; this release adds one narrow accounting route and
navigation/guidance to existing repair tools. See [workflow](payout-guided-closeout.md).

## Customer import from a blocked payout case — 18.0.3.35

Validated October 11, 2026 in a disposable Odoo 18 Community / PostgreSQL 16
database with the actual connector, common connector library, sale order type,
delivery hold, Manufacturing, Payment Payout Reconciliation and Loop Returns.
Fresh module installation and subsequent upgrades loaded the action, history
selection and all three existing views. Production inspection was read-only;
no live customer imports, credits, payments, settlement or bank matches were applied.

- **218 database tests passed; 3 Enterprise widget tests skipped.** The final
  Odoo runner reported **0 failures and 0 errors across 221 tests**. Nine focused
  customer-import tests passed, including a real manager with standard connector
  and contact-creation permissions, and an accountant denied before reading source.
- **101 standalone tests passed.** Standalone discovery reported 396 tests with
  295 database/fixture cases skipped outside Odoo; actual database validation ran
  separately above. Ruff's F checks on changed Python files, compilation, edited
  XML parsing, manifest file/version checks and `git diff --check` passed.
- A missing customer mapping became a Ready historical refund preview without
  creating a sales order, invoice, credit, payment, delivery or manufacturing order.
  Repeating the action reused the same customer/mapping and left the ordinary
  customer-import checkpoint unchanged. The history retained the initiating user.
- A unique existing email reused the customer. The unchanged Apply workflow then
  posted a synthetic **215.35** credit and outgoing payment, reconciled the exact
  refund statement, created its negative settlement, and matched one synthetic
  bank withdrawal. Credit and transit residuals were zero, and repeated settlement
  creation reused the same move. Community matching used ledger reconciliation
  in place of the Enterprise widget; it is not an Enterprise UI validation.
- Ambiguous/inactive email candidates, conflicting customer mappings, missing
  email and a different returned customer stayed in review. A remaining accounting
  lock after customer import retained its own blocker and created no accounting.

The action reuses the existing customer importer and case refresh; accounting
eligibility and approval stay in the existing preview/apply flow. Existing importer
tag/customer-channel behavior and access controls remain in force. Check the new
button and complete one new/reused customer case through the actual Enterprise
bank widget in staging before production use. No production payout is claimed closed.

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
PostgreSQL 16 database with Manufacturing, Payment Payout Reconciliation 18.0.1.18.0 and the
connector's dependencies. The final connector test run reported **zero failures and
zero errors across 97 database tests: 94 passed and 3 Enterprise widget checks skipped**.
The standalone suite passed **85 tests**; its 97 database tests were executed separately.
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

Silent-import checks use a storable product with manufacture and make-to-order routes
and a BOM. Creating and processing the canceled order creates no MO or delivery,
adds no followers and sends no emails or notifications. A second check subscribes a
customer and an internal email follower to an existing order: both a successful cash
import and a failed retry retain internal audit/review notes without sending notifications
or creating canceled-order review activities.

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

## Archived full refunds, version 18.0.3.51

The isolated Odoo 18/PostgreSQL 16 run completed **349 connector tests with zero
failures and zero errors**. Three Enterprise bank-widget checks were skipped; the
remaining 346 passed. Manufacturing and Payment Payout Reconciliation were installed.
The standalone run passed **161 tests** and skipped 493 Odoo-dependent tests, which
were exercised through the database suite where selected by Odoo. Python compilation
and `git diff --check` also passed.

The archived-order tests verify separate original 735 receipt/refund payments, their
source dates and transaction IDs, closed customer receivables, and reuse on retry.
A payout containing both movements and its processing fee matches both cash lines
without an invoice or credit note. Existing separate diagnoses regroup into one order
case while the superseded case history remains archived. Tests also cover original
removed order lines, paid-workflow fallback, no deliveries/manufacturing/notifications,
customer mapping, active stock, changed source after preview, outside receivable
allocations, invalid full-refund evidence, retryable import failure, and pre-cutover
opening-balance review.

The source is a controlled fixture, not a live merchant API call. The owner must
upgrade the deployed connector to 18.0.3.51 before live closeout. Verify both exact
payout matches and zero customer/outstanding residuals in Enterprise, then capture
the actual resolution in Tango with screenshots at every retained step.
