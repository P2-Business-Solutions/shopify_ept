# Customer import for blocked payout refunds

Version **18.0.3.35** adds **Import Customer** to a saved missing-order refund
case. The regular customer import advances from its last sync date; an older
customer needed by a historical refund may not be included. Importing the old
sales order solely to establish that mapping also brings unnecessary order work.

The action reads the exact Shopify customer named by the source order, calls the
existing customer importer and refreshes only that saved case. Its buttons live
in the existing bulk workspace, case Details and payout repair-progress list.
The permanent history records the source/customer identity and initiating user.
Existing mappings are reused, and an email match must identify one commercial
customer. Ambiguous, inactive or conflicting identities remain for review.

Customer import does not create accounting or apply the refreshed preview. Review
the accounts, tax and source evidence in Details; apply the proven credit/payment
through the existing bulk repair. Keep any remaining document, fulfillment, gateway
or period blocker in that same workspace. Finish statement validation, settlement
and the actual bank match through the existing payout actions.

Scope is limited to the mapping prerequisite and its case refresh. Historical
fulfillment checks, credit linking, payment correction and settlement rules are
unchanged. A completed customer import is not a completed payout.

See [operator steps](payout-bulk-repair.md) and the
[executed validation](accounting-review.md). Enterprise staging must verify the
button and real bank widget before production use. The release was validated with
synthetic data; it does not claim any live payout was closed.
