# Shopify payout repair update 18.0.3.57

Opening-balance collection/refund previews now respect the existing payment's Shopify store, order and exact cash transaction identity. A different identified capture/refund no longer blocks another transaction solely because its customer, amount and date match. Same-order receipts without a transaction ID and unidentified same-customer/amount/date payments still require review. The warning lists the matching payments and the expected source order/transaction so accountants can inspect the actual evidence.

Upgrade `shopify_ept` to 18.0.3.57, reopen the affected Bulk Repair workspace and use Preview / Refresh. Existing posted accounting is retained. This correction needs no changes to the payout or Loop modules.

## Previous update: 18.0.3.53

Opening-balance refunds with imported A2000 credit-memo allocations now validate and attach the same evidence during preview and apply. Previously the historical-refund preview omitted that evidence, so apply reported that source data or accounting had changed even when the reviewed allocation was unchanged. The saved case now shows its A2000 evidence and retains it in the clearing audit. Actual source changes and modified evidence files still stop accounting.

Upgrade `shopify_ept` from 18.0.3.52, reopen the affected saved Bulk Repair workspace, and use Preview / Refresh before applying only reviewed ready cases. Reuse the already attached allocation CSV; do not import it again. The existing clearing entry and payout match are reused on subsequent runs. No new credit note, customer refund payment, sale or stock movement is required by this correction. The other two modules do not need a version change for this patch.

## Previous update: 18.0.3.52

Deploy with Payment Payout Reconciliation 18.0.1.21.0 and Loop Returns 18.0.1.15.0. Upgrade the payout module, Shopify connector and Loop connector together after deploying all three code changes. No existing posted accounting is automatically changed.

- Imported invoices retain source order dates when the workflow requests order dates; otherwise completed shipments use their latest successful source fulfillment date. Imported stock moves, move lines, deliveries and valuation journal entries retain their exact fulfillment dates. Missing/ambiguous source dates, locked periods and historical FIFO/average-cost automatic valuation require review. Existing historical misdated postings require controller-directed correction.
- **Prepare Credit Repair** on an individual payout refund previews the successful source refund's invoice components. It prepares a new draft or corrects a selected unsettled, unlocked credit, retaining its prior details as an attachment. Review/post the draft, then use the existing credit-link and payout matching actions. Paid/allocated or hash-locked credits require separate review.
- **Import A2000 Opening Evidence** in Bulk Repair attaches verified PAID invoice/credit-memo allocations to exact cash transactions. It retains the export file, checksum and reviewer, and exposes its evidence in the case and accounting audit. It does not query A2000 from the Odoo server or post cash during import. A2000 MCP can prepare the export outside Odoo.

The full CSV schema, upgrade sequence and related PayPal/Authorize/Loop procedures are in the payout repository's [repair upgrade instructions](https://github.com/P2-Business-Solutions/odoo-payment-reconciliation-app/blob/main/docs/repair-upgrade.md). Tango capture updates follow the module upgrades and live validation.
