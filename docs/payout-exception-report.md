# Payout reconciliation exception report — 18.0.3.44

Open **Shopify → Sales → Payout Reconciliation Exceptions**. The same report is
available from a payout's **Reconciliation Exceptions** button, scoped to that
payout. **Open Exceptions** is enabled by default and payouts sort oldest first.

The report reads the existing imported transactions, statement entries and saved
repair workspaces. Opening it does not call Shopify, create a preview, post
accounting or apply repairs. Reload it after completing work in another tab to see
the current ledger state. Use the payout's existing reimport action when the
imported Shopify data itself needs refreshing.

Each nonzero payout transaction has one row, including transactions without a
statement entry or a saved repair case. Offsetting charges and refunds remain
separate exceptions even when their signed net balance is zero. Matching an item
changes that same row to Resolved. A matched entry that still needs review, or
has duplicate links or inconsistent amount/type/currency/journal, stays open.
Unlinked statement entries are included separately, including already matched
entries that have no originating transaction.

After the transaction work is complete, a single payout closeout row directs
validation, settlement transfer creation, settlement review or the bank match.
Payouts without imported nonzero transactions have a closeout exception too.
Cash repair and bank matching are separate stages: a repaired transaction does
not mean its deposit has reached the bank.

Use **Payout**, **Exception Stage**, **Repair Issue**, store, currency and payout
date to narrow the work. **No Saved Repair Case**, **Ready Cases**, **Cases Needing
Review** and **Applied / Awaiting Match** identify the existing repair status.
Repair Status describes the saved preview; Exception Stage describes the current
statement entry. A case covering several transactions can therefore appear beside
both resolved and unresolved transaction rows.

Open a row for its exact Shopify payout, balance transaction, cash transaction and
order IDs, its Odoo order/statement links, signed cash residual and next step.
**Repair Details** opens the latest saved active case with the existing customer,
payment, document, source and credit-link routes. **Open Repair Workspace** resumes
that case's workspace and preserves its settings; if no case exists, it opens the
existing payout workspace for a reviewed preview. Only accounting managers can
prepare/apply those repairs. **Statement Transactions** includes every linked
statement so duplicate entries can be investigated.

The detail tabs retain the breadcrumbs:

- Current source/ledger evidence, accounting preview, explanation and processing
  error, with the preview/apply times.
- All saved cases for that transaction, including archived or superseded cases.
- Immutable preview, apply, customer import, error and matching events, with the
  initiating user, timestamp, recorded plan/evidence and actual accounting links.
- Applied repair audits and their payment audit, credits, payments and clearing
  entries. Older order-level audits without transaction links are included by
  their saved source order reference.
- The payout's last reimport result, settlement entry and matched bank transactions.
  **Open Payout** also gives access to its original chatter and processing history.

Clear **Open Exceptions**, then use **Resolved**, to inspect completed work.
Resolving an item does not delete its case or history. Historical snapshots retain
the status at the time they were recorded. The report's current status comes from
the ledger; old warning text alone does not reopen a matched transaction.

Transaction Amount and Unmatched Amount use the payout currency. Closeout rows
have no transaction residual; their Payout Amount is the net deposit to investigate.
Report amounts are not automatically aggregated across currencies or repeated
payout references.

The report is read-only and restricted to accounting users and their selected
companies. All corrections continue through the existing guarded workflows.
Upgrade `shopify_ept` to **18.0.3.44** to install its model, views, access rule and
menu. The local implementation must still be deployed and upgraded in the target
Odoo database before this menu is available there.

Validated October 14, 2026 in disposable Odoo **18.0-20260908 Community** /
PostgreSQL 16 databases. A clean full connector run passed **307 tests**, followed
by a final **14-test report run** covering grouping, cash residuals, foreign
currency, matching/unmatching, statement review/structure, bank settlement,
archived breadcrumbs, source navigation and company/read-only access. All **116
standalone checks** passed. Module installation/upgrades, XML, manifest paths,
Python syntax and whitespace checks passed. Browser checks with synthetic data
verified the menu, open filter, row detail, archived cases and immutable history.
No production module upgrade, source API call or business-record repair was
performed. The report routes to the existing Enterprise bank matching workflow;
that widget still requires verification in the target Enterprise environment.
